#!/usr/bin/env python3
"""
Categorise benchmark results into error classes and report frequencies.

Categories
----------
Success                 passed == 1
Simulation Faulty Logic passed == 0, no error message (compiled, wrong output)
Compiler Error          passed == 0, error contains iverilog diagnostics
Code Generation Error   passed == 0, extraction failure or session timeout
Simulation Timeout      passed == 0, error contains SIM TIMEOUT

Usage:
  python error_analysis.py
  python error_analysis.py --baseline results_baseline.csv --rlm results_rlm.csv
"""

from __future__ import annotations

import argparse
import csv
from collections import Counter
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()

BASELINE_CSV = SCRIPT_DIR / "results_baseline.csv"
RLM_CSV      = SCRIPT_DIR / "results_rlm.csv"

CATEGORIES = [
    "Success",
    "Simulation Faulty Logic",
    "Compiler Error",
    "Code Generation Error",
    "Simulation Timeout",
]

PRIMARY_CAUSE = {
    "Success":                  "Code is syntactically and functionally correct.",
    "Simulation Faulty Logic":  "Code compiles but fails functional verification (mismatches).",
    "Compiler Error":           "Syntax errors, interface mismatches, or naming issues.",
    "Code Generation Error":    "Extraction failure or generator/session timeout.",
    "Simulation Timeout":       "Infinite loops or excessive logic complexity.",
}


def categorize(row: dict) -> str:
    if row.get("passed", "").strip() == "1":
        return "Success"

    error = row.get("error", "").strip()

    if "SIM TIMEOUT" in error:
        return "Simulation Timeout"

    if "iverilog" in error:
        return "Compiler Error"

    # Any other non-empty message = something went wrong before/during generation.
    if error:
        return "Code Generation Error"

    # No message at all = code compiled and ran but produced wrong output.
    return "Simulation Faulty Logic"


def load_counts(csv_path: Path) -> tuple[Counter, int]:
    """Return (category_counter, total_valid_rows).

    Skips rows that failed due to infrastructure issues (wrong CLI flag, etc.)
    rather than model behaviour.  These are identified by 'unknown option' in
    the error string — they do not represent a model result at all.
    """
    counts: Counter = Counter()
    total = 0
    with csv_path.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if "unknown option" in row.get("error", "").lower():
                continue
            counts[categorize(row)] += 1
            total += 1
    return counts, total


def print_table(baseline_counts: Counter, baseline_total: int,
                rlm_counts: Counter, rlm_total: int) -> None:
    col_w = [28, 12, 12, 12, 12]
    sep = "+" + "+".join("-" * w for w in col_w) + "+"

    def row(*cells: str) -> str:
        padded = [str(c).ljust(col_w[i] - 1) for i, c in enumerate(cells)]
        return "| " + " | ".join(padded) + " |"

    print(sep)
    print(row("Error Category", "Baseline #", "Baseline %", "RLM #", "RLM %"))
    print(sep)
    for cat in CATEGORIES:
        b_n = baseline_counts[cat]
        r_n = rlm_counts[cat]
        b_pct = f"{b_n / baseline_total * 100:.1f}%" if baseline_total else "—"
        r_pct = f"{r_n / rlm_total * 100:.1f}%"     if rlm_total     else "—"
        print(row(cat, str(b_n), b_pct, str(r_n), r_pct))
    print(sep)
    print(row("TOTAL", str(baseline_total), "100%", str(rlm_total), "100%"))
    print(sep)

    print()
    print("Primary causes:")
    for cat in CATEGORIES:
        print(f"  {cat:<28}  {PRIMARY_CAUSE[cat]}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Break down benchmark results into error categories.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--baseline", type=Path, default=BASELINE_CSV)
    parser.add_argument("--rlm",      type=Path, default=RLM_CSV)
    args = parser.parse_args()

    for p in (args.baseline, args.rlm):
        if not p.exists():
            raise SystemExit(f"ERROR: file not found: {p}")

    baseline_counts, baseline_total = load_counts(args.baseline)
    rlm_counts,      rlm_total      = load_counts(args.rlm)

    print(f"\nBaseline : {args.baseline.name}  ({baseline_total} valid runs)")
    print(f"RLM      : {args.rlm.name}  ({rlm_total} valid runs)\n")
    print_table(baseline_counts, baseline_total, rlm_counts, rlm_total)


if __name__ == "__main__":
    main()
