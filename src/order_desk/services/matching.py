"""Match one read chitti line to one catalog product (SKU).

This is deterministic code, not a model call. The model reads the
handwriting; this code decides which product that is. That split makes
matching fast, free and testable (see evals/run_matching.py).

How a line is matched
---------------------
1. Keep products of the same category.
2. Keep products whose spec agrees on every key that BOTH sides have
   (1.5 sq mm, red, 32 A, ...).
3. Brand:
   - written and known  -> keep that brand only
   - written but unknown ("Lec") -> unclear: ask the dealer
   - not written        -> if one product is left, use it; else use the
                           shop's default brand and mark the line 'assumed'
4. Quantity and unit: convert to the product's sell unit when possible
   (2 box of a product sold in nos with pack_size 10 -> 20 nos).
5. Any doubt from the reader (ambiguity, a tick or strike, poor legibility)
   makes the line 'unclear', but we still keep the best candidates so the
   read-back can offer them.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

from order_desk.domain.chitti import ChitLine


SPEC_WORDS = {"size_sqmm": "size (sq mm)", "color": "colour", "rating_amp": "rating (amps)",
              "watt": "wattage", "size_mm": "size (mm)", "ways": "number of ways", "modules": "number of modules"}


@dataclass(frozen=True)
class Product:
    sku: str
    name: str
    category: str
    brand: str | None
    spec: dict
    unit: str
    pack_size: int
    is_default_brand: bool


@dataclass
class MatchResult:
    status: str                     # ok | assumed | unclear | unmatched
    sku: str | None = None
    qty: int | None = None          # in the product's sell unit
    unit: str | None = None
    note: str | None = None         # reason, shown to the dealer
    candidates: list[str] = field(default_factory=list)


def load_products(conn: sqlite3.Connection) -> list[Product]:
    rows = conn.execute(
        "SELECT sku, name, category, brand, spec_json, unit, pack_size, is_default_brand "
        "FROM products WHERE active = 1"
    ).fetchall()
    return [
        Product(r["sku"], r["name"], r["category"], r["brand"], json.loads(r["spec_json"]),
                r["unit"], r["pack_size"], bool(r["is_default_brand"]))
        for r in rows
    ]


def _spec_agrees(line_spec: dict, product_spec: dict) -> bool:
    for key, value in line_spec.items():
        if key not in product_spec:
            continue
        if isinstance(value, float) or isinstance(product_spec[key], float):
            try:
                if abs(float(value) - float(product_spec[key])) > 1e-6:
                    return False
            except (TypeError, ValueError):
                return False
        elif str(value).lower() != str(product_spec[key]).lower():
            return False
    return True


def _convert_quantity(line: ChitLine, product: Product) -> tuple[int | None, str | None]:
    """Return (quantity in sell units, problem text or None)."""
    if line.quantity is None:
        return None, "Quantity is missing."
    qty = line.quantity
    if line.unit and line.unit != product.unit:
        if line.unit == "box" and product.unit in ("nos", "pcs"):
            qty = qty * product.pack_size
        elif line.unit in ("nos", "pcs") and product.unit in ("nos", "pcs"):
            pass                                   # same thing for counting
        else:
            return None, f"Unit '{line.unit}' does not fit this product (sold in {product.unit})."
    if qty <= 0 or abs(qty - round(qty)) > 1e-9:
        return None, f"Quantity {line.quantity:g} is not a whole number."
    return int(round(qty)), None


def match_line(line: ChitLine, products: list[Product]) -> MatchResult:
    if line.legibility == "unreadable" or not line.raw_text:
        return MatchResult("unclear", note="The line cannot be read.")
    if line.category is None and _reader_doubt(line):
        # The reader could not tell what the item is (ditto mark, "LED" without a type).
        # Ask the dealer; "not in our catalog" would be wrong here.
        return MatchResult("unclear", note=_reader_doubt(line))
    if line.category is None or line.category == "other":
        return MatchResult("unmatched", note="This item is not in our catalog.")

    pool = [p for p in products if p.category == line.category and _spec_agrees(line.spec, p.spec)]
    if not pool:
        return MatchResult("unmatched", note="We do not stock this item with this specification.")

    # The spec can still leave several products (for example: no color written for a wire).
    spec_keys_that_vary = {
        k for k in {k for p in pool for k in p.spec}
        if len({str(p.spec.get(k)) for p in pool}) > 1 and k not in line.spec
    }

    reader_doubt = _reader_doubt(line)

    # ---- brand
    if line.brand:
        branded = [p for p in pool if p.brand == line.brand]
        if not branded:
            return MatchResult(
                "unclear",
                note=f"We do not stock {line.brand} for this item.",
                candidates=[p.sku for p in pool][:5],
            )
        pool = branded
    elif line.brand_raw:
        return MatchResult(
            "unclear",
            note=f"Brand '{line.brand_raw}' is not known to us.",
            candidates=[p.sku for p in pool][:5],
        )

    if spec_keys_that_vary:
        missing = " and ".join(SPEC_WORDS.get(k, k) for k in sorted(spec_keys_that_vary))
        return MatchResult("unclear", note=f"Please give the {missing}.", candidates=[p.sku for p in pool][:5])

    status, note = "ok", None
    if len(pool) == 1:
        product = pool[0]
    else:
        defaults = [p for p in pool if p.is_default_brand]
        if len(defaults) != 1:
            return MatchResult("unclear", note="Please give the brand.", candidates=[p.sku for p in pool][:5])
        product = defaults[0]
        status, note = "assumed", f"No brand written; we chose {product.brand}."

    qty, qty_problem = _convert_quantity(line, product)
    others = [p.sku for p in pool if p.sku != product.sku][:4]
    if qty_problem:
        return MatchResult("unclear", sku=product.sku, unit=product.unit, note=qty_problem, candidates=others)
    if reader_doubt:
        return MatchResult("unclear", sku=product.sku, qty=qty, unit=product.unit, note=reader_doubt,
                           candidates=others)
    return MatchResult(status, sku=product.sku, qty=qty, unit=product.unit, note=note, candidates=others)


def _reader_doubt(line: ChitLine) -> str | None:
    if line.ambiguity:
        return line.ambiguity
    if line.mark == "strike":
        return "This line is crossed out. Do you want it?"
    if line.mark == "tick":
        return "This line has a tick mark. Do you want it?"
    if line.mark == "unclear":
        return "There is a mark on this line. Do you want it?"
    if line.legibility == "partial":
        return "Part of this line is not clear."
    return None
