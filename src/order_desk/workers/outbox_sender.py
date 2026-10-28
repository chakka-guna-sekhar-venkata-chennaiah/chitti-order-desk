"""Send queued Telegram messages from the outbox table.

Loop:  claim due rows (with a lease) -> send each -> mark sent / retry later.
Run it with:  order-desk sender

Rows for the 'dealer' bot go out with DEALER_BOT_TOKEN, rows for the 'shop'
bot with SHOP_BOT_TOKEN.
"""

from __future__ import annotations

import logging
from typing import Callable, Protocol

from order_desk.config import Settings
from order_desk.db.connection import Database
from order_desk.services import outbox
from order_desk.telegram.bot_api import BotApi, TelegramError
from order_desk.workers.lifecycle import StopFlag

log = logging.getLogger(__name__)


class Sender(Protocol):
    def send_message(self, chat_id: int, text: str) -> None: ...
    def send_document(self, chat_id: int, path: str, caption: str | None = None) -> None: ...
    def send_photo(self, chat_id: int, path: str, caption: str | None = None) -> None: ...


def deliver(message: outbox.OutboxMessage, sender: Sender) -> None:
    p = message.payload
    if message.kind == "text":
        sender.send_message(message.chat_id, p["text"])
    elif message.kind == "document":
        sender.send_document(message.chat_id, p["path"], p.get("caption"))
    elif message.kind == "photo":
        sender.send_photo(message.chat_id, p["path"], p.get("caption"))
    else:
        raise TelegramError(f"unknown kind {message.kind}", retryable=False)


def run_batch(db: Database, senders: dict[str, Sender], stop: StopFlag | None = None) -> int:
    """Send one batch. Returns the number of messages handled."""
    batch = outbox.claim_due(db)
    unsent = [m.id for m in batch]
    try:
        for message in batch:
            if stop is not None and stop.stopped:
                break
            try:
                deliver(message, senders[message.bot])
                outbox.mark_sent(db, message.id)
            except TelegramError as exc:
                # A permanent error (chat not found, bot blocked by the user) is not retried.
                outbox.mark_failed_attempt(db, message, str(exc), permanent=not exc.retryable)
                log.warning("outbox %s: %s", message.id, exc)
            except Exception as exc:              # file missing, bug: retry with backoff
                outbox.mark_failed_attempt(db, message, repr(exc))
                log.exception("outbox %s failed", message.id)
            unsent.remove(message.id)
    finally:
        if unsent:                               # stopped in the middle: give the rest back
            outbox.release_leases(db, unsent)
    return len(batch) - len(unsent)


def main(settings: Settings, make_api: Callable[[str], Sender] = BotApi, poll_seconds: float = 2.0) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    db = Database(settings.db_path)
    db.initialize()
    senders = {"dealer": make_api(settings.dealer_bot_token), "shop": make_api(settings.shop_bot_token)}
    stop = StopFlag().install()
    log.info("outbox sender started")
    while not stop.stopped:
        handled = run_batch(db, senders, stop)
        if handled == 0:
            stop.sleep(poll_seconds)
    log.info("outbox sender stopped cleanly")
