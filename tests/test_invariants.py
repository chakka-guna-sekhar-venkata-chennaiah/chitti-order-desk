"""The invariant checker must catch a broken database (so a clean result means something)."""

from __future__ import annotations

from order_desk import clock
from order_desk.config import Settings
from order_desk.db.connection import Database
from order_desk.db.seed import seed_demo
from order_desk.services.invariants import check


def test_checker_finds_stock_removed_without_approval(tmp_path):
    db = Database(tmp_path / "broken.sqlite3")
    db.initialize()
    seed_demo(db, Settings(db_path=db.path))
    assert check(db) == []
    now = clock.now_iso()
    with db.write() as conn:
        conn.execute("INSERT INTO orders(id, dealer_id, state, source_kind, source_ref, idempotency_key, created_at, updated_at) "
                     "VALUES (99, 1, 'AWAITING_DEALER', 'photo', 'x', 'k', ?, ?)", (now, now))
        conn.execute("INSERT INTO stock_movements(sku, qty_delta, reason, ref_type, ref_id, actor, created_at) "
                     "VALUES ('TAPE-3M', -5, 'order_deduct', 'order', 99, 'bug', ?)", (now,))
        conn.execute("INSERT INTO order_events(order_id, from_state, to_state, actor, created_at) "
                     "VALUES (99, 'RECEIVED', 'PACKED', 'bug', ?)", (now,))
    problems = check(db)
    assert any("no owner approval" in p for p in problems)
    assert any("illegal move RECEIVED -> PACKED" in p for p in problems)
