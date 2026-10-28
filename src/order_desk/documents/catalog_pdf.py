"""Build the catalog PDF from the database.

The PDF is made fresh from `products` and `v_stock`, so it always shows the
current items and prices. A dealer gets it after /start. Prices are the
dealer's own prices (base price or the dealer's override).
"""

from __future__ import annotations

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from order_desk import clock
from order_desk.db.connection import Database
from order_desk.services.pricing import rupees

CATEGORY_TITLES = {
    "wire": "Wires", "switch": "Switches", "socket": "Sockets", "mcb": "MCBs",
    "led_bulb": "LED bulbs", "led_panel": "LED panels", "led_tube": "LED tubes",
    "led_downlight": "LED downlights", "track_light": "Track lights", "batten": "Battens",
    "ceiling_fan": "Ceiling fans", "exhaust_fan": "Exhaust fans", "pvc_pipe": "PVC pipes",
    "pvc_bend": "PVC bends", "pvc_clamp": "PVC clamps", "pvc_tape": "Insulation tape",
    "modular_box": "Modular boxes", "distribution_box": "Distribution boxes",
    "cat6_cable": "Network cable", "rj45_connector": "Network connectors",
}


def build_catalog_pdf(db: Database, out_dir: Path, shop_name: str, dealer_id: int | None = None) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"catalog_{dealer_id or 'all'}_{clock.now().strftime('%Y%m%d')}.pdf"
    with db.read() as conn:
        rows = conn.execute(
            """SELECT p.sku, p.name, p.category, p.unit,
                      COALESCE(dp.price_paise, p.base_price_paise) AS price,
                      s.sellable
                 FROM products p
                 JOIN v_stock s ON s.sku = p.sku
                 LEFT JOIN dealer_prices dp ON dp.sku = p.sku AND dp.dealer_id = ?
                WHERE p.active = 1
                ORDER BY p.category, p.name""",
            (dealer_id or -1,),
        ).fetchall()

    styles = getSampleStyleSheet()
    story = [
        Paragraph(f"{shop_name} - Product catalog", styles["Title"]),
        Paragraph(f"Date: {clock.now().strftime('%d %b %Y')}. Prices in rupees. Demo prices.", styles["Normal"]),
        Spacer(1, 6 * mm),
    ]
    current = None
    table_rows: list[list[str]] = []

    def flush() -> None:
        if not table_rows:
            return
        table = Table([["Product", "Unit", "Price", "In stock"]] + table_rows,
                      colWidths=[95 * mm, 20 * mm, 30 * mm, 25 * mm])
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2b5ca6")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cccccc")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f2f4f8")]),
            ("ALIGN", (2, 1), (3, -1), "RIGHT"),
        ]))
        story.extend([table, Spacer(1, 5 * mm)])
        table_rows.clear()

    for r in rows:
        if r["category"] != current:
            flush()
            current = r["category"]
            story.append(Paragraph(CATEGORY_TITLES.get(current, current), styles["Heading2"]))
        table_rows.append([r["name"], r["unit"], rupees(r["price"]).replace("₹", "Rs "),
                           "yes" if r["sellable"] > 0 else "no"])
    flush()

    SimpleDocTemplate(str(path), pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm,
                      topMargin=15 * mm, bottomMargin=15 * mm, title=f"{shop_name} catalog").build(story)
    return path
