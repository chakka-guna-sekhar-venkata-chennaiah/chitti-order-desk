"""Eval 1: the chitti reader (vision model on Token Factory). Needs NEBIUS_API_KEY.

    python -m evals.run_chit_reader                    # all *.checked.json labels
    python -m evals.run_chit_reader --include-examples # also *.example.json
    python -m evals.run_chit_reader --model <id>       # try another vision model

For each labelled chitti image the model reads the image, and we compare the
reading with the human label FIELD BY FIELD. Then we run the product matcher
on both and compare the final order lines: that is the number the business
cares about.

Metrics (see docs/07-evals.md for why each one matters)
    line_recall          labelled lines the model found
    line_precision       model lines that exist on the paper
    text_similarity      1 - edit distance / length, for raw_text (0..1)
    qty_accuracy         quantity exactly right, on found lines
    item_accuracy        category + spec + brand all right, on found lines
    order_line_accuracy  after matching: same SKU and same quantity as the label
    clarify_recall       lines that NEED a question and got one   (safety)
    unsafe_accept_rate   lines that need a question, got none, AND are wrong (must be 0)
    decision_accuracy    confirm_all vs clarify, for the whole chitti
"""

from __future__ import annotations

import argparse
import json
import tempfile
import time
from pathlib import Path

from evals.common import GOLDEN, Run, fresh_deps, ratio
from order_desk.domain.chitti import ChitLine, ChitReading
from order_desk.services.matching import load_products, match_line
from order_desk.vision.reader import TokenFactoryReader


def levenshtein(a: str, b: str) -> int:
    if len(a) < len(b):
        a, b = b, a
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]


def norm(text: str | None) -> str:
    return " ".join((text or "").lower().replace("(", " ").replace(")", " ").split())


def similarity(a: str | None, b: str | None) -> float:
    a, b = norm(a), norm(b)
    if not a and not b:
        return 1.0
    return 1 - levenshtein(a, b) / max(len(a), len(b))


def align(labels: list[ChitLine], preds: list[ChitLine]) -> list[tuple[ChitLine, ChitLine | None]]:
    """Pair each labelled line with the most similar unused predicted line (>= 0.5)."""
    free = list(preds)
    pairs = []
    for gt in labels:
        best = max(free, key=lambda p: similarity(gt.raw_text, p.raw_text), default=None)
        if best is not None and similarity(gt.raw_text, best.raw_text) >= 0.5:
            free.remove(best)
            pairs.append((gt, best))
        else:
            pairs.append((gt, None))
    return pairs


def same_item(a: ChitLine, b: ChitLine) -> bool:
    spec_ok = all(str(a.spec.get(k)) == str(b.spec.get(k)) for k in a.spec)
    return a.category == b.category and spec_ok and a.brand == b.brand


def main() -> dict:
    parser = argparse.ArgumentParser()
    parser.add_argument("--include-examples", action="store_true")
    parser.add_argument("--model", default=None)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    from evals.common import REPO
    from order_desk.config import load_settings
    s = load_settings(REPO / ".env")
    reader = TokenFactoryReader(s.nebius_api_key, s.nebius_base_url, args.model or s.vision_model,
                                max_tokens=s.vision_max_tokens)
    return evaluate(reader, args.include_examples, args.limit)


def evaluate(reader, include_examples: bool = False, limit: int | None = None) -> dict:
    deps = fresh_deps(Path(tempfile.mkdtemp()) / "chit_reader")
    with deps.db.read() as conn:
        products = load_products(conn)

    patterns = ["*.checked.json"] + (["*.example.json"] if include_examples else [])
    label_files = sorted({p for pat in patterns for p in (GOLDEN / "labels").glob(pat)})[:limit]
    if not label_files:
        raise SystemExit("No labels found. Add evals/golden/labels/<chit_id>.checked.json (see evals/golden/README.md).")

    run = Run("chit_reader", {"model": getattr(reader, "model", "?"), "labels": len(label_files)})
    t = {k: 0 for k in ("gt", "pred", "found", "qty", "item", "order_ok", "needs_q", "asked", "unsafe",
                        "decision_ok")}
    text_sims: list[float] = []
    seconds: list[float] = []

    for label_file in label_files:
        label = json.loads(label_file.read_text(encoding="utf-8"))
        chit_id = label["chit_id"]
        image = next((GOLDEN / "chits").glob(f"{chit_id}.*"), None)
        if image is None:
            run.add({"id": chit_id, "passed": False, "reason": "image file missing"})
            continue
        gt = ChitReading.from_model_json(label)
        started = time.time()
        try:
            pred = reader.read_chit_image(image.read_bytes(), "image/png")
        except Exception as exc:
            run.add({"id": chit_id, "passed": False, "reason": f"model error: {exc}"})
            continue
        seconds.append(time.time() - started)

        pairs = align(gt.lines, pred.lines)
        found = [(g, p) for g, p in pairs if p is not None]
        t["gt"] += len(gt.lines)
        t["pred"] += len(pred.lines)
        t["found"] += len(found)
        line_notes = []
        for g, p in pairs:
            if p is None:
                line_notes.append(f"missed line {g.line_index}: {g.raw_text!r}")
                if g.needs_question:
                    t["needs_q"] += 1
                continue
            text_sims.append(similarity(g.raw_text, p.raw_text))
            t["qty"] += g.quantity == p.quantity
            t["item"] += same_item(g, p)
            mg, mp = match_line(g, products), match_line(p, products)
            correct = mg.sku == mp.sku and mg.qty == mp.qty
            t["order_ok"] += correct
            if g.needs_question:
                t["needs_q"] += 1
                if p.needs_question:
                    t["asked"] += 1
                elif not correct:
                    t["unsafe"] += 1
                    line_notes.append(f"UNSAFE line {g.line_index}: needed a question, model was sure and wrong")
            if not correct:
                line_notes.append(f"line {g.line_index}: label {mg.sku} x{mg.qty} vs model {mp.sku} x{mp.qty}")
        expected = label.get("expected_action", "confirm_all")
        predicted = "clarify" if any(l.needs_question for l in pred.lines) else "confirm_all"
        t["decision_ok"] += expected == predicted
        passed = (len(found) == len(gt.lines) and len(pred.lines) == len(gt.lines)
                  and all(match_line(g, products).sku == match_line(p, products).sku for g, p in found)
                  and expected == predicted)
        run.add({"id": chit_id, "passed": passed,
                 "reason": f"{len(found)}/{len(gt.lines)} lines; decision {predicted} (label {expected}); "
                           + "; ".join(line_notes[:4]),
                 "seconds": round(seconds[-1], 1), "prediction": pred.to_dict()})

    n = len(text_sims)
    metrics = {
        "line_recall": ratio(t["found"], t["gt"]),
        "line_precision": ratio(t["found"], t["pred"]),
        "text_similarity": round(sum(text_sims) / n, 3) if n else None,
        "qty_accuracy": ratio(t["qty"], t["found"]),
        "item_accuracy": ratio(t["item"], t["found"]),
        "order_line_accuracy": ratio(t["order_ok"], t["gt"]),
        "clarify_recall": ratio(t["asked"], t["needs_q"]),
        "unsafe_accept_rate": ratio(t["unsafe"], t["needs_q"]) if t["needs_q"] else 0.0,
        "decision_accuracy": ratio(t["decision_ok"], len(label_files)),
        "chitti_pass_rate": ratio(sum(c["passed"] for c in run.cases), len(run.cases)),
        "avg_seconds": round(sum(seconds) / len(seconds), 1) if seconds else None,
    }
    return run.save(metrics)


if __name__ == "__main__":
    main()
