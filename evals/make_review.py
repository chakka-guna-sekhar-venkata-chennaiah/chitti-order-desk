"""Make a review page: each chitti image next to its label, line by line.

    python -m evals.make_review
    open evals/golden/review.html        (macOS)

Use it to check the draft labels. For each chitti, compare every row with
the image. Fix the JSON file, run this script again, and when a chitti is
correct, rename its file from <id>.example.json to <id>.checked.json.
The page shows which labels are already checked.
"""

from __future__ import annotations

import html
import json

from evals.common import GOLDEN

STYLE = """
body{font:14px system-ui,sans-serif;margin:16px;background:#f4f5f2;color:#1b1f1d}
h1{font-size:20px} .chit{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.4fr);gap:16px;
background:#fff;border:1px solid #ccd;border-radius:6px;padding:12px;margin:0 0 20px}
img{width:100%;border:1px solid #ddd} table{border-collapse:collapse;width:100%;font-size:12.5px}
td,th{border-bottom:1px solid #e3e3e3;padding:4px 6px;text-align:left;vertical-align:top}
th{background:#f0f0ee} .doubt{background:#fff4d6} .tag{font-size:12px;padding:2px 6px;border-radius:3px}
.checked{background:#dff0e3;color:#24613a} .draft{background:#fde2dd;color:#8a2a1c}
@media(max-width:800px){.chit{grid-template-columns:1fr}}
"""


def row(line: dict) -> str:
    item = line["item"]
    spec = ", ".join(f"{k}={v}" for k, v in item["spec"].items())
    doubt = line["ambiguity"] or line["mark"] != "none" or line["legibility"] != "clear"
    cells = [line["line_index"], line["line_no_written"] or "-", line["raw_text"], item["category"] or "-",
             spec or "-", f"{item['brand'] or '-'} ({item['brand_raw'] or '-'})",
             line["quantity"] if line["quantity"] is not None else "-", line["unit"] or "-",
             line["mark"], "yes" if line["uses_ditto"] else "", line["ambiguity"] or ""]
    tds = "".join(f"<td>{html.escape(str(c))}</td>" for c in cells)
    return f"<tr class=\"{'doubt' if doubt else ''}\">{tds}</tr>"


def main() -> None:
    parts = [f"<!doctype html><meta charset='utf-8'><title>Chitti label review</title><style>{STYLE}</style>",
             "<h1>Chitti label review</h1><p>Yellow rows will make the system ask the dealer a question.</p>"]
    done = 0
    for image in sorted((GOLDEN / "chits").glob("*.png")):
        cid = image.stem
        checked = GOLDEN / "labels" / f"{cid}.checked.json"
        draft = GOLDEN / "labels" / f"{cid}.example.json"
        path = checked if checked.exists() else draft if draft.exists() else None
        if path is None:
            parts.append(f"<h2>{cid}: no label</h2>")
            continue
        done += path == checked
        label = json.loads(path.read_text(encoding="utf-8"))
        tag = "<span class='tag checked'>checked</span>" if path == checked else "<span class='tag draft'>draft: check me</span>"
        head = label["header"]
        rows = "".join(row(l) for l in label["lines"])
        parts.append(
            f"<div class='chit'><div><h2>{cid} {tag}</h2><img src='chits/{image.name}' alt='{cid}'></div><div>"
            f"<p>Header: {html.escape(str(head.get('shop_name_raw')))} | date {html.escape(str(head.get('date_raw')))}"
            f" | expected: <b>{label['expected_action']}</b> {label['clarify_line_indexes']}<br>File: labels/{path.name}</p>"
            "<table><tr><th>#</th><th>No.</th><th>raw_text</th><th>category</th><th>spec</th><th>brand (raw)</th>"
            "<th>qty</th><th>unit</th><th>mark</th><th>ditto</th><th>ambiguity</th></tr>"
            f"{rows}</table></div></div>")
    parts.insert(2, f"<p><b>{done} of {len(list((GOLDEN / 'chits').glob('*.png')))} checked.</b></p>")
    out = GOLDEN / "review.html"
    out.write_text("".join(parts), encoding="utf-8")
    print(f"wrote {out}  ({done} checked)")


if __name__ == "__main__":
    main()
