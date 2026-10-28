# Golden set: how to label a chitti

The chitti-reader eval compares the vision model's reading with a human label.
A wrong label makes the score wrong, so the rules here are strict.

## Where the images come from

The 15 images in `chits/` are synthetic. We asked electrical shops for real
chittis, and they did not share them. We made these images with an image
model, from patterns seen in real shops. When real chittis arrive, add them
with the prefix `real_` (for example `real_01.jpg`) and label them with the
same rules. Keep the synthetic and real results separate.

## Files

```
chits/<chit_id>.png             one chitti in each image (one photo = one chitti, like real dealers send)
labels/<chit_id>.checked.json   the label, checked by a person against the image  <- the eval uses these
labels/<chit_id>.example.json   a draft label, NOT used unless --include-examples
```

`chit_id` is `easy_01` ... `hard_05` today. Use the same id for the image and the label.

## Label format

The same shape that the vision model returns (`src/order_desk/vision/prompts.py`),
plus `chit_id`, `difficulty` and the expected decision:

```json
{
  "chit_id": "hard_04",
  "difficulty": "easy | medium | hard",
  "header": {"shop_name_raw": null, "shop_name_script": "latin | telugu | none",
             "date_raw": "24/10", "date_iso": "2026-10-24"},
  "lines": [
    {
      "line_index": 1,
      "line_no_written": "1",
      "raw_text": "1.5 r - 2c HV",
      "mark": "none | tick | strike | unclear",
      "item": {"category": "wire", "spec": {"size_sqmm": 1.5, "color": "red"},
               "brand": "Havells", "brand_raw": "HV"},
      "quantity": 2,
      "unit": "coil",
      "uses_ditto": false,
      "legibility": "clear | partial | unreadable",
      "ambiguity": null
    }
  ],
  "expected_action": "confirm_all | clarify",
  "clarify_line_indexes": []
}
```

Allowed values (from `src/order_desk/domain/catalog_terms.py`):

- `category`: wire, switch, socket, mcb, led_bulb, led_panel, led_tube, led_downlight, track_light, batten, ceiling_fan, exhaust_fan, pvc_pipe, pvc_bend, pvc_clamp, pvc_tape, modular_box, distribution_box, cat6_cable, rj45_connector, other
- `spec` keys: `size_sqmm` (number), `color` (red/black/green/yellow/blue), `rating_amp` (number), `watt` (number), `size_mm` (number), `ways` (number), `modules` (number)
- `unit`: coil, nos, box, pcs, metre
- `brand`: Finolex, Polycab, Havells, Anchor, GM, Legrand, Schneider, L&T, Crompton, Orient, Syska, Wipro, Philips, Supreme, 3M, D-Link, AMP, or null

## Rules

1. `raw_text`: copy the line exactly, with abbreviations and spelling errors.
2. `item`, `quantity`, `unit`: fill only what the text makes certain. Otherwise null.
3. Brand codes: Fin=Finolex, Pol/Poly=Polycab, HV/Hv/H=Havells, A/Anch=Anchor, Cr=Crompton,
   Sch=Schneider, sys=Syska, wip=Wipro, phil=Philips, Sup=Supreme, Leg/Legr=Legrand.
   An unknown code (for example "Lec"): `brand` null, `brand_raw` as written, and explain in `ambiguity`.
4. No brand written: `brand` and `brand_raw` null. This is NOT an ambiguity (the shop uses its default brand).
5. Ditto mark (`'`, `"`, a dot under the line above): `uses_ditto` true. Fill the value only if certain.
6. `mark`: "tick" = a check mark or a slash at the number; "strike" = a line through the item. Do not decide what it means.
7. `line_no_written` copies the written number, even when wrong or repeated. `line_index` is the real order from 1.
8. `expected_action` = "clarify" if any line has an `ambiguity`, a `mark` other than "none", or `legibility` other than "clear". List those `line_index` values in `clarify_line_indexes`.
9. Do not guess. A wrong label damages the eval more than a missing label.

## Check step (do not skip)

Fast way: run `python -m evals.make_review` and open `evals/golden/review.html`.
It shows each image next to its label, line by line. Then:

1. Open the image and the JSON next to each other.
2. Compare each line of the image with `raw_text`.
3. If they differ, change the JSON to match the image.
4. Save as `labels/<chit_id>.checked.json`. Only checked files count.

## Prompts for an assistant model (to draft labels faster)

**Prompt A - draft a label for one existing image** (send ONE image with it):

```
You make ground-truth labels for an evaluation dataset. Accuracy is more important than completeness.
The image shows one handwritten order list ("chitti") from an electrical dealer in Vijayawada, India.
Return ONE JSON object in exactly the format below and no other text.
<paste the label format and the allowed values from this file>
Follow these rules exactly:
<paste the 9 rules from this file>
```

**Prompt B - make new test cases, label first** (the label is right by construction):

```
Make 10 order lists ("chittis") for an electrical shop in Vijayawada, India, as a JSON array in the label format below.
<paste the label format>
3 easy (full names, brand on each line), 4 medium (short names, some brands), 3 hard (codes like "1.5 r - 2c", ditto marks,
wrong line numbers, ticks or strikes). Include at least once: a Telugu shop name, a date without the year, an item that an
electrical shop does not sell, an unclear quantity ("2 or 3"), the same item on two lines, a line with only a brand and a quantity.
6 to 12 lines each. Set expected_action and clarify_line_indexes by rule 8.
```

**Prompt C - draw one image from one label:**

```
Make a realistic phone photo of ONE handwritten order list on a small sheet of paper.
Write exactly these lines, in this order, with exactly this spelling. Do not add, remove or correct anything:
<the header and the raw_text values>
Handwriting: <neat print | fast and uneven | messy, with the listed marks>. Blue or black ball pen.
Slight angle, indoor light, wooden counter, one sheet only, no text outside the paper.
```

After Prompt C, do the check step: an image model can write something other
than what you asked. The label must describe the IMAGE.
