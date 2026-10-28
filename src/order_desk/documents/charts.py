"""Draw a simple chart (PNG) from the rows of a read-only query.

The owner asks "show sales for this week as a chart". The agent writes the
SQL; this code draws the first column as labels and the second column as
values. The image goes to the owner through the outbox.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")                     # no screen; draw to a file
import matplotlib.pyplot as plt  # noqa: E402

from order_desk import clock  # noqa: E402
from order_desk.domain.errors import InvalidInput  # noqa: E402


def draw_chart(rows: list[dict], kind: str, title: str, out_dir: Path) -> Path:
    if not rows:
        raise InvalidInput("The query returned no rows, so there is nothing to draw.")
    columns = list(rows[0].keys())
    if len(columns) < 2:
        raise InvalidInput("A chart needs two columns: a label column and a number column.")
    labels = [str(r[columns[0]]) for r in rows]
    try:
        values = [float(r[columns[1]] or 0) for r in rows]
    except (TypeError, ValueError):
        raise InvalidInput(f"Column '{columns[1]}' must contain numbers.")
    if kind not in ("bar", "line"):
        raise InvalidInput("Chart kind must be 'bar' or 'line'.")

    fig, ax = plt.subplots(figsize=(8, 4.5), dpi=150)
    if kind == "bar":
        ax.bar(labels, values, color="#2b5ca6")
    else:
        ax.plot(labels, values, marker="o", color="#2b5ca6")
    ax.set_title(title)
    ax.set_xlabel(columns[0])
    ax.set_ylabel(columns[1])
    ax.grid(axis="y", alpha=0.3)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    if len(labels) > 6:
        plt.setp(ax.get_xticklabels(), rotation=45, ha="right")
    fig.tight_layout()

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"chart_{clock.now().strftime('%Y%m%d_%H%M%S_%f')}.png"
    fig.savefig(path)
    plt.close(fig)
    return path
