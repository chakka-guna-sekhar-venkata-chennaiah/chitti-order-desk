"""Eval 4: full dealer conversations with a simulated dealer. Needs NEBIUS_API_KEY.

    python -m evals.run_conversations
    python -m evals.run_conversations --only answer-ambiguity --sim-model <id>

How one scenario runs
---------------------
1. A fresh shop database. The chitti "reading" is FIXED by the scenario, so
   this eval measures the AGENT (tool choice, questions, confirmation), not
   the vision model. The vision model has its own eval (run_chit_reader).
2. The dealer simulator is a second model call. It plays a persona, knows the
   hidden truth ("line 4 is a 3-module Anchor box") and has a goal. It must
   answer in JSON with its intent: confirm / change / question / cancel / other.
   When it sets "done", that last message is still answered by the agent; then the chat ends.
   The photo reaches the agent as an upload reference ("up:<id>", as POST /uploads returns it).
3. The agent under test is the reference agent with the dealer tools and the
   same instructions as the Hermes skill.
4. After the chat, CODE checks the database and the trace. No model grades
   another model here.

Checks for each scenario (all must pass)
    final_state        the dealer's latest order ends in the expected state
    lines              expected SKU / quantity / status per line
    must_call / must_not_call   tools that must (not) appear in the trace
    confirm_after_yes  confirm_order only in a turn where the dealer said yes
    invariants         services/invariants.py finds no broken rule

Extra metrics: readback_fidelity (read-back lines copied exactly into the
reply), turns, tool calls and seconds per conversation.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import tempfile
import time
from pathlib import Path

from PIL import Image

from evals.common import GOLDEN, Run, fresh_deps, ratio, read_jsonl
from evals.scenario import QueueReader
from order_desk.agent.loop import ToolAgent, load_instructions
from order_desk.mcp_server.tools import dealer_tools
from order_desk.services import invariants
from order_desk.services.dealer_desk import DealerDesk
from order_desk.vision.images import store

MAX_TURNS = 8

SIMULATOR = """You play a dealer who orders from an electrical shop on Telegram. You are NOT an assistant.
Persona: {persona}
Hidden truth about your chitti (the shop does not know it): {truth}
Your goal: {goal}

Rules:
- Answer the shop's last message the way this person would: short, natural, in the persona's language.
- Only say yes / confirm when the latest read-back is fully correct for you.
- Set "done" to true when the goal is reached (the order is confirmed or cancelled) or clearly impossible.
Return ONLY a JSON object: {{"message": "...", "intent": "confirm|change|question|cancel|other", "done": true|false}}"""


def simulate(client, model: str, scenario: dict, transcript: list[dict]) -> dict:
    system = SIMULATOR.format(**scenario)
    convo = "\n".join(f"{'SHOP' if t['who'] == 'agent' else 'YOU'}: {t['text']}" for t in transcript)
    response = client.chat.completions.create(
        model=model, temperature=0.3,
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": f"Conversation so far:\n{convo}\n\nYour next message (JSON):"}])
    text = response.choices[0].message.content or "{}"
    try:
        data = json.loads(text[text.find("{"): text.rfind("}") + 1])
    except ValueError:
        data = {"message": text.strip()[:300], "intent": "other", "done": False}
    data.setdefault("intent", "other")
    data.setdefault("done", False)
    return data


def readback_fidelity(trace_events: list[dict], reply: str) -> float | None:
    """Share of read-back lines (from the last tool result) that appear exactly in the reply."""
    readbacks = [e["result"].get("readback") for e in trace_events
                 if e["kind"] == "tool_call" and isinstance(e.get("result"), dict) and e["result"].get("readback")]
    if not readbacks:
        return None
    lines = [l.strip() for l in readbacks[-1].splitlines() if re.match(r"^\d+\.", l.strip())]
    if not lines:
        return None
    return sum(1 for l in lines if l in reply) / len(lines)


def run_scenario(scenario: dict, client, agent_model: str, sim_model: str) -> dict:
    workdir = Path(tempfile.mkdtemp()) / scenario["id"]
    reader = QueueReader()
    reader.chits.extend([{"lines": scenario["reading"]}] * 3)        # a re-read gets the same reading
    deps = fresh_deps(workdir, reader=reader)
    agent = ToolAgent(dealer_tools(DealerDesk(deps)), load_instructions("dealer"), client, agent_model)

    buf = io.BytesIO()
    Image.new("RGB", (40, 60), (230, 230, 220)).save(buf, format="JPEG")
    # The photo arrives the way the server's own POST /uploads route reports it: "up:<id>". It used to be
    # "file:<path>", a form that the instructions and the tool text do not list (sandbox:, tg:, up:). The model
    # rewrote it into those forms, every read failed and no order left RECEIVED (KI-064).
    image_ref = f"up:{store(deps.db, deps.settings.upload_dir, buf.getvalue(), source='upload').upload_id}"

    dealer_id = scenario["dealer"]
    content = (f"[telegram message_id=5001 from_user_id={dealer_id}]\n"
               f"The person sent a PHOTO. image_ref={image_ref}\nCaption: order")
    transcript = [{"who": "dealer", "text": "(photo of chitti)", "intent": "other"}]
    history: list[dict] = []
    all_calls: list[dict] = []
    violations: list[str] = []
    fidelities: list[float] = []
    started = time.time()
    intent = "other"
    finishing = False        # the simulator said "done": answer its last message, then stop
    for turn in range(MAX_TURNS):
        reply, history, trace = agent.run(history, content, dealer_id)
        calls = trace.tool_calls()
        all_calls.extend(calls)
        for c in calls:
            if c["name"] == "confirm_order" and c["result"].get("ok") and intent != "confirm":
                violations.append(f"turn {turn}: confirm_order called but the dealer's intent was '{intent}'")
        fid = readback_fidelity(trace.events, reply)
        if fid is not None:
            fidelities.append(fid)
        transcript.append({"who": "agent", "text": reply})
        if finishing:
            break
        sim = simulate(client, sim_model, scenario, transcript)
        transcript.append({"who": "dealer", "text": sim["message"], "intent": sim["intent"]})
        # "done" comes WITH the dealer's final message ("Yes.", "Cancel it."). That message must still reach the
        # agent, or the order is never confirmed or cancelled (KI-063: clean-confirm ended before confirm_order).
        intent, finishing = sim["intent"], bool(sim.get("done"))
        content = f"[telegram message_id={5002 + turn} from_user_id={dealer_id}]\n{sim['message']}"

    # ---- checks on the database and the trace
    expect = scenario["expect"]
    with deps.db.read() as conn:
        row = conn.execute("SELECT o.id, o.state FROM orders o JOIN dealers d ON d.id = o.dealer_id "
                           "WHERE d.telegram_user_id = ? ORDER BY o.id DESC LIMIT 1", (dealer_id,)).fetchone()
        final_state = row["state"] if row else None
        lines = {r["line_index"]: dict(r) for r in conn.execute(
            "SELECT * FROM order_lines WHERE order_id = ?", (row["id"],))} if row else {}
    problems = list(violations)
    if final_state != expect.get("final_state"):
        problems.append(f"final state {final_state}, expected {expect.get('final_state')}")
    for want in expect.get("lines", []):
        got = lines.get(want["line_index"], {})
        for key in ("sku", "qty", "status"):
            if key in want and got.get(key) != want[key]:
                problems.append(f"line {want['line_index']} {key}={got.get(key)}, expected {want[key]}")
    names = [c["name"] for c in all_calls]
    problems += [f"did not call {n}" for n in expect.get("must_call", []) if n not in names]
    problems += [f"called {n}" for n in expect.get("must_not_call", []) if n in names]
    problems += [f"invariant: {p}" for p in invariants.check(deps.db)]
    bad_args = sum(1 for c in all_calls if c["result"].get("error") == "invalid_arguments")

    return {
        "id": scenario["id"], "passed": not problems, "reason": "; ".join(problems) or "all checks passed",
        "final_state": final_state, "turns": len([t for t in transcript if t["who"] == "agent"]),
        "tool_calls": len(all_calls), "invalid_argument_calls": bad_args,
        "confirm_violations": len(violations),
        "readback_fidelity": round(sum(fidelities) / len(fidelities), 3) if fidelities else None,
        "seconds": round(time.time() - started, 1),
        "transcript": transcript, "tools": names,
    }


def main() -> dict:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", default=None, help="run one scenario id")
    parser.add_argument("--model", default=None, help="agent model")
    parser.add_argument("--sim-model", default=None, help="dealer simulator model")
    args = parser.parse_args()

    from openai import OpenAI
    from order_desk.config import load_settings
    from evals.common import REPO
    s = load_settings(REPO / ".env")
    client = OpenAI(api_key=s.nebius_api_key, base_url=s.nebius_base_url, timeout=120)
    agent_model, sim_model = args.model or s.agent_model, args.sim_model or s.agent_model

    scenarios = [x for x in read_jsonl(GOLDEN / "conversations.jsonl") if not args.only or x["id"] == args.only]
    run = Run("conversations", {"agent_model": agent_model, "sim_model": sim_model})
    for scenario in scenarios:
        try:
            run.add(run_scenario(scenario, client, agent_model, sim_model))
        except Exception as exc:
            run.add({"id": scenario["id"], "passed": False, "reason": f"crashed: {exc!r}"})
    done = [c for c in run.cases if "turns" in c]
    fids = [c["readback_fidelity"] for c in done if c.get("readback_fidelity") is not None]
    return run.save({
        "task_success_rate": ratio(sum(c["passed"] for c in run.cases), len(run.cases)),
        "confirm_violations": sum(c.get("confirm_violations", 0) for c in done),
        "invariant_failures": sum(1 for c in done if "invariant:" in c["reason"]),
        "readback_fidelity": round(sum(fids) / len(fids), 3) if fids else None,
        "avg_turns": round(sum(c["turns"] for c in done) / len(done), 1) if done else None,
        "avg_tool_calls": round(sum(c["tool_calls"] for c in done) / len(done), 1) if done else None,
        "invalid_argument_calls": sum(c.get("invalid_argument_calls", 0) for c in done),
        "avg_seconds": round(sum(c["seconds"] for c in done) / len(done), 1) if done else None,
    })


if __name__ == "__main__":
    main()
