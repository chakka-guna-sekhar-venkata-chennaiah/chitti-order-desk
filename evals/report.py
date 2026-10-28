"""Make evals/results/SCORECARD.md from the latest run of each eval,
with the change since the run before it (the "before / after" view).

    python -m evals.report
"""

from __future__ import annotations

import json

from evals.common import RESULTS

ORDER = ["chit_reader", "matching", "text2sql", "conversations"]
TITLES = {
    "chit_reader": "1. Chitti reader (vision model)",
    "matching": "2. Product matcher (code)",
    "text2sql": "3. Owner questions (text-to-SQL)",
    "conversations": "4. Dealer conversations (agent + simulated dealer)",
}
# Metrics where a LOWER value is better.
LOWER_IS_BETTER = {"unsafe_accept_rate", "refused_rate", "confirm_violations", "invariant_failures",
                   "invalid_argument_calls", "avg_seconds", "avg_turns", "avg_tool_calls"}


def main() -> None:
    lines = ["# Eval scorecard", "",
             "Made by `python -m evals.report` from the latest run of each eval.",
             "Change = latest run minus the run before it.", ""]
    for name in ORDER:
        history_file = RESULTS / name / "history.jsonl"
        lines.append(f"## {TITLES[name]}")
        if not history_file.exists():
            lines += ["", "Not run yet.", ""]
            continue
        runs = [json.loads(l) for l in history_file.read_text(encoding="utf-8").splitlines() if l.strip()]
        latest, previous = runs[-1], (runs[-2] if len(runs) > 1 else None)
        lines += ["", f"Run: {latest['finished_at']} - {latest['cases']} cases - config: `{json.dumps(latest['config'])}`",
                  "", "| Metric | Value | Change |", "|---|---|---|"]
        for metric, value in latest["metrics"].items():
            change = ""
            if previous and isinstance(value, (int, float)) and isinstance(previous["metrics"].get(metric), (int, float)):
                delta = round(value - previous["metrics"][metric], 3)
                if delta:
                    better = (delta < 0) if metric in LOWER_IS_BETTER else (delta > 0)
                    change = f"{delta:+g} {'better' if better else 'WORSE'}"
            lines.append(f"| {metric} | {value} | {change} |")
        lines.append("")
    out = RESULTS / "SCORECARD.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(out.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
