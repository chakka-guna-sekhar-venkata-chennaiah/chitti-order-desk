"""Test fixtures: a fresh seeded database, a fixed clock and a fake vision reader.

No test calls the network. The fake reader returns readings that the test
gives it, so each test controls exactly what the "model" saw.
"""

from __future__ import annotations

import io
from datetime import datetime, timezone
from pathlib import Path

import pytest
from PIL import Image

from order_desk import clock
from order_desk.config import Settings
from order_desk.db.seed import seed_demo
from order_desk.domain.chitti import ChitReading
from order_desk.services.dealer_desk import DealerDesk
from order_desk.services.deps import Deps
from order_desk.services.shop_desk import ShopDesk
from order_desk.vision.reader import InvoiceReading

OWNER = 111
PACKER = 222
RAMESH = 900000001      # active dealer, nothing overdue
SURESH = 900000002      # active dealer, one overdue bill
VENKAT = 900000004      # blocked dealer


class FakeReader:
    """Returns the next queued reading. Counts calls, so tests can check retries."""

    def __init__(self):
        self.chits: list[ChitReading] = []
        self.invoices: list[InvoiceReading] = []
        self.calls = 0

    def read_chit_image(self, image: bytes, mime: str) -> ChitReading:
        self.calls += 1
        return self.chits.pop(0)

    def read_chit_text(self, text: str) -> ChitReading:
        self.calls += 1
        return self.chits.pop(0)

    def read_invoice_image(self, image: bytes, mime: str) -> InvoiceReading:
        self.calls += 1
        return self.invoices.pop(0)


def line(text, category, spec=None, brand=None, qty=None, unit=None, **extra) -> dict:
    """A model-style line dict (the same shape the vision model returns)."""
    return {"raw_text": text, "item": {"category": category, "spec": spec or {}, "brand": brand,
                                       "brand_raw": extra.pop("brand_raw", brand)},
            "quantity": qty, "unit": unit, **extra}


def reading(*lines: dict) -> ChitReading:
    return ChitReading.from_model_json({"header": {}, "lines": list(lines)}, model="fake")


@pytest.fixture
def fixed_clock():
    moment = {"now": datetime(2026, 10, 25, 6, 0, 0, tzinfo=timezone.utc)}
    clock.set_clock(lambda: moment["now"])
    yield moment
    clock.reset_clock()


@pytest.fixture
def deps(tmp_path: Path, fixed_clock):
    settings = Settings(
        db_path=tmp_path / "test.sqlite3",
        upload_dir=tmp_path / "uploads",
        generated_dir=tmp_path / "generated",
        owner_telegram_ids=[OWNER],
        packer_telegram_ids=[PACKER],
    )
    d = Deps.build(settings, reader=FakeReader(), allow_local_files=True)
    d.db.initialize()
    seed_demo(d.db, settings)
    yield d
    # After EVERY test: the database rules must still hold (see services/invariants.py).
    from order_desk.services.invariants import check
    assert check(d.db) == []


@pytest.fixture
def dealer(deps) -> DealerDesk:
    return DealerDesk(deps)


@pytest.fixture
def shop(deps) -> ShopDesk:
    return ShopDesk(deps)


@pytest.fixture
def photo(tmp_path: Path):
    """Make a small unique JPEG file and return 'file:<path>'."""
    counter = {"n": 0}

    def make() -> str:
        counter["n"] += 1
        n = counter["n"]
        color = (n * 37 % 256, n * 91 % 256, n * 53 % 256)     # different bytes for each photo
        path = tmp_path / f"photo_{counter['n']}.jpg"
        buf = io.BytesIO()
        Image.new("RGB", (40, 30), color).save(buf, format="JPEG")
        path.write_bytes(buf.getvalue())
        return f"file:{path}"

    return make


def stock_of(deps: Deps, sku: str) -> dict:
    with deps.db.read() as conn:
        return dict(conn.execute("SELECT * FROM v_stock WHERE sku = ?", (sku,)).fetchone())


def outbox_rows(deps: Deps) -> list[dict]:
    with deps.db.read() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM outbox ORDER BY id")]
