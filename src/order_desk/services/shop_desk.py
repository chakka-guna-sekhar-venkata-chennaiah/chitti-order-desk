"""Everything the owner and the packing team can do. The shop MCP tools call these.

Every method takes `staff_telegram_user_id`, the ID of the person who sent
the Telegram message to the shop bot. `_staff()` checks that this person is
in the staff table with a role that may do the action:

    owner : everything
    packer: list and read orders, mark packed / dispatched, stock questions

Only allowlisted Telegram accounts can talk to the shop bot at all
(TELEGRAM_ALLOWED_IDS in NemoClaw). This role check is the second gate.
"""

from __future__ import annotations

import json
import sqlite3

from order_desk import clock
from order_desk.documents.charts import draw_chart
from order_desk.domain.catalog_terms import CATEGORIES, SPEC_KEYS, UNITS
from order_desk.domain.chitti import ChitLine
from order_desk.domain.errors import (
    Conflict, IllegalTransition, InvalidInput, NotAllowed, NotFound,
)
from order_desk.domain.states import Actor, State
from order_desk.services import credit, messages, orders, outbox, reports, stock
from order_desk.services.deps import Deps
from order_desk.services.matching import Product, _convert_quantity, load_products, match_line
from order_desk.services.pricing import rupees, unit_price_paise

OWNER = ("owner",)
ANY_STAFF = ("owner", "packer")


class ShopDesk:
    def __init__(self, deps: Deps):
        self.d = deps
        self.db = deps.db

    # =============================================================== orders
    def list_orders(self, staff_telegram_user_id: int, state: str | None = None, limit: int = 20) -> dict:
        with self.db.read() as conn:
            _staff(conn, staff_telegram_user_id, ANY_STAFF)
            if state is not None and state not in State.__members__:
                raise InvalidInput(f"Unknown state {state}. Use one of: {', '.join(State.__members__)}.")
            rows = conn.execute(
                "SELECT * FROM v_orders WHERE (? IS NULL OR state = ?) ORDER BY order_id DESC LIMIT ?",
                (state, state, max(1, min(int(limit), 100)))).fetchall()
        return {"ok": True, "orders": [dict(r) for r in rows]}

    def order_details(self, staff_telegram_user_id: int, order_id: int) -> dict:
        with self.db.read() as conn:
            _staff(conn, staff_telegram_user_id, ANY_STAFF)
            order = orders.get_order(conn, order_id)
            dealer = dict(conn.execute("SELECT * FROM dealers WHERE id = ?", (order["dealer_id"],)).fetchone())
            lines = orders.get_lines(conn, order_id, include_removed=False)
            names = orders.product_names(conn, [l["sku"] for l in lines])
            levels = stock.levels(conn, [l["sku"] for l in lines if l["sku"]])
            total = sum(unit_price_paise(conn, dealer["id"], l["sku"]) * l["qty"]
                        for l in lines if l["sku"] and l["qty"] and l["status"] in ("ok", "assumed"))
            events = [dict(r) for r in conn.execute(
                "SELECT from_state, to_state, actor, created_at FROM order_events WHERE order_id = ? ORDER BY id",
                (order_id,))]
            return {
                "ok": True, "order_id": order_id, "state": order["state"], "reason": order["state_reason"],
                "dealer": {"name": dealer["name"], "shop": dealer["shop_name"], "city": dealer["city"],
                           "status": dealer["status"], "telegram_user_id": dealer["telegram_user_id"]},
                "credit": credit.summary(conn, dealer["id"]).one_line(),
                "lines": [{"line_index": l["line_index"], "product": names.get(l["sku"], l["raw_text"]),
                           "sku": l["sku"], "qty": l["qty"], "unit": l["unit"], "status": l["status"],
                           "sellable_now": levels[l["sku"]].sellable if l["sku"] in levels else None}
                          for l in lines],
                "value": rupees(total),
                "history": events,
            }

    def approve(self, staff_telegram_user_id: int, order_id: int, edits: list[dict] | None = None,
                note: str | None = None) -> dict:
        with self.db.write() as conn:
            _staff(conn, staff_telegram_user_id, OWNER)
            order = orders.get_order(conn, order_id)
            if order["state"] in ("APPROVED", "PACKING", "PACKED", "DISPATCHED"):
                return {"ok": True, "order_id": order_id, "state": order["state"],
                        "message": f"Order #{order_id} is already approved."}
            if order["state"] != State.AWAITING_OWNER.value:
                raise IllegalTransition(f"Order #{order_id} is {order['state']}; only AWAITING_OWNER can be approved.")
            dealer = dict(conn.execute("SELECT * FROM dealers WHERE id = ?", (order["dealer_id"],)).fetchone())
            changes = _apply_owner_edits(conn, order_id, edits or [])
            lines = orders.get_lines(conn, order_id, include_removed=False)
            items = orders.active_items(lines)
            if not items:
                raise InvalidInput("After the edits the order has no lines. Use reject_order instead.")

            # Prices are fixed now, at approval time.
            total = 0
            for l in lines:
                price = unit_price_paise(conn, dealer["id"], l["sku"])
                conn.execute("UPDATE order_lines SET unit_price_paise = ? WHERE id = ?", (price, l["id"]))
                total += price * l["qty"]

            actor = f"staff:{staff_telegram_user_id}"
            stock.deduct_for_order(conn, order_id, items, actor)
            credit.create_bill(conn, dealer_id=dealer["id"], order_id=order_id, amount_paise=total,
                               terms_days=dealer["payment_terms_days"])
            if note:
                conn.execute("UPDATE orders SET owner_note = ? WHERE id = ?", (note, order_id))
            order = orders.transition(conn, order, State.APPROVED, Actor.OWNER, actor, detail={"changes": changes})
            order = orders.transition(conn, order, State.PACKING, Actor.SYSTEM, "system")

            names = orders.product_names(conn, [l["sku"] for l in lines])
            outbox.notify_staff(conn, "packer", messages.packing_slip(order_id, dealer, lines, names),
                                dedupe_key=f"packing:{order_id}")
            offers = credit.discount_offers(conn, clock.now_iso(), total)
            outbox.enqueue(conn, bot="dealer", chat_id=dealer["telegram_user_id"],
                           text=messages.dealer_approved(order_id, changes, total, offers),
                           dedupe_key=f"approved:{order_id}")
        return {"ok": True, "order_id": order_id, "state": "PACKING", "value": rupees(total),
                "changes": changes, "message": f"Order #{order_id} approved. Packing slip sent to the team."}

    def reject(self, staff_telegram_user_id: int, order_id: int, reason: str) -> dict:
        if not reason or not reason.strip():
            raise InvalidInput("Give a short reason. The dealer will see it.")
        with self.db.write() as conn:
            _staff(conn, staff_telegram_user_id, OWNER)
            order = orders.get_order(conn, order_id)
            if order["state"] == State.REJECTED.value:
                return {"ok": True, "order_id": order_id, "state": "REJECTED", "message": "Already rejected."}
            orders.transition(conn, order, State.REJECTED, Actor.OWNER, f"staff:{staff_telegram_user_id}",
                              reason=reason.strip())
            stock.release(conn, order_id)
            dealer_tg = conn.execute("SELECT telegram_user_id FROM dealers WHERE id = ?",
                                     (order["dealer_id"],)).fetchone()[0]
            outbox.enqueue(conn, bot="dealer", chat_id=dealer_tg,
                           text=messages.dealer_rejected(order_id, reason.strip()),
                           dedupe_key=f"rejected:{order_id}")
        return {"ok": True, "order_id": order_id, "state": "REJECTED", "message": f"Order #{order_id} rejected."}

    def mark_packed(self, staff_telegram_user_id: int, order_id: int) -> dict:
        return self._simple_move(staff_telegram_user_id, order_id, State.PACKING, State.PACKED,
                                 messages.dealer_packed(order_id), f"packed:{order_id}")

    def mark_dispatched(self, staff_telegram_user_id: int, order_id: int) -> dict:
        return self._simple_move(staff_telegram_user_id, order_id, State.PACKED, State.DISPATCHED,
                                 messages.dealer_dispatched(order_id), f"dispatched:{order_id}")

    def _simple_move(self, staff_tg: int, order_id: int, source: State, target: State,
                     dealer_text: str, dedupe: str) -> dict:
        with self.db.write() as conn:
            staff = _staff(conn, staff_tg, ANY_STAFF)
            order = orders.get_order(conn, order_id)
            if order["state"] == target.value:
                return {"ok": True, "order_id": order_id, "state": target.value, "message": "Already done."}
            if order["state"] != source.value:
                raise IllegalTransition(f"Order #{order_id} is {order['state']}, not {source.value}.")
            actor = Actor.OWNER if staff["role"] == "owner" else Actor.PACKER
            orders.transition(conn, order, target, actor, f"staff:{staff_tg}")
            dealer_tg = conn.execute("SELECT telegram_user_id FROM dealers WHERE id = ?",
                                     (order["dealer_id"],)).fetchone()[0]
            outbox.enqueue(conn, bot="dealer", chat_id=dealer_tg, text=dealer_text, dedupe_key=dedupe)
        return {"ok": True, "order_id": order_id, "state": target.value, "message": "Done. The dealer is told."}

    # ============================================================== dealers
    def set_dealer_status(self, staff_telegram_user_id: int, dealer_telegram_user_id: int, status: str) -> dict:
        if status not in ("active", "blocked"):
            raise InvalidInput("status must be 'active' or 'blocked'.")
        with self.db.write() as conn:
            _staff(conn, staff_telegram_user_id, OWNER)
            dealer = conn.execute("SELECT * FROM dealers WHERE telegram_user_id = ?",
                                  (int(dealer_telegram_user_id),)).fetchone()
            if dealer is None:
                raise NotFound(f"No dealer with Telegram ID {dealer_telegram_user_id}.")
            conn.execute("UPDATE dealers SET status = ? WHERE id = ?", (status, dealer["id"]))
            _audit(conn, staff_telegram_user_id, "dealer_status", {"dealer": dealer["id"], "status": status})
            if status == "active" and dealer["status"] == "pending":
                outbox.enqueue(conn, bot="dealer", chat_id=dealer["telegram_user_id"],
                               text=messages.dealer_welcome_active(dealer["name"]),
                               dedupe_key=f"welcome:{dealer['id']}")
        return {"ok": True, "dealer": dealer["name"], "status": status}

    def set_dealer_price(self, staff_telegram_user_id: int, dealer_telegram_user_id: int, sku: str,
                         price_rupees: float) -> dict:
        paise = _rupees_to_paise(price_rupees)
        with self.db.write() as conn:
            _staff(conn, staff_telegram_user_id, OWNER)
            dealer = conn.execute("SELECT id, name FROM dealers WHERE telegram_user_id = ?",
                                  (int(dealer_telegram_user_id),)).fetchone()
            if dealer is None:
                raise NotFound(f"No dealer with Telegram ID {dealer_telegram_user_id}.")
            _product(conn, sku)
            conn.execute(
                "INSERT INTO dealer_prices(dealer_id, sku, price_paise) VALUES (?, ?, ?) "
                "ON CONFLICT(dealer_id, sku) DO UPDATE SET price_paise = excluded.price_paise",
                (dealer["id"], sku, paise))
            _audit(conn, staff_telegram_user_id, "dealer_price", {"dealer": dealer["id"], "sku": sku, "paise": paise})
        return {"ok": True, "dealer": dealer["name"], "sku": sku, "price": rupees(paise)}

    def dealer_bills(self, staff_telegram_user_id: int, dealer_telegram_user_id: int) -> dict:
        with self.db.read() as conn:
            _staff(conn, staff_telegram_user_id, OWNER)
            dealer = conn.execute("SELECT id, name FROM dealers WHERE telegram_user_id = ?",
                                  (int(dealer_telegram_user_id),)).fetchone()
            if dealer is None:
                raise NotFound(f"No dealer with Telegram ID {dealer_telegram_user_id}.")
            bills = [{"bill_id": r["id"], "order_id": r["order_id"], "amount": rupees(r["amount_paise"]),
                      "paid": rupees(r["paid_paise"]), "issued_at": r["issued_at"], "due_at": r["due_at"],
                      "settled": r["paid_at"] is not None}
                     for r in conn.execute("SELECT * FROM bills WHERE dealer_id = ? ORDER BY id DESC", (dealer["id"],))]
            return {"ok": True, "dealer": dealer["name"], "summary": credit.summary(conn, dealer["id"]).one_line(),
                    "bills": bills}

    def record_payment(self, staff_telegram_user_id: int, bill_id: int, amount_rupees: float,
                       payment_ref: str) -> dict:
        """`payment_ref` (UPI reference, cheque number, or the Telegram message ID)
        makes this safe to retry: the same reference is recorded once."""
        if not payment_ref:
            raise InvalidInput("payment_ref is required, so one payment is never recorded two times.")
        key = f"payment:{payment_ref}"
        with self.db.write() as conn:
            _staff(conn, staff_telegram_user_id, OWNER)
            seen = conn.execute("SELECT result_json FROM idempotency_keys WHERE key = ?", (key,)).fetchone()
            if seen:
                return json.loads(seen["result_json"]) | {"repeat": True}
            result = {"ok": True} | credit.record_payment(conn, int(bill_id), _rupees_to_paise(amount_rupees))
            conn.execute("INSERT INTO idempotency_keys(key, result_json, created_at) VALUES (?, ?, ?)",
                         (key, json.dumps(result), clock.now_iso()))
            _audit(conn, staff_telegram_user_id, "payment", {"bill": bill_id, "ref": payment_ref})
        return result

    # ================================================================ stock
    def adjust_stock(self, staff_telegram_user_id: int, sku: str, counted_qty: int, note: str = "") -> dict:
        """Set the stock to what was counted on the shelf. Records the difference."""
        if not isinstance(counted_qty, int) or counted_qty < 0:
            raise InvalidInput("counted_qty must be a whole number, 0 or more.")
        with self.db.write() as conn:
            _staff(conn, staff_telegram_user_id, OWNER)
            level = stock.level(conn, sku)
            delta = counted_qty - level.on_hand
            if delta != 0:
                stock.add_movement(conn, sku=sku, delta=delta, reason="adjustment",
                                   actor=f"staff:{staff_telegram_user_id}", ref_type="count",
                                   note=note or "shelf count")
        return {"ok": True, "sku": sku, "before": level.on_hand, "after": counted_qty, "difference": delta}

    def add_product(self, staff_telegram_user_id: int, sku: str, name: str, category: str, unit: str,
                    price_rupees: float, brand: str | None = None, spec: dict | None = None,
                    pack_size: int = 1, safety_stock: int = 0) -> dict:
        if category not in CATEGORIES:
            raise InvalidInput(f"category must be one of: {', '.join(CATEGORIES)}.")
        if unit not in UNITS:
            raise InvalidInput(f"unit must be one of: {', '.join(UNITS)}.")
        spec = {k: v for k, v in (spec or {}).items() if k in SPEC_KEYS}
        with self.db.write() as conn:
            _staff(conn, staff_telegram_user_id, OWNER)
            if conn.execute("SELECT 1 FROM products WHERE sku = ?", (sku,)).fetchone():
                raise InvalidInput(f"SKU {sku} exists already.")
            conn.execute(
                """INSERT INTO products(sku, name, category, brand, spec_json, unit, pack_size,
                                        base_price_paise, safety_stock)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (sku, name, category, brand, json.dumps(spec), unit, int(pack_size),
                 _rupees_to_paise(price_rupees), int(safety_stock)))
            _audit(conn, staff_telegram_user_id, "add_product", {"sku": sku})
        return {"ok": True, "sku": sku, "message": "Product added with 0 stock. Use stock-in to add stock."}

    def add_alias(self, staff_telegram_user_id: int, alias: str, sku: str) -> dict:
        alias = " ".join((alias or "").lower().split())
        if not alias:
            raise InvalidInput("alias is empty.")
        with self.db.write() as conn:
            _staff(conn, staff_telegram_user_id, OWNER)
            _product(conn, sku)
            conn.execute("INSERT OR IGNORE INTO product_aliases(alias, sku) VALUES (?, ?)", (alias, sku))
        return {"ok": True, "alias": alias, "sku": sku}

    # ============================================================ stock-in
    def read_supplier_invoice(self, staff_telegram_user_id: int, image_ref: str,
                              supplier: str | None = None, invoice_no: str | None = None) -> dict:
        with self.db.read() as conn:
            _staff(conn, staff_telegram_user_id, OWNER)
        image = self.d.shop_images.resolve(image_ref)
        reading = self.d.reader.read_invoice_image(image.read(), image.mime)
        supplier = (supplier or reading.supplier or "").strip() or None
        invoice_no = (invoice_no or reading.invoice_no or "").strip() or None
        with self.db.write() as conn:
            if supplier and invoice_no:
                _refuse_known_invoice(conn, supplier, invoice_no)
            cur = conn.execute(
                """INSERT INTO supplier_invoices(supplier, invoice_no, invoice_date, state, source_ref,
                                                 created_by, created_at)
                   VALUES (?, ?, ?, 'draft', ?, ?, ?)""",
                (supplier, invoice_no, reading.invoice_date, image_ref,
                 f"staff:{staff_telegram_user_id}", clock.now_iso()))
            invoice_id = int(cur.lastrowid)
            products = load_products(conn)
            for line in reading.lines:
                _insert_invoice_line(conn, invoice_id, line, products)
            return self._invoice_preview(conn, invoice_id)

    def update_stock_in(self, staff_telegram_user_id: int, invoice_id: int, changes: list[dict]) -> dict:
        """Change forms: {"supplier": "..."}, {"invoice_no": "..."},
        {"line_index": n, "sku": "...", "sell_units": 20}, {"line_index": n, "remove": true},
        {"add_sku": "...", "sell_units": 10}."""
        with self.db.write() as conn:
            _staff(conn, staff_telegram_user_id, OWNER)
            inv = _draft_invoice(conn, invoice_id)
            for change in changes or []:
                if "supplier" in change or "invoice_no" in change:
                    supplier = change.get("supplier", inv["supplier"])
                    number = change.get("invoice_no", inv["invoice_no"])
                    if supplier and number:
                        _refuse_known_invoice(conn, supplier, number, exclude_id=invoice_id)
                    conn.execute("UPDATE supplier_invoices SET supplier = ?, invoice_no = ? WHERE id = ?",
                                 (supplier, number, invoice_id))
                    inv = _draft_invoice(conn, invoice_id)
                elif "add_sku" in change:
                    _product(conn, change["add_sku"])
                    index = conn.execute("SELECT COALESCE(MAX(line_index), 0) + 1 FROM supplier_invoice_lines "
                                         "WHERE invoice_id = ?", (invoice_id,)).fetchone()[0]
                    conn.execute(
                        """INSERT INTO supplier_invoice_lines(invoice_id, line_index, raw_text, sku, sell_units, status)
                           VALUES (?, ?, '(added by owner)', ?, ?, 'ok')""",
                        (invoice_id, index, change["add_sku"], _positive_int(change.get("sell_units"))))
                else:
                    row = conn.execute("SELECT * FROM supplier_invoice_lines WHERE invoice_id = ? AND line_index = ?",
                                       (invoice_id, change.get("line_index"))).fetchone()
                    if row is None:
                        raise NotFound(f"Invoice {invoice_id} has no line {change.get('line_index')}.")
                    if change.get("remove"):
                        conn.execute("UPDATE supplier_invoice_lines SET status = 'removed' WHERE id = ?", (row["id"],))
                        continue
                    sku = change.get("sku", row["sku"])
                    units = change.get("sell_units", row["sell_units"])
                    if not sku or not units:
                        raise InvalidInput(f"Line {row['line_index']} needs a sku and sell_units.")
                    _product(conn, sku)
                    conn.execute("UPDATE supplier_invoice_lines SET sku = ?, sell_units = ?, status = 'ok', "
                                 "note = NULL WHERE id = ?", (sku, _positive_int(units), row["id"]))
            return self._invoice_preview(conn, invoice_id)

    def apply_stock_in(self, staff_telegram_user_id: int, invoice_id: int) -> dict:
        with self.db.write() as conn:
            _staff(conn, staff_telegram_user_id, OWNER)
            inv = conn.execute("SELECT * FROM supplier_invoices WHERE id = ?", (invoice_id,)).fetchone()
            if inv is None:
                raise NotFound(f"No stock-in draft {invoice_id}.")
            if inv["state"] == "applied":
                return {"ok": True, "invoice_id": invoice_id, "message": "This invoice is already added to stock."}
            if inv["state"] != "draft":
                raise IllegalTransition(f"Stock-in {invoice_id} is {inv['state']}.")
            if not inv["supplier"] or not inv["invoice_no"]:
                raise InvalidInput("Give the supplier name and the invoice number first (update_stock_in).")
            lines = conn.execute("SELECT * FROM supplier_invoice_lines WHERE invoice_id = ? AND status <> 'removed'",
                                 (invoice_id,)).fetchall()
            open_lines = [l["line_index"] for l in lines if l["status"] != "ok"]
            if open_lines:
                raise InvalidInput(f"Lines {open_lines} still need an answer (update_stock_in).")
            if not lines:
                raise InvalidInput("The draft has no lines.")
            actor = f"staff:{staff_telegram_user_id}"
            for l in lines:
                stock.add_movement(conn, sku=l["sku"], delta=l["sell_units"], reason="stock_in", actor=actor,
                                   ref_type="supplier_invoice", ref_id=invoice_id,
                                   note=f"{inv['supplier']} {inv['invoice_no']}")
            conn.execute("UPDATE supplier_invoices SET state = 'applied', applied_at = ? WHERE id = ?",
                         (clock.now_iso(), invoice_id))
            _audit(conn, staff_telegram_user_id, "stock_in", {"invoice_id": invoice_id, "lines": len(lines)})
        return {"ok": True, "invoice_id": invoice_id, "lines_added": len(lines),
                "message": f"Stock added from {inv['supplier']} invoice {inv['invoice_no']}."}

    def discard_stock_in(self, staff_telegram_user_id: int, invoice_id: int) -> dict:
        with self.db.write() as conn:
            _staff(conn, staff_telegram_user_id, OWNER)
            _draft_invoice(conn, invoice_id)
            conn.execute("UPDATE supplier_invoices SET state = 'discarded' WHERE id = ?", (invoice_id,))
        return {"ok": True, "invoice_id": invoice_id, "message": "The draft is discarded. Stock did not change."}

    def _invoice_preview(self, conn: sqlite3.Connection, invoice_id: int) -> dict:
        inv = conn.execute("SELECT * FROM supplier_invoices WHERE id = ?", (invoice_id,)).fetchone()
        lines = conn.execute("SELECT * FROM supplier_invoice_lines WHERE invoice_id = ? AND status <> 'removed' "
                             "ORDER BY line_index", (invoice_id,)).fetchall()
        levels = stock.levels(conn, [l["sku"] for l in lines if l["sku"]])
        names = orders.product_names(conn, [l["sku"] for l in lines])
        text = [f"STOCK-IN DRAFT {invoice_id}: {inv['supplier'] or 'supplier ?'}, invoice {inv['invoice_no'] or '?'}", ""]
        for l in lines:
            if l["status"] == "ok":
                before = levels[l["sku"]].on_hand if l["sku"] in levels else 0
                text.append(f"{l['line_index']}. {names.get(l['sku'], l['sku'])}: {before} -> "
                            f"{before + l['sell_units']} (+{l['sell_units']})")
            else:
                text.append(f"{l['line_index']}. {l['raw_text']} (?) -> {l['note']}")
        missing = [x for x, v in (("supplier", inv["supplier"]), ("invoice number", inv["invoice_no"])) if not v]
        if missing:
            text.append(f"\nPlease give the {' and '.join(missing)}.")
        text.append("\nReply OK to add this stock, or send the changes.")
        return {"ok": True, "invoice_id": invoice_id, "preview": "\n".join(text),
                "needs_answers": [l["line_index"] for l in lines if l["status"] != "ok"],
                "missing": missing}

    # ============================================================== reports
    def report(self, staff_telegram_user_id: int, name: str) -> dict:
        with self.db.read() as conn:
            staff = _staff(conn, staff_telegram_user_id, ANY_STAFF)
        handlers = {"today": reports.today_summary, "pending_orders": reports.pending_orders,
                    "debtors": reports.debtors, "low_stock": reports.low_stock,
                    # Dealer requests carry phone, city and Telegram ID: the owner decides on them (KI-067).
                    "pending_work": lambda db: reports.pending_work(db, include_dealers=staff["role"] == "owner")}
        if name not in handlers:
            raise InvalidInput(f"Unknown report. Use one of: {', '.join(handlers)}.")
        return {"ok": True, "report": name, "data": handlers[name](self.db)}

    def describe_views(self, staff_telegram_user_id: int) -> dict:
        with self.db.read() as conn:
            _staff(conn, staff_telegram_user_id, OWNER)
        return {"ok": True, "views": reports.describe_views(self.db),
                "notes": "Money columns ending in _rupees are rupees. Times are UTC ISO text. "
                         "Use substr(created_at,1,10) for the date."}

    def run_query(self, staff_telegram_user_id: int, sql: str) -> dict:
        with self.db.read() as conn:
            _staff(conn, staff_telegram_user_id, OWNER)
        return {"ok": True} | reports.run_readonly_query(self.db, sql)

    def make_chart(self, staff_telegram_user_id: int, sql: str, kind: str, title: str) -> dict:
        with self.db.read() as conn:
            _staff(conn, staff_telegram_user_id, OWNER)
        result = reports.run_readonly_query(self.db, sql)
        path = draw_chart(result["rows"], kind, title, self.d.settings.generated_dir)
        with self.db.write() as conn:
            outbox.enqueue(conn, bot="shop", chat_id=int(staff_telegram_user_id), photo_path=str(path),
                           caption=title, dedupe_key=f"chart:{path.name}")
        return {"ok": True, "message": "The chart is on its way to the chat.", "rows": result["row_count"]}


# ------------------------------------------------------------------ helpers

def _staff(conn: sqlite3.Connection, telegram_user_id: int, roles: tuple[str, ...]) -> dict:
    row = conn.execute("SELECT * FROM staff WHERE telegram_user_id = ? AND active = 1",
                       (int(telegram_user_id),)).fetchone()
    if row is None:
        raise NotAllowed("This Telegram account is not shop staff.")
    if row["role"] not in roles:
        raise NotAllowed(f"Only the {' or '.join(roles)} can do this.")
    return dict(row)


def _product(conn: sqlite3.Connection, sku: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM products WHERE sku = ?", (sku,)).fetchone()
    if row is None:
        raise NotFound(f"No product {sku}. Use search_catalog or add_product.")
    return row


def _apply_owner_edits(conn: sqlite3.Connection, order_id: int, edits: list[dict]) -> list[str]:
    """Owner edits at approval: change a quantity or remove a line."""
    done = []
    for e in edits:
        row = conn.execute("SELECT * FROM order_lines WHERE order_id = ? AND line_index = ? AND status <> 'removed'",
                           (order_id, e.get("line_index"))).fetchone()
        if row is None:
            raise NotFound(f"Order #{order_id} has no line {e.get('line_index')}.")
        if e.get("remove"):
            conn.execute("UPDATE order_lines SET status = 'removed' WHERE id = ?", (row["id"],))
            done.append(f"line {row['line_index']} ({row['raw_text']}) removed")
        elif "qty" in e:
            qty = _positive_int(e["qty"])
            if qty != row["qty"]:
                conn.execute("UPDATE order_lines SET qty = ? WHERE id = ?", (qty, row["id"]))
                done.append(f"line {row['line_index']}: quantity {row['qty']} -> {qty} {row['unit']}")
        else:
            raise InvalidInput("Each edit needs 'qty' or 'remove'.")
    return done


def _insert_invoice_line(conn: sqlite3.Connection, invoice_id: int, line: ChitLine, products: list[Product]) -> None:
    """Match one invoice line and store it with status ok / unclear / unmatched.

    Stock-in is stricter than ordering: a line is 'ok' only when the product
    AND the number of sell units are certain. A guessed brand is not enough.
    """
    match = match_line(line, products)
    units: int | None = None
    if match.sku is None:
        status = "unmatched" if match.status == "unmatched" else "unclear"
        note = match.note
    else:
        product = next(p for p in products if p.sku == match.sku)
        status, note = "unclear", None
        if line.quantity is None:
            note = "Quantity is missing."
        elif line.unit is None and product.pack_size > 1:
            note = (f"Is this {line.quantity:g} {product.unit} or "
                    f"{line.quantity:g} packs of {product.pack_size}?")
        else:
            units, problem = _convert_quantity(line, product)
            if problem:
                note = problem
            elif match.status != "ok":
                note = match.note or "Please check this line."
            else:
                status = "ok"
    index = conn.execute("SELECT COALESCE(MAX(line_index), 0) + 1 FROM supplier_invoice_lines WHERE invoice_id = ?",
                         (invoice_id,)).fetchone()[0]
    conn.execute(
        """INSERT INTO supplier_invoice_lines(invoice_id, line_index, raw_text, item_json, sku, packs,
                                              sell_units, status, note)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (invoice_id, index, line.raw_text,
         json.dumps({"category": line.category, "spec": line.spec, "brand": line.brand}),
         match.sku, int(line.quantity) if line.quantity else None, units, status, note))


def _draft_invoice(conn: sqlite3.Connection, invoice_id: int) -> sqlite3.Row:
    inv = conn.execute("SELECT * FROM supplier_invoices WHERE id = ?", (invoice_id,)).fetchone()
    if inv is None:
        raise NotFound(f"No stock-in draft {invoice_id}.")
    if inv["state"] != "draft":
        raise IllegalTransition(f"Stock-in {invoice_id} is {inv['state']}; it cannot change.")
    return inv


def _refuse_known_invoice(conn: sqlite3.Connection, supplier: str, invoice_no: str, exclude_id: int | None = None) -> None:
    row = conn.execute(
        "SELECT id, state FROM supplier_invoices WHERE lower(supplier) = lower(?) AND invoice_no = ? "
        "AND state <> 'discarded' AND id <> COALESCE(?, -1)", (supplier, invoice_no, exclude_id)).fetchone()
    if row is not None:
        raise Conflict(f"Invoice {invoice_no} from {supplier} is already recorded (stock-in {row['id']}, "
                       f"{row['state']}). It cannot be added two times.")


def _positive_int(value) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0 or int(value) != value:
        raise InvalidInput(f"{value!r} must be a whole number above 0.")
    return int(value)


def _rupees_to_paise(value) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise InvalidInput(f"{value!r} is not a valid rupee amount.")
    return int(round(float(value) * 100))


def _audit(conn: sqlite3.Connection, staff_tg: int, action: str, detail: dict) -> None:
    conn.execute("INSERT INTO audit_log(actor, action, detail_json, created_at) VALUES (?, ?, ?, ?)",
                 (f"staff:{staff_tg}", action, json.dumps(detail), clock.now_iso()))
