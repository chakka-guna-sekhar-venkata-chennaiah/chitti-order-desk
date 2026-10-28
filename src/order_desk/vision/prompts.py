"""Prompts for the vision model (Nemotron 3.5 Super VL on Token Factory).

The chitti prompt asks for the SAME JSON shape as the golden-set labels, so
the eval compares like with like. The rules repeat the labelling rules from
evals/golden/README.md: copy the text exactly, never guess, and report doubt.
Change a rule here and in the labelling guide together.
"""

from __future__ import annotations

from order_desk.domain.catalog_terms import vocabulary_for_prompt

CHIT_SYSTEM = """You read handwritten order lists ("chittis") for an electrical shop in Vijayawada, India.
A wrong value is worse than a missing value. Do not guess."""

CHIT_INSTRUCTIONS = """Read the order list and return ONE JSON object, with no other text:

{
  "header": {"shop_name_raw": string|null, "date_raw": string|null},
  "lines": [
    {
      "line_no_written": string|null,
      "raw_text": string,
      "mark": "none"|"tick"|"strike"|"unclear",
      "item": {"category": string|null, "spec": object, "brand": string|null, "brand_raw": string|null},
      "quantity": number|null,
      "unit": string|null,
      "uses_ditto": boolean,
      "legibility": "clear"|"partial"|"unreadable",
      "ambiguity": string|null
    }
  ]
}

Rules:
1. One entry for each written order line, top to bottom. Do not include the header or the date as lines.
2. raw_text: copy the line exactly, with abbreviations and spelling errors. Do not correct it.
3. item, quantity, unit: fill a value only when the text makes it certain. Otherwise use null.
4. Ditto marks (' or " or a dot under the line above): set uses_ditto true. Copy the value from the line above only if it is certain. If not, explain in "ambiguity".
5. "tick" = a check mark or a slash at the line number. "strike" = a line through the item text.
6. A missing brand is normal. Then brand and brand_raw are null. This is not an ambiguity.
7. A brand code that is not in the list: brand null, brand_raw as written, and explain in "ambiguity".
8. If you are not sure about any value on a line, write the reason in "ambiguity" (one short sentence).

Vocabulary:
{vocabulary}
"""

INVOICE_SYSTEM = """You read supplier invoices (bills from manufacturers or distributors) for an electrical shop.
A wrong value is worse than a missing value. Do not guess."""

INVOICE_INSTRUCTIONS = """Read the invoice and return ONE JSON object, with no other text:

{
  "supplier": string|null,
  "invoice_no": string|null,
  "invoice_date": string|null,
  "lines": [
    {
      "raw_text": string,
      "item": {"category": string|null, "spec": object, "brand": string|null, "brand_raw": string|null},
      "quantity": number|null,
      "unit": string|null,
      "legibility": "clear"|"partial"|"unreadable",
      "ambiguity": string|null
    }
  ]
}

Rules:
1. One entry for each product line. Ignore tax lines, totals, addresses and bank details.
2. raw_text: copy the product description and quantity as printed.
3. quantity and unit: the quantity of PACKS or units as printed (for example 10 box, 5 coil).
4. Fill a value only when it is certain. Otherwise use null and explain in "ambiguity".

Vocabulary:
{vocabulary}
"""


def chit_instructions() -> str:
    return CHIT_INSTRUCTIONS.replace("{vocabulary}", vocabulary_for_prompt())


def invoice_instructions() -> str:
    return INVOICE_INSTRUCTIONS.replace("{vocabulary}", vocabulary_for_prompt())
