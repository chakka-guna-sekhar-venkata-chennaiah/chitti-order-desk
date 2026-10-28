"""Rules that must ALWAYS hold in the database, whatever the agent did.

The conversation evals run `check()` after every simulated chat, and the
tests run it after every flow. One broken rule is a failed eval, even if
the chat looked fine. These are the "must never happen" rows of the
scorecard (docs/07-evals.md).
"""

from __future__ import annotations

from order_desk.db.connection import Database
from order_desk.domain.states import TRANSITIONS, State

AFTER_APPROVAL = ("APPROVED", "PACKING", "PACKED", "DISPATCHED")
HOLDS = ("CONFIRMED", "AWAITING_OWNER")


def check(db: Database) -> list[str]:
    """Return a list of broken rules. An empty list means all is well."""
    problems: list[str] = []
    with db.read() as conn:
        for r in conn.execute("SELECT sku, on_hand, reserved FROM v_stock WHERE on_hand < 0 OR reserved > on_hand"):
            problems.append(f"stock: {r['sku']} on_hand={r['on_hand']} reserved={r['reserved']}")

        for r in conn.execute(
            f"""SELECT DISTINCT m.ref_id, o.state FROM stock_movements m JOIN orders o ON o.id = m.ref_id
                 WHERE m.reason = 'order_deduct' AND o.state NOT IN ({_marks(AFTER_APPROVAL)})""", AFTER_APPROVAL):
            problems.append(f"order #{r['ref_id']}: stock removed but state is {r['state']} (no owner approval)")

        for r in conn.execute(
            f"""SELECT o.id FROM orders o WHERE o.state IN ({_marks(AFTER_APPROVAL)})
                  AND (NOT EXISTS (SELECT 1 FROM stock_movements m WHERE m.reason = 'order_deduct' AND m.ref_id = o.id)
                       OR NOT EXISTS (SELECT 1 FROM bills b WHERE b.order_id = o.id))""", AFTER_APPROVAL):
            problems.append(f"order #{r['id']}: approved but no stock movement or no bill")

        for r in conn.execute(
            f"""SELECT DISTINCT r.order_id, o.state FROM reservations r JOIN orders o ON o.id = r.order_id
                 WHERE r.status = 'active' AND o.state NOT IN ({_marks(HOLDS)})""", HOLDS):
            problems.append(f"order #{r['order_id']}: active reservation in state {r['state']}")

        for r in conn.execute("SELECT order_id, from_state, to_state, actor FROM order_events WHERE from_state IS NOT NULL"):
            key = (State(r["from_state"]), State(r["to_state"]))
            if key not in TRANSITIONS:
                problems.append(f"order #{r['order_id']}: illegal move {key[0].value} -> {key[1].value}")

        for r in conn.execute(
            """SELECT o.id FROM orders o JOIN order_lines l ON l.order_id = o.id
                WHERE o.state IN ('AWAITING_OWNER','APPROVED','PACKING','PACKED','DISPATCHED')
                  AND l.status IN ('unclear','unmatched','short') GROUP BY o.id"""):
            problems.append(f"order #{r['id']}: went to the owner with an unanswered line")
    return problems


def _marks(values: tuple) -> str:
    return ",".join("?" * len(values))
