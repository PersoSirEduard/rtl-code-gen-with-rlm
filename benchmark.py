#!/usr/bin/env python3
"""
Benchmark runner for RTL code generation using the RLM Claude agent.

For each problem in the verilog-eval dataset:
  1. Call `claude -p "/rlm ..."` with stream-json output to capture the full trace.
  2. Detect newly generated .v/.sv files.
  3. Compile and run iverilog + vvp simulation against the test harness.
  4. Record pass@1, mismatches, total samples, and session duration in results.csv.
  5. Copy generated files and traces to generated/<problem_name>/.
  6. Remove generated .v/.sv files from the workdir.

Usage:
  python benchmark.py --mode haiku
  python benchmark.py --mode codev --server-url http://localhost:8000
  python benchmark.py --mode haiku --limit 10 --start-from 3
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Default paths (all relative to this script's directory = rtl-code-gen-with-rlm)
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).parent.resolve()
DATASET_DIR = SCRIPT_DIR.parent / "verilog-eval" / "dataset_spec-to-rtl"
GENERATED_DIR = SCRIPT_DIR / "generated"
RESULTS_CSV = SCRIPT_DIR / "results.csv"
RLM_STATE = SCRIPT_DIR / ".claude" / "rlm_state" / "state.pkl"

CSV_FIELDS = [
    "problem",
    "mode",
    "passed",
    "mismatches",
    "total_samples",
    "duration_s",
    "claude_exit_ok",
    "error",
]


# ---------------------------------------------------------------------------
# Dataset helpers
# ---------------------------------------------------------------------------

def find_problems(dataset_dir: Path) -> list[dict]:
    """Return problems sorted by name, each with prompt/ref/test paths."""
    problems = []
    for prompt_file in sorted(dataset_dir.glob("*_prompt.txt")):
        stem = prompt_file.stem[: -len("_prompt")]  # e.g. "Prob003_step_one"
        ref_sv = dataset_dir / f"{stem}_ref.sv"
        test_sv = dataset_dir / f"{stem}_test.sv"
        if ref_sv.exists() and test_sv.exists():
            problems.append(
                {"name": stem, "prompt": prompt_file, "ref": ref_sv, "test": test_sv}
            )
    return problems


# ---------------------------------------------------------------------------
# Claude invocation
# ---------------------------------------------------------------------------

def call_claude_rlm(
    prompt_path: Path,
    mode: str,
    server_url: str | None,
    workdir: Path,
    timeout: int,
) -> tuple[bool, float, str, str]:
    """
    Run `claude -p '/rlm ...'` with stream-json output and return
    (exit_ok, duration_s, raw_jsonl, stderr).

    stream-json emits one JSON object per line for every event in the session:
    assistant messages, tool calls, tool results, and the final result.
    This gives us the complete execution trace.
    """
    spec_arg = f"spec={prompt_path}"
    mode_arg = f"mode={mode}"
    extra = f" server_url={server_url}" if server_url else ""
    message = f"/rlm {spec_arg} {mode_arg}{extra}"

    cmd = [
        "claude",
        "--dangerously-skip-permissions",
        "--output-format", "stream-json",
        "--verbose",
        "-p", message,
    ]

    start = time.monotonic()
    try:
        result = subprocess.run(
            cmd,
            cwd=str(workdir),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        duration = time.monotonic() - start
        return result.returncode == 0, duration, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        duration = time.monotonic() - start
        return False, duration, "", f"TIMEOUT after {timeout}s"
    except FileNotFoundError:
        duration = time.monotonic() - start
        return False, duration, "", "ERROR: `claude` binary not found in PATH"


# ---------------------------------------------------------------------------
# Trace formatting
# ---------------------------------------------------------------------------

def format_trace(raw_jsonl: str) -> str:
    """
    Convert stream-json NDJSON into a human-readable trace.

    Each line of raw_jsonl is one JSON event. We render:
      - assistant text chunks
      - tool calls (name + truncated input)
      - tool results (truncated content)
      - the final result summary
    """
    lines: list[str] = []

    for raw_line in raw_jsonl.splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            lines.append(f"[RAW] {raw_line}")
            continue

        etype = event.get("type", "")

        if etype == "assistant":
            msg = event.get("message", {})
            for block in msg.get("content", []):
                btype = block.get("type", "")
                if btype == "text":
                    text = block.get("text", "").strip()
                    if text:
                        lines.append(f"[ASSISTANT]\n{text}\n")
                elif btype == "thinking":
                    thinking = block.get("thinking", "").strip()
                    if thinking:
                        lines.append(f"[THINKING]\n{thinking}\n")
                elif btype == "tool_use":
                    tool_name = block.get("name", "?")
                    tool_input = block.get("input", {})
                    input_str = json.dumps(tool_input, ensure_ascii=False)
                    if len(input_str) > 600:
                        input_str = input_str[:600] + "…"
                    lines.append(f"[TOOL CALL] {tool_name}\n{input_str}\n")

        elif etype == "user":
            msg = event.get("message", {})
            for block in msg.get("content", []):
                btype = block.get("type", "")
                if btype == "tool_result":
                    tool_id = block.get("tool_use_id", "?")
                    content = block.get("content", "")
                    if isinstance(content, list):
                        content = " ".join(
                            c.get("text", "") for c in content if isinstance(c, dict)
                        )
                    content = str(content).strip()
                    if len(content) > 800:
                        content = content[:800] + "…"
                    lines.append(f"[TOOL RESULT] (id={tool_id})\n{content}\n")

        elif etype == "result":
            subtype = event.get("subtype", "")
            result_text = event.get("result", "")
            cost = event.get("cost_usd")
            tokens = event.get("usage", {})
            summary = f"[SESSION RESULT] subtype={subtype}"
            if cost is not None:
                summary += f"  cost=${cost:.4f}"
            if tokens:
                summary += f"  tokens={tokens}"
            lines.append(summary)
            if result_text:
                lines.append(f"  final_result: {result_text.strip()[:400]}")

        elif etype == "system":
            subtype = event.get("subtype", "")
            if subtype == "init":
                session_id = event.get("session_id", "?")
                lines.append(f"[SESSION INIT] session_id={session_id}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# File detection
# ---------------------------------------------------------------------------

_EXCLUDED_DIRS = {"generated", ".claude", ".git"}


def snapshot_v_files(directory: Path) -> set[Path]:
    """Recursively find all .v/.sv files, skipping dirs we manage ourselves."""
    found: set[Path] = set()
    for ext in ("*.v", "*.sv"):
        for p in directory.rglob(ext):
            if not any(part in _EXCLUDED_DIRS for part in p.relative_to(directory).parts):
                found.add(p)
    return found


def find_new_v_files(workdir: Path, before: set[Path]) -> list[Path]:
    """Return .v/.sv files added to workdir since the before snapshot."""
    after = snapshot_v_files(workdir)
    return sorted(after - before)


def pick_top_module_file(files: list[Path]) -> Path | None:
    """Return the file that defines `TopModule`, or the first file if none match."""
    if not files:
        return None
    for f in files:
        try:
            if re.search(r"\bmodule\s+TopModule\b", f.read_text(encoding="utf-8", errors="replace")):
                return f
        except OSError:
            pass
    return files[0]


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------

def run_simulation(
    test_sv: Path,
    ref_sv: Path,
    generated_v: Path,
    workdir: Path,
) -> dict:
    """
    Compile with iverilog -g2012 and run with vvp.
    Returns {"passed": bool, "mismatches": int, "total": int, "error": str}.
    """
    sim_bin = workdir / "_bench_sim_out"

    compile_result = subprocess.run(
        [
            "iverilog",
            "-g2012",
            "-o", str(sim_bin),
            str(test_sv),
            str(ref_sv),
            str(generated_v),
        ],
        capture_output=True,
        text=True,
        cwd=str(workdir),
    )

    if compile_result.returncode != 0:
        return {
            "passed": False,
            "mismatches": -1,
            "total": -1,
            "error": f"iverilog: {compile_result.stderr.strip()[:400]}",
        }

    try:
        run_result = subprocess.run(
            ["vvp", str(sim_bin)],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(workdir),
        )
        output = run_result.stdout + run_result.stderr
    except subprocess.TimeoutExpired:
        sim_bin.unlink(missing_ok=True)
        return {"passed": False, "mismatches": -1, "total": -1, "error": "vvp TIMEOUT"}
    finally:
        sim_bin.unlink(missing_ok=True)

    if "TIMEOUT" in output:
        return {"passed": False, "mismatches": -1, "total": -1, "error": "SIM TIMEOUT"}

    m = re.search(r"Mismatches:\s*(\d+)\s+in\s+(\d+)\s+samples", output)
    if not m:
        snippet = output.strip()[:300].replace("\n", " ")
        return {"passed": False, "mismatches": -1, "total": -1, "error": f"parse failed: {snippet}"}

    mismatches = int(m.group(1))
    total = int(m.group(2))
    return {"passed": mismatches == 0, "mismatches": mismatches, "total": total, "error": ""}


# ---------------------------------------------------------------------------
# Main benchmark loop
# ---------------------------------------------------------------------------

def run_benchmark(
    problems: list[dict],
    mode: str,
    server_url: str | None,
    workdir: Path,
    generated_dir: Path,
    results_csv: Path,
    timeout: int,
) -> None:
    generated_dir.mkdir(parents=True, exist_ok=True)

    write_header = not results_csv.exists()
    total = len(problems)
    passed_count = 0

    with results_csv.open("a", newline="", encoding="utf-8") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=CSV_FIELDS)
        if write_header:
            writer.writeheader()

        for i, prob in enumerate(problems, 1):
            name = prob["name"]
            print(f"\n[{i}/{total}] {name}  mode={mode}", flush=True)

            # Reset REPL state so each session starts clean
            RLM_STATE.unlink(missing_ok=True)

            # Snapshot workdir before calling claude
            before = snapshot_v_files(workdir)

            # --- Call claude ---
            print("  Calling claude...", flush=True)
            claude_ok, duration, raw_jsonl, stderr = call_claude_rlm(
                prob["prompt"], mode, server_url, workdir, timeout
            )
            print(f"  Done in {duration:.1f}s  exit_ok={claude_ok}", flush=True)

            # --- Detect generated files ---
            new_files = find_new_v_files(workdir, before)
            print(f"  Generated: {[f.name for f in new_files]}", flush=True)

            sim_result: dict = {"passed": False, "mismatches": -1, "total": -1, "error": ""}

            if not new_files:
                err_snippet = stderr.strip()[:200].replace("\n", " ") if stderr else ""
                sim_result["error"] = f"no .v file generated; claude stderr: {err_snippet}"
            else:
                target = pick_top_module_file(new_files)
                print(f"  Simulating {target.name}...", flush=True)
                sim_result = run_simulation(prob["test"], prob["ref"], target, workdir)

            passed = sim_result["passed"]
            if passed:
                passed_count += 1
                print(f"  PASS  (0/{sim_result['total']} mismatches)", flush=True)
            else:
                print(
                    f"  FAIL  mismatches={sim_result['mismatches']}  "
                    f"error={sim_result['error'][:100]}",
                    flush=True,
                )

            # --- Save generated files, raw NDJSON, and formatted trace ---
            dest = generated_dir / name
            dest.mkdir(parents=True, exist_ok=True)
            for f in new_files:
                shutil.copy2(f, dest / f.name)

            # Raw stream-json for programmatic use
            (dest / "claude_trace.jsonl").write_text(raw_jsonl, encoding="utf-8")
            # Human-readable formatted trace
            (dest / "claude_trace.txt").write_text(
                format_trace(raw_jsonl)
                + (f"\n\n=== stderr ===\n{stderr}" if stderr.strip() else ""),
                encoding="utf-8",
            )

            # --- Write CSV row ---
            writer.writerow(
                {
                    "problem": name,
                    "mode": mode,
                    "passed": int(passed),
                    "mismatches": sim_result["mismatches"],
                    "total_samples": sim_result["total"],
                    "duration_s": f"{duration:.2f}",
                    "claude_exit_ok": int(claude_ok),
                    "error": sim_result["error"][:300],
                }
            )
            csvfile.flush()

            # --- Clean up generated .v files from workdir ---
            for f in new_files:
                f.unlink(missing_ok=True)

    print(f"\n{'='*60}")
    print(f"Results: {passed_count}/{total} passed  (pass@1 = {passed_count / total:.3f})")
    print(f"CSV: {results_csv}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Benchmark RTL generation via the RLM Claude agent.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--mode",
        choices=["haiku", "codev"],
        default="haiku",
        help="Coder mode to pass to /rlm",
    )
    parser.add_argument(
        "--server-url",
        default=None,
        metavar="URL",
        help="vllm server base URL (required for codev mode, e.g. http://localhost:8000)",
    )
    parser.add_argument(
        "--dataset-dir",
        type=Path,
        default=DATASET_DIR,
        help="Path to verilog-eval/dataset_spec-to-rtl",
    )
    parser.add_argument(
        "--generated-dir",
        type=Path,
        default=GENERATED_DIR,
        help="Directory where copies of generated files are stored",
    )
    parser.add_argument(
        "--results-csv",
        type=Path,
        default=RESULTS_CSV,
        help="Output CSV file",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="Per-problem claude session timeout in seconds",
    )
    parser.add_argument(
        "--start-from",
        type=int,
        default=1,
        metavar="N",
        help="Start from problem number N (1-indexed)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Run at most N problems",
    )
    args = parser.parse_args()

    if args.mode == "codev" and not args.server_url:
        parser.error("--server-url is required when --mode codev")

    dataset_dir = args.dataset_dir.resolve()
    if not dataset_dir.exists():
        sys.exit(f"ERROR: dataset dir not found: {dataset_dir}")

    problems = find_problems(dataset_dir)
    if not problems:
        sys.exit(f"ERROR: no problems found in {dataset_dir}")

    # Apply --start-from and --limit
    start_idx = max(0, args.start_from - 1)
    selected = problems[start_idx:]
    if args.limit is not None:
        selected = selected[: args.limit]

    print(f"Found {len(problems)} problems, running {len(selected)} "
          f"(start={args.start_from}, limit={args.limit})")
    print(f"Mode: {args.mode}  workdir: {SCRIPT_DIR}")

    run_benchmark(
        problems=selected,
        mode=args.mode,
        server_url=args.server_url,
        workdir=SCRIPT_DIR,
        generated_dir=args.generated_dir.resolve(),
        results_csv=args.results_csv.resolve(),
        timeout=args.timeout,
    )


if __name__ == "__main__":
    main()
