"""Stock-in from a supplier invoice: draft -> corrections -> apply, never twice."""

from __future__ import annotations

import pytest

from conftest import OWNER, PACKER, line, stock_of
from order_desk.domain.chitti import ChitReading
from order_desk.domain.errors import Conflict, InvalidInput, NotAllowed
from order_desk.vision.reader import InvoiceReading


def _invoice(supplier, number, *lines):
    return InvoiceReading(supplier, number, "2026-10-24", ChitReading.from_model_json({"lines": list(lines)}).lines)


def test_invoice_adds_stock_only_after_apply(shop, deps, photo):
    before = stock_of(deps, "MCB-32A-SCHNEIDER")["on_hand"]
    deps.reader.invoices.append(_invoice("Schneider Dist", "INV-1",
                                         line("MCB 32A - 2 box", "mcb", {"rating_amp": 32}, "Schneider", 2, "box")))
    draft = shop.read_supplier_invoice(OWNER, photo())
    assert "+24" in draft["preview"]
    assert stock_of(deps, "MCB-32A-SCHNEIDER")["on_hand"] == before          # not yet
    shop.apply_stock_in(OWNER, draft["invoice_id"])
    assert stock_of(deps, "MCB-32A-SCHNEIDER")["on_hand"] == before + 24


def test_same_invoice_cannot_be_added_twice(shop, deps, photo):
    for _ in range(2):
        deps.reader.invoices.append(_invoice("Havells Dist", "H-77",
                                             line("6A switch 1 box", "switch", {"rating_amp": 6}, "Havells", 1, "box")))
    first = shop.read_supplier_invoice(OWNER, photo())
    shop.apply_stock_in(OWNER, first["invoice_id"])
    with pytest.raises(Conflict):
        shop.read_supplier_invoice(OWNER, photo())


def test_apply_twice_adds_once(shop, deps, photo):
    deps.reader.invoices.append(_invoice("X", "1", line("tape 2 box", "pvc_tape", {}, "3M", 2, "box")))
    draft = shop.read_supplier_invoice(OWNER, photo())
    before = stock_of(deps, "TAPE-3M")["on_hand"]
    shop.apply_stock_in(OWNER, draft["invoice_id"])
    again = shop.apply_stock_in(OWNER, draft["invoice_id"])
    assert "already" in again["message"]
    assert stock_of(deps, "TAPE-3M")["on_hand"] == before + 20


def test_missing_unit_for_a_pack_product_asks(shop, deps, photo):
    deps.reader.invoices.append(_invoice("X", "2", line("Anchor 6A switch 10", "switch", {"rating_amp": 6}, "Anchor", 10, None)))
    draft = shop.read_supplier_invoice(OWNER, photo())
    assert draft["needs_answers"] == [1]
    with pytest.raises(InvalidInput):
        shop.apply_stock_in(OWNER, draft["invoice_id"])
    shop.update_stock_in(OWNER, draft["invoice_id"], [{"line_index": 1, "sell_units": 100}])
    assert shop.apply_stock_in(OWNER, draft["invoice_id"])["lines_added"] == 1


def test_missing_invoice_number_must_be_given(shop, deps, photo):
    deps.reader.invoices.append(_invoice(None, None, line("tape 1 box", "pvc_tape", {}, "3M", 1, "box")))
    draft = shop.read_supplier_invoice(OWNER, photo())
    assert draft["missing"] == ["supplier", "invoice number"]
    with pytest.raises(InvalidInput):
        shop.apply_stock_in(OWNER, draft["invoice_id"])
    shop.update_stock_in(OWNER, draft["invoice_id"], [{"supplier": "3M Dist", "invoice_no": "Z9"}])
    assert shop.apply_stock_in(OWNER, draft["invoice_id"])["ok"]


def test_packer_cannot_do_stock_in(shop, photo):
    with pytest.raises(NotAllowed):
        shop.read_supplier_invoice(PACKER, photo())


def test_shelf_count_records_the_difference(shop, deps):
    before = stock_of(deps, "TAPE-3M")["on_hand"]
    result = shop.adjust_stock(OWNER, "TAPE-3M", before - 7, "monthly count")
    assert result["difference"] == -7
    assert stock_of(deps, "TAPE-3M")["on_hand"] == before - 7


def test_payment_with_same_reference_is_recorded_once(shop, deps):
    with deps.db.read() as conn:
        bill_id = conn.execute("SELECT id FROM bills WHERE amount_paise = 2200000").fetchone()[0]
    first = shop.record_payment(OWNER, bill_id, 22000, "UPI-123")
    second = shop.record_payment(OWNER, bill_id, 22000, "UPI-123")
    assert first["settled"] is True and second.get("repeat") is True
    with deps.db.read() as conn:
        assert conn.execute("SELECT paid_paise FROM bills WHERE id = ?", (bill_id,)).fetchone()[0] == 2200000
