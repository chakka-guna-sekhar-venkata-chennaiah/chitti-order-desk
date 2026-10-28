"""Answers for the owner: fixed reports and safe read-only SQL.

Two layers
----------
1. Fixed reports (today, pending orders, debtors, low stock, pending work). They
   are plain SQL written by us: fast, correct, and the first choice for common questions.
2. `run_readonly_query()` for other questions. The agent writes the SQL. Four
   guards make this safe:
     a) the connection is opened read-only by SQLite (mode=ro, query_only);
     b) an authorizer callback allows only SELECT and only reads of the
        reporting views (v_*). Base tables, PRAGMA, ATTACH, and any write are
        refused by SQLite itself, before the query runs;
     c) at most MAX_ROWS rows are returned;
     d) a query that runs longer than TIME_LIMIT_S is stopped.
"""

from __future__ import annotations

import sqlite3
import time

from order_desk import clock
from order_desk.db.connection import Database
from order_desk.domain.errors import InvalidInput
from order_desk.services.credit import summary as credit_summary
from order_desk.services.pricing import rupees

REPORT_VIEWS = ("v_stock", "v_orders", "v_order_lines", "v_dealer_credit", "v_sales_daily")
# Base tables under the views. SQLite sometimes checks a whole-table read
# (column name "") with no view named, for example for COUNT(*) over a view.
# We allow that table-level check for these tables only. It can reveal a row
# COUNT, never a column value.
_VIEW_BASE_TABLES = ("orders", "dealers", "order_lines", "bills", "products", "stock_movements", "reservations")
MAX_ROWS = 200
TIME_LIMIT_S = 3.0

# Authorizer action codes -> readable names, for error messages.
_ACTION_NAMES = {getattr(sqlite3, f"SQLITE_{n}"): n for n in (
    "INSERT", "UPDATE", "DELETE", "PRAGMA", "ATTACH", "DETACH", "TRANSACTION", "ALTER_TABLE",
    "CREATE_TABLE", "CREATE_VIEW", "CREATE_INDEX", "CREATE_TRIGGER", "DROP_TABLE", "DROP_VIEW",
    "DROP_INDEX", "DROP_TRIGGER", "ANALYZE", "REINDEX", "SAVEPOINT", "CREATE_VTABLE", "DROP_VTABLE",
    "CREATE_TEMP_TABLE", "CREATE_TEMP_VIEW",
) if hasattr(sqlite3, f"SQLITE_{n}")}

# SQLite functions that are safe to call from text-to-SQL (pure: no writes, no files, no side effects).
# `like` is here because SQLite runs the operators `x LIKE y` / `x NOT LIKE y` as a call of the function
# like(y, x), and the authorizer reports that call. Without it every name search was refused (KI-061).
_SAFE_FUNCTIONS = {
    "count", "sum", "avg", "min", "max", "total", "round", "abs", "coalesce", "ifnull", "nullif",
    "lower", "upper", "length", "substr", "substring", "trim", "ltrim", "rtrim", "replace", "instr",
    "date", "time", "datetime", "strftime", "julianday", "printf", "format", "cast", "iif",
    "group_concat", "string_agg", "like",
}


# ------------------------------------------------------------ fixed reports

def today_summary(db: Database) -> dict:
    today = clock.now_iso()[:10]
    with db.read() as conn:
        by_state = {r["state"]: r["n"] for r in conn.execute(
            "SELECT state, COUNT(*) AS n FROM orders WHERE substr(created_at, 1, 10) = ? GROUP BY state", (today,))}
        sales = conn.execute(
            "SELECT COUNT(*) AS bills, COALESCE(SUM(amount_paise), 0) AS paise FROM bills "
            "WHERE order_id IS NOT NULL AND substr(issued_at, 1, 10) = ?", (today,)).fetchone()
        waiting = conn.execute("SELECT COUNT(*) FROM orders WHERE state = 'AWAITING_OWNER'").fetchone()[0]
        packing = conn.execute("SELECT COUNT(*) FROM orders WHERE state = 'PACKING'").fetchone()[0]
    return {
        "date": today,
        "orders_received_today_by_state": by_state,
        "approved_bills_today": sales["bills"],
        "approved_value_today": rupees(sales["paise"]),
        "waiting_for_your_approval_now": waiting,
        "being_packed_now": packing,
    }


def pending_orders(db: Database) -> list[dict]:
    with db.read() as conn:
        return [dict(r) for r in conn.execute(
            """SELECT order_id, state, dealer_name, dealer_shop, created_at, line_count
                 FROM v_orders
                WHERE state IN ('RECEIVED','PARSED','AWAITING_DEALER','AWAITING_OWNER','PACKING','PACKED')
                ORDER BY order_id""")]


def debtors(db: Database) -> list[dict]:
    with db.read() as conn:
        out = []
        for d in conn.execute("SELECT id, name, shop_name, status FROM dealers ORDER BY name"):
            s = credit_summary(conn, d["id"])
            if s.outstanding_paise > 0:
                out.append({"dealer": d["name"], "shop": d["shop_name"], "status": d["status"],
                            "outstanding": rupees(s.outstanding_paise), "overdue_bills": s.overdue_bills,
                            "overdue_amount": rupees(s.overdue_paise),
                            "oldest_overdue_days": s.oldest_overdue_days})
    return sorted(out, key=lambda r: (-(r["oldest_overdue_days"] or 0), r["dealer"]))


def low_stock(db: Database) -> list[dict]:
    with db.read() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT sku, name, on_hand, reserved, safety_stock, sellable FROM v_stock "
            "WHERE is_low = 1 ORDER BY category, sku")]


def pending_work(db: Database, include_dealers: bool = True) -> dict:
    """What waits for the shop, read from the tables and not from the outbox (KI-066, KI-067).

    The "new dealer" card and the approval card travel through the outbox, and the outbox can lose one:
    Telegram answers 400 "chat not found" until the owner has pressed START in the shop bot. The tables
    are the truth, so this report is the way back to that work. Each pending dealer has the Telegram ID
    that `set_dealer_status` needs, so the owner can approve a dealer by name.
    `include_dealers=False` leaves the dealer details (phone, city, Telegram ID) out for a packer."""
    with db.read() as conn:
        out: dict = {}
        if include_dealers:
            out["pending_dealers"] = [dict(r) for r in conn.execute(
                """SELECT telegram_user_id AS dealer_telegram_user_id, name, shop_name AS shop, phone, city,
                          created_at AS requested_at
                     FROM dealers WHERE status = 'pending' ORDER BY id""")]
        out["orders_waiting_for_approval"] = [dict(r) for r in conn.execute(
            """SELECT order_id, dealer_name AS dealer, dealer_shop AS shop, line_count, created_at
                 FROM v_orders WHERE state = 'AWAITING_OWNER' ORDER BY order_id""")]
    return out


# --------------------------------------------------------- read-only SQL

def describe_views(db: Database) -> dict[str, list[str]]:
    """The views and their columns. The agent reads this before it writes SQL."""
    with db.read() as conn:
        return {v: [f"{c['name']} {c['type'] or ''}".strip() for c in conn.execute(f"PRAGMA table_info({v})")]
                for v in REPORT_VIEWS}


def run_readonly_query(db: Database, sql: str) -> dict:
    sql = (sql or "").strip().rstrip(";").strip()
    if not sql:
        raise InvalidInput("The query is empty.")
    if ";" in sql:
        raise InvalidInput("Send one SELECT statement only.")

    refused: list[str] = []

    def authorizer(action, arg1, arg2, _db_name, source):
        if action == sqlite3.SQLITE_SELECT:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_READ:
            # Reading a view, or a base table on behalf of a view (source = view name).
            if arg1 in REPORT_VIEWS or source in REPORT_VIEWS:
                return sqlite3.SQLITE_OK
            if arg2 == "" and arg1 in _VIEW_BASE_TABLES:
                return sqlite3.SQLITE_OK
            if arg1 not in real_tables:                 # a WITH (CTE) name, not stored data
                return sqlite3.SQLITE_OK
            refused.append(f"read of '{arg1}'")
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_FUNCTION:
            if (arg2 or "").lower() in _SAFE_FUNCTIONS:
                return sqlite3.SQLITE_OK
            refused.append(f"function '{arg2}'")
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_RECURSIVE:          # WITH RECURSIVE: fine for date series
            return sqlite3.SQLITE_OK
        refused.append(_ACTION_NAMES.get(action, f"operation {action}"))
        return sqlite3.SQLITE_DENY

    deadline = time.monotonic() + TIME_LIMIT_S
    with db.read_only() as conn:
        real_tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")}
        real_tables |= {"sqlite_master", "sqlite_schema", "sqlite_temp_master", "sqlite_sequence"}
        conn.set_authorizer(authorizer)
        conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
        try:
            cursor = conn.execute(sql)
            rows = cursor.fetchmany(MAX_ROWS + 1)
        except sqlite3.DatabaseError as exc:
            if refused:
                raise InvalidInput(
                    f"Not allowed: {', '.join(refused)}. Use only SELECT on these views: "
                    f"{', '.join(REPORT_VIEWS)}.") from exc
            if "interrupted" in str(exc):
                raise InvalidInput(f"The query took longer than {TIME_LIMIT_S:g} s. Make it simpler.") from exc
            raise InvalidInput(f"SQL error: {exc}") from exc
        columns = [d[0] for d in cursor.description or []]
    truncated = len(rows) > MAX_ROWS
    return {
        "columns": columns,
        "rows": [dict(zip(columns, tuple(r))) for r in rows[:MAX_ROWS]],
        "row_count": min(len(rows), MAX_ROWS),
        "truncated": truncated,
    }
