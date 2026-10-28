"""Call the vision model and turn its answer into a ChitReading.

The model is reached through Nebius Token Factory, which is OpenAI-compatible,
so we use the standard `openai` client with a different base URL.

Design notes
- temperature 0: the same image should give the same reading.
- The image is resized to at most 2000 px and sent as JPEG. Handwriting stays
  readable, and the request stays small.
- The vision model THINKS before it answers, and its thinking tokens count against
  max_tokens. In the 2026-10-27 eval one call spent 3906 of its 4000 tokens thinking:
  the JSON was cut off (or never started), and 10 of 15 chittis failed (KI-060).
  So the budget is large (`VISION_MAX_TOKENS`, default 16000; billing is per token
  used, not per token allowed). Nebius' docs do not say how to switch the thinking
  off or confirm JSON mode for this model, so we send neither.
- finish_reason "length" means the answer was cut off. A cut-off answer is NEVER
  accepted as a partial reading (a silently dropped line is worse than an error), and
  it is not retried with a repair turn (that cannot fix it): the call ends at once
  with an ExternalServiceError that says what happened.
- If the answer is not valid JSON for another reason, we ask one more time with the
  error text. Then we give up with ExternalServiceError (the tool call is safe to retry).
- `Reader` is an interface. Tests and evals can pass a fake reader.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import re
from dataclasses import dataclass
from typing import Protocol

from order_desk.domain.chitti import ChitLine, ChitReading
from order_desk.domain.errors import ExternalServiceError
from order_desk.vision import prompts

log = logging.getLogger(__name__)

MAX_SIDE_PX = 2000
DEFAULT_MAX_TOKENS = 16000     # thinking + answer; see the design notes


@dataclass
class InvoiceReading:
    supplier: str | None
    invoice_no: str | None
    invoice_date: str | None
    lines: list[ChitLine]


class Reader(Protocol):
    def read_chit_image(self, image: bytes, mime: str) -> ChitReading: ...
    def read_chit_text(self, text: str) -> ChitReading: ...
    def read_invoice_image(self, image: bytes, mime: str) -> InvoiceReading: ...


def prepare_image(image: bytes) -> tuple[bytes, str]:
    """Resize to MAX_SIDE_PX and convert to JPEG. Returns (bytes, mime)."""
    from PIL import Image, ImageOps

    with Image.open(io.BytesIO(image)) as im:
        im = ImageOps.exif_transpose(im)          # phone photos carry their rotation in EXIF
        im = im.convert("RGB")
        im.thumbnail((MAX_SIDE_PX, MAX_SIDE_PX))
        out = io.BytesIO()
        im.save(out, format="JPEG", quality=88)
    return out.getvalue(), "image/jpeg"


class TokenFactoryReader:
    """Reader that uses a vision model on Nebius Token Factory."""

    def __init__(self, api_key: str, base_url: str, model: str, timeout_s: float = 90.0,
                 max_tokens: int = DEFAULT_MAX_TOKENS):
        if not api_key:
            raise ExternalServiceError("NEBIUS_API_KEY is not set. See README step 2.")
        from openai import OpenAI

        self._client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout_s, max_retries=4)
        self.model = model
        self.max_tokens = max_tokens

    # ------------------------------------------------------------ public API
    def read_chit_image(self, image: bytes, mime: str) -> ChitReading:
        data = self._ask(prompts.CHIT_SYSTEM, prompts.chit_instructions(), image=image)
        return ChitReading.from_model_json(data, model=self.model)

    def read_chit_text(self, text: str) -> ChitReading:
        instructions = prompts.chit_instructions() + "\n\nThe order list is this text message:\n" + text
        data = self._ask(prompts.CHIT_SYSTEM, instructions, image=None)
        return ChitReading.from_model_json(data, model=self.model)

    def read_invoice_image(self, image: bytes, mime: str) -> InvoiceReading:
        data = self._ask(prompts.INVOICE_SYSTEM, prompts.invoice_instructions(), image=image)
        reading = ChitReading.from_model_json({"lines": data.get("lines") or []}, model=self.model)
        return InvoiceReading(
            supplier=(data.get("supplier") or None),
            invoice_no=(str(data["invoice_no"]).strip() if data.get("invoice_no") else None),
            invoice_date=(data.get("invoice_date") or None),
            lines=reading.lines,
        )

    # -------------------------------------------------------------- internal
    def _ask(self, system: str, instructions: str, image: bytes | None) -> dict:
        content: list[dict] = [{"type": "text", "text": instructions}]
        if image is not None:
            jpeg, mime = prepare_image(image)
            b64 = base64.b64encode(jpeg).decode("ascii")
            content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
        messages = [{"role": "system", "content": system}, {"role": "user", "content": content}]

        last_error = ""
        for attempt in (1, 2):
            try:
                response = self._client.chat.completions.create(
                    model=self.model, messages=messages, temperature=0, max_tokens=self.max_tokens,
                )
            except Exception as exc:  # network, 5xx, rate limit (after the client's own retries)
                raise ExternalServiceError(f"The vision model did not answer: {exc}") from exc
            choice = response.choices[0]
            text = choice.message.content or ""
            try:
                return _json_object(text)
            except ValueError as exc:
                last_error = str(exc)
                facts = _describe_answer(response)
                if choice.finish_reason == "length":
                    # Cut off by max_tokens, mostly because the thinking used the budget (KI-060). The same
                    # request would be cut off again and a repair turn cannot finish the answer.
                    log.warning("vision answer cut off at max_tokens=%s [%s]; raise VISION_MAX_TOKENS if this "
                                "repeats", self.max_tokens, facts)
                    raise ExternalServiceError(
                        f"The vision model ran out of output tokens before it finished the reading "
                        f"(max_tokens={self.max_tokens}; {facts}).") from exc
                log.warning("vision answer was not JSON (attempt %s): %s [%s]", attempt, last_error, facts)
                if text.strip():             # an empty answer is simply asked again
                    messages = messages + [
                        {"role": "assistant", "content": text},
                        {"role": "user", "content": f"That was not one valid JSON object ({last_error}). "
                                                    "Return only the JSON object."},
                    ]
        raise ExternalServiceError(f"The vision model did not return valid JSON: {last_error}")


def _describe_answer(response) -> str:
    """Counts only, never the text (it can hold a dealer's order): why did this answer fail?"""
    choice = response.choices[0]
    usage = getattr(response, "usage", None)
    used = getattr(usage, "completion_tokens", None)
    thinking = getattr(getattr(usage, "completion_tokens_details", None), "reasoning_tokens", None)
    parts = [f"finish_reason={choice.finish_reason}"]
    if used is not None:
        parts.append(f"{used} output tokens" + (f", {thinking} of them thinking" if thinking else ""))
    parts.append(f"{len(choice.message.content or '')} characters of answer")
    return ", ".join(parts)


_THINK_BLOCK = re.compile(r"<think(?:ing)?>.*?</think(?:ing)?>", re.DOTALL | re.IGNORECASE)
_THINK_CLOSE = re.compile(r"</think(?:ing)?>", re.IGNORECASE)
_THINK_OPEN = re.compile(r"<think(?:ing)?>", re.IGNORECASE)


def _strip_thinking(text: str) -> str:
    """Drop thinking that came back inside the answer text (some servers do this)."""
    text = _THINK_BLOCK.sub("", text)
    closers = list(_THINK_CLOSE.finditer(text))
    if closers:                                  # only the closing tag: the opening one was in the prompt
        text = text[closers[-1].end():]
    opener = _THINK_OPEN.search(text)
    if opener:                                   # thinking that never ended: nothing after it is an answer
        text = text[: opener.start()]
    return text


def _closing_brace(text: str, start: int) -> int:
    """Index of the `}` that closes the `{` at `start`, or -1. Braces inside strings do not count."""
    depth = 0
    in_string = escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
    return -1


def _json_object(text: str) -> dict:
    """The JSON object in a model answer, or a ValueError that says why there is none.

    Accepts a bare object, an object in a ``` fence, and an object with text before or after it.
    Thinking inside the text (<think>...</think>) is dropped first, so braces in it cannot confuse
    the search. If there are several objects, the last valid one wins (a draft, then the answer).

    It never returns part of an answer: an object that does not close (the output was cut off) or
    does not parse is an error, and we do not look for smaller objects inside it. A reading with a
    silently missing line is worse than an error.
    """
    raw = text or ""
    text = _strip_thinking(raw)
    if not text.strip():
        raise ValueError("the answer was empty" if not raw.strip() else "the answer was only thinking")
    found: dict | None = None
    problem = ""
    pos = 0
    while True:
        start = text.find("{", pos)
        if start == -1:
            break
        end = _closing_brace(text, start)
        if end == -1:
            problem = problem or "the JSON object is cut off (it never closes)"
            break
        try:
            found = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            problem = problem or f"invalid JSON: {exc}"
        pos = end + 1
    if found is None:
        raise ValueError(problem or "no JSON object found")
    return found
