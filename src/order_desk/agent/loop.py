"""The reference agent: a plain tool-calling loop over the SAME tools.

Why does this exist when Hermes is the main runtime?
1. Evals. The conversation evals need an agent that runs in-process, fast,
   and the same way every time. This loop uses the same tools and the same
   instructions (agent/instructions/*.md) as the Hermes skills.
2. Fallback. If the NemoClaw + Hermes setup is not ready on a machine,
   `order-desk devbot` runs this loop on Telegram, so the demo still works.
3. Safety reference. Here the caller's Telegram ID is NOT a model input:
   the loop removes it from every tool schema and fills it from the real
   Telegram update. The model cannot act as another person.

Each run returns a `Trace`: every model turn and every tool call with its
arguments, result and time. The evals score these traces.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from importlib import resources
from typing import Any

from mcp.server.mcpserver.tools.base import Tool
from pydantic import ValidationError

from order_desk.mcp_server.server import build_mcp
from order_desk.mcp_server.tools import ToolSpec

log = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 10


def load_instructions(role: str) -> str:
    return resources.files("order_desk.agent").joinpath(f"instructions/{role}.md").read_text("utf-8")


@dataclass
class Trace:
    events: list[dict] = field(default_factory=list)

    def add(self, kind: str, **data: Any) -> None:
        self.events.append({"t": round(time.time(), 3), "kind": kind, **data})

    def tool_calls(self) -> list[dict]:
        return [e for e in self.events if e["kind"] == "tool_call"]


def openai_tool_schemas(specs: list[ToolSpec]) -> list[dict]:
    """OpenAI-style tool definitions from the MCP tool schemas, minus the identity field."""
    server = build_mcp("schema-only", "", specs)
    tools = asyncio.run(server.list_tools()) if not _loop_running() else _list_in_thread(server)
    by_name = {s.name: s for s in specs}
    out = []
    for t in tools:
        schema = json.loads(json.dumps(t.input_schema))
        hidden = by_name[t.name].identity_param
        if hidden:
            schema.get("properties", {}).pop(hidden, None)
            schema["required"] = [r for r in schema.get("required", []) if r != hidden]
        out.append({"type": "function",
                    "function": {"name": t.name, "description": t.description or "", "parameters": schema}})
    return out


class ToolAgent:
    """One role (dealer or shop), one model, one set of tools."""

    def __init__(self, specs: list[ToolSpec], instructions: str, client, model: str):
        self.specs = {s.name: s for s in specs}
        # The SDK's own argument models: the same validation and type coercion
        # ("12" -> 12) as a call through the MCP server.
        self._arg_models = {s.name: Tool.from_function(s.fn, name=s.name).fn_metadata for s in specs}
        self.tools = openai_tool_schemas(specs)
        self.instructions = instructions
        self.client = client           # an openai.OpenAI client (Token Factory) or a fake
        self.model = model

    def run(self, history: list[dict], user_content: str, caller_id: int) -> tuple[str, list[dict], Trace]:
        """Answer one incoming message. Returns (reply, new history, trace)."""
        trace = Trace()
        messages = [{"role": "system", "content": self.instructions}] + history + [
            {"role": "user", "content": user_content}]
        trace.add("user", content=user_content)
        for _round in range(MAX_TOOL_ROUNDS):
            started = time.time()
            response = self.client.chat.completions.create(
                model=self.model, messages=messages, tools=self.tools, tool_choice="auto", temperature=0.2)
            msg = response.choices[0].message
            usage = getattr(response, "usage", None)
            trace.add("model", seconds=round(time.time() - started, 2),
                      prompt_tokens=getattr(usage, "prompt_tokens", None),
                      completion_tokens=getattr(usage, "completion_tokens", None))
            if not msg.tool_calls:
                reply = (msg.content or "").strip()
                trace.add("reply", content=reply)
                return reply, _compact(history, user_content, reply), trace
            messages.append({"role": "assistant", "content": msg.content or "",
                             "tool_calls": [{"id": c.id, "type": "function",
                                             "function": {"name": c.function.name,
                                                          "arguments": c.function.arguments}}
                                            for c in msg.tool_calls]})
            for call in msg.tool_calls:
                result = self._execute(call.function.name, call.function.arguments, caller_id, trace)
                messages.append({"role": "tool", "tool_call_id": call.id,
                                 "content": json.dumps(result, ensure_ascii=False, default=str)})
        reply = "Sorry, this needs a person. The shop will reply soon."
        trace.add("gave_up", rounds=MAX_TOOL_ROUNDS)
        return reply, _compact(history, user_content, reply), trace

    def _execute(self, name: str, raw_args: str, caller_id: int, trace: Trace) -> dict:
        started = time.time()
        spec = self.specs.get(name)
        if spec is None:
            result = {"ok": False, "error": "unknown_tool", "message": f"No tool named {name}."}
        else:
            try:
                args = json.loads(raw_args or "{}")
                if not isinstance(args, dict):
                    raise ValueError("arguments must be a JSON object")
            except ValueError as exc:
                result = {"ok": False, "error": "invalid_arguments", "message": str(exc)}
            else:
                if spec.identity_param:
                    args[spec.identity_param] = caller_id      # the model never chooses who it is
                try:
                    kwargs = self._arg_models[name].validate_arguments(args)
                    result = spec.fn(**kwargs)
                except ValidationError as exc:                   # wrong types or missing arguments
                    result = {"ok": False, "error": "invalid_arguments",
                              "message": exc.errors(include_url=False)[0].get("msg", str(exc))}
        trace.add("tool_call", name=name, args=raw_args, result=result, seconds=round(time.time() - started, 3))
        return result


# -------------------------------------------------------------- helpers

def _compact(history: list[dict], user_content: str, reply: str) -> list[dict]:
    """Keep only what people said, not the tool calls. The database holds the
    real state, so the agent can always look it up again with a tool. This also
    keeps the history valid when old turns are cut off."""
    return history + [{"role": "user", "content": user_content}, {"role": "assistant", "content": reply}]


def _loop_running() -> bool:
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


def _list_in_thread(server):
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, server.list_tools()).result()
