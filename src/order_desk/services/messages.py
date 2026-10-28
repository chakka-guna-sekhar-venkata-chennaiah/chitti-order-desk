"""Every message text that code sends or returns, in one file.

Why here and not in the model's prompt?
    The read-back, the approval card and the packing slip must be exact and
    the same every time. Code builds them from the database. The agent sends
    them as they are and may add a short sentence around them.

Wording follows ASD-STE100 style: short sentences, active voice, one
instruction in each sentence, the same word for the same thing.
"""

from __future__ import annotations

from typing import Iterable

from order_desk.services.pricing import rupees

STATUS_MARK = {
    "ok": "",
    "assumed": " (?)",
    "unclear": " (?)",
    "unmatched": " (NOT IN CATALOG)",
    "short": " (LOW STOCK)",
}


def _line(line: dict, product_names: dict[str, str]) -> str:
    name = product_names.get(line["sku"]) if line["sku"] else None
    qty = f"{line['qty']} {line['unit']}" if line["qty"] is not None else "qty ?"
    head = f"{line['line_index']}. {name or line['raw_text']} - {qty}{STATUS_MARK.get(line['status'], '')}"
    if line.get("note") and line["status"] != "ok":
        head += f"\n   -> {line['note']}"
    return head


def readback(order_id: int, lines: list[dict], product_names: dict[str, str], *,
             duplicate_hint: int | None = None) -> str:
    active = [l for l in lines if l["status"] != "removed"]
    questions = [l for l in active if l["status"] in ("unclear", "unmatched", "short")]
    parts = [f"Order #{order_id}. We read your chitti like this:", ""]
    parts += [_line(l, product_names) for l in active]
    parts.append("")
    if duplicate_hint:
        parts.append(f"Note: this looks like order #{duplicate_hint}. Is this a new order?")
    if questions:
        numbers = ", ".join(str(l["line_index"]) for l in questions)
        parts.append(f"Please answer about line(s) {numbers}.")
    parts.append("Reply YES to confirm, or send the changes.")
    return "\n".join(parts)


def approval_card(order: dict, dealer: dict, lines: list[dict], product_names: dict[str, str],
                  stock_notes: dict[str, str], credit_line: str, total_paise: int) -> str:
    parts = [
        f"NEW ORDER #{order['id']}",
        f"Dealer: {dealer['name']} ({dealer['shop_name'] or '-'}), {dealer['city'] or ''}".rstrip(", "),
        f"Credit: {credit_line}",
        "",
    ]
    for l in lines:
        if l["status"] == "removed":
            continue
        note = stock_notes.get(l["sku"], "")
        parts.append(f"{l['line_index']}. {product_names.get(l['sku'], l['raw_text'])} x {l['qty']} {l['unit']}{note}")
    parts += [
        "",
        f"Value: {rupees(total_paise)}",
        f"Reply: APPROVE {order['id']}  or  REJECT {order['id']} <reason>",
        f"To change a quantity: APPROVE {order['id']} with line 3 = 10",
    ]
    return "\n".join(parts)


def packing_slip(order_id: int, dealer: dict, lines: list[dict], product_names: dict[str, str]) -> str:
    parts = [f"PACKING SLIP - ORDER #{order_id}", f"For: {dealer['name']} ({dealer['shop_name'] or '-'})", ""]
    for l in lines:
        if l["status"] == "removed":
            continue
        parts.append(f"[ ] {product_names.get(l['sku'], l['raw_text'])} x {l['qty']} {l['unit']}")
    parts += ["", f"When all items are packed, reply: PACKED {order_id}"]
    return "\n".join(parts)


def dealer_approved(order_id: int, changes: list[str], total_paise: int, offers: Iterable[str]) -> str:
    parts = [f"Order #{order_id} is approved. The team is packing it now."]
    if changes:
        parts += ["The shop changed these lines:"] + [f"- {c}" for c in changes]
    parts.append(f"Bill amount: {rupees(total_paise)}.")
    offers = list(offers)
    if offers:
        parts.append("Cash discount: " + "; ".join(offers) + ".")
    return "\n".join(parts)


def dealer_rejected(order_id: int, reason: str) -> str:
    return f"Order #{order_id} is not accepted. Reason: {reason}. Please call the shop for help."


def dealer_packed(order_id: int) -> str:
    return f"Order #{order_id} is packed. It is ready for pickup or dispatch."


def dealer_dispatched(order_id: int) -> str:
    return f"Order #{order_id} has left the shop."


def dealer_expired(order_id: int) -> str:
    return (f"Order #{order_id} is closed because we did not get your confirmation in time. "
            "Send the chitti again to order.")


def owner_reminder(order_id: int, hours: int) -> str:
    return f"Reminder: order #{order_id} has waited {hours} hour(s) for your approval."


def owner_withdrawn(order_id: int) -> str:
    return f"Order #{order_id} is withdrawn by the dealer for changes. Do not pack it."


def owner_cancelled(order_id: int) -> str:
    return f"Order #{order_id} is cancelled by the dealer. Do not pack it."


def new_dealer_request(dealer: dict) -> str:
    return (
        "NEW DEALER REQUEST\n"
        f"Name: {dealer['name']}\nShop: {dealer.get('shop_name') or '-'}\n"
        f"Phone: {dealer.get('phone') or '-'}\nCity: {dealer.get('city') or '-'}\n"
        f"Telegram ID: {dealer['telegram_user_id']}\n"
        f"Reply: APPROVE DEALER {dealer['telegram_user_id']}  or  BLOCK DEALER {dealer['telegram_user_id']}"
    )


def dealer_welcome_active(name: str) -> str:
    return f"Welcome, {name}. Your account is active. You can send your order now."
