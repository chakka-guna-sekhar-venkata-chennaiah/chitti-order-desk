"""Credit ledger: what a dealer owes, what is overdue, and cash-discount dates.

The owner sees this summary on every approval card. The code only REPORTS
credit; the owner DECIDES. The only automatic stop is a dealer that the owner
marked 'blocked'.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import timedelta

from order_desk import clock
from order_desk.domain.errors import NotFound
from order_desk.services.pricing import rupees


@dataclass(frozen=True)
class CreditSummary:
    outstanding_paise: int
    overdue_bills: int
    overdue_paise: int
    oldest_overdue_days: int | None

    @property
    def is_clear(self) -> bool:
        return self.overdue_bills == 0

    def one_line(self) -> str:
        if self.outstanding_paise == 0:
            return "No money due."
        text = f"Due: {rupees(self.outstanding_paise)}."
        if self.overdue_bills:
            text += (
                f" OVERDUE: {self.overdue_bills} bill(s), {rupees(self.overdue_paise)},"
                f" oldest {self.oldest_overdue_days} days past due."
            )
        else:
            text += " Nothing overdue."
        return text


def summary(conn: sqlite3.Connection, dealer_id: int) -> CreditSummary:
    now_iso = clock.now_iso()
    rows = conn.execute(
        "SELECT amount_paise, paid_paise, due_at FROM bills WHERE dealer_id = ? AND paid_at IS NULL",
        (dealer_id,),
    ).fetchall()
    outstanding = sum(r["amount_paise"] - r["paid_paise"] for r in rows)
    overdue = [r for r in rows if r["due_at"] < now_iso]
    oldest = None
    if overdue:
        oldest_due = min(clock.from_iso(r["due_at"]) for r in overdue)
        oldest = (clock.now() - oldest_due).days
    return CreditSummary(
        outstanding_paise=outstanding,
        overdue_bills=len(overdue),
        overdue_paise=sum(r["amount_paise"] - r["paid_paise"] for r in overdue),
        oldest_overdue_days=oldest,
    )


def discount_offers(conn: sqlite3.Connection, issued_at_iso: str, amount_paise: int) -> list[str]:
    """['Pay by 24 Nov for 3% off (save ₹123)', ...] for a new bill."""
    issued = clock.from_iso(issued_at_iso)
    offers = []
    for r in conn.execute("SELECT within_days, percent_bp FROM discount_tiers ORDER BY within_days"):
        by = (issued + timedelta(days=r["within_days"])).strftime("%d %b")
        saving = amount_paise * r["percent_bp"] // 10_000
        offers.append(f"Pay by {by} for {r['percent_bp'] / 100:g}% off (save {rupees(saving)})")
    return offers


def create_bill(conn: sqlite3.Connection, *, dealer_id: int, order_id: int, amount_paise: int,
                terms_days: int) -> int:
    issued = clock.now_iso()
    due = clock.iso_in(days=terms_days)
    cur = conn.execute(
        "INSERT INTO bills(dealer_id, order_id, amount_paise, issued_at, due_at) VALUES (?, ?, ?, ?, ?)",
        (dealer_id, order_id, amount_paise, issued, due),
    )
    return int(cur.lastrowid)


def record_payment(conn: sqlite3.Connection, bill_id: int, amount_paise: int) -> dict:
    """Record a payment. The discount tier is chosen by the payment date."""
    bill = conn.execute("SELECT * FROM bills WHERE id = ?", (bill_id,)).fetchone()
    if bill is None:
        raise NotFound(f"No bill {bill_id}.")
    days = (clock.now() - clock.from_iso(bill["issued_at"])).days
    tier = conn.execute(
        "SELECT percent_bp FROM discount_tiers WHERE within_days >= ? ORDER BY within_days LIMIT 1",
        (days,),
    ).fetchone()
    discount = bill["amount_paise"] * (tier["percent_bp"] if tier else 0) // 10_000
    paid = bill["paid_paise"] + amount_paise
    settled = paid + discount >= bill["amount_paise"]
    conn.execute(
        "UPDATE bills SET paid_paise = ?, discount_paise = ?, paid_at = ? WHERE id = ?",
        (paid, discount if settled else bill["discount_paise"], clock.now_iso() if settled else None, bill_id),
    )
    return {"bill_id": bill_id, "paid": rupees(paid), "discount": rupees(discount if settled else 0),
            "settled": settled}
