"""The eval harness itself, run offline with fake models.

A perfect fake model must score 1.0, and a broken one must fail. This proves
the scoring code before we trust it with real model runs.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import evals.common as common  # noqa: E402
from evals import run_chit_reader, run_conversations, run_text2sql  # noqa: E402
from order_desk.domain.chitti import ChitReading  # noqa: E402


@pytest.fixture(autouse=True)
def results_in_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "RESULTS", tmp_path / "results")


def _response(content: str = "", tool_calls=None):
    msg = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=None)


# ------------------------------------------------------------ chit reader

class LabelReader:
    """A 'perfect' vision model for easy_01: returns its label (the tests use limit=1)."""
    model = "label-oracle"

    def read_chit_image(self, image, mime):
        label = json.loads((common.GOLDEN / "labels" / "easy_01.example.json").read_text())
        return ChitReading.from_model_json(label)


class LazyReader(LabelReader):
    """A bad model: drops the last line and reads 20 as 2."""
    def read_chit_image(self, image, mime):
        reading = super().read_chit_image(image, mime)
        reading.lines = reading.lines[:-1]
        reading.lines[3].quantity = 2
        return reading


def test_chit_reader_perfect_model_scores_one():
    m = run_chit_reader.evaluate(LabelReader(), include_examples=True, limit=1)["metrics"]
    assert m["line_recall"] == 1.0 and m["order_line_accuracy"] == 1.0 and m["decision_accuracy"] == 1.0


def test_chit_reader_bad_model_is_caught():
    m = run_chit_reader.evaluate(LazyReader(), include_examples=True, limit=1)["metrics"]
    assert m["line_recall"] == 0.9
    assert m["order_line_accuracy"] == 0.8
    assert m["chitti_pass_rate"] == 0.0


def test_text_similarity():
    assert run_chit_reader.similarity("6A switch (Anchor)", "6A switch Anchor") == 1.0
    assert run_chit_reader.similarity("1.5 r - 2c", "1.5 b - 2c") < 1.0


# ------------------------------------------------------------ text2sql

class SqlOracle:
    """Answers each question with its reference SQL (a perfect model)."""
    def __init__(self, perfect=True):
        self.cases = {c["question"]: c["reference_sql"] for c in common.read_jsonl(common.GOLDEN / "text2sql.jsonl")}
        self.perfect = perfect
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kw):
        question = re.search(r"Question: (.*)\n", kw["messages"][0]["content"]).group(1)
        sql = self.cases[question] if self.perfect else "SELECT * FROM dealers"
        return _response(f"```sql\n{sql}\n```")


def test_text2sql_perfect_and_refused():
    assert run_text2sql.evaluate(SqlOracle(True), "oracle")["metrics"]["execution_accuracy"] == 1.0
    bad = run_text2sql.evaluate(SqlOracle(False), "bad")["metrics"]
    assert bad["execution_accuracy"] == 0.0 and bad["refused_rate"] == 1.0


# ------------------------------------------------------------ conversations

class RuleAgentAndDealer:
    """Plays BOTH roles with simple rules: a correct agent, and a dealer who says yes.

    done_with_yes: the dealer sets "done" in the same message as its final yes, as the real simulator does.
    documented_refs_only: the agent accepts only the photo references that the instructions and the tool
    describe (sandbox:, tg:, up:), as the real model did. A "file:" reference made it rewrite the reference,
    so every read failed (KI-064).
    """

    def __init__(self, confirm_early=False, done_with_yes=False, documented_refs_only=False):
        self.confirm_early = confirm_early
        self.done_with_yes = done_with_yes
        self.documented_refs_only = documented_refs_only
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kw):
        msgs = kw["messages"]
        if "tools" not in kw:                                     # the dealer simulator
            text = msgs[-1]["content"]
            if "is confirmed" in text:
                return _response('{"message": "thanks", "intent": "other", "done": true}')
            return _response(json.dumps({"message": "yes, correct", "intent": "confirm",
                                         "done": self.done_with_yes}))
        last = msgs[-1]
        if last["role"] == "user" and last["content"].endswith("thanks"):
            return _response("You are welcome.")
        call = lambda name, args: _response("", [SimpleNamespace(id="c1", function=SimpleNamespace(
            name=name, arguments=json.dumps(args)))])
        if last["role"] == "user" and "PHOTO" in last["content"]:
            self.first_user_message = last["content"]
            if self.confirm_early:
                return call("confirm_order", {"order_id": 1})
            return call("start_order", {"source_kind": "photo", "source_ref": "5001"})
        if last["role"] == "tool":
            result = json.loads(last["content"])
            if result.get("state") == "RECEIVED":
                image_ref = re.search(r"image_ref=(\S+)", msgs[1]["content"]).group(1)
                if self.documented_refs_only:
                    assert image_ref.split(":", 1)[0] in ("sandbox", "tg", "up"), image_ref
                return call("read_chitti_photo", {"order_id": result["order_id"], "image_ref": image_ref})
            if result.get("readback") and result.get("state") == "AWAITING_DEALER":
                return _response(result["readback"])
            return _response(result.get("message", "ok"))
        order_id = int(re.findall(r"Order #(\d+)", json.dumps(msgs))[-1])
        return call("confirm_order", {"order_id": order_id})


def _scenario(sid):
    return next(x for x in common.read_jsonl(common.GOLDEN / "conversations.jsonl") if x["id"] == sid)


def test_conversation_harness_passes_a_correct_agent():
    result = run_conversations.run_scenario(_scenario("clean-confirm"), RuleAgentAndDealer(), "a", "s")
    assert result["passed"], result["reason"]
    assert result["readback_fidelity"] == 1.0


def test_conversation_harness_fails_a_wrong_end_state():
    result = run_conversations.run_scenario(_scenario("cancel"), RuleAgentAndDealer(), "a", "s")
    assert not result["passed"]
    assert "final state AWAITING_OWNER, expected CANCELLED" in result["reason"]


def test_the_photo_reaches_the_agent_as_a_reference_the_instructions_describe():
    agent = RuleAgentAndDealer(documented_refs_only=True)
    result = run_conversations.run_scenario(_scenario("clean-confirm"), agent, "a", "s")
    assert result["passed"], result["reason"]
    assert re.search(r"image_ref=up:up_[0-9a-f]{16}\b", agent.first_user_message)


def test_the_dealers_last_message_is_still_answered_when_the_simulator_says_done():
    # The real simulator sets "done" in the same message as "Yes." The old harness stopped before the agent
    # saw it, so confirm_order was never called and the order stayed AWAITING_DEALER (KI-063).
    result = run_conversations.run_scenario(_scenario("clean-confirm"), RuleAgentAndDealer(done_with_yes=True),
                                            "a", "s")
    assert result["passed"], result["reason"]
    assert result["final_state"] == "AWAITING_OWNER"
    assert result["confirm_violations"] == 0
