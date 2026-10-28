"""The reference agent loop with a scripted fake model (no network).

Checks the safety rule of this runtime: the caller's Telegram ID is filled
by the loop, so a model that tries to act as another person cannot.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from conftest import RAMESH, SURESH
from order_desk.agent.loop import ToolAgent, load_instructions, openai_tool_schemas
from order_desk.mcp_server.tools import dealer_tools
from order_desk.services.dealer_desk import DealerDesk


class ScriptedModel:
    """Plays back a fixed list of model turns: tool calls first, then a reply."""

    def __init__(self, turns):
        self.turns = list(turns)
        self.seen_tools = None
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.seen_tools = kwargs["tools"]
        turn = self.turns.pop(0)
        if isinstance(turn, str):
            message = SimpleNamespace(content=turn, tool_calls=None)
        else:
            calls = [SimpleNamespace(id=f"c{i}", function=SimpleNamespace(name=n, arguments=json.dumps(a)))
                     for i, (n, a) in enumerate(turn)]
            message = SimpleNamespace(content="", tool_calls=calls)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)


def test_identity_is_hidden_from_the_model(deps):
    schemas = openai_tool_schemas(dealer_tools(DealerDesk(deps)))
    for s in schemas:
        assert "telegram_user_id" not in s["function"]["parameters"].get("properties", {})


def test_model_cannot_act_as_another_dealer(deps):
    model = ScriptedModel([
        [("get_dealer_profile", {"telegram_user_id": SURESH})],       # the model tries to use another ID
        "done",
    ])
    agent = ToolAgent(dealer_tools(DealerDesk(deps)), load_instructions("dealer"), model, "fake")
    reply, history, trace = agent.run([], "hello", caller_id=RAMESH)
    result = trace.tool_calls()[0]["result"]
    assert result["name"] == "Ramesh Babu"                            # the loop used the real caller
    assert reply == "done"
    assert [m["role"] for m in history] == ["user", "assistant"]


def test_string_numbers_are_coerced_like_mcp(deps):
    model = ScriptedModel([[("get_order_status", {"order_id": "999"})], "ok"])
    agent = ToolAgent(dealer_tools(DealerDesk(deps)), load_instructions("dealer"), model, "fake")
    _, _, trace = agent.run([], "status of 999", caller_id=RAMESH)
    assert trace.tool_calls()[0]["result"]["error"] == "not_found"    # validated and called, not a type error


def test_unknown_tool_and_bad_json_are_reported_to_the_model(deps):
    model = ScriptedModel([[("approve_order", {"order_id": 1})], "sorry"])
    agent = ToolAgent(dealer_tools(DealerDesk(deps)), load_instructions("dealer"), model, "fake")
    _, _, trace = agent.run([], "approve it", caller_id=RAMESH)
    assert trace.tool_calls()[0]["result"]["error"] == "unknown_tool"  # dealer scope has no approve tool


# ---------------------------------------------------------------- instructions (KI-065)

def test_dealer_instructions_forbid_asking_for_yes_without_a_readback():
    # In the 2026-10-27 conversation eval, three runs told the dealer "reply YES to confirm" after the photo
    # could not be read, so there was no read-back. The rule is in the instructions the model gets.
    text = " ".join(load_instructions("dealer").split())
    assert "never ask the dealer to reply YES, unless a tool result in this turn gave you a `readback`" in text


def test_instructions_require_a_status_call_in_this_turn_before_stating_an_order_state():
    # In the 2026-10-28 live take the dealer agent said "Your order #4 is still waiting for the shop's approval"
    # after the owner had approved and packed it. Approvals, bills and "packed" go out through the outbox, so they
    # never enter the agent's conversation, and an old tool result looked current (KI-075).
    dealer = " ".join(load_instructions("dealer").split())
    shop = " ".join(load_instructions("shop").split())
    assert "Before you mention an order's state, call `get_order_status` in this turn." in dealer
    assert "Before you mention an order's state, call `get_order_details` or `list_orders` in this turn." in shop


def test_the_hermes_files_match_the_instructions(tmp_path):
    # `order-desk export-skills` output is kept in hermes/ and nothing else checked that it was run.
    from pathlib import Path

    from order_desk.agent.skills_export import export

    repo = Path(__file__).resolve().parents[1]
    for path in export(tmp_path):
        committed = repo / "hermes" / path.relative_to(tmp_path)
        assert committed.read_text(encoding="utf-8") == path.read_text(encoding="utf-8"), \
            f"{committed.relative_to(repo)} is out of date: run `order-desk export-skills`"
