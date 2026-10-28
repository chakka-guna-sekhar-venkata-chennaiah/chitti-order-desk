"""Everything a dealer can do. The dealer MCP tools call these methods.

Pattern used by every method that calls the model
-------------------------------------------------
    1. short transaction: check the caller and the order state
    2. NO transaction:    download the photo, call the vision model
    3. short transaction: check that nobody changed the order (version),
                          then write the result and queue the messages
A crash between the steps leaves the order in its earlier state, and the
same tool call can simply run again. See docs/06-reliability.md.

Every method takes `telegram_user_id`, the ID of the person who sent the
Telegram message. The dealer can only touch his own orders.
"""

from __future__ import annotations

import re
import sqlite3

from order_desk import clock
from order_desk.documents.catalog_pdf import build_catalog_pdf
from order_desk.domain.chitti import ChitReading
from order_desk.domain.errors import Conflict, IllegalTransition, InvalidInput, NotAllowed
from order_desk.domain.states import Actor, State
from order_desk.services import messages, orders, outbox, stock
from order_desk.services.credit import summary as credit_summary
from order_desk.services.deps import Deps
from order_desk.services.matching import load_products, match_line
from order_desk.services.pricing import rupees, unit_price_paise


class DealerDesk:
    def __init__(self, deps: Deps):
        self.d = deps
        self.db = deps.db

    # ================================================================ account
    def profile(self, telegram_user_id: int) -> dict:
        with self.db.read() as conn:
            dealer = _dealer_or_none(conn, telegram_user_id)
            if dealer is None:
                return {"ok": True, "status": "unknown",
                        "next_step": "Ask for name, shop name, phone and city. Then call register_dealer."}
            open_orders = [dict(r) for r in conn.execute(
                """SELECT id AS order_id, state, created_at FROM orders
                    WHERE dealer_id = ? AND state NOT IN ('DISPATCHED','REJECTED','EXPIRED','CANCELLED','PACKED')
                    ORDER BY id DESC LIMIT 10""", (dealer["id"],))]
        return {"ok": True, "status": dealer["status"], "name": dealer["name"],
                "shop_name": dealer["shop_name"], "open_orders": open_orders}

    def register(self, telegram_user_id: int, name: str, shop_name: str, phone: str, city: str) -> dict:
        if not name or not name.strip():
            raise InvalidInput("The name is required.")
        with self.db.write() as conn:
            dealer = _dealer_or_none(conn, telegram_user_id)
            if dealer is not None:
                return {"ok": True, "status": dealer["status"], "message": "You are already registered."}
            conn.execute(
                """INSERT INTO dealers(telegram_user_id, name, shop_name, phone, city, status, created_at)
                   VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
                (telegram_user_id, name.strip(), shop_name.strip() or None, phone.strip() or None,
                 city.strip() or None, clock.now_iso()),
            )
            outbox.notify_staff(conn, "owner", messages.new_dealer_request({
                "name": name, "shop_name": shop_name, "phone": phone, "city": city,
                "telegram_user_id": telegram_user_id}), dedupe_key=f"dealer_request:{telegram_user_id}")
        return {"ok": True, "status": "pending",
                "message": "Thank you. The shop will check your request. We will send a message when it is approved."}

    # ================================================================ catalog
    def search_catalog(self, query: str, category: str | None = None, limit: int = 10) -> dict:
        return {"ok": True, "results": search_products(self.db, query, category, limit)}

    def send_catalog(self, telegram_user_id: int) -> dict:
        with self.db.read() as conn:
            dealer = _dealer_or_none(conn, telegram_user_id)
        dealer_id = dealer["id"] if dealer else None
        path = build_catalog_pdf(self.db, self.d.settings.generated_dir, self.d.settings.shop_name, dealer_id)
        with self.db.write() as conn:
            outbox.enqueue(conn, bot="dealer", chat_id=telegram_user_id, document_path=str(path),
                           caption=f"{self.d.settings.shop_name} - catalog",
                           dedupe_key=f"catalog:{telegram_user_id}:{clock.now_iso()[:13]}")
        return {"ok": True, "message": "The catalog PDF is on its way to the chat."}

    # ================================================================ orders
    def start_order(self, telegram_user_id: int, source_kind: str, source_ref: str,
                    linked_to_order_id: int | None = None) -> dict:
        if source_kind not in ("photo", "text"):
            raise InvalidInput("source_kind must be 'photo' or 'text'.")
        if not source_ref:
            raise InvalidInput("source_ref is required (the Telegram message ID).")
        with self.db.write() as conn:
            dealer = _dealer(conn, telegram_user_id)
            orders.require_dealer_can_order(dealer)
            if linked_to_order_id is not None:
                parent = orders.order_owned_by(conn, linked_to_order_id, dealer["id"])
                if parent["state"] not in ("APPROVED", "PACKING", "PACKED"):
                    raise InvalidInput("Only an approved order can get a linked extra order. "
                                       "Change the open order instead.")
            order, created = orders.create(conn, dealer_id=dealer["id"], source_kind=source_kind,
                                           source_ref=str(source_ref), linked_order_id=linked_to_order_id)
        return {"ok": True, "order_id": order["id"], "state": order["state"], "created": created}

    def read_chit(self, telegram_user_id: int, order_id: int, image_ref: str, append: bool = False) -> dict:
        # 1. check
        with self.db.read() as conn:
            dealer = _dealer(conn, telegram_user_id)
            order = orders.order_owned_by(conn, order_id, dealer["id"])
            done = self._already_read(conn, order, append)
            if done:
                return done
        # 2. network: download, duplicate check, read
        image = self.d.dealer_images.resolve(image_ref)
        with self.db.write() as conn:
            order = orders.order_owned_by(conn, order_id, dealer["id"])
            duplicate = None if append else self._same_file_recently(conn, order, image.sha256)
            if duplicate:
                orders.transition(conn, order, State.CANCELLED, Actor.SYSTEM, "system",
                                  reason=f"duplicate photo of order #{duplicate}")
                return {"ok": True, "order_id": order_id, "state": "CANCELLED", "duplicate_of": duplicate,
                        "message": f"This is the same photo as order #{duplicate}. "
                                   f"We keep order #{duplicate} only."}
            if not append:
                conn.execute("UPDATE orders SET image_sha256 = ? WHERE id = ?", (image.sha256, order_id))
            version = order["version"]
        reading = self.d.reader.read_chit_image(image.read(), image.mime)
        # 3. write
        return self._save_reading(dealer, order_id, version, reading, append)

    def read_text_order(self, telegram_user_id: int, order_id: int, text: str) -> dict:
        if not text or not text.strip():
            raise InvalidInput("The order text is empty.")
        with self.db.read() as conn:
            dealer = _dealer(conn, telegram_user_id)
            order = orders.order_owned_by(conn, order_id, dealer["id"])
            done = self._already_read(conn, order, append=False)
            if done:
                return done
            version = order["version"]
        reading = self.d.reader.read_chit_text(text)
        return self._save_reading(dealer, order_id, version, reading, append=False)

    def update_lines(self, telegram_user_id: int, order_id: int, changes: list[dict]) -> dict:
        with self.db.write() as conn:
            dealer = _dealer(conn, telegram_user_id)
            order = orders.order_owned_by(conn, order_id, dealer["id"])
            _require_state(order, State.AWAITING_DEALER, "change lines")
            done = orders.apply_dealer_changes(conn, order_id, changes)
            orders.apply_stock_check(conn, order_id)
            orders.transition(conn, order, State.AWAITING_DEALER, Actor.DEALER, f"dealer:{telegram_user_id}",
                              detail={"changes": done})
            conn.execute("UPDATE orders SET dealer_check_deadline = ? WHERE id = ?",
                         (clock.iso_in(hours=self.d.settings.dealer_check_hours), order_id))
            result = self._readback_result(conn, order_id)
        result["changes"] = done
        return result

    def confirm(self, telegram_user_id: int, order_id: int) -> dict:
        with self.db.write() as conn:
            dealer = _dealer(conn, telegram_user_id)
            order = orders.order_owned_by(conn, order_id, dealer["id"])
            if order["state"] == State.AWAITING_OWNER.value:          # retry of a call that worked
                return {"ok": True, "order_id": order_id, "state": order["state"],
                        "message": f"Order #{order_id} is confirmed. The shop will check it now."}
            _require_state(order, State.AWAITING_DEALER, "confirm")
            orders.apply_stock_check(conn, order_id)
            lines = orders.get_lines(conn, order_id)
            open_questions = orders.lines_needing_answers(lines)
            if open_questions:
                result = self._readback_result(conn, order_id)
                result.update(ok=False, error="needs_dealer_answer",
                              message="Some lines still need an answer before the order can be confirmed.")
                return result                        # the 'short' marks are saved (commit)
            items = orders.active_items(lines)
            if not items:
                raise InvalidInput("The order has no lines. Add a line or cancel the order.")
            stock.reserve(conn, order_id, items)
            order = orders.transition(conn, order, State.CONFIRMED, Actor.DEALER, f"dealer:{telegram_user_id}")
            order = orders.transition(conn, order, State.AWAITING_OWNER, Actor.SYSTEM, "system")
            self._send_approval_card(conn, order, dealer)
        return {"ok": True, "order_id": order_id, "state": "AWAITING_OWNER",
                "message": f"Order #{order_id} is confirmed. The shop will check it now."}

    def reopen(self, telegram_user_id: int, order_id: int) -> dict:
        with self.db.write() as conn:
            dealer = _dealer(conn, telegram_user_id)
            order = orders.order_owned_by(conn, order_id, dealer["id"])
            _require_state(order, State.AWAITING_OWNER, "reopen")
            stock.release(conn, order_id)
            orders.transition(conn, order, State.AWAITING_DEALER, Actor.DEALER, f"dealer:{telegram_user_id}")
            conn.execute("UPDATE orders SET dealer_check_deadline = ? WHERE id = ?",
                         (clock.iso_in(hours=self.d.settings.dealer_check_hours), order_id))
            outbox.notify_staff(conn, "owner", messages.owner_withdrawn(order_id),
                                dedupe_key=f"withdrawn:{order_id}:{order['version']}")
            result = self._readback_result(conn, order_id)
        result["message"] = "The order is open again. Send the changes, then confirm again."
        return result

    def cancel(self, telegram_user_id: int, order_id: int, reason: str = "") -> dict:
        with self.db.write() as conn:
            dealer = _dealer(conn, telegram_user_id)
            order = orders.order_owned_by(conn, order_id, dealer["id"])
            if order["state"] == State.CANCELLED.value:
                return {"ok": True, "order_id": order_id, "state": "CANCELLED", "message": "The order is already cancelled."}
            was_with_owner = order["state"] == State.AWAITING_OWNER.value
            orders.transition(conn, order, State.CANCELLED, Actor.DEALER, f"dealer:{telegram_user_id}",
                              reason=reason or "cancelled by dealer")
            if was_with_owner:
                stock.release(conn, order_id)
                outbox.notify_staff(conn, "owner", messages.owner_cancelled(order_id),
                                    dedupe_key=f"cancelled:{order_id}")
        return {"ok": True, "order_id": order_id, "state": "CANCELLED", "message": f"Order #{order_id} is cancelled."}

    def order_status(self, telegram_user_id: int, order_id: int | None = None) -> dict:
        with self.db.read() as conn:
            dealer = _dealer(conn, telegram_user_id)
            if order_id is None:
                rows = conn.execute(
                    "SELECT id AS order_id, state, created_at, updated_at FROM orders WHERE dealer_id = ? "
                    "ORDER BY id DESC LIMIT 10", (dealer["id"],)).fetchall()
                return {"ok": True, "orders": [dict(r) for r in rows]}
            order = orders.order_owned_by(conn, order_id, dealer["id"])
            result = self._readback_result(conn, order_id)
            result.update(state=order["state"], reason=order["state_reason"])
            return result

    # ============================================================== internals
    def _already_read(self, conn: sqlite3.Connection, order: dict, append: bool) -> dict | None:
        """Make read_chit safe to call two times."""
        if append:
            if order["state"] != State.AWAITING_DEALER.value:
                raise IllegalTransition("A second photo can only be added while the order waits for your check.")
            return None
        if order["state"] in (State.PARSED.value, State.AWAITING_DEALER.value):
            return self._readback_result(conn, order["id"])
        if order["state"] != State.RECEIVED.value:
            raise IllegalTransition(f"Order #{order['id']} is {order['state']}; it cannot be read again.")
        return None

    def _same_file_recently(self, conn: sqlite3.Connection, order: dict, sha: str) -> int | None:
        since = clock.iso_in(hours=-24)
        row = conn.execute(
            """SELECT id FROM orders WHERE dealer_id = ? AND id <> ? AND image_sha256 = ?
                 AND created_at >= ? AND state NOT IN ('CANCELLED','EXPIRED','REJECTED')
               ORDER BY id LIMIT 1""",
            (order["dealer_id"], order["id"], sha, since)).fetchone()
        return int(row["id"]) if row else None

    def _save_reading(self, dealer: dict, order_id: int, version: int, reading: ChitReading, append: bool) -> dict:
        if not reading.lines:
            raise InvalidInput("No order lines were found. Please send a clearer photo, or type the order.")
        with self.db.write() as conn:
            order = orders.get_order(conn, order_id)
            if order["version"] != version:
                raise Conflict(f"Order #{order_id} changed while the chitti was read. Call order_status.")
            products = load_products(conn)
            results = [(line, match_line(line, products)) for line in reading.lines]
            orders.replace_lines(conn, order_id, results, append=append)
            orders.apply_stock_check(conn, order_id)
            if append:
                order = orders.transition(conn, order, State.AWAITING_DEALER, Actor.SYSTEM, "system",
                                          detail={"appended_lines": len(results)})
            else:
                order = orders.transition(conn, order, State.PARSED, Actor.SYSTEM, "system",
                                          detail={"lines": len(results), "model": reading.model})
                order = orders.transition(conn, order, State.AWAITING_DEALER, Actor.SYSTEM, "system")
            similar = orders.similar_recent_order(conn, order, self.d.settings.duplicate_window_hours)
            conn.execute(
                "UPDATE orders SET dealer_check_deadline = ?, possible_duplicate_of = ? WHERE id = ?",
                (clock.iso_in(hours=self.d.settings.dealer_check_hours), similar, order_id))
            return self._readback_result(conn, order_id)

    def _readback_result(self, conn: sqlite3.Connection, order_id: int) -> dict:
        order = orders.get_order(conn, order_id)
        lines = orders.get_lines(conn, order_id)
        names = orders.product_names(conn, [l["sku"] for l in lines])
        return {
            "ok": True,
            "order_id": order_id,
            "state": order["state"],
            "readback": messages.readback(order_id, lines, names, duplicate_hint=order["possible_duplicate_of"]),
            "lines": [{"line_index": l["line_index"], "raw_text": l["raw_text"], "sku": l["sku"],
                       "product": names.get(l["sku"]), "qty": l["qty"], "unit": l["unit"],
                       "status": l["status"], "note": l["note"], "candidates": l["candidates"]}
                      for l in lines if l["status"] != "removed"],
            "needs_answers": [l["line_index"] for l in orders.lines_needing_answers(lines)],
            "possible_duplicate_of": order["possible_duplicate_of"],
        }

    def _send_approval_card(self, conn: sqlite3.Connection, order: dict, dealer: dict) -> None:
        lines = orders.get_lines(conn, order["id"], include_removed=False)
        names = orders.product_names(conn, [l["sku"] for l in lines])
        levels = stock.levels(conn, [l["sku"] for l in lines if l["sku"]])
        notes = {sku: f"  [left after: {lv.sellable}]" for sku, lv in levels.items()}
        total = sum(unit_price_paise(conn, dealer["id"], l["sku"]) * l["qty"]
                    for l in lines if l["sku"] and l["qty"])
        credit = credit_summary(conn, dealer["id"]).one_line()
        card = messages.approval_card(order, dealer, lines, names, notes, credit, total)
        outbox.notify_staff(conn, "owner", card, dedupe_key=f"approval:{order['id']}:{order['version']}")


# ------------------------------------------------------------------ helpers

def _dealer_or_none(conn: sqlite3.Connection, telegram_user_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM dealers WHERE telegram_user_id = ?", (int(telegram_user_id),)).fetchone()
    return dict(row) if row else None


def _dealer(conn: sqlite3.Connection, telegram_user_id: int) -> dict:
    dealer = _dealer_or_none(conn, telegram_user_id)
    if dealer is None:
        raise NotAllowed("This Telegram account is not registered. Call register_dealer first.")
    return dealer


def _require_state(order: dict, state: State, action: str) -> None:
    if order["state"] != state.value:
        raise IllegalTransition(f"Cannot {action}: order #{order['id']} is {order['state']}, not {state.value}.")


_WORD = re.compile(r"[a-z0-9.&-]+")


def search_products(db, query: str, category: str | None = None, limit: int = 10) -> list[dict]:
    """Simple word-overlap search over names, aliases, brands and categories."""
    words = set(_WORD.findall((query or "").lower()))
    if not words and not category:
        raise InvalidInput("Give a search text or a category.")
    with db.read() as conn:
        rows = conn.execute(
            """SELECT p.sku, p.name, p.category, p.brand, p.unit, p.base_price_paise, s.sellable,
                      COALESCE((SELECT group_concat(alias, ' ') FROM product_aliases a WHERE a.sku = p.sku), '')
                        AS aliases
                 FROM products p JOIN v_stock s ON s.sku = p.sku
                WHERE p.active = 1 AND (? IS NULL OR p.category = ?)""",
            (category, category)).fetchall()
    scored = []
    for r in rows:
        haystack = set(_WORD.findall(f"{r['name']} {r['category']} {r['brand']} {r['aliases']}".lower()))
        score = len(words & haystack)
        if score or not words:
            scored.append((score, r))
    scored.sort(key=lambda x: (-x[0], x[1]["name"]))
    return [{"sku": r["sku"], "name": r["name"], "unit": r["unit"], "price": rupees(r["base_price_paise"]),
             "in_stock": r["sellable"] > 0} for _, r in scored[:limit]]
