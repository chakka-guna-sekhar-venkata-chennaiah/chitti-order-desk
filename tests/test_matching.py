"""The deterministic matcher: chitti line -> SKU, or a clear reason why not."""

from __future__ import annotations

import pytest

from order_desk.domain.chitti import ChitReading
from order_desk.services.matching import load_products, match_line


@pytest.fixture
def products(deps):
    with deps.db.read() as conn:
        return load_products(conn)


def _line(**kw):
    item = {"category": kw.pop("category", None), "spec": kw.pop("spec", {}),
            "brand": kw.pop("brand", None), "brand_raw": kw.pop("brand_raw", None)}
    return ChitReading.from_model_json({"lines": [{"raw_text": kw.pop("raw", "x"), "item": item, **kw}]}).lines[0]


def test_full_line_matches_exactly(products):
    m = match_line(_line(category="wire", spec={"size_sqmm": 1.5, "color": "red"}, brand="Finolex",
                         quantity=2, unit="coil"), products)
    assert (m.status, m.sku, m.qty) == ("ok", "WIRE-1.5-RED-FINOLEX", 2)


def test_brand_code_is_normalized(products):
    m = match_line(_line(category="mcb", spec={"rating_amp": 32}, brand_raw="Sch", quantity=2), products)
    assert m.sku == "MCB-32A-SCHNEIDER" and m.status == "ok"


def test_missing_brand_uses_default_and_says_so(products):
    m = match_line(_line(category="switch", spec={"rating_amp": 6}, quantity=20, unit="nos"), products)
    assert (m.status, m.sku) == ("assumed", "SW-6A-ANCHOR")
    assert "Anchor" in m.note


def test_unknown_brand_code_asks(products):
    m = match_line(_line(category="modular_box", spec={"modules": 3}, brand_raw="Lec", quantity=5), products)
    assert m.status == "unclear" and "Lec" in m.note


def test_missing_wire_color_asks(products):
    m = match_line(_line(category="wire", spec={"size_sqmm": 2.5}, brand="Polycab", quantity=1, unit="coil"), products)
    assert m.status == "unclear" and "colour" in m.note
    assert len(m.candidates) == 3


def test_brand_not_stocked_offers_others(products):
    m = match_line(_line(category="ceiling_fan", spec={"size_mm": 1200}, brand="Philips", quantity=1), products)
    assert m.status == "unclear" and m.candidates


def test_box_is_converted_to_pieces(products):
    m = match_line(_line(category="switch", spec={"rating_amp": 6}, brand="GM", quantity=2, unit="box"), products)
    assert (m.sku, m.qty, m.unit) == ("SW-6A-GM", 20, "nos")


def test_coil_for_a_switch_is_refused(products):
    m = match_line(_line(category="switch", spec={"rating_amp": 6}, brand="GM", quantity=2, unit="coil"), products)
    assert m.status == "unclear" and "Unit" in m.note


def test_strike_mark_asks_even_when_matched(products):
    m = match_line(_line(category="switch", spec={"rating_amp": 6}, brand="Anchor", quantity=20, mark="strike"), products)
    assert m.status == "unclear" and m.sku == "SW-6A-ANCHOR"


def test_not_in_catalog(products):
    m = match_line(_line(category="other", raw="paint 1 L", quantity=1), products)
    assert m.status == "unmatched"


def test_fraction_quantity_asks(products):
    m = match_line(_line(category="pvc_tape", brand="3M", quantity=2.5), products)
    assert m.status == "unclear" and "whole number" in m.note


def test_model_json_with_fences_and_bad_values_is_cleaned():
    raw = """Here is the JSON:
```json
{"header": {"date_raw": "25/10"}, "lines": [
  {"raw_text": "6A sw - 20 A", "mark": "weird", "legibility": "great",
   "item": {"category": "spaceship", "spec": {"rating_amp": "6", "colour": "red"}, "brand_raw": "A"},
   "quantity": "20", "unit": "Nos"}]}
```"""
    reading = ChitReading.from_model_json(raw)
    line = reading.lines[0]
    assert line.mark == "none" and line.legibility == "clear"
    assert line.category is None                      # unknown category is dropped, not guessed
    assert line.spec == {"rating_amp": 6.0}           # unknown spec key dropped
    assert line.brand == "Anchor" and line.quantity == 20.0 and line.unit == "nos"
