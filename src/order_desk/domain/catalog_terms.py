"""The shop vocabulary: categories, units, spec keys and brand codes.

This file is the ONE place that lists these words. The vision prompt, the
matcher, the golden-set format and the seed catalog all import from here,
so a new brand or category is added in one place only.
"""

from __future__ import annotations

CATEGORIES: tuple[str, ...] = (
    "wire", "switch", "socket", "mcb", "led_bulb", "led_panel", "led_tube",
    "led_downlight", "track_light", "batten", "ceiling_fan", "exhaust_fan",
    "pvc_pipe", "pvc_bend", "pvc_clamp", "pvc_tape", "modular_box",
    "distribution_box", "cat6_cable", "rj45_connector", "other",
)

UNITS: tuple[str, ...] = ("coil", "nos", "box", "pcs", "metre")

# Spec keys and the categories that use them. A product matches a chitti line
# only when every spec key that BOTH sides have is equal.
SPEC_KEYS: tuple[str, ...] = ("size_sqmm", "color", "rating_amp", "watt", "size_mm", "ways", "modules")

BRANDS: tuple[str, ...] = (
    "Finolex", "Polycab", "Havells", "Anchor", "GM", "Legrand", "Schneider", "L&T",
    "Crompton", "Orient", "Syska", "Wipro", "Philips", "Supreme", "3M", "D-Link", "AMP",
)

# Short forms seen on real chittis -> brand. Lower-case keys.
BRAND_CODES: dict[str, str] = {
    "fin": "Finolex", "finolex": "Finolex",
    "pol": "Polycab", "poly": "Polycab", "polycab": "Polycab",
    "hv": "Havells", "h": "Havells", "hav": "Havells", "havells": "Havells",
    "a": "Anchor", "anch": "Anchor", "anchor": "Anchor",
    "gm": "GM",
    "leg": "Legrand", "legr": "Legrand", "legrand": "Legrand",
    "sch": "Schneider", "schneider": "Schneider",
    "l&t": "L&T", "lt": "L&T",
    "cr": "Crompton", "crompton": "Crompton",
    "orient": "Orient", "or": "Orient",
    "sys": "Syska", "syska": "Syska",
    "wip": "Wipro", "wipro": "Wipro",
    "phil": "Philips", "philips": "Philips",
    "sup": "Supreme", "supreme": "Supreme",
    "3m": "3M",
    "d-link": "D-Link", "dlink": "D-Link",
    "amp": "AMP",
}

COLOR_CODES: dict[str, str] = {
    "r": "red", "red": "red",
    "b": "black", "blk": "black", "black": "black",
    "g": "green", "grn": "green", "green": "green",
    "y": "yellow", "yellow": "yellow",
    "bl": "blue", "blue": "blue",
}

UNIT_CODES: dict[str, str] = {
    "c": "coil", "coil": "coil", "coils": "coil",
    "nos": "nos", "no": "nos", "no.": "nos", "n": "nos",
    "box": "box", "boxes": "box",
    "pcs": "pcs", "pc": "pcs",
    "m": "metre", "mtr": "metre", "metre": "metre", "meter": "metre",
}


def normalize_brand(raw: str | None) -> str | None:
    """'HV' -> 'Havells'. Unknown codes return None (never a guess)."""
    if not raw:
        return None
    key = raw.strip().lower().rstrip(".")
    if key in BRAND_CODES:
        return BRAND_CODES[key]
    for brand in BRANDS:
        if brand.lower() == key:
            return brand
    return None


def normalize_unit(raw: str | None) -> str | None:
    if not raw:
        return None
    return UNIT_CODES.get(raw.strip().lower())


def vocabulary_for_prompt() -> str:
    """The vocabulary as text, for the extraction prompt."""
    codes = ", ".join(f"{k}={v}" for k, v in BRAND_CODES.items() if k != v.lower())
    colors = ", ".join(f"{k}={v}" for k, v in COLOR_CODES.items() if k != v)
    units = ", ".join(f"{k}={v}" for k, v in UNIT_CODES.items() if k != v)
    return (
        f"categories: {', '.join(CATEGORIES)}\n"
        f"units: {', '.join(UNITS)}\n"
        f"spec keys: {', '.join(SPEC_KEYS)}\n"
        f"brands: {', '.join(BRANDS)}\n"
        f"brand codes: {codes}\n"
        f"color codes: {colors}\n"
        f"unit codes: {units}; 'sw'=switch, 'sc'/'so'/'sock'=socket, 'sq'=sq mm"
    )
