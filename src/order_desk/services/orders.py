"""Order operations on one open transaction.

Every function here takes `conn` from `db.write()` (or `db.read()` for the
pure reads). They do not call the network. The facade in services/desk.py
calls the model first and then calls these functions in one transaction.
"""

from __future__ import annotations

import json
import sqlite3

from order_desk import clock
from order_desk.domain.chitti import ChitLine
from order_desk.domain.errors import Conflict, InvalidInput, NotAllowed, NotFound
from order_desk.domain.states import Actor, State, check_transition
from order_desk.services import stock
from order_desk.services.matching import MatchResult


# ------------------------------------------------------------------ reads

def get_order(conn: sqlite3.Connection, order_id: int) -> dict:
    row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if row is None:
        raise NotFound(f"There is no order #{order_id}.")
    return dict(row)


def get_lines(conn: sqlite3.Connection, order_id: int, include_removed: bool = True) -> list[dict]:
    sql = "SELECT * FROM order_lines WHERE order_id = ?"
    if not include_removed:
        sql += " AND status <> 'removed'"
    rows = conn.execute(sql + " ORDER BY line_index", (order_id,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["candidates"] = json.loads(d.pop("candidates_json"))
        d["item"] = json.loads(d.pop("item_json"))
        out.append(d)
    return out


def product_names(conn: sqlite3.Connection, skus: list[str | None]) -> dict[str, str]:
    skus = [s for s in skus if s]
    if not skus:
        return {}
    marks = ",".join("?" * len(skus))
    return {r["sku"]: r["name"] for r in conn.execute(
        f"SELECT sku, name FROM products WHERE sku IN ({marks})", skus)}


def active_items(lines: list[dict]) -> dict[str, int]:
    """SKU -> total quantity, for lines that are part of the order."""
    items: dict[str, int] = {}
    for l in lines:
        if l["status"] in ("ok", "assumed") and l["sku"] and l["qty"]:
            items[l["sku"]] = items.get(l["sku"], 0) + int(l["qty"])
    return items


def order_owned_by(conn: sqlite3.Connection, order_id: int, dealer_id: int) -> dict:
    order = get_order(conn, order_id)
    if order["dealer_id"] != dealer_id:
        # Same message as "not found": do not tell a dealer that another dealer's order exists.
        raise NotFound(f"There is no order #{order_id} for you.")
    return order


# ---------------------------------------------------------------- changes

def create(conn: sqlite3.Connection, *, dealer_id: int, source_kind: str, source_ref: str,
           linked_order_id: int | None = None) -> tuple[dict, bool]:
    """Create an order in RECEIVED, or return the existing one for the same source.

    The idempotency key is dealer + source message. If the agent calls the
    tool two times for the same Telegram message, there is still one order.
    """
    key = f"dealer:{dealer_id}:src:{source_ref}"
    existing = conn.execute("SELECT * FROM orders WHERE idempotency_key = ?", (key,)).fetchone()
    if existing:
        return dict(existing), False
    now = clock.now_iso()
    cur = conn.execute(
        """INSERT INTO orders(dealer_id, state, source_kind, source_ref, idempotency_key,
                              linked_order_id, created_at, updated_at)
           VALUES (?, 'RECEIVED', ?, ?, ?, ?, ?, ?)""",
        (dealer_id, source_kind, source_ref, key, linked_order_id, now, now),
    )
    order_id = int(cur.lastrowid)
    _event(conn, order_id, None, State.RECEIVED, "system", {"source_kind": source_kind})
    return get_order(conn, order_id), True


def transition(conn: sqlite3.Connection, order: dict, target: State, actor: Actor, actor_label: str,
               *, reason: str | None = None, detail: dict | None = None) -> dict:
    """Move an order to `target` after the state-machine check.

    `order` must be the row the caller read in THIS transaction. The UPDATE
    checks `version`, so a stale row cannot overwrite a newer change.
    """
    check_transition(order["state"], target, actor)
    cur = conn.execute(
        """UPDATE orders SET state = ?, version = version + 1, updated_at = ?,
                             state_reason = COALESCE(?, state_reason)
            WHERE id = ? AND version = ?""",
        (target.value, clock.now_iso(), reason, order["id"], order["version"]),
    )
    if cur.rowcount != 1:
        raise Conflict(f"Order #{order['id']} changed at the same time. Please try again.")
    _event(conn, order["id"], order["state"], target, actor_label, detail or {})
    return get_order(conn, order["id"])


def replace_lines(conn: sqlite3.Connection, order_id: int,
                  results: list[tuple[ChitLine, MatchResult]], *, append: bool = False) -> None:
    """Write matched lines. `append=True` adds a second photo of the same chitti."""
    if not append:
        conn.execute("DELETE FROM order_lines WHERE order_id = ?", (order_id,))
        start = 0
    else:
        start = conn.execute(
            "SELECT COALESCE(MAX(line_index), 0) FROM order_lines WHERE order_id = ?", (order_id,)
        ).fetchone()[0]
    for offset, (line, match) in enumerate(results, start=1):
        conn.execute(
            """INSERT INTO order_lines(order_id, line_index, raw_text, item_json, sku, qty, unit,
                                       status, note, candidates_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (order_id, start + offset, line.raw_text,
             json.dumps({"category": line.category, "spec": line.spec, "brand": line.brand,
                         "brand_raw": line.brand_raw, "mark": line.mark}, ensure_ascii=False),
             match.sku, match.qty, match.unit, match.status, match.note,
             json.dumps(match.candidates)),
        )


def apply_stock_check(conn: sqlite3.Connection, order_id: int) -> None:
    """Mark lines 'short' when the sellable stock is lower than the quantity.

    A 'short' line becomes 'ok' again when stock is enough (for example after
    the dealer lowered the quantity). The note tells the dealer the number.
    """
    lines = [l for l in get_lines(conn, order_id) if l["sku"] and l["qty"]]
    levels = stock.levels(conn, [l["sku"] for l in lines])
    for l in lines:
        if l["status"] not in ("ok", "assumed", "short"):
            continue
        sellable = levels[l["sku"]].sellable if l["sku"] in levels else 0
        if l["qty"] > sellable:
            note = f"Only {sellable} {l['unit']} available now." if sellable else "Out of stock now."
            conn.execute("UPDATE order_lines SET status = 'short', note = ? WHERE id = ?", (note, l["id"]))
        elif l["status"] == "short":
            conn.execute("UPDATE order_lines SET status = 'ok', note = NULL WHERE id = ?", (l["id"],))


def apply_dealer_changes(conn: sqlite3.Connection, order_id: int, changes: list[dict]) -> list[str]:
    """Apply the dealer's answers. Returns a short description of each change.

    Change forms (one key set per dict):
      {"line_index": 3, "qty": 10}             set the quantity (sell units)
      {"line_index": 3, "sku": "SW-6A-GM"}     choose the product
      {"line_index": 3, "accept": true}        keep the line as read
      {"line_index": 3, "remove": true}        remove the line
      {"add_sku": "TAPE-3M", "qty": 5}         add a new line
    A line that the dealer answered becomes 'ok' when it has a SKU and a quantity.
    """
    if not changes:
        raise InvalidInput("No changes were given.")
    done: list[str] = []
    for change in changes:
        if "add_sku" in change:
            done.append(_add_line(conn, order_id, change))
            continue
        line = _line_by_index(conn, order_id, change.get("line_index"))
        if change.get("remove"):
            conn.execute("UPDATE order_lines SET status = 'removed', note = NULL WHERE id = ?", (line["id"],))
            done.append(f"line {line['line_index']} removed")
            continue
        sku, qty = line["sku"], line["qty"]
        if "sku" in change:
            sku = _valid_sku(conn, change["sku"])
        if "qty" in change:
            qty = _valid_qty(change["qty"])
        if not change.get("accept") and "sku" not in change and "qty" not in change:
            raise InvalidInput(f"Change for line {line['line_index']} has nothing to do.")
        if not sku or not qty:
            raise InvalidInput(f"Line {line['line_index']} still needs a product and a quantity.")
        unit = conn.execute("SELECT unit FROM products WHERE sku = ?", (sku,)).fetchone()["unit"]
        conn.execute(
            "UPDATE order_lines SET sku = ?, qty = ?, unit = ?, status = 'ok', note = NULL WHERE id = ?",
            (sku, qty, unit, line["id"]),
        )
        done.append(f"line {line['line_index']}: {sku} x {qty} {unit}")
    return done


def _add_line(conn: sqlite3.Connection, order_id: int, change: dict) -> str:
    sku = _valid_sku(conn, change["add_sku"])
    qty = _valid_qty(change.get("qty"))
    product = conn.execute("SELECT name, unit FROM products WHERE sku = ?", (sku,)).fetchone()
    index = conn.execute(
        "SELECT COALESCE(MAX(line_index), 0) + 1 FROM order_lines WHERE order_id = ?", (order_id,)
    ).fetchone()[0]
    conn.execute(
        """INSERT INTO order_lines(order_id, line_index, raw_text, sku, qty, unit, status)
           VALUES (?, ?, ?, ?, ?, ?, 'ok')""",
        (order_id, index, f"(added) {product['name']}", sku, qty, product["unit"]),
    )
    return f"line {index} added: {sku} x {qty} {product['unit']}"


def _line_by_index(conn: sqlite3.Connection, order_id: int, line_index) -> dict:
    if not isinstance(line_index, int):
        raise InvalidInput("Each change needs a whole-number 'line_index' (or 'add_sku').")
    row = conn.execute(
        "SELECT * FROM order_lines WHERE order_id = ? AND line_index = ?", (order_id, line_index)
    ).fetchone()
    if row is None:
        raise NotFound(f"Order #{order_id} has no line {line_index}.")
    return dict(row)


def _valid_sku(conn: sqlite3.Connection, sku) -> str:
    row = conn.execute("SELECT sku FROM products WHERE sku = ? AND active = 1", (str(sku),)).fetchone()
    if row is None:
        raise NotFound(f"There is no product with SKU {sku}. Use search_catalog to find the SKU.")
    return row["sku"]


def _valid_qty(qty) -> int:
    if isinstance(qty, bool) or not isinstance(qty, (int, float)) or qty <= 0 or int(qty) != qty:
        raise InvalidInput(f"Quantity {qty!r} must be a whole number above 0.")
    return int(qty)


def lines_needing_answers(lines: list[dict]) -> list[dict]:
    return [l for l in lines if l["status"] in ("unclear", "unmatched", "short")]


def require_dealer_can_order(dealer: dict) -> None:
    if dealer["status"] == "blocked":
        raise NotAllowed("The shop does not take orders from this account now. Please call the shop.")
    if dealer["status"] == "pending":
        raise NotAllowed("The shop has not approved this account yet. Please wait for the shop.")


def similar_recent_order(conn: sqlite3.Connection, order: dict, window_hours: int) -> int | None:
    """Another open order from the same dealer with mostly the same SKUs, recently.

    Jaccard similarity of the SKU sets >= 0.8 counts as similar. This catches
    "the same chitti photographed again", which a file hash cannot catch.
    """
    mine = set(active_items(get_lines(conn, order["id"])))
    if not mine:
        return None
    since = clock.iso_in(hours=-window_hours)
    rows = conn.execute(
        """SELECT id FROM orders
            WHERE dealer_id = ? AND id <> ? AND created_at >= ?
              AND state NOT IN ('CANCELLED', 'EXPIRED', 'REJECTED')
            ORDER BY id DESC""",
        (order["dealer_id"], order["id"], since),
    ).fetchall()
    for r in rows:
        theirs = set(active_items(get_lines(conn, r["id"])))
        if theirs and len(mine & theirs) / len(mine | theirs) >= 0.8:
            return int(r["id"])
    return None


def _event(conn: sqlite3.Connection, order_id: int, from_state, to_state: State, actor: str,
           detail: dict) -> None:
    conn.execute(
        """INSERT INTO order_events(order_id, from_state, to_state, actor, detail_json, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (order_id, from_state, to_state.value, actor, json.dumps(detail, ensure_ascii=False),
         clock.now_iso()),
    )
