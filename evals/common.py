"""Shared parts of the eval runners: paths, a fresh database, and the scorecard.

Every runner writes two files to evals/results/<eval>/<timestamp>/:
    results.jsonl   one line per case: input, output, pass/fail, reason
    summary.json    the metrics
and appends one line to evals/results/<eval>/history.jsonl, so you can see
the numbers go up (or down) after each change. `evals/report.py` turns the
latest summaries into evals/results/SCORECARD.md for the README.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
GOLDEN = ROOT / "golden"
RESULTS = ROOT / "results"

sys.path.insert(0, str(REPO / "src"))

from order_desk.config import load_settings  # noqa: E402
from order_desk.db.seed import seed_demo  # noqa: E402
from order_desk.services.deps import Deps  # noqa: E402


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def fresh_deps(workdir: Path, reader=None, owner_id: int = 111111, packer_id: int = 222222) -> Deps:
    """A new seeded database in `workdir`, with the real settings for model access."""
    workdir.mkdir(parents=True, exist_ok=True)
    base = load_settings(REPO / ".env")
    settings = replace(base, db_path=workdir / "eval.sqlite3", upload_dir=workdir / "uploads",
                       generated_dir=workdir / "generated", owner_telegram_ids=[owner_id],
                       packer_telegram_ids=[packer_id])
    if settings.db_path.exists():
        settings.db_path.unlink()
    deps = Deps.build(settings, reader=reader, allow_local_files=True)
    deps.db.initialize()
    seed_demo(deps.db, settings)
    return deps


class Run:
    """One eval run: collect case results, then save them with a summary."""

    def __init__(self, name: str, config: dict | None = None):
        self.name = name
        self.config = config or {}
        self.started = time.time()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        self.dir = RESULTS / name / stamp
        self.dir.mkdir(parents=True, exist_ok=True)
        self.cases: list[dict] = []

    def add(self, case: dict) -> None:
        self.cases.append(case)
        mark = "PASS" if case.get("passed") else "FAIL"
        print(f"  [{mark}] {case.get('id')}: {case.get('reason', '')}"[:160])

    def save(self, metrics: dict) -> dict:
        summary = {"eval": self.name, "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   "seconds": round(time.time() - self.started, 1), "cases": len(self.cases),
                   "config": self.config, "metrics": metrics}
        with (self.dir / "results.jsonl").open("w", encoding="utf-8") as fh:
            for case in self.cases:
                fh.write(json.dumps(case, ensure_ascii=False, default=str) + "\n")
        (self.dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        with (RESULTS / self.name / "history.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(summary) + "\n")
        print(f"\n{self.name}: " + ", ".join(f"{k}={v}" for k, v in metrics.items()))
        print(f"saved: {self.dir}")
        return summary


def ratio(numerator: float, denominator: float) -> float | None:
    return round(numerator / denominator, 3) if denominator else None
