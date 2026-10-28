"""Jobs that run on a timer (workers/scheduler.py calls `run_once()`).

Each job is a plain function that is safe to run again at any time:
running it two times in a row does the same as running it one time. So a
crash or a restart of the scheduler never causes harm. This is the
"recovery on boot" idea: the scheduler simply runs every job at start.
"""

from __future__ import annotations

import logging

from order_desk import clock
from order_desk.config import Settings
from order_desk.db.connection import Database
from order_desk.domain.states import Actor, State
from order_desk.services import messages, orders, outbox

log = logging.getLogger(__name__)


def expire_unconfirmed_orders(db: Database) -> int:
    """AWAITING_DEALER orders past their deadline -> EXPIRED. No stock is held in
    that state, so nothing must be released."""
    now = clock.now_iso()
    count = 0
    with db.write() as conn:
        rows = conn.execute(
            "SELECT o.*, d.telegram_user_id AS dealer_tg FROM orders o JOIN dealers d ON d.id = o.dealer_id "
            "WHERE o.state = 'AWAITING_DEALER' AND o.dealer_check_deadline < ?", (now,)).fetchall()
        for row in rows:
            order = dict(row)
            orders.transition(conn, order, State.EXPIRED, Actor.SYSTEM, "system", reason="no dealer confirmation in time")
            outbox.enqueue(conn, bot="dealer", chat_id=order["dealer_tg"], text=messages.dealer_expired(order["id"]),
                           dedupe_key=f"expired:{order['id']}")
            count += 1
    return count


def remind_owner(db: Database, settings: Settings) -> int:
    """One reminder per waiting order per `owner_reminder_hours` window."""
    hours = settings.owner_reminder_hours
    limit = clock.iso_in(hours=-hours)
    count = 0
    with db.write() as conn:
        rows = conn.execute(
            "SELECT id, updated_at FROM orders WHERE state = 'AWAITING_OWNER' AND updated_at < ?", (limit,)
        ).fetchall()
        for r in rows:
            waited = int((clock.now() - clock.from_iso(r["updated_at"])).total_seconds() // 3600)
            bucket = waited // max(hours, 1)                  # 1 message per window
            sent = outbox.notify_staff(conn, "owner", messages.owner_reminder(r["id"], waited),
                                       dedupe_key=f"reminder:{r['id']}:{bucket}")
            count += 1 if sent else 0
    return count


def report_stuck_reads(db: Database, minutes: int = 15) -> list[int]:
    """Orders that stayed in RECEIVED: the read was interrupted (crash, model
    timeout). They are safe: no stock is held. We only log them; the agent
    calls read_chit again when the dealer writes, or the dealer resends."""
    limit = clock.iso_in(seconds=-60 * minutes)
    with db.read() as conn:
        ids = [r[0] for r in conn.execute(
            "SELECT id FROM orders WHERE state = 'RECEIVED' AND created_at < ?", (limit,))]
    if ids:
        log.warning("orders still in RECEIVED after %s min: %s", minutes, ids)
    return ids


def run_once(db: Database, settings: Settings) -> dict:
    return {
        "expired": expire_unconfirmed_orders(db),
        "reminders": remind_owner(db, settings),
        "stuck_reads": report_stuck_reads(db),
    }
