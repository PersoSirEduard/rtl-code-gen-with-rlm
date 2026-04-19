#!/usr/bin/env python3
"""
Grouped bar chart comparing error category distributions between
the zero-shot baseline and the RLM benchmark.

Usage:
  python plot_categories.py
  python plot_categories.py --baseline results_baseline.csv --rlm results_rlm.csv
  python plot_categories.py --out categories.png
"""

from __future__ import annotations

import argparse
import csv
from collections import Counter
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

# Re-use the categorisation logic from error_analysis.py
from error_analysis import CATEGORIES, categorize

SCRIPT_DIR = Path(__file__).parent.resolve()

BASELINE_CSV = SCRIPT_DIR / "results_baseline.csv"
RLM_CSV      = SCRIPT_DIR / "results_rlm.csv"
DEFAULT_OUT  = SCRIPT_DIR / "categories_plot.png"

BASELINE_COLOR = "#4C72B0"
RLM_COLOR      = "#DD8452"

SHORT_LABELS = [
    "Success",
    "Faulty Logic",
    "Compiler\nError",
    "Code Gen\nError",
    "Sim\nTimeout",
]


def load_counts(csv_path: Path) -> tuple[Counter, int]:
    counts: Counter = Counter()
    total = 0
    with csv_path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if "unknown option" in row.get("error", "").lower():
                continue
            counts[categorize(row)] += 1
            total += 1
    return counts, total


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Bar chart: error category breakdown for baseline vs RLM.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--baseline", type=Path, default=BASELINE_CSV)
    parser.add_argument("--rlm",      type=Path, default=RLM_CSV)
    parser.add_argument("--out",      type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    b_counts, b_total = load_counts(args.baseline)
    r_counts, r_total = load_counts(args.rlm)

    b_pcts = [b_counts[c] / b_total * 100 for c in CATEGORIES]
    r_pcts = [r_counts[c] / r_total * 100 for c in CATEGORIES]

    x      = np.arange(len(CATEGORIES))
    width  = 0.35

    fig, ax = plt.subplots(figsize=(11, 6))

    bars_b = ax.bar(x - width / 2, b_pcts, width,
                    label="Zero-shot baseline",
                    color=BASELINE_COLOR, alpha=0.88, edgecolor="white", linewidth=0.6)
    bars_r = ax.bar(x + width / 2, r_pcts, width,
                    label="RLM",
                    color=RLM_COLOR, alpha=0.88, edgecolor="white", linewidth=0.6)

    # Value labels on top of each bar
    for bar in (*bars_b, *bars_r):
        h = bar.get_height()
        if h > 0:
            ax.text(
                bar.get_x() + bar.get_width() / 2,
                h + 0.6,
                f"{h:.1f}%",
                ha="center", va="bottom", fontsize=9.5, color="#333333",
            )

    ax.set_xticks(x)
    ax.set_xticklabels(SHORT_LABELS, fontsize=11)
    ax.set_ylabel("Frequency (%)", fontsize=12)
    ax.set_title("Error Category Distribution: Zero-Shot Baseline vs RLM", fontsize=13)
    ax.set_ylim(0, max(max(b_pcts), max(r_pcts)) + 12)
    ax.legend(fontsize=11, framealpha=0.9)
    ax.yaxis.grid(True, alpha=0.35, linestyle="--")
    ax.set_axisbelow(True)

    fig.tight_layout()
    fig.savefig(args.out, dpi=150)
    print(f"Saved → {args.out}")


if __name__ == "__main__":
    main()
