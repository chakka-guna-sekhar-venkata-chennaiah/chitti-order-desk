"""Stock rules. Plain code; the model never calculates stock.

    on_hand  = SUM(stock_movements.qty_delta)
    reserved = SUM(active reservations)
    sellable = on_hand - reserved - safety_stock   (never below 0)

All functions take a connection that is inside `db.write()`, so the check
and the change happen in one transaction (no other writer can come between).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from order_desk import clock
from order_desk.domain.errors import InvalidInput, NotEnoughStock, NotFound


@dataclass(frozen=True)
class StockLevel:
    sku: str
    on_hand: int
    reserved: int
    safety_stock: int

    @property
    def sellable(self) -> int:
        return max(self.on_hand - self.reserved - self.safety_stock, 0)


def levels(conn: sqlite3.Connection, skus: list[str]) -> dict[str, StockLevel]:
    if not skus:
        return {}
    marks = ",".join("?" * len(skus))
    rows = conn.execute(
        f"SELECT sku, on_hand, reserved, safety_stock FROM v_stock WHERE sku IN ({marks})",
        list(skus),
    ).fetchall()
    return {r["sku"]: StockLevel(r["sku"], r["on_hand"], r["reserved"], r["safety_stock"]) for r in rows}


def level(conn: sqlite3.Connection, sku: str) -> StockLevel:
    found = levels(conn, [sku])
    if sku not in found:
        raise NotFound(f"No active product with SKU {sku}.")
    return found[sku]


def add_movement(
    conn: sqlite3.Connection, *, sku: str, delta: int, reason: str, actor: str,
    ref_type: str | None = None, ref_id: int | None = None, note: str | None = None,
) -> None:
    if delta == 0:
        raise InvalidInput("A stock movement cannot be zero.")
    conn.execute(
        """INSERT INTO stock_movements(sku, qty_delta, reason, ref_type, ref_id, actor, note, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (sku, delta, reason, ref_type, ref_id, actor, note, clock.now_iso()),
    )


def reserve(conn: sqlite3.Connection, order_id: int, items: dict[str, int]) -> None:
    """Hold stock for a confirmed order. All lines or none (raises NotEnoughStock).

    `reservations` is UNIQUE (order_id, sku) over every status and `release()` keeps its
    rows. So an order that was reopened and is confirmed again already has a 'released'
    row for each SKU it held before: that row is made active again with the new quantity
    (KI-001). Only a SKU the order never held gets a new row.
    """
    stock = levels(conn, list(items))
    short = {
        sku: (qty, stock[sku].sellable if sku in stock else 0)
        for sku, qty in items.items()
        if sku not in stock or stock[sku].sellable < qty
    }
    if short:
        detail = "; ".join(f"{sku}: wants {want}, sellable {have}" for sku, (want, have) in short.items())
        raise NotEnoughStock(f"Not enough stock: {detail}")
    now = clock.now_iso()
    for sku, qty in items.items():
        revived = conn.execute(
            "UPDATE reservations SET qty = ?, status = 'active', created_at = ?, closed_at = NULL"
            " WHERE order_id = ? AND sku = ? AND status = 'released'",
            (qty, now, order_id, sku),
        ).rowcount
        if not revived:
            conn.execute(
                "INSERT INTO reservations(order_id, sku, qty, status, created_at) VALUES (?, ?, ?, 'active', ?)",
                (order_id, sku, qty, now),
            )


def release(conn: sqlite3.Connection, order_id: int) -> int:
    """Give back all active reservations of an order (reject, cancel)."""
    cur = conn.execute(
        "UPDATE reservations SET status = 'released', closed_at = ? WHERE order_id = ? AND status = 'active'",
        (clock.now_iso(), order_id),
    )
    return cur.rowcount


def deduct_for_order(conn: sqlite3.Connection, order_id: int, items: dict[str, int], actor: str) -> None:
    """Remove stock for an approved order.

    The owner may have changed quantities. The stock this order may use is
    its own reservation plus what is sellable for everyone. The unique index
    ux_moves_order_deduct makes a second deduction for the same order fail.
    """
    held = {
        r["sku"]: r["qty"]
        for r in conn.execute(
            "SELECT sku, qty FROM reservations WHERE order_id = ? AND status = 'active'", (order_id,)
        )
    }
    stock = levels(conn, list(items))
    for sku, qty in items.items():
        available = (stock[sku].sellable if sku in stock else 0) + held.get(sku, 0)
        if qty > available:
            raise NotEnoughStock(f"{sku}: wants {qty}, available {available}.")
    for sku, qty in items.items():
        add_movement(conn, sku=sku, delta=-qty, reason="order_deduct", actor=actor,
                     ref_type="order", ref_id=order_id)
    conn.execute(
        "UPDATE reservations SET status = 'consumed', closed_at = ? WHERE order_id = ? AND status = 'active'",
        (clock.now_iso(), order_id),
    )
