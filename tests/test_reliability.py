"""Outbox, leases, crash recovery, SIGTERM give-back and the timed jobs."""

from __future__ import annotations

from datetime import timedelta

from conftest import OWNER, RAMESH, line, outbox_rows, reading
from order_desk.services import maintenance, outbox
from order_desk.telegram.bot_api import TelegramError
from order_desk.workers.lifecycle import StopFlag
from order_desk.workers.outbox_sender import run_batch


class RecordingSender:
    def __init__(self, fail_with: TelegramError | None = None):
        self.sent: list[tuple[int, str]] = []
        self.fail_with = fail_with

    def send_message(self, chat_id, text):
        if self.fail_with:
            raise self.fail_with
        self.sent.append((chat_id, text))

    def send_document(self, chat_id, path, caption=None):
        self.sent.append((chat_id, f"doc:{path}"))

    def send_photo(self, chat_id, path, caption=None):
        self.sent.append((chat_id, f"photo:{path}"))


def _queue(deps, n=2):
    with deps.db.write() as conn:
        for i in range(n):
            outbox.enqueue(conn, bot="dealer", chat_id=RAMESH, text=f"hello {i}", dedupe_key=f"t{i}")


def test_same_dedupe_key_is_queued_once(deps):
    _queue(deps, 1)
    _queue(deps, 1)
    assert len(outbox_rows(deps)) == 1


def test_sender_sends_and_marks_sent(deps):
    _queue(deps)
    sender = RecordingSender()
    assert run_batch(deps.db, {"dealer": sender, "shop": sender}) == 2
    assert [r["status"] for r in outbox_rows(deps)] == ["sent", "sent"]
    assert run_batch(deps.db, {"dealer": sender, "shop": sender}) == 0         # nothing sent twice


def test_retryable_error_is_retried_later(deps, fixed_clock):
    _queue(deps, 1)
    failing = RecordingSender(TelegramError("429 too many requests", retryable=True))
    run_batch(deps.db, {"dealer": failing, "shop": failing})
    row = outbox_rows(deps)[0]
    assert row["status"] == "pending" and row["attempts"] == 1
    assert run_batch(deps.db, {"dealer": failing, "shop": failing}) == 0       # not due yet (backoff)
    fixed_clock["now"] += timedelta(minutes=1)
    ok = RecordingSender()
    assert run_batch(deps.db, {"dealer": ok, "shop": ok}) == 1


def test_permanent_error_stops_at_once(deps):
    _queue(deps, 1)
    failing = RecordingSender(TelegramError("chat not found", retryable=False))
    run_batch(deps.db, {"dealer": failing, "shop": failing})
    assert outbox_rows(deps)[0]["status"] == "failed"


def test_crashed_worker_rows_are_taken_again_after_the_lease(deps, fixed_clock):
    _queue(deps, 1)
    claimed = outbox.claim_due(deps.db)                 # a worker takes the row ... and dies
    assert len(claimed) == 1
    assert outbox.claim_due(deps.db) == []              # still leased to the dead worker
    fixed_clock["now"] += timedelta(seconds=outbox.LEASE_SECONDS + 1)
    again = outbox.claim_due(deps.db)                   # recovery: the lease ended
    assert [m.id for m in again] == [claimed[0].id]


def test_sigterm_gives_unsent_rows_back_at_once(deps):
    _queue(deps, 3)
    stop = StopFlag()
    stop.set()                                          # as if SIGTERM arrived before the first send
    sender = RecordingSender()
    run_batch(deps.db, {"dealer": sender, "shop": sender}, stop)
    assert sender.sent == []
    rows = outbox_rows(deps)
    assert {r["status"] for r in rows} == {"pending"} and {r["attempts"] for r in rows} == {0}


def test_unconfirmed_order_expires_and_dealer_is_told(dealer, deps, photo, fixed_clock):
    deps.reader.chits.append(reading(line("6A sw - 10", "switch", {"rating_amp": 6}, "Anchor", 10, "nos")))
    order_id = dealer.start_order(RAMESH, "photo", "m-exp")["order_id"]
    dealer.read_chit(RAMESH, order_id, photo())
    assert maintenance.expire_unconfirmed_orders(deps.db) == 0
    fixed_clock["now"] += timedelta(hours=deps.settings.dealer_check_hours + 1)
    assert maintenance.expire_unconfirmed_orders(deps.db) == 1
    assert maintenance.expire_unconfirmed_orders(deps.db) == 0            # safe to run again
    assert dealer.order_status(RAMESH, order_id)["state"] == "EXPIRED"
    assert any("closed" in r["payload_json"] for r in outbox_rows(deps))


def test_owner_reminder_once_per_window(dealer, deps, photo, fixed_clock):
    deps.reader.chits.append(reading(line("6A sw - 10", "switch", {"rating_amp": 6}, "Anchor", 10, "nos")))
    order_id = dealer.start_order(RAMESH, "photo", "m-rem")["order_id"]
    dealer.read_chit(RAMESH, order_id, photo())
    dealer.confirm(RAMESH, order_id)
    fixed_clock["now"] += timedelta(hours=3)
    maintenance.remind_owner(deps.db, deps.settings)
    maintenance.remind_owner(deps.db, deps.settings)
    reminders = [r for r in outbox_rows(deps) if "Reminder" in r["payload_json"]]
    assert len(reminders) == 1 and reminders[0]["chat_id"] == OWNER
