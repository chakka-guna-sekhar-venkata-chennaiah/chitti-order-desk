"""Data structures for one read chitti (or one supplier invoice).

The model returns JSON in this shape, and the golden set uses the SAME shape
(see evals/golden/README.md). So the chitti-reader eval can compare the
model output with the label field by field.

`ChitReading.from_model_json()` accepts imperfect model output: it fixes
types, drops unknown values and never invents a value. A field the model
did not give stays None.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from order_desk.domain.catalog_terms import (
    CATEGORIES, SPEC_KEYS, UNITS, normalize_brand, normalize_unit,
)

MARKS = ("none", "tick", "strike", "unclear")
LEGIBILITY = ("clear", "partial", "unreadable")


@dataclass
class ChitLine:
    line_index: int                       # real order, top to bottom, from 1
    raw_text: str                         # exactly as written
    line_no_written: str | None = None    # the number the writer wrote (can be wrong)
    mark: str = "none"                    # none | tick | strike | unclear
    category: str | None = None
    spec: dict[str, Any] = field(default_factory=dict)
    brand: str | None = None              # normalized brand, or None
    brand_raw: str | None = None          # as written ("HV")
    quantity: float | None = None
    unit: str | None = None
    uses_ditto: bool = False
    legibility: str = "clear"             # clear | partial | unreadable
    ambiguity: str | None = None          # free text: why a person cannot be sure

    @property
    def needs_question(self) -> bool:
        """True if the dealer must answer before this line is safe to use."""
        return (
            self.ambiguity is not None
            or self.mark != "none"
            or self.legibility != "clear"
        )


@dataclass
class ChitReading:
    shop_name_raw: str | None
    date_raw: str | None
    lines: list[ChitLine]
    model: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    # ------------------------------------------------------------------ parse
    @classmethod
    def from_model_json(cls, text_or_obj: str | dict, model: str = "") -> "ChitReading":
        data = _load_json(text_or_obj)
        header = data.get("header") or {}
        lines_in = data.get("lines") or []
        if not isinstance(lines_in, list):
            raise ValueError("'lines' must be a list")

        lines: list[ChitLine] = []
        for position, item in enumerate(lines_in, start=1):
            if not isinstance(item, dict):
                continue
            lines.append(_parse_line(item, position))
        return cls(
            shop_name_raw=_str_or_none(header.get("shop_name_raw")),
            date_raw=_str_or_none(header.get("date_raw")),
            lines=lines,
            model=model,
        )


# ---------------------------------------------------------------- helpers

def _load_json(text_or_obj: str | dict) -> dict:
    if isinstance(text_or_obj, dict):
        return text_or_obj
    text = text_or_obj.strip()
    # Models sometimes wrap JSON in ```json fences. Take the outermost object.
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in the model output")
    return json.loads(text[start : end + 1])


def _parse_line(item: dict, position: int) -> ChitLine:
    entry = item.get("item") or {}
    spec_in = entry.get("spec") or {}
    spec = {k: _spec_value(v) for k, v in spec_in.items() if k in SPEC_KEYS and v not in (None, "")}

    category = entry.get("category")
    category = category if category in CATEGORIES else None

    brand_raw = _str_or_none(entry.get("brand_raw"))
    brand = normalize_brand(entry.get("brand")) or normalize_brand(brand_raw)

    unit = item.get("unit")
    unit = unit if unit in UNITS else normalize_unit(unit)

    mark = item.get("mark") if item.get("mark") in MARKS else "none"
    legibility = item.get("legibility") if item.get("legibility") in LEGIBILITY else "clear"

    return ChitLine(
        line_index=position,
        raw_text=str(item.get("raw_text") or "").strip(),
        line_no_written=_str_or_none(item.get("line_no_written")),
        mark=mark,
        category=category,
        spec=spec,
        brand=brand,
        brand_raw=brand_raw,
        quantity=_number_or_none(item.get("quantity")),
        unit=unit,
        uses_ditto=bool(item.get("uses_ditto", False)),
        legibility=legibility,
        ambiguity=_str_or_none(item.get("ambiguity")),
    )


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _number_or_none(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except ValueError:
        return None


def _spec_value(value: Any) -> Any:
    """Numbers become floats ("1.5" -> 1.5); text becomes lower-case."""
    number = _number_or_none(value)
    if number is not None:
        return number
    return str(value).strip().lower()
