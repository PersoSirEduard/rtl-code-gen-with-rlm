#!/usr/bin/env python3
"""
Plot duration distributions for baseline (zero-shot) vs RLM benchmarks.

Reads results_baseline.csv and results_rlm.csv, filters to successful Claude
sessions (claude_exit_ok == 1), then overlays a histogram + fitted normal
bell curve for each, annotated with mean ± std-dev.

Usage:
  python plot_duration.py
  python plot_duration.py --baseline results_baseline.csv --rlm results_rlm.csv
  python plot_duration.py --out duration_plot.png
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from scipy.stats import norm

SCRIPT_DIR = Path(__file__).parent.resolve()

BASELINE_CSV = SCRIPT_DIR / "results_baseline.csv"
RLM_CSV      = SCRIPT_DIR / "results_rlm.csv"
DEFAULT_OUT  = SCRIPT_DIR / "duration_plot.png"

BASELINE_COLOR = "#4C72B0"   # blue
RLM_COLOR      = "#DD8452"   # orange


def load_durations(csv_path: Path) -> list[float]:
    """Return duration_s values for rows where claude_exit_ok == 1."""
    durations: list[float] = []
    with csv_path.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                if row.get("claude_exit_ok", "").strip() != "1":
                    continue
                durations.append(float(row["duration_s"]))
            except (ValueError, KeyError):
                continue
    return durations


def plot_bell(ax: plt.Axes, data: list[float], color: str, label: str) -> None:
    """Plot a histogram + fitted normal curve for data on ax."""
    arr = np.array(data)
    mu, sigma = arr.mean(), arr.std()

    # Histogram (normalised to density so it aligns with the PDF)
    ax.hist(
        arr,
        bins=30,
        density=True,
        alpha=0.25,
        color=color,
        edgecolor="none",
    )

    # Fitted normal PDF
    x = np.linspace(arr.min() - sigma, arr.max() + sigma, 500)
    ax.plot(
        x,
        norm.pdf(x, mu, sigma),
        color=color,
        linewidth=2.5,
        label=f"{label}\n$\\mu={mu:.1f}$s,  $\\sigma={sigma:.1f}$s",
    )

    # Mean line
    ax.axvline(mu, color=color, linewidth=1.2, linestyle="--", alpha=0.8)

    # ±1σ shaded band
    ax.axvspan(mu - sigma, mu + sigma, alpha=0.07, color=color)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Plot duration bell curves for baseline vs RLM benchmarks.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--baseline", type=Path, default=BASELINE_CSV,
                        help="Path to results_baseline.csv")
    parser.add_argument("--rlm",      type=Path, default=RLM_CSV,
                        help="Path to results_rlm.csv")
    parser.add_argument("--out",      type=Path, default=DEFAULT_OUT,
                        help="Output image path (.png / .pdf / .svg)")
    args = parser.parse_args()

    baseline_data = load_durations(args.baseline)
    rlm_data      = load_durations(args.rlm)

    if not baseline_data:
        print(f"WARNING: no valid rows found in {args.baseline}")
    if not rlm_data:
        print(f"WARNING: no valid rows found in {args.rlm}")

    fig, ax = plt.subplots(figsize=(10, 5))

    if baseline_data:
        plot_bell(ax, baseline_data, BASELINE_COLOR, "Zero-shot baseline (Haiku)")
    if rlm_data:
        plot_bell(ax, rlm_data, RLM_COLOR, "RLM (Haiku)")

    ax.set_xlabel("Session duration (s)", fontsize=13)
    ax.set_ylabel("Probability density", fontsize=13)
    ax.set_title("Session Duration Distribution: Zero-Shot Baseline vs RLM", fontsize=14)
    ax.legend(fontsize=11, framealpha=0.9)
    ax.set_xlim(left=0)
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()

    fig.savefig(args.out, dpi=150)
    print(f"Saved → {args.out}")


if __name__ == "__main__":
    main()
