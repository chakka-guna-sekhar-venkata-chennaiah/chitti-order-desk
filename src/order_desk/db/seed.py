"""Demo data: catalog, opening stock, dealers, staff, discount tiers, old bills.

All prices are DEMO prices for the hackathon, not market prices.
Seeding runs only on an empty database, so `order-desk init` is safe to repeat.
"""

from __future__ import annotations

import csv
import sqlite3
from importlib import resources

from order_desk import clock
from order_desk.config import Settings
from order_desk.db.connection import Database


def _rows(name: str) -> list[dict[str, str]]:
    text = resources.files("order_desk.db").joinpath(f"seed_data/{name}").read_text("utf-8")
    return list(csv.DictReader(text.splitlines()))


def seed_demo(db: Database, settings: Settings) -> bool:
    """Insert demo data. Returns False if the catalog already has products."""
    with db.write() as conn:
        if conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] > 0:
            _seed_staff(conn, settings)          # staff IDs can change; keep them current
            return False
        now = clock.now_iso()
        _seed_catalog(conn, now)
        _seed_dealers(conn, now)
        _seed_staff(conn, settings)
        conn.executemany(
            "INSERT INTO discount_tiers(within_days, percent_bp) VALUES (?, ?)",
            [(30, 300), (60, 200)],               # 3 % within 30 days, 2 % within 60 days
        )
        _seed_old_bills(conn)
    return True


def _seed_catalog(conn: sqlite3.Connection, now: str) -> None:
    for r in _rows("catalog.csv"):
        conn.execute(
            """INSERT INTO products(sku, name, category, brand, spec_json, unit, pack_size,
                                    base_price_paise, safety_stock, is_default_brand)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (r["sku"], r["name"], r["category"], r["brand"], r["spec_json"], r["unit"],
             int(r["pack_size"]), int(r["base_price_rupees"]) * 100, int(r["safety_stock"]),
             int(r["is_default_brand"])),
        )
        conn.execute(
            """INSERT INTO stock_movements(sku, qty_delta, reason, ref_type, actor, note, created_at)
               VALUES (?, ?, 'opening', 'seed', 'system', 'opening stock', ?)""",
            (r["sku"], int(r["opening_stock"]), now),
        )
    conn.executemany(
        "INSERT OR IGNORE INTO product_aliases(alias, sku) VALUES (?, ?)",
        [(a["alias"], a["sku"]) for a in _rows("aliases.csv")],
    )


def _seed_dealers(conn: sqlite3.Connection, now: str) -> None:
    for d in _rows("dealers.csv"):
        conn.execute(
            """INSERT INTO dealers(telegram_user_id, name, shop_name, phone, city, status,
                                   payment_terms_days, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (int(d["telegram_user_id"]), d["name"], d["shop_name"], d["phone"], d["city"],
             d["status"], int(d["payment_terms_days"]), now),
        )


def _seed_staff(conn: sqlite3.Connection, settings: Settings) -> None:
    for tid in settings.owner_telegram_ids:
        conn.execute(
            "INSERT INTO staff(telegram_user_id, name, role) VALUES (?, 'Owner', 'owner') "
            "ON CONFLICT(telegram_user_id) DO UPDATE SET role = 'owner', active = 1",
            (tid,),
        )
    for tid in settings.packer_telegram_ids:
        conn.execute(
            "INSERT INTO staff(telegram_user_id, name, role) VALUES (?, 'Packer', 'packer') "
            "ON CONFLICT(telegram_user_id) DO NOTHING",
            (tid,),
        )


def _seed_old_bills(conn: sqlite3.Connection) -> None:
    """Opening balances so the credit check has something to show in the demo."""
    def dealer(tid: int) -> int:
        return conn.execute("SELECT id FROM dealers WHERE telegram_user_id = ?", (tid,)).fetchone()[0]

    bills = [
        # Suresh: one bill 75 days old, due 45 days ago -> overdue
        (dealer(900000002), 4_850_000, clock.iso_in(days=-75), clock.iso_in(days=-45)),
        # Venkat (blocked): two old bills, both overdue
        (dealer(900000004), 12_000_000, clock.iso_in(days=-120), clock.iso_in(days=-60)),
        (dealer(900000004), 3_500_000, clock.iso_in(days=-90), clock.iso_in(days=-30)),
        # Ramesh: one recent bill, not due yet
        (dealer(900000001), 2_200_000, clock.iso_in(days=-10), clock.iso_in(days=50)),
    ]
    conn.executemany(
        "INSERT INTO bills(dealer_id, amount_paise, issued_at, due_at) VALUES (?, ?, ?, ?)", bills
    )
