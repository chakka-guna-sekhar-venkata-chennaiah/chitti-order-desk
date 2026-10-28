"""`order-desk demo`: one full order and one stock-in, offline.

No API key and no Telegram are needed. A "static reader" returns a fixed
reading of chitti hard_01 (the same lines a good model run returns), so you
can see every step and every message that the system produces:

    chitti -> read-back -> dealer answers -> confirm -> owner card -> approve
           -> packing slip -> packed -> dealer told
    invoice -> stock-in draft -> apply -> stock changes
    owner question -> fixed report and a read-only SQL query
"""

from __future__ import annotations

import io
import json
import shutil
from pathlib import Path

from PIL import Image

from order_desk.config import Settings
from order_desk.db.seed import seed_demo
from order_desk.domain.chitti import ChitReading
from order_desk.services.dealer_desk import DealerDesk
from order_desk.services.deps import Deps
from order_desk.services.shop_desk import ShopDesk
from order_desk.vision.reader import InvoiceReading

OWNER, PACKER, DEALER = 111111, 222222, 900000001


def _l(raw, category, spec=None, brand=None, qty=None, unit=None, brand_raw=None, **extra):
    return {"raw_text": raw, "quantity": qty, "unit": unit,
            "item": {"category": category, "spec": spec or {}, "brand": brand, "brand_raw": brand_raw or brand},
            **extra}


HARD_01 = {
    "header": {"shop_name_raw": None, "date_raw": "25/10"},
    "lines": [
        _l("1.5 r - 2c (Fin)", "wire", {"size_sqmm": 1.5, "color": "red"}, "Finolex", 2, "coil", "Fin"),
        _l("1.5 b - 2c", "wire", {"size_sqmm": 1.5, "color": "black"}, None, 2, "coil"),
        _l("2.5 g - 1c", "wire", {"size_sqmm": 2.5, "color": "green"}, None, 1, "coil"),
        _l("6A sw - 20 (Anch)", "switch", {"rating_amp": 6}, "Anchor", 20, "nos", "Anch"),
        _l("6A sc - 10", "socket", {"rating_amp": 6}, None, 10, "nos"),
        _l("16A sw - 5", "switch", {"rating_amp": 16}, None, 5, "nos"),
        _l("LED 9w - 12 (Hv)", "led_bulb", {"watt": 9}, "Havells", 12, "nos", "Hv"),
        _l("fan 1200 - 3 (Cr)", "ceiling_fan", {"size_mm": 1200}, "Crompton", 3, "nos", "Cr"),
        _l("MCB 32 - 2 (Sch)", "mcb", {"rating_amp": 32}, "Schneider", 2, "nos", "Sch"),
        _l("Box 3M - 5", "modular_box", {}, None, 5, "nos",
           ambiguity="'3M' can mean a 3-module box or the brand 3M."),
    ],
}

INVOICE = {
    "lines": [
        _l("Schneider MCB SP 32A - 2 box", "mcb", {"rating_amp": 32}, "Schneider", 2, "box"),
        _l("Anchor 6A switch - 10 box", "switch", {"rating_amp": 6}, "Anchor", 10, "box"),
    ]
}


class StaticReader:
    def read_chit_image(self, image, mime):
        return ChitReading.from_model_json(HARD_01, model="static-demo")

    def read_chit_text(self, text):
        return ChitReading.from_model_json(HARD_01, model="static-demo")

    def read_invoice_image(self, image, mime):
        lines = ChitReading.from_model_json(INVOICE).lines
        return InvoiceReading("Schneider Distributor Vijayawada", "SD-2026-1187", "2026-10-24", lines)


def _box(title: str, text: str) -> None:
    print(f"\n=== {title} " + "=" * max(0, 60 - len(title)))
    print(text)


def run_demo(workdir: Path) -> None:
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)
    settings = Settings(db_path=workdir / "demo.sqlite3", upload_dir=workdir / "uploads",
                        generated_dir=workdir / "generated", owner_telegram_ids=[OWNER],
                        packer_telegram_ids=[PACKER])
    deps = Deps.build(settings, reader=StaticReader(), allow_local_files=True)
    deps.db.initialize()
    seed_demo(deps.db, settings)
    dealer, shop = DealerDesk(deps), ShopDesk(deps)

    photo = workdir / "chitti.jpg"
    buf = io.BytesIO()
    Image.new("RGB", (60, 80), (240, 240, 230)).save(buf, format="JPEG")
    photo.write_bytes(buf.getvalue())

    order_id = dealer.start_order(DEALER, "photo", "tg-msg-1001")["order_id"]
    result = dealer.read_chit(DEALER, order_id, f"file:{photo}")
    _box("1. Read-back sent to the dealer", result["readback"])

    result = dealer.update_lines(DEALER, order_id, [{"line_index": 10, "sku": "BOX-3M-ANCHOR", "qty": 5}])
    _box("2. Dealer: 'line 10 is the 3-module Anchor box'", result["readback"])

    _box("3. Dealer: 'YES'", dealer.confirm(DEALER, order_id)["message"])
    _box("4. Approval card queued for the owner", _last_outbox(deps, "NEW ORDER"))

    approved = shop.approve(OWNER, order_id, edits=[{"line_index": 6, "qty": 4}])
    _box("5. Owner: 'APPROVE {0} with line 6 = 4'".format(order_id), approved["message"])
    _box("6. Packing slip queued for the team", _last_outbox(deps, "PACKING SLIP"))
    _box("7. Message queued for the dealer", _last_outbox(deps, "is approved"))

    _box("8. Packer: 'PACKED {0}'".format(order_id), shop.mark_packed(PACKER, order_id)["message"])
    _box("9. Message queued for the dealer", _last_outbox(deps, "is packed"))

    draft = shop.read_supplier_invoice(OWNER, f"file:{photo}")
    _box("10. Owner sends a supplier invoice photo", draft["preview"])
    applied = shop.apply_stock_in(OWNER, draft["invoice_id"])
    _box("11. Owner: 'OK'", applied["message"])

    _box("12. Owner: 'How is today?'", json.dumps(shop.report(OWNER, "today")["data"], indent=2))
    q = shop.run_query(OWNER, "SELECT dealer_name, outstanding_rupees, overdue_bills FROM v_dealer_credit "
                              "WHERE outstanding_rupees > 0 ORDER BY outstanding_rupees DESC")
    _box("13. Owner: 'Who owes money?' (read-only SQL)", json.dumps(q["rows"], indent=2))
    try:
        shop.run_query(OWNER, "DELETE FROM orders")
    except Exception as exc:  # the guard refuses it
        _box("14. A bad query is refused", str(exc))
    print(f"\nDemo database: {settings.db_path}  (open it with any SQLite viewer)")


def _last_outbox(deps: Deps, needle: str) -> str:
    with deps.db.read() as conn:
        for row in conn.execute("SELECT payload_json FROM outbox ORDER BY id DESC"):
            text = json.loads(row["payload_json"]).get("text", "")
            if needle in text:
                return text
    return "(not found)"
