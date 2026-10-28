"""A small, realistic shop history for the text-to-SQL eval.

The text-to-SQL questions need orders in different states, bills and a
stock-in. This builds them through the real services (not raw SQL), so the
data has the same shape as real use.
"""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image

from order_desk.domain.chitti import ChitReading
from order_desk.services.dealer_desk import DealerDesk
from order_desk.services.deps import Deps
from order_desk.services.shop_desk import ShopDesk
from order_desk.vision.reader import InvoiceReading

OWNER = 111111
PACKER = 222222
RAMESH, SURESH, SAI = 900000001, 900000002, 900000003


def _l(raw, category, spec, brand, qty, unit):
    return {"raw_text": raw, "quantity": qty, "unit": unit,
            "item": {"category": category, "spec": spec, "brand": brand, "brand_raw": brand}}


class QueueReader:
    def __init__(self):
        self.chits: list[dict] = []
        self.invoices: list[InvoiceReading] = []

    def read_chit_image(self, image, mime):
        return ChitReading.from_model_json(self.chits.pop(0))

    def read_chit_text(self, text):
        return ChitReading.from_model_json(self.chits.pop(0))

    def read_invoice_image(self, image, mime):
        return self.invoices.pop(0)


def _photo(folder: Path, n: int) -> str:
    path = folder / f"p{n}.jpg"
    buf = io.BytesIO()
    Image.new("RGB", (30, 30), (n * 40 % 256, n * 70 % 256, n * 90 % 256)).save(buf, format="JPEG")
    path.write_bytes(buf.getvalue())
    return f"file:{path}"


def build_history(deps: Deps, folder: Path) -> None:
    reader: QueueReader = deps.reader  # type: ignore[assignment]
    dealer, shop = DealerDesk(deps), ShopDesk(deps)

    def order(who: int, ref: str, lines: list[dict]) -> int:
        reader.chits.append({"lines": lines})
        order_id = dealer.start_order(who, "photo", ref)["order_id"]
        dealer.read_chit(who, order_id, _photo(folder, order_id))
        return order_id

    # 1. Ramesh: approved and packed
    a = order(RAMESH, "r1", [_l("MCB 32A Schneider 4", "mcb", {"rating_amp": 32}, "Schneider", 4, "nos"),
                             _l("6A switch Anchor 30", "switch", {"rating_amp": 6}, "Anchor", 30, "nos")])
    dealer.confirm(RAMESH, a)
    shop.approve(OWNER, a)
    shop.mark_packed(PACKER, a)
    # 2. Suresh: approved, still packing
    b = order(SURESH, "s1", [_l("Fan 1200 Crompton 2", "ceiling_fan", {"size_mm": 1200}, "Crompton", 2, "nos"),
                             _l("MCB 16A Havells 6", "mcb", {"rating_amp": 16}, "Havells", 6, "nos")])
    dealer.confirm(SURESH, b)
    shop.approve(OWNER, b)
    # 3. Sai: waiting for the owner
    c = order(SAI, "sai1", [_l("1.5 red Finolex 3 coil", "wire", {"size_sqmm": 1.5, "color": "red"}, "Finolex", 3, "coil")])
    dealer.confirm(SAI, c)
    # 4. Ramesh: waiting for the dealer's check
    order(RAMESH, "r2", [_l("PVC tape 3M 10", "pvc_tape", {}, "3M", 10, "nos")])
    # 5. Suresh: rejected
    e = order(SURESH, "s2", [_l("Exhaust fan Havells 1", "exhaust_fan", {}, "Havells", 1, "nos")])
    dealer.confirm(SURESH, e)
    shop.reject(OWNER, e, "Clear the old bill first")
    # 6. Stock-in: Schneider MCBs
    reader.invoices.append(InvoiceReading("Schneider Dist", "SD-1", "2026-10-24",
                                          ChitReading.from_model_json({"lines": [
                                              _l("MCB 32A Schneider 1 box", "mcb", {"rating_amp": 32}, "Schneider", 1, "box")]}).lines))
    draft = shop.read_supplier_invoice(OWNER, _photo(folder, 99))
    shop.apply_stock_in(OWNER, draft["invoice_id"])
    # 7. A shelf count finds only 1 Crompton exhaust fan (safety stock is 2): low stock
    shop.adjust_stock(OWNER, "EXH-CROMPTON", 1, "shelf count")
