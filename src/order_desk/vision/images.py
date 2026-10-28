"""Where an image comes from: an `image_ref` string -> bytes.

A tool cannot receive a photo inside its arguments: NemoClaw limits an MCP
request to 128 KB, and a phone photo is 1-5 MB. So tools take a short
reference instead, and the server fetches the bytes itself:

    sandbox:<path>  a photo that Hermes cached INSIDE its NemoClaw sandbox. Hermes
                    shows it to the agent as "[Image attached at: <path>]". The
                    server copies it out with NemoClaw's own command
                    `nemohermes <sandbox> download <path> <dir>` (host side, no
                    extra network policy, no token inside the sandbox).
    tg:<file_id>    a Telegram file. The server downloads it with the bot token
                    (used by the reference runtime, which sees the file_id).
    up:<upload_id>  a file sent to POST /uploads on this server before the call.
    file:<path>     a local file. Only for tests and evals (off in the server).

Every image is saved in the upload folder and recorded in the `uploads`
table with its SHA-256. The hash finds "the same file sent two times".
"""

from __future__ import annotations

import hashlib
import secrets
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from order_desk import clock
from order_desk.db.connection import Database
from order_desk.domain.errors import ExternalServiceError, InvalidInput, NotFound

MAX_IMAGE_BYTES = 15 * 1024 * 1024
# Only these sandbox folders may be copied out: Hermes' caches for received media.
SANDBOX_MEDIA_DIRS = ("/sandbox/.hermes/image_cache/", "/sandbox/.hermes/document_cache/",
                      "/sandbox/.hermes/cache/")
_MAGIC = {b"\xff\xd8\xff": "image/jpeg", b"\x89PNG": "image/png", b"RIFF": "image/webp", b"%PDF": "application/pdf"}


@dataclass(frozen=True)
class StoredImage:
    upload_id: str
    sha256: str
    path: Path
    mime: str

    def read(self) -> bytes:
        return self.path.read_bytes()


def sniff_mime(data: bytes) -> str | None:
    for magic, mime in _MAGIC.items():
        if data.startswith(magic):
            return mime
    return None


def store(db: Database, upload_dir: Path, data: bytes, source: str) -> StoredImage:
    """Save bytes to disk and record them. Same bytes -> same file on disk."""
    if len(data) > MAX_IMAGE_BYTES:
        raise InvalidInput("The file is larger than 15 MB.")
    mime = sniff_mime(data)
    if mime is None:
        raise InvalidInput("The file is not a JPEG, PNG, WEBP or PDF.")
    if mime == "application/pdf":
        raise InvalidInput("PDF files are not read yet. Please send a photo or a screenshot of the page.")
    digest = hashlib.sha256(data).hexdigest()
    upload_dir.mkdir(parents=True, exist_ok=True)
    path = upload_dir / f"{digest}{_extension(mime)}"
    if not path.exists():
        path.write_bytes(data)
    upload_id = "up_" + secrets.token_hex(8)
    with db.write() as conn:
        conn.execute(
            "INSERT INTO uploads(id, sha256, path, mime, bytes, source, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (upload_id, digest, str(path), mime, len(data), source, clock.now_iso()),
        )
    return StoredImage(upload_id, digest, path, mime)


class ImageResolver:
    """Turns an image_ref into a StoredImage. One resolver per bot scope."""

    def __init__(self, db: Database, upload_dir: Path, bot_token: str = "", allow_local_files: bool = False,
                 sandbox_name: str = "", nemohermes_bin: str = "nemohermes", runner=subprocess.run):
        self.db = db
        self.upload_dir = upload_dir
        self.bot_token = bot_token
        self.allow_local_files = allow_local_files
        self.sandbox_name = sandbox_name
        self.nemohermes_bin = nemohermes_bin
        self._run = runner                       # tests pass a fake runner

    def resolve(self, image_ref: str) -> StoredImage:
        kind, _, value = (image_ref or "").partition(":")
        if not value:
            raise InvalidInput("image_ref must look like 'sandbox:<path>', 'tg:<file_id>' or 'up:<upload_id>'.")
        if kind == "sandbox":
            return self._from_sandbox(value)
        if kind == "up":
            return self._from_upload(value)
        if kind == "tg":
            return self._from_telegram(value)
        if kind == "file" and self.allow_local_files:
            return store(self.db, self.upload_dir, Path(value).read_bytes(), source="file")
        raise InvalidInput(f"Unknown image_ref type '{kind}'.")

    def _from_upload(self, upload_id: str) -> StoredImage:
        with self.db.read() as conn:
            row = conn.execute("SELECT * FROM uploads WHERE id = ?", (upload_id,)).fetchone()
        if row is None:
            raise NotFound(f"No upload {upload_id}.")
        return StoredImage(row["id"], row["sha256"], Path(row["path"]), row["mime"])

    def _from_telegram(self, file_id: str) -> StoredImage:
        from order_desk.telegram.bot_api import BotApi, TelegramError

        try:
            api = BotApi(self.bot_token)
            try:
                data, _name = api.download_file(file_id)
            finally:
                api.close()
        except TelegramError as exc:
            raise ExternalServiceError(f"Could not download the photo from Telegram: {exc}") from exc
        return store(self.db, self.upload_dir, data, source="telegram")


    def _from_sandbox(self, path: str) -> StoredImage:
        """Copy one cached media file out of the agent's sandbox, on the host side."""
        if not self.sandbox_name:
            raise InvalidInput("This server has no sandbox name set (DEALER_SANDBOX / SHOP_SANDBOX).")
        path = path.strip()
        if (".." in path or not path.startswith(SANDBOX_MEDIA_DIRS) or len(path) > 300
                or any(c in path for c in "\n\r\x00")):
            raise InvalidInput("Only photos from the Hermes media cache can be read.")
        with tempfile.TemporaryDirectory() as tmp:
            try:
                result = self._run([self.nemohermes_bin, self.sandbox_name, "download", path, tmp + "/"],
                                   capture_output=True, text=True, timeout=60)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise ExternalServiceError(f"Could not copy the photo from the sandbox: {exc}") from exc
            if result.returncode != 0:
                raise ExternalServiceError(f"Could not copy the photo from the sandbox: "
                                           f"{(result.stderr or result.stdout or '').strip()[:200]}")
            copied = Path(tmp) / Path(path).name
            if not copied.is_file():
                raise ExternalServiceError("The sandbox copy did not produce the file.")
            data = copied.read_bytes()
        return store(self.db, self.upload_dir, data, source="sandbox")


def _extension(mime: str) -> str:
    return {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}.get(mime, ".bin")
