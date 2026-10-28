"""The shared parts that every service needs, built once.

Tests build a `Deps` with a temporary database and a fake reader, so no test
calls the network.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from order_desk.config import Settings
from order_desk.db.connection import Database
from order_desk.vision.images import ImageResolver
from order_desk.vision.reader import Reader, TokenFactoryReader


@dataclass
class Deps:
    settings: Settings
    db: Database
    dealer_images: ImageResolver
    shop_images: ImageResolver
    _reader: Reader | None = field(default=None, repr=False)

    @property
    def reader(self) -> Reader:
        """The vision reader. Made on first use, so the server starts without a key."""
        if self._reader is None:
            s = self.settings
            self._reader = TokenFactoryReader(s.nebius_api_key, s.nebius_base_url, s.vision_model,
                                           max_tokens=s.vision_max_tokens)
        return self._reader

    @classmethod
    def build(cls, settings: Settings, reader: Reader | None = None, allow_local_files: bool = False) -> "Deps":
        db = Database(settings.db_path)
        return cls(
            settings=settings,
            db=db,
            dealer_images=ImageResolver(db, settings.upload_dir, settings.dealer_bot_token, allow_local_files,
                                        settings.dealer_sandbox, settings.nemohermes_bin),
            shop_images=ImageResolver(db, settings.upload_dir, settings.shop_bot_token, allow_local_files,
                                      settings.shop_sandbox, settings.nemohermes_bin),
            _reader=reader,
        )
