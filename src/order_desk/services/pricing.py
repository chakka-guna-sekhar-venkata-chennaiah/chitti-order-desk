"""Prices: base price on the product, with an optional price for one dealer.

This is the general model: one price list for everybody, plus exceptions.
A shop with price tiers can later add a `price_tiers` table without changing
callers, because every caller asks `unit_price()`.
"""

from __future__ import annotations

import sqlite3

from order_desk.domain.errors import NotFound


def unit_price_paise(conn: sqlite3.Connection, dealer_id: int, sku: str) -> int:
    row = conn.execute(
        """SELECT COALESCE(dp.price_paise, p.base_price_paise) AS price
             FROM products p
             LEFT JOIN dealer_prices dp ON dp.sku = p.sku AND dp.dealer_id = ?
            WHERE p.sku = ?""",
        (dealer_id, sku),
    ).fetchone()
    if row is None:
        raise NotFound(f"No product with SKU {sku}.")
    return int(row["price"])


def rupees(paise: int | None) -> str:
    """12345678 -> '₹1,23,456.78' (Indian digit grouping)."""
    if paise is None:
        return "-"
    negative = paise < 0
    paise = abs(paise)
    whole, frac = divmod(paise, 100)
    digits = str(whole)
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        digits = ",".join(groups) + "," + tail
    text = f"₹{digits}" + (f".{frac:02d}" if frac else "")
    return f"-{text}" if negative else text
