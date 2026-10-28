"""Eval 2: the product matcher (offline, free, runs in seconds).

    python -m evals.run_matching

Input : evals/golden/matching.jsonl - a read line (as the model returns it)
        and the right answer (status, SKU, quantity in sell units).
Checks: status exact; SKU exact (when expected); quantity exact (when expected).
Metric: accuracy overall and by expected status. A drop here after a change
        to matching.py or the catalog is a regression.
"""

from __future__ import annotations

import tempfile
from collections import Counter
from pathlib import Path

from evals.common import GOLDEN, Run, fresh_deps, ratio, read_jsonl
from order_desk.domain.chitti import ChitReading
from order_desk.services.matching import load_products, match_line


def main() -> dict:
    deps = fresh_deps(Path(tempfile.mkdtemp()) / "matching")
    with deps.db.read() as conn:
        products = load_products(conn)
    run = Run("matching")
    by_status: Counter = Counter()
    ok_by_status: Counter = Counter()
    for case in read_jsonl(GOLDEN / "matching.jsonl"):
        line = ChitReading.from_model_json({"lines": [case["line"]]}).lines[0]
        got = match_line(line, products)
        want = case["expect"]
        problems = []
        if got.status != want["status"]:
            problems.append(f"status {got.status} != {want['status']}")
        if "sku" in want and got.sku != want["sku"]:
            problems.append(f"sku {got.sku} != {want['sku']}")
        if "qty" in want and got.qty != want["qty"]:
            problems.append(f"qty {got.qty} != {want['qty']}")
        by_status[want["status"]] += 1
        ok_by_status[want["status"]] += not problems
        run.add({"id": case["id"], "passed": not problems, "reason": "; ".join(problems) or got.note or "",
                 "input": case["line"]["raw_text"], "got": {"status": got.status, "sku": got.sku, "qty": got.qty}})
    metrics = {"accuracy": ratio(sum(ok_by_status.values()), sum(by_status.values()))}
    metrics |= {f"accuracy_{s}": ratio(ok_by_status[s], n) for s, n in sorted(by_status.items())}
    return run.save(metrics)


if __name__ == "__main__":
    main()
