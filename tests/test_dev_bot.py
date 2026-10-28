"""The dev bot's send path: plain text (KI-068) and a small retry (KI-069). No test calls Telegram.

Live run 2026-10-27: the model wrote "**PACKING**" and Telegram showed the stars, and one reply was lost
for good after "network error: [Errno 54] Connection reset by peer".
"""

from __future__ import annotations

import json

import httpx
import pytest

from conftest import OWNER, PACKER, RAMESH, line, outbox_rows, reading
from order_desk.agent.dev_bot import SEND_RETRY_WAITS, _safe_send, plain_text
from order_desk.agent.loop import load_instructions
from order_desk.telegram.bot_api import BotApi, TelegramError


class FakeApi:
    """Stands in for BotApi.send_message. Each outcome is None (sent) or an exception to raise."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.sent: list[tuple[int, str]] = []          # one entry for every try

    def send_message(self, chat_id: int, text: str) -> None:
        self.sent.append((chat_id, text))
        outcome = self.outcomes.pop(0)
        if outcome is not None:
            raise outcome


def _real_api_answering(*answers):
    """A real BotApi whose HTTP layer is local: each answer is a status code or an exception to raise.
    The last answer repeats. This checks how BotApi marks an error as `retryable`, without a network."""
    api = BotApi("123456:not-a-real-token")
    api._http.close()
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        answer = answers[min(len(calls), len(answers)) - 1]
        if isinstance(answer, Exception):
            raise answer
        if answer == 200:
            return httpx.Response(200, json={"ok": True, "result": {}})
        return httpx.Response(answer, json={"ok": False, "description": f"error {answer}"})

    api._http = httpx.Client(transport=httpx.MockTransport(handler))
    return api, calls


# ------------------------------------------------------- plain text (bug 3, KI-068)

@pytest.mark.parametrize("raw, clean", [
    ("Order 1 is **PACKING** now", "Order 1 is PACKING now"),
    ("**PACKED**", "PACKED"),
    ("__PACKED__ and **ready**", "PACKED and ready"),
    ("**Order #1** is **PACKING**.", "Order #1 is PACKING."),
    ("# Pending\n## Orders\n### 1. Order #1", "Pending\nOrders\n1. Order #1"),
])
def test_plain_text_removes_simple_markdown(raw, clean):
    assert plain_text(raw) == clean


@pytest.mark.parametrize("text", [
    "Order #1 is confirmed",                            # a # that is not a heading
    "#12 Ravi Electricals",
    "Rs 1,55,000 across 2 bills (?) No brand written; we chose Anchor",
    "SKU WIRE-1.5-RED-FINOLEX x 2",
    "my_var, a__b__c, 2 * 3 * 4 and ** alone **",
])
def test_plain_text_keeps_text_that_is_not_markdown(text):
    assert plain_text(text) == text


def test_the_code_built_messages_pass_through_unchanged(dealer, shop, deps, photo):
    # services/messages.py is not changed, and the strip must not change its texts when the model relays them.
    deps.reader.chits.append(reading(line("6A switch - 20 nos (Anchor)", "switch", {"rating_amp": 6},
                                          "Anchor", 20, "nos")))
    order_id = dealer.start_order(RAMESH, "photo", "msg-1")["order_id"]
    readback = dealer.read_chit(RAMESH, order_id, photo())["readback"]
    dealer.confirm(RAMESH, order_id)
    shop.approve(OWNER, order_id)
    shop.mark_packed(PACKER, order_id)
    texts = [readback] + [json.loads(r["payload_json"]).get("text", "") for r in outbox_rows(deps)]
    assert len(texts) >= 5                                  # read-back, owner card, slip, bill, packed notice
    for text in texts:
        assert plain_text(text) == text


def test_the_send_path_removes_markdown_before_telegram_gets_the_text():
    api = FakeApi(None)
    assert _safe_send(api, 7, "Order 1 is **PACKING**") is True
    assert api.sent == [(7, "Order 1 is PACKING")]


@pytest.mark.parametrize("role", ["dealer", "shop"])
def test_both_instruction_files_ask_for_plain_text(role):
    text = " ".join(load_instructions(role).split())
    assert "Write plain text only." in text
    assert "Do not use markdown." in text


# ------------------------------------------------------------ retry (bug 4, KI-069)

def test_a_send_is_tried_again_after_a_network_error():
    api = FakeApi(TelegramError("sendMessage: network error: [Errno 54] Connection reset by peer", retryable=True),
                  None)
    slept: list[float] = []
    assert _safe_send(api, 7, "hello", sleep=slept.append) is True
    assert api.sent == [(7, "hello"), (7, "hello")]
    assert slept == [1.0]


def test_a_send_gives_up_after_three_tries_and_waits_one_then_three_seconds():
    error = TelegramError("sendMessage: network error", retryable=True)
    api = FakeApi(error, error, error, None)               # the 4th outcome must never be used
    slept: list[float] = []
    assert SEND_RETRY_WAITS == (1.0, 3.0)
    assert _safe_send(api, 7, "hello", sleep=slept.append) is False     # no exception reaches the bot loop
    assert len(api.sent) == 3
    assert slept == [1.0, 3.0]


def test_the_waits_are_arguments_so_that_tests_need_no_real_sleep():
    api = FakeApi(TelegramError("x", retryable=True), TelegramError("x", retryable=True), None)
    slept: list[float] = []
    assert _safe_send(api, 7, "hello", waits=(0.0, 0.0), sleep=slept.append) is True
    assert slept == [0.0, 0.0]


@pytest.mark.parametrize("status", [400, 401, 403])
def test_a_400_401_or_403_is_never_retried(status):
    api, calls = _real_api_answering(status)
    slept: list[float] = []
    assert _safe_send(api, 7, "hello", sleep=slept.append) is False
    assert len(calls) == 1
    assert slept == []


@pytest.mark.parametrize("answer", [
    429, 500, 502, 503,
    httpx.ReadError("[Errno 54] Connection reset by peer"),
    httpx.ConnectError("[Errno 8] nodename nor servname provided"),
])
def test_a_network_error_429_or_5xx_is_retried_with_the_real_error_marks(answer):
    api, calls = _real_api_answering(answer, 200)
    slept: list[float] = []
    assert _safe_send(api, 7, "hello", sleep=slept.append) is True
    assert len(calls) == 2
    assert slept == [1.0]
