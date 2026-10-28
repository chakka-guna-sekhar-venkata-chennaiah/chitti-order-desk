"""Transactional outbox for Telegram messages.

Why an outbox?
    A state change and its message must not drift apart. If the code changed
    the order to APPROVED and then crashed before it sent "your order is
    approved", the dealer would never know. So we INSERT the message into the
    `outbox` table in the SAME transaction as the state change. Either both
    are saved or neither is. A separate worker (workers/outbox_sender.py)
    reads the table and sends.

Delivery guarantee: at-least-once.
    Telegram has no "send exactly once" API. If the worker crashes after
    Telegram accepted a message but before we mark it 'sent', the message is
    sent again after the lease ends. The lease keeps this window small.

`dedupe_key` stops the same logical message from being queued twice
(for example when a tool call is retried).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from order_desk import clock
from order_desk.db.connection import Database

MAX_ATTEMPTS = 6
LEASE_SECONDS = 60


@dataclass
class OutboxMessage:
    id: int
    bot: str
    chat_id: int
    kind: str
    payload: dict
    attempts: int


def enqueue(
    conn: sqlite3.Connection,
    *,
    bot: str,
    chat_id: int,
    text: str | None = None,
    document_path: str | None = None,
    photo_path: str | None = None,
    caption: str | None = None,
    dedupe_key: str,
) -> None:
    """Queue one message. Call this inside the transaction that changes state."""
    if document_path:
        kind, payload = "document", {"path": document_path, "caption": caption}
    elif photo_path:
        kind, payload = "photo", {"path": photo_path, "caption": caption}
    else:
        kind, payload = "text", {"text": text or ""}
    now = clock.now_iso()
    conn.execute(
        """INSERT OR IGNORE INTO outbox(bot, chat_id, kind, payload_json, dedupe_key, status,
                                        next_attempt_at, created_at)
           VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)""",
        (bot, chat_id, kind, json.dumps(payload, ensure_ascii=False), dedupe_key, now, now),
    )


def staff_chat_ids(conn: sqlite3.Connection, role: str) -> list[int]:
    rows = conn.execute(
        "SELECT telegram_user_id FROM staff WHERE role = ? AND active = 1 ORDER BY id", (role,)
    ).fetchall()
    return [r[0] for r in rows]


def notify_staff(conn: sqlite3.Connection, role: str, text: str, dedupe_key: str) -> int:
    """Send `text` to every active staff member with `role`. Packing messages
    fall back to the owners when no packer is registered."""
    targets = staff_chat_ids(conn, role)
    if not targets and role == "packer":
        targets = staff_chat_ids(conn, "owner")
    for chat_id in targets:
        enqueue(conn, bot="shop", chat_id=chat_id, text=text, dedupe_key=f"{dedupe_key}:{chat_id}")
    return len(targets)


# ------------------------------------------------------------ worker side

def claim_due(db: Database, limit: int = 20) -> list[OutboxMessage]:
    """Take up to `limit` due messages and lease them to this worker.

    A row in 'sending' whose lease has ended belongs to a worker that died.
    It is claimed again. This is the crash-recovery path.
    """
    now = clock.now_iso()
    lease = clock.iso_in(seconds=LEASE_SECONDS)
    with db.write() as conn:
        rows = conn.execute(
            """UPDATE outbox
                  SET status = 'sending', lease_until = ?, attempts = attempts + 1
                WHERE id IN (
                    SELECT id FROM outbox
                     WHERE (status = 'pending' AND next_attempt_at <= ?)
                        OR (status = 'sending' AND lease_until < ?)
                     ORDER BY id
                     LIMIT ?)
            RETURNING id, bot, chat_id, kind, payload_json, attempts""",
            (lease, now, now, limit),
        ).fetchall()
    return [
        OutboxMessage(r["id"], r["bot"], r["chat_id"], r["kind"], json.loads(r["payload_json"]), r["attempts"])
        for r in sorted(rows, key=lambda r: r["id"])
    ]


def mark_sent(db: Database, message_id: int) -> None:
    with db.write() as conn:
        conn.execute(
            "UPDATE outbox SET status = 'sent', sent_at = ?, lease_until = NULL WHERE id = ?",
            (clock.now_iso(), message_id),
        )


def mark_failed_attempt(db: Database, message: OutboxMessage, error: str, permanent: bool = False) -> None:
    """Retry later with exponential backoff, or give up after MAX_ATTEMPTS.
    `permanent=True` (for example "chat not found") gives up at once."""
    give_up = permanent or message.attempts >= MAX_ATTEMPTS
    delay = min(10 * 2 ** (message.attempts - 1), 600)
    with db.write() as conn:
        conn.execute(
            """UPDATE outbox
                  SET status = ?, next_attempt_at = ?, lease_until = NULL, last_error = ?
                WHERE id = ?""",
            ("failed" if give_up else "pending", clock.iso_in(seconds=delay), error[:500], message.id),
        )
        if give_up:
            conn.execute(
                "INSERT INTO audit_log(actor, action, detail_json, created_at) VALUES ('system', ?, ?, ?)",
                ("outbox_gave_up", json.dumps({"outbox_id": message.id, "error": error[:200]}),
                 clock.now_iso()),
            )


def release_leases(db: Database, message_ids: list[int]) -> int:
    """On a clean shutdown (SIGTERM), give this worker's unsent rows back at
    once, so the next start does not wait for the lease to end. The attempt
    is not counted, because the message was never tried."""
    if not message_ids:
        return 0
    marks = ",".join("?" * len(message_ids))
    with db.write() as conn:
        cur = conn.execute(
            f"""UPDATE outbox SET status = 'pending', lease_until = NULL, attempts = attempts - 1
                 WHERE status = 'sending' AND id IN ({marks})""",
            message_ids,
        )
        return cur.rowcount
