"""A small client for the Telegram Bot API (only the calls we need).

We call Telegram directly from our own code for two jobs:
1. The outbox sender sends messages, PDFs and chart images.
2. The image source downloads a photo by its Telegram file_id.
Hermes keeps its own connection for chat. Sending from a second process with
the same bot token is allowed; only reading updates (getUpdates) must have a
single reader, and we do not read updates here.

API reference: https://core.telegram.org/bots/api
"""

from __future__ import annotations

from pathlib import Path

import httpx

API = "https://api.telegram.org"
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024        # Bot API download limit


class TelegramError(Exception):
    def __init__(self, message: str, retryable: bool):
        super().__init__(message)
        self.retryable = retryable


class BotApi:
    def __init__(self, token: str, timeout_s: float = 30.0):
        if not token:
            raise TelegramError("Bot token is empty. Set DEALER_BOT_TOKEN / SHOP_BOT_TOKEN.", retryable=False)
        self._token = token
        self._http = httpx.Client(timeout=timeout_s)

    def close(self) -> None:
        self._http.close()

    # ------------------------------------------------------------- sending
    def send_message(self, chat_id: int, text: str) -> None:
        # Telegram limit is 4096 characters; long texts are split at line ends.
        for chunk in _split(text, 4000):
            self._call("sendMessage", data={"chat_id": chat_id, "text": chunk})

    def send_document(self, chat_id: int, path: str, caption: str | None = None) -> None:
        with open(path, "rb") as fh:
            self._call("sendDocument", data=_caption(chat_id, caption), files={"document": (Path(path).name, fh)})

    def send_photo(self, chat_id: int, path: str, caption: str | None = None) -> None:
        with open(path, "rb") as fh:
            self._call("sendPhoto", data=_caption(chat_id, caption), files={"photo": (Path(path).name, fh)})

    # ------------------------------------------------------------- reading
    def download_file(self, file_id: str) -> tuple[bytes, str]:
        """Return (bytes, file name) for a Telegram file_id."""
        info = self._call("getFile", data={"file_id": file_id})
        if info.get("file_size", 0) > MAX_DOWNLOAD_BYTES:
            raise TelegramError("The file is larger than 20 MB.", retryable=False)
        file_path = info["file_path"]
        try:
            resp = self._http.get(f"{API}/file/bot{self._token}/{file_path}")
        except httpx.HTTPError as exc:
            raise TelegramError(f"Download failed: {exc}", retryable=True) from exc
        if resp.status_code != 200:
            raise TelegramError(f"Download failed with HTTP {resp.status_code}", retryable=resp.status_code >= 500)
        return resp.content, Path(file_path).name

    def get_updates(self, offset: int | None, timeout_s: int = 25) -> list[dict]:
        """Long polling. Used ONLY by the reference runtime (agent/dev_bot.py)."""
        data = {"timeout": timeout_s, "allowed_updates": '["message"]'}
        if offset is not None:
            data["offset"] = offset
        return self._call("getUpdates", data=data, timeout=timeout_s + 10)

    # ------------------------------------------------------------- internal
    def _call(self, method: str, data: dict, files: dict | None = None, timeout: float | None = None):
        try:
            resp = self._http.post(f"{API}/bot{self._token}/{method}", data=data, files=files,
                                   timeout=timeout or self._http.timeout)
        except httpx.HTTPError as exc:
            raise TelegramError(f"{method}: network error: {exc}", retryable=True) from exc
        try:
            body = resp.json()
        except ValueError:
            raise TelegramError(f"{method}: HTTP {resp.status_code}, not JSON", retryable=resp.status_code >= 500)
        if not body.get("ok"):
            # 429 = too many requests; 5xx = Telegram problem. Both: try again later.
            retryable = resp.status_code == 429 or resp.status_code >= 500
            raise TelegramError(f"{method}: {body.get('description', 'unknown error')}", retryable=retryable)
        return body["result"]


def _caption(chat_id: int, caption: str | None) -> dict:
    data = {"chat_id": chat_id}
    if caption:
        data["caption"] = caption[:1000]
    return data


def _split(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks, current = [], ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > limit and current:
            chunks.append(current)
            current = ""
        current += line
    if current:
        chunks.append(current)
    return chunks
