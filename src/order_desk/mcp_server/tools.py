"""The tools that the agents can call, in two scopes.

    dealer scope  -> /dealer/mcp  (dealer bot sandbox)
    shop scope    -> /shop/mcp    (shop bot sandbox: owner and packers)

A sandbox holds ONE bearer token, and the token decides the scope. So the
dealer agent cannot even SEE the approve or stock-in tools. Permission comes
from the construction, not from the prompt.

Each tool is a small function with type hints. The MCP SDK makes the JSON
schema from the hints and the `Field(description=...)` text, so the text here
is what the model reads. Keep it short and exact.

`ToolSpec.identity_param` names the parameter that carries the caller's
Telegram user ID. The reference agent (agent/loop.py) hides this parameter
from the model and fills it from the real Telegram update, so the model can
never act as somebody else. Under Hermes the model must pass it; see
docs/09-security.md for that limit.
"""

from __future__ import annotations

import functools
import logging
from dataclasses import dataclass
from typing import Annotated, Any, Callable

from pydantic import Field

from order_desk.domain.errors import OrderDeskError
from order_desk.services.dealer_desk import DealerDesk
from order_desk.services.shop_desk import ShopDesk

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    fn: Callable[..., dict]
    scope: str                       # "dealer" or "shop"
    identity_param: str | None


def _safe(fn: Callable[..., dict]) -> Callable[..., dict]:
    """Turn our known errors into a result the model can read and act on."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except OrderDeskError as exc:
            return exc.to_dict()
        except Exception:                      # a bug: log it, do not leak internals
            log.exception("tool %s failed", fn.__name__)
            return {"ok": False, "error": "internal_error",
                    "message": "Something went wrong on the server. Please try again later."}
    return wrapper


DealerId = Annotated[int, Field(description="Telegram user ID of the person who sent the message.")]
StaffId = Annotated[int, Field(description="Telegram user ID of the staff member who sent the message.")]
OrderId = Annotated[int, Field(description="Order number, for example 12.")]
ImageRef = Annotated[str, Field(description=(
    "Photo reference. If the message says '[Image attached at: <path>]', use 'sandbox:<path>'. "
    "Other forms: 'tg:<telegram file_id>' or 'up:<upload id>'."))]


# =============================================================== dealer scope

def dealer_tools(desk: DealerDesk) -> list[ToolSpec]:

    def get_dealer_profile(telegram_user_id: DealerId) -> dict:
        """Call FIRST for every new conversation. Returns the dealer status
        (unknown, pending, active, blocked) and the open orders."""
        return desk.profile(telegram_user_id)

    def register_dealer(
        telegram_user_id: DealerId,
        name: Annotated[str, Field(description="Person name.")],
        shop_name: Annotated[str, Field(description="Shop or company name.")] = "",
        phone: Annotated[str, Field(description="Phone number.")] = "",
        city: Annotated[str, Field(description="City or town.")] = "",
    ) -> dict:
        """Ask the shop to accept a new dealer. Use only when the status is 'unknown'."""
        return desk.register(telegram_user_id, name, shop_name, phone, city)

    def search_catalog(
        query: Annotated[str, Field(description="Words to search, for example '6a switch anchor'.")],
        category: Annotated[str | None, Field(description="Optional category, for example 'switch'.")] = None,
    ) -> dict:
        """Find products and their SKU codes. Use it to answer product questions
        and to find the SKU when the dealer adds or changes a line."""
        return desk.search_catalog(query, category)

    def send_catalog_pdf(telegram_user_id: DealerId) -> dict:
        """Send the current catalog PDF (products and prices) to the dealer's chat."""
        return desk.send_catalog(telegram_user_id)

    def start_order(
        telegram_user_id: DealerId,
        source_kind: Annotated[str, Field(description="'photo' for a chitti photo, 'text' for a typed order.")],
        source_ref: Annotated[str, Field(description="The Telegram message ID of the order message.")],
        linked_to_order_id: Annotated[int | None, Field(description="Only for 'add more' to an APPROVED order.")] = None,
    ) -> dict:
        """Create an order for a new chitti or typed order. Calling it again with
        the same source_ref returns the same order."""
        return desk.start_order(telegram_user_id, source_kind, source_ref, linked_to_order_id)

    def read_chitti_photo(
        telegram_user_id: DealerId,
        order_id: OrderId,
        image_ref: ImageRef,
        append: Annotated[bool, Field(description="True when this photo is page 2 of the same chitti.")] = False,
    ) -> dict:
        """Read the chitti photo with the vision model, match the products and check
        the stock. Returns 'readback': send this text to the dealer AS IT IS."""
        return desk.read_chit(telegram_user_id, order_id, image_ref, append)

    def read_text_order(
        telegram_user_id: DealerId,
        order_id: OrderId,
        text: Annotated[str, Field(description="The typed order, exactly as the dealer wrote it.")],
    ) -> dict:
        """Like read_chitti_photo, for an order typed as a message."""
        return desk.read_text_order(telegram_user_id, order_id, text)

    def update_order_lines(
        telegram_user_id: DealerId,
        order_id: OrderId,
        changes: Annotated[list[dict[str, Any]], Field(description=(
            "List of changes. Forms: {'line_index':3,'qty':10} | {'line_index':3,'sku':'SW-6A-GM'} | "
            "{'line_index':3,'accept':true} | {'line_index':3,'remove':true} | {'add_sku':'TAPE-3M','qty':5}"))],
    ) -> dict:
        """Apply the dealer's answers or changes. Returns a new 'readback' to send."""
        return desk.update_lines(telegram_user_id, order_id, changes)

    def confirm_order(telegram_user_id: DealerId, order_id: OrderId) -> dict:
        """Call ONLY after the dealer clearly said yes to the latest read-back.
        Reserves the stock and sends the order to the owner for approval."""
        return desk.confirm(telegram_user_id, order_id)

    def reopen_order(telegram_user_id: DealerId, order_id: OrderId) -> dict:
        """The dealer wants a change after confirming, before the owner decided."""
        return desk.reopen(telegram_user_id, order_id)

    def cancel_order(
        telegram_user_id: DealerId, order_id: OrderId,
        reason: Annotated[str, Field(description="Short reason from the dealer.")] = "",
    ) -> dict:
        """Cancel an order that the owner did not approve yet."""
        return desk.cancel(telegram_user_id, order_id, reason)

    def get_order_status(
        telegram_user_id: DealerId,
        order_id: Annotated[int | None, Field(description="Order number. Leave empty to list recent orders.")] = None,
    ) -> dict:
        """The state and the lines of one order, or the dealer's recent orders."""
        return desk.order_status(telegram_user_id, order_id)

    fns = [get_dealer_profile, register_dealer, search_catalog, send_catalog_pdf, start_order,
           read_chitti_photo, read_text_order, update_order_lines, confirm_order, reopen_order,
           cancel_order, get_order_status]
    return [ToolSpec(f.__name__, _safe(f), "dealer",
                     "telegram_user_id" if "telegram_user_id" in f.__code__.co_varnames else None)
            for f in fns]


# ================================================================= shop scope

def shop_tools(desk: ShopDesk, dealer_desk: DealerDesk) -> list[ToolSpec]:

    def list_orders(
        staff_telegram_user_id: StaffId,
        state: Annotated[str | None, Field(description="Filter: AWAITING_OWNER, PACKING, PACKED, ...")] = None,
        limit: Annotated[int, Field(description="Maximum rows (1-100).")] = 20,
    ) -> dict:
        """List orders, newest first."""
        return desk.list_orders(staff_telegram_user_id, state, limit)

    def get_order_details(staff_telegram_user_id: StaffId, order_id: OrderId) -> dict:
        """One order with dealer, credit, lines, stock left and history."""
        return desk.order_details(staff_telegram_user_id, order_id)

    def approve_order(
        staff_telegram_user_id: StaffId, order_id: OrderId,
        edits: Annotated[list[dict[str, Any]] | None, Field(description=(
            "Optional owner changes: {'line_index':3,'qty':10} or {'line_index':3,'remove':true}."))] = None,
        note: Annotated[str | None, Field(description="Optional note.")] = None,
    ) -> dict:
        """OWNER ONLY. Approve an order: removes the stock, makes the bill, sends the
        packing slip to the team and tells the dealer."""
        return desk.approve(staff_telegram_user_id, order_id, edits, note)

    def reject_order(
        staff_telegram_user_id: StaffId, order_id: OrderId,
        reason: Annotated[str, Field(description="Reason. The dealer sees it.")],
    ) -> dict:
        """OWNER ONLY. Reject an order and release its stock."""
        return desk.reject(staff_telegram_user_id, order_id, reason)

    def mark_packed(staff_telegram_user_id: StaffId, order_id: OrderId) -> dict:
        """All items of the order are packed. Tells the dealer."""
        return desk.mark_packed(staff_telegram_user_id, order_id)

    def mark_dispatched(staff_telegram_user_id: StaffId, order_id: OrderId) -> dict:
        """The packed order has left the shop. Tells the dealer."""
        return desk.mark_dispatched(staff_telegram_user_id, order_id)

    def set_dealer_status(
        staff_telegram_user_id: StaffId,
        dealer_telegram_user_id: Annotated[int, Field(description="The dealer's Telegram user ID.")],
        status: Annotated[str, Field(description="'active' to approve or unblock, 'blocked' to stop orders.")],
    ) -> dict:
        """OWNER ONLY. Approve a new dealer, block a dealer or unblock a dealer."""
        return desk.set_dealer_status(staff_telegram_user_id, dealer_telegram_user_id, status)

    def set_dealer_price(
        staff_telegram_user_id: StaffId,
        dealer_telegram_user_id: Annotated[int, Field(description="The dealer's Telegram user ID.")],
        sku: Annotated[str, Field(description="Product SKU.")],
        price_rupees: Annotated[float, Field(description="Price for this dealer, in rupees.")],
    ) -> dict:
        """OWNER ONLY. Give one dealer a special price for one product."""
        return desk.set_dealer_price(staff_telegram_user_id, dealer_telegram_user_id, sku, price_rupees)

    def get_dealer_bills(
        staff_telegram_user_id: StaffId,
        dealer_telegram_user_id: Annotated[int, Field(description="The dealer's Telegram user ID.")],
    ) -> dict:
        """OWNER ONLY. The dealer's bills and credit summary."""
        return desk.dealer_bills(staff_telegram_user_id, dealer_telegram_user_id)

    def record_payment(
        staff_telegram_user_id: StaffId,
        bill_id: Annotated[int, Field(description="Bill number from get_dealer_bills.")],
        amount_rupees: Annotated[float, Field(description="Amount paid, in rupees.")],
        payment_ref: Annotated[str, Field(description="UPI ref, cheque no. or the Telegram message ID.")],
    ) -> dict:
        """OWNER ONLY. Record a payment. The same payment_ref is recorded only once."""
        return desk.record_payment(staff_telegram_user_id, bill_id, amount_rupees, payment_ref)

    def adjust_stock(
        staff_telegram_user_id: StaffId,
        sku: Annotated[str, Field(description="Product SKU.")],
        counted_qty: Annotated[int, Field(description="Quantity counted on the shelf, in sell units.")],
        note: Annotated[str, Field(description="Why, for example 'monthly count'.")] = "",
    ) -> dict:
        """OWNER ONLY. Set the stock to the counted quantity. The difference is recorded."""
        return desk.adjust_stock(staff_telegram_user_id, sku, counted_qty, note)

    def add_product(
        staff_telegram_user_id: StaffId,
        sku: Annotated[str, Field(description="New unique code, for example 'SW-6A-LEGRAND'.")],
        name: Annotated[str, Field(description="Name shown to people.")],
        category: Annotated[str, Field(description="One of the catalog categories, for example 'switch'.")],
        unit: Annotated[str, Field(description="Sell unit: coil, nos, box, pcs or metre.")],
        price_rupees: Annotated[float, Field(description="Base price in rupees.")],
        brand: Annotated[str | None, Field(description="Brand.")] = None,
        spec: Annotated[dict[str, Any] | None, Field(description="For example {'rating_amp': 6}.")] = None,
        pack_size: Annotated[int, Field(description="Sell units in one supplier pack.")] = 1,
        safety_stock: Annotated[int, Field(description="Never sell below this quantity.")] = 0,
    ) -> dict:
        """OWNER ONLY. Add a new product with 0 stock."""
        return desk.add_product(staff_telegram_user_id, sku, name, category, unit, price_rupees,
                                brand, spec, pack_size, safety_stock)

    def add_product_alias(
        staff_telegram_user_id: StaffId,
        alias: Annotated[str, Field(description="Local name people write, for example 'fin 1.5 red'.")],
        sku: Annotated[str, Field(description="Product SKU.")],
    ) -> dict:
        """OWNER ONLY. Teach a local name for a product."""
        return desk.add_alias(staff_telegram_user_id, alias, sku)

    def read_supplier_invoice(
        staff_telegram_user_id: StaffId, image_ref: ImageRef,
        supplier: Annotated[str | None, Field(description="Supplier name, if the owner said it.")] = None,
        invoice_no: Annotated[str | None, Field(description="Invoice number, if the owner said it.")] = None,
    ) -> dict:
        """OWNER ONLY. Read a manufacturer invoice photo into a STOCK-IN DRAFT.
        Stock does not change yet. Send 'preview' to the owner AS IT IS."""
        return desk.read_supplier_invoice(staff_telegram_user_id, image_ref, supplier, invoice_no)

    def update_stock_in(
        staff_telegram_user_id: StaffId,
        invoice_id: Annotated[int, Field(description="Stock-in draft number.")],
        changes: Annotated[list[dict[str, Any]], Field(description=(
            "Forms: {'supplier':'...'} | {'invoice_no':'...'} | {'line_index':2,'sku':'...','sell_units':20} | "
            "{'line_index':2,'remove':true} | {'add_sku':'...','sell_units':10}"))],
    ) -> dict:
        """OWNER ONLY. Correct a stock-in draft. Returns a new preview."""
        return desk.update_stock_in(staff_telegram_user_id, invoice_id, changes)

    def apply_stock_in(
        staff_telegram_user_id: StaffId,
        invoice_id: Annotated[int, Field(description="Stock-in draft number.")],
    ) -> dict:
        """OWNER ONLY. Call only after the owner said OK to the latest preview. Adds the stock."""
        return desk.apply_stock_in(staff_telegram_user_id, invoice_id)

    def discard_stock_in(
        staff_telegram_user_id: StaffId,
        invoice_id: Annotated[int, Field(description="Stock-in draft number.")],
    ) -> dict:
        """OWNER ONLY. Throw away a stock-in draft. Stock does not change."""
        return desk.discard_stock_in(staff_telegram_user_id, invoice_id)

    def get_report(
        staff_telegram_user_id: StaffId,
        name: Annotated[str, Field(description=(
            "'today', 'pending_orders', 'debtors', 'low_stock' or 'pending_work'. 'pending_work' lists the "
            "dealer requests that wait for a decision (with dealer_telegram_user_id, name, shop, phone, city; "
            "owner only) and the orders waiting for approval."))],
    ) -> dict:
        """Ready-made reports. Use these first for common questions. 'pending_work' reads the database,
        so it is right even when a Telegram card was lost. Use it for /start and for "what is pending"."""
        return desk.report(staff_telegram_user_id, name)

    def describe_reporting_views(staff_telegram_user_id: StaffId) -> dict:
        """OWNER ONLY. The views and columns that run_readonly_query may read.
        Call this before writing SQL."""
        return desk.describe_views(staff_telegram_user_id)

    def run_readonly_query(
        staff_telegram_user_id: StaffId,
        sql: Annotated[str, Field(description="ONE SQLite SELECT statement on the v_* views only.")],
    ) -> dict:
        """OWNER ONLY. Answer a question that no report covers. Read-only; max 200 rows."""
        return desk.run_query(staff_telegram_user_id, sql)

    def send_chart(
        staff_telegram_user_id: StaffId,
        sql: Annotated[str, Field(description="SELECT with 2 columns: label, number.")],
        kind: Annotated[str, Field(description="'bar' or 'line'.")],
        title: Annotated[str, Field(description="Chart title.")],
    ) -> dict:
        """OWNER ONLY. Draw a chart from a read-only query and send it to the chat."""
        return desk.make_chart(staff_telegram_user_id, sql, kind, title)

    def search_catalog(
        query: Annotated[str, Field(description="Words to search, for example 'mcb 32'.")],
        category: Annotated[str | None, Field(description="Optional category.")] = None,
    ) -> dict:
        """Find products and SKU codes."""
        return dealer_desk.search_catalog(query, category)

    fns = [list_orders, get_order_details, approve_order, reject_order, mark_packed, mark_dispatched,
           set_dealer_status, set_dealer_price, get_dealer_bills, record_payment, adjust_stock,
           add_product, add_product_alias, read_supplier_invoice, update_stock_in, apply_stock_in,
           discard_stock_in, get_report, describe_reporting_views, run_readonly_query, send_chart,
           search_catalog]
    return [ToolSpec(f.__name__, _safe(f), "shop",
                     "staff_telegram_user_id" if "staff_telegram_user_id" in f.__code__.co_varnames else None)
            for f in fns]
