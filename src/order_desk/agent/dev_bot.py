"""Reference runtime: run the reference agent directly on Telegram.

    order-desk devbot --role dealer     # dealer bot (DEALER_BOT_TOKEN)
    order-desk devbot --role shop       # shop bot   (SHOP_BOT_TOKEN)

Use this when NemoClaw + Hermes is not set up on a machine, or to compare the
two runtimes. Do NOT run it at the same time as a Hermes sandbox that uses
the same bot token: Telegram allows one reader of updates per bot.

What this runtime adds over Hermes, by design:
- The photo's Telegram file_id is passed to the agent as `image_ref`.
- The caller's Telegram ID comes from the update, never from the model.
- A photo gets an instant acknowledgement from code, before the model runs.
- Each turn's trace is saved to data/traces/<role>.jsonl for error analysis.
- Replies go out as plain text (**bold**, __bold__ and a leading # are removed), and a failed send is
  tried again after a network error, 429 or 5xx (1 s, then 3 s).
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections import defaultdict
from pathlib import Path

from order_desk.agent.loop import ToolAgent, load_instructions
from order_desk.config import Settings
from order_desk.mcp_server.tools import dealer_tools, shop_tools
from order_desk.services.dealer_desk import DealerDesk
from order_desk.services.deps import Deps
from order_desk.services.shop_desk import ShopDesk
from order_desk.telegram.bot_api import BotApi, TelegramError
from order_desk.workers.lifecycle import StopFlag

log = logging.getLogger(__name__)
HISTORY_MESSAGES = 30          # older turns are dropped; the database keeps the real state
SEND_RETRY_WAITS = (1.0, 3.0)  # seconds to wait before the second and the third try to send (KI-069)

_BOLD_STARS = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")
_BOLD_UNDERSCORES = re.compile(r"(?<!\w)__(?=\S)(.+?)(?<=\S)__(?!\w)")
_HEADING = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+", re.MULTILINE)


def describe_update(message: dict) -> str | None:
    """Turn a Telegram message into the text the agent sees (with metadata)."""
    meta = f"[telegram message_id={message['message_id']} from_user_id={message['from']['id']}"
    name = message["from"].get("first_name")
    if name:
        meta += f" first_name={name}"
    if "photo" in message:
        largest = max(message["photo"], key=lambda p: p.get("file_size", 0) or p["width"] * p["height"])
        caption = message.get("caption", "")
        return f"{meta}]\nThe person sent a PHOTO. image_ref=tg:{largest['file_id']}\nCaption: {caption}"
    if "document" in message and str(message["document"].get("mime_type", "")).startswith("image/"):
        return f"{meta}]\nThe person sent an IMAGE FILE. image_ref=tg:{message['document']['file_id']}"
    if "text" in message:
        return f"{meta}]\n{message['text']}"
    return None


def build_agent(role: str, deps: Deps) -> ToolAgent:
    from openai import OpenAI

    s = deps.settings
    client = OpenAI(api_key=s.nebius_api_key, base_url=s.nebius_base_url, timeout=120, max_retries=4)
    dealer_desk = DealerDesk(deps)
    specs = dealer_tools(dealer_desk) if role == "dealer" else shop_tools(ShopDesk(deps), dealer_desk)
    return ToolAgent(specs, load_instructions(role), client, s.agent_model)


def main(settings: Settings, role: str) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if role not in ("dealer", "shop"):
        raise SystemExit("--role must be 'dealer' or 'shop'")
    token = settings.dealer_bot_token if role == "dealer" else settings.shop_bot_token
    deps = Deps.build(settings)
    deps.db.initialize()
    agent = build_agent(role, deps)
    api = BotApi(token)
    histories: dict[int, list[dict]] = defaultdict(list)
    trace_file = Path(settings.db_path).parent / "traces" / f"{role}.jsonl"
    trace_file.parent.mkdir(parents=True, exist_ok=True)

    stop = StopFlag().install()
    offset = None
    log.info("%s dev bot is running (reference runtime)", role)
    while not stop.stopped:
        try:
            updates = api.get_updates(offset)
        except TelegramError as exc:
            log.warning("getUpdates failed: %s", exc)
            stop.sleep(5)
            continue
        for update in updates:
            offset = update["update_id"] + 1           # never process an update twice
            message = update.get("message")
            if not message or message.get("chat", {}).get("type") != "private":
                continue
            content = describe_update(message)
            if content is None:
                continue
            user_id = message["from"]["id"]
            if "photo" in message and role == "dealer":
                _safe_send(api, user_id, "Got your chitti. Checking it now...")
            try:
                reply, history, trace = agent.run(histories[user_id], content, user_id)
            except Exception:
                log.exception("agent failed")
                _safe_send(api, user_id, "Sorry, something went wrong. Please try again in a minute.")
                continue
            histories[user_id] = history[-HISTORY_MESSAGES:]
            with trace_file.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"user": user_id, "events": trace.events}, ensure_ascii=False, default=str) + "\n")
            if reply:
                _safe_send(api, user_id, reply)
    log.info("%s dev bot stopped cleanly", role)


def plain_text(text: str) -> str:
    """Remove simple markdown: **bold**, __bold__ and a leading # (KI-068).

    The bot sends plain text (no parse_mode), so Telegram shows the marks as they are, for example
    "**PACKING**". The instructions already ask the model for plain text; this is the safety net.
    Only the devbot send path uses it. The code-built texts in services/messages.py are not changed."""
    text = _BOLD_STARS.sub(r"\1", text)
    text = _BOLD_UNDERSCORES.sub(r"\1", text)
    return _HEADING.sub("", text)


def _safe_send(api: BotApi, chat_id: int, text: str, *,
               waits: tuple[float, ...] = SEND_RETRY_WAITS, sleep=time.sleep) -> bool:
    """Send plain text and never raise. Return True when Telegram took it.

    A network error, 429 or 5xx (`TelegramError.retryable`) is tried again, waiting `waits` seconds before
    each new try: 1 s and 3 s, so 3 tries. A 400, 401 or 403 is not retried, because it would fail again
    (KI-069). `waits` and `sleep` are arguments so that tests run at once. A network error can also come
    after Telegram took the message, so a retry may show a reply twice; a reply of more than 4000
    characters is sent in parts, and a retry starts again at the first part."""
    text = plain_text(text)
    for wait in (*waits, None):                  # the last try has no wait after it
        try:
            api.send_message(chat_id, text)
            return True
        except TelegramError as exc:
            if not exc.retryable or wait is None:
                log.warning("send failed: %s", exc)
                return False
            log.warning("send failed, trying again in %g s: %s", wait, exc)
            sleep(wait)
