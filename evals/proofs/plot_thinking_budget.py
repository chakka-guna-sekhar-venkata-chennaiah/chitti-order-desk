"""Draw docs/images/thinking_budget.png: where the vision model's output budget went.

The numbers are the table in FEEDBACK.md section 3, measured by evals/proofs/thinking_budget.py
(same photo, temperature 0). This script only draws them; it calls no model.
Run from the repo root: .venv/bin/python evals/proofs/plot_thinking_budget.py
"""
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402

# (row label, max_tokens, thinking tokens, answer tokens, outcome, empty answer?)
RUNS = [
    ("Short prompt\nmax_tokens 4,000", 4000, 1255, 310, "reply: 735 characters  (finish: stop)", False),
    ("Production prompt\nmax_tokens 4,000", 4000, 4000, 0, "empty reply  (finish: length)", True),
    ("Production prompt\nmax_tokens 16,000", 16000, 5655, 1277, "reply: all 10 lines  (finish: stop)", False),
]

# Reference palette of the dataviz method: light surface, ink, chrome, categorical slots 1 and 2.
SURFACE, INK, INK_2, MUTED = "#fcfcfb", "#0b0b0b", "#52514e", "#898781"
GRID, BASELINE, TRACK = "#e1e0d9", "#c3c2b7", "#f0efec"
THINKING, ANSWER = "#2a78d6", "#eb6834"


def main(out: Path = Path("docs/images/thinking_budget.png")) -> None:
    plt.rcParams["font.family"] = ["Helvetica Neue", "Helvetica", "Arial", "DejaVu Sans"]
    fig, ax = plt.subplots(figsize=(15, 10), dpi=100)  # 1500 x 1000 px, the 3:2 gallery ratio
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)
    ax.set_axisbelow(True)

    for i, (label, budget, thinking, answer, outcome, empty) in enumerate(RUNS):
        y = len(RUNS) - 1 - i
        ax.barh(y, budget, height=0.4, color=TRACK, edgecolor=GRID, linewidth=1)
        ax.barh(y, thinking, height=0.4, color=THINKING, edgecolor=SURFACE, linewidth=2)
        if answer:
            ax.barh(y, answer, left=thinking, height=0.4, color=ANSWER, edgecolor=SURFACE, linewidth=2)
        # Short budgets: put the labels after the budget's end, on a surface patch that hides the gridlines.
        # Long budgets: put them on the unused-budget track, which already hides the gridlines.
        after_budget = budget + 180 < 9000
        x = budget + 180 if after_budget else thinking + answer + 180
        box = dict(facecolor=SURFACE, edgecolor="none", pad=1.5) if after_budget else None
        ax.text(x, y + 0.04, f"{thinking:,} thinking + {answer:,} answer", va="bottom", fontsize=15, color=INK_2,
                bbox=box)
        ax.text(x, y - 0.04, outcome, va="top", fontsize=16, color=INK, fontweight="bold" if empty else "normal",
                bbox=box)

    ax.set_yticks(range(len(RUNS)), [r[0] for r in reversed(RUNS)], fontsize=15, color=INK)
    ax.set_xlim(0, 16800)
    ax.set_xticks(range(0, 16001, 4000), [f"{v:,}" for v in range(0, 16001, 4000)], fontsize=13, color=MUTED)
    ax.set_xlabel("Output tokens (thinking + answer)", fontsize=14, color=INK_2, labelpad=12)
    ax.tick_params(axis="y", length=0, pad=14)
    ax.tick_params(axis="x", length=0, pad=8)
    ax.grid(axis="x", color=GRID, linewidth=0.8)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(BASELINE)

    fig.text(0.06, 0.93, "Where the vision model's output budget went", fontsize=26, fontweight="bold", color=INK)
    fig.text(0.06, 0.885, "NVIDIA Nemotron 3.5 Super VL on Nebius Token Factory · the same chitti photo (hard_01.png),"
             " temperature 0", fontsize=15, color=INK_2)
    fig.text(0.06, 0.85, "Thinking tokens count inside max_tokens. At 4,000 the model thought until the budget ran out.",
             fontsize=15, color=INK_2)
    fig.legend(handles=[Patch(color=THINKING, label="Thinking (reasoning tokens)"),
                        Patch(color=ANSWER, label="Answer tokens"),
                        Patch(facecolor=TRACK, edgecolor=GRID, label="Unused budget (max_tokens)")],
               loc="upper left", bbox_to_anchor=(0.055, 0.82), ncol=3, frameon=False, fontsize=14,
               labelcolor=INK_2, handlelength=1.4, columnspacing=2.2)
    fig.text(0.06, 0.025, "Source: evals/proofs/thinking_budget.py · the table in FEEDBACK.md section 3",
             fontsize=12, color=MUTED)
    fig.subplots_adjust(left=0.2, right=0.96, top=0.74, bottom=0.12)

    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, facecolor=SURFACE)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
