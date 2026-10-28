"""Eval 3: owner questions -> SQL -> answer. Needs NEBIUS_API_KEY.

    python -m evals.run_text2sql
    python -m evals.run_text2sql --model <id>

We build a small shop history (evals/scenario.py), ask each golden question,
let the model write ONE SELECT (with the same view description the shop
agent gets from describe_reporting_views), run it through the read-only
guard, and compare the RESULT with the result of the reference SQL.

We compare results, not SQL text: many different queries are correct.
Column names and row order are ignored; numbers are compared after rounding.

Metrics: execution_accuracy (same result), valid_sql_rate (ran without error),
refused_rate (blocked by the guard - should be 0 for honest questions).
"""

from __future__ import annotations

import argparse
import re
import tempfile
from pathlib import Path

from evals.common import GOLDEN, Run, fresh_deps, ratio, read_jsonl
from evals.scenario import QueueReader, build_history
from order_desk.services.reports import describe_views, run_readonly_query

PROMPT = """You write SQLite queries for an electrical shop's database.
Use ONLY these views (no other tables):
{views}
Notes: money columns ending in _rupees are rupees. Times are UTC ISO text; use substr(created_at,1,10) for a date.
Order states: RECEIVED, PARSED, AWAITING_DEALER, AWAITING_OWNER, APPROVED, PACKING, PACKED, DISPATCHED,
REJECTED, EXPIRED, CANCELLED. Sold = order_state in APPROVED, PACKING, PACKED, DISPATCHED.

Question: {question}
Return ONE SELECT statement in a ```sql block and nothing else."""


def canonical(rows: list[dict]) -> list[tuple]:
    def value(v):
        if isinstance(v, float):
            return round(v, 2)
        return v
    return sorted((tuple(value(v) for v in r.values()) for r in rows), key=repr)


def extract_sql(text: str) -> str:
    match = re.search(r"```(?:sql)?\s*(.*?)```", text or "", re.DOTALL | re.IGNORECASE)
    return (match.group(1) if match else text or "").strip().rstrip(";")


def evaluate(client, model: str) -> dict:
    workdir = Path(tempfile.mkdtemp()) / "text2sql"
    deps = fresh_deps(workdir, reader=QueueReader())
    build_history(deps, workdir)
    views = "\n".join(f"- {v}({', '.join(cols)})" for v, cols in describe_views(deps.db).items())

    run = Run("text2sql", {"model": model})
    valid = refused = correct = 0
    cases = read_jsonl(GOLDEN / "text2sql.jsonl")
    for case in cases:
        expected = canonical(run_readonly_query(deps.db, case["reference_sql"])["rows"])
        response = client.chat.completions.create(
            model=model, temperature=0,
            messages=[{"role": "user", "content": PROMPT.format(views=views, question=case["question"])}])
        sql = extract_sql(response.choices[0].message.content)
        try:
            got = canonical(run_readonly_query(deps.db, sql)["rows"])
            valid += 1
            ok = got == expected
            correct += ok
            reason = "same result" if ok else f"got {got[:3]} expected {expected[:3]}"
        except Exception as exc:
            ok, reason = False, f"query failed: {exc}"
            refused += "Not allowed" in str(exc)
        run.add({"id": case["id"], "passed": ok, "reason": reason, "question": case["question"], "sql": sql})
    return run.save({"execution_accuracy": ratio(correct, len(cases)), "valid_sql_rate": ratio(valid, len(cases)),
                     "refused_rate": ratio(refused, len(cases))})


def main() -> dict:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=None)
    args = parser.parse_args()
    from openai import OpenAI

    from evals.common import REPO
    from order_desk.config import load_settings
    s = load_settings(REPO / ".env")
    client = OpenAI(api_key=s.nebius_api_key, base_url=s.nebius_base_url, timeout=60)
    return evaluate(client, args.model or s.agent_model)


if __name__ == "__main__":
    main()
