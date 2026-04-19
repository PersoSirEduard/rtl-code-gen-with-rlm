#!/usr/bin/env python3
"""
Zero-shot benchmark: single Claude Haiku session per problem.

For each problem in the verilog-eval dataset:
  1. Build a prompt from system_prompt.txt + problem spec.
  2. Call `claude -p "..."` with stream-json output (workdir = /tmp so that
     no project CLAUDE.md is picked up), asking Claude to:
       a. Plan/decompose the hardware spec.
       b. Generate the complete Verilog in <answer>...</answer>, using
          // FILE: name.v  headers to name each file in a multi-file design.
  3. Extract one or more named Verilog files from the response.
  4. Compile and run iverilog + vvp simulation against the test harness.
  5. Record pass@1, mismatches, total samples, and session duration in results_baseline.csv.
  6. Copy generated files and traces to generated_baseline/<problem_name>/.

Prerequisites:
  - system_prompt.txt must exist next to this script.
  - iverilog and vvp must be on PATH.

Usage:
  python benchmark_baseline.py
  python benchmark_baseline.py --model claude-haiku-4-5-20251001 --limit 10
  python benchmark_baseline.py --start-from 3 --limit 20
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Default paths (all relative to this script's directory = rtl-code-gen-with-rlm)
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).parent.resolve()
DATASET_DIR = SCRIPT_DIR.parent / "verilog-eval" / "dataset_spec-to-rtl"
GENERATED_DIR = SCRIPT_DIR / "generated_baseline"
RESULTS_CSV = SCRIPT_DIR / "results_baseline.csv"
SYSTEM_PROMPT_FILE = SCRIPT_DIR / "system_prompt.txt"

# Run claude from /tmp so no project CLAUDE.md is picked up.
CLAUDE_WORKDIR = Path("/tmp")

DEFAULT_MODEL = "claude-haiku-4-5-20251001"

CSV_FIELDS = [
    "problem",
    "model",
    "passed",
    "mismatches",
    "total_samples",
    "duration_s",
    "claude_exit_ok",
    "files_extracted",
    "peak_input_tokens",
    "total_input_tokens",
    "error",
]

# Prompt appended to the system context + spec to instruct the model.
TASK_INSTRUCTION = """\
---

## Your Task

Step 1 — Plan: Produce a concise implementation plan covering:
  1. Sub-module decomposition: name, paradigm (combinatorial/sequential/behavioral/structural), one-sentence description.
  2. Port maps: for each module list all ports with name, direction, width, clock domain, reset polarity.
  3. Architectural patterns: clock domains, reset strategy, shared signals.

Step 2 — Generate: Implement the full hardware specification as synthesisable Verilog. \
Place every generated file inside the `<answer>` block below. \
Each file must be preceded by a `// FILE: <filename>.v` header on its own line, \
immediately followed by a ```verilog fenced block containing that file's source.

Single-file example:

<answer>
// FILE: TopModule.v
```verilog
module TopModule ( ... );
  ...
endmodule
```
</answer>

Multi-file example (sub-module in a separate file):

<answer>
// FILE: Adder.v
```verilog
module Adder ( ... );
  ...
endmodule
```
// FILE: TopModule.v
```verilog
module TopModule ( ... );
  Adder u0 ( ... );
endmodule
```
</answer>

Rules:
- Every file inside `<answer>` must start with `// FILE: <name>.v`.
- Do NOT include any prose or comments outside the fenced blocks inside `<answer>`.
- The top-level module must be named `TopModule`.
- Every module must compile cleanly under `iverilog -g2012 -t null`.
- Match every port name, direction, and width exactly as specified.
"""


# ---------------------------------------------------------------------------
# Dataset helpers (identical to benchmark.py)
# ---------------------------------------------------------------------------

def find_problems(dataset_dir: Path) -> list[dict]:
    """Return problems sorted by name, each with prompt/ref/test paths."""
    problems = []
    for prompt_file in sorted(dataset_dir.glob("*_prompt.txt")):
        stem = prompt_file.stem[: -len("_prompt")]
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

def _progress(elapsed: float, msg: str) -> None:
    print(f"  [{elapsed:6.1f}s] {msg}", flush=True)


def call_claude_zeroshot(
    full_prompt: str,
    timeout: int,
    model: str,
) -> tuple[bool, float, str, str, str, int, int]:
    """
    Run `claude -p <prompt>` with stream-json output.
    Returns (exit_ok, duration_s, raw_jsonl, stderr, assistant_text,
             peak_input_tokens, total_input_tokens).

    peak_input_tokens  — largest input_tokens seen across all assistant turns.
    total_input_tokens — input_tokens reported in the final result event.

    Runs from CLAUDE_WORKDIR (/tmp) so that no project CLAUDE.md, skills, or
    RLM tooling are loaded into the session context.
    """
    cmd = [
        "claude",
        "--dangerously-skip-permissions",
        "--output-format", "stream-json",
        "--verbose",
        "--model", model,
        "-p", full_prompt,
    ]

    start = time.monotonic()
    jsonl_lines: list[str] = []
    stderr_lines: list[str] = []
    assistant_text_parts: list[str] = []
    peak_input_tokens = 0
    total_input_tokens = 0

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(CLAUDE_WORKDIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError:
        duration = time.monotonic() - start
        return False, duration, "", "ERROR: `claude` binary not found in PATH", "", 0, 0

    def _drain_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            stderr_lines.append(line)

    stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
    stderr_thread.start()

    try:
        assert proc.stdout is not None
        for raw_line in proc.stdout:
            if time.monotonic() - start > timeout:
                proc.kill()
                duration = time.monotonic() - start
                stderr_thread.join(timeout=2)
                return (
                    False, duration, "\n".join(jsonl_lines),
                    f"TIMEOUT after {timeout}s", "".join(assistant_text_parts),
                    peak_input_tokens, total_input_tokens,
                )

            raw_line = raw_line.rstrip("\n")
            if not raw_line:
                continue
            jsonl_lines.append(raw_line)

            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError:
                continue

            elapsed = time.monotonic() - start
            etype = event.get("type", "")

            if etype == "system" and event.get("subtype") == "init":
                _progress(elapsed, "session started")

            elif etype == "assistant":
                usage = event.get("message", {}).get("usage", {})
                it = usage.get("input_tokens", 0)
                if it > peak_input_tokens:
                    peak_input_tokens = it
                for block in event.get("message", {}).get("content", []):
                    btype = block.get("type", "")
                    if btype == "text":
                        text = block.get("text", "")
                        assistant_text_parts.append(text)
                        snippet = text.strip().replace("\n", " ")[:80]
                        if snippet:
                            _progress(elapsed, f"assistant: {snippet}")
                    elif btype == "thinking":
                        _progress(elapsed, "  <thinking...>")

            elif etype == "result":
                subtype = event.get("subtype", "")
                cost = event.get("cost_usd")
                cost_str = f"  ${cost:.4f}" if cost is not None else ""
                total_input_tokens = event.get("usage", {}).get("input_tokens", 0)
                _progress(elapsed, f"session done  subtype={subtype}{cost_str}"
                          f"  in={total_input_tokens}")

    except KeyboardInterrupt:
        proc.kill()
        raise

    proc.wait()
    stderr_thread.join(timeout=5)
    duration = time.monotonic() - start
    assistant_text = "".join(assistant_text_parts)
    return (
        proc.returncode == 0, duration, "\n".join(jsonl_lines),
        "".join(stderr_lines), assistant_text,
        peak_input_tokens, total_input_tokens,
    )


# ---------------------------------------------------------------------------
# Verilog extraction
# ---------------------------------------------------------------------------

# Matches:  // FILE: SomeName.v  (optional surrounding whitespace)
_FILE_HEADER_RE = re.compile(r"//\s*FILE:\s*(\S+\.(?:v|sv))", re.IGNORECASE)
_VERILOG_FENCE_RE = re.compile(r"```verilog\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def extract_verilog_files(assistant_text: str) -> dict[str, str]:
    """
    Extract named Verilog files from the assistant response.

    Returns a dict mapping filename -> source.

    Primary strategy — inside <answer>...</answer>:
      Scans for  // FILE: name.v  headers followed by ```verilog...``` blocks.
      Each header names the next fenced block.

    Fallback — no <answer> block or no FILE headers found:
      Returns {"TopModule.v": <last ```verilog block in entire response>} so that
      single-file responses without headers still work.
    """
    # Prefer content inside <answer>...</answer>
    answer_match = re.search(r"<answer>(.*?)</answer>", assistant_text, re.DOTALL | re.IGNORECASE)
    search_text = answer_match.group(1) if answer_match else assistant_text

    files: dict[str, str] = {}

    # Walk through FILE headers and pair each with the next fenced block
    # Split the search_text into segments separated by FILE headers
    segments = _FILE_HEADER_RE.split(search_text)
    # segments = [pre-header-text, name1, body1, name2, body2, ...]
    # (split with a capturing group produces alternating name/body pairs after idx 0)
    i = 1
    while i + 1 < len(segments):
        filename = segments[i].strip()
        body = segments[i + 1]
        fence_match = _VERILOG_FENCE_RE.search(body)
        if fence_match:
            files[filename] = fence_match.group(1).strip()
        i += 2

    if files:
        return files

    # Fallback: no FILE headers — grab the last ```verilog block
    all_fences = _VERILOG_FENCE_RE.findall(assistant_text)
    if all_fences:
        return {"TopModule.v": all_fences[-1].strip()}

    return {}


# ---------------------------------------------------------------------------
# Trace formatting (reused from benchmark.py)
# ---------------------------------------------------------------------------

def format_trace(raw_jsonl: str) -> str:
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
            if event.get("subtype") == "init":
                lines.append(f"[SESSION INIT] session_id={event.get('session_id', '?')}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Simulation (identical to benchmark.py)
# ---------------------------------------------------------------------------

def run_simulation(
    test_sv: Path,
    ref_sv: Path,
    generated_files: list[Path],
    workdir: Path,
) -> dict:
    """Compile test_sv + ref_sv + all generated_files and run the simulation."""
    sim_bin = workdir / "_bench_sim_out"

    compile_result = subprocess.run(
        ["iverilog", "-g2012", "-o", str(sim_bin),
         str(test_sv), str(ref_sv)] + [str(f) for f in generated_files],
        capture_output=True, text=True, cwd=str(workdir),
    )

    if compile_result.returncode != 0:
        return {
            "passed": False, "mismatches": -1, "total": -1,
            "error": f"iverilog: {compile_result.stderr.strip()[:400]}",
        }

    try:
        run_result = subprocess.run(
            ["vvp", str(sim_bin)],
            capture_output=True, text=True, timeout=30, cwd=str(workdir),
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
    model: str,
    generated_dir: Path,
    results_csv: Path,
    timeout: int,
) -> None:
    generated_dir.mkdir(parents=True, exist_ok=True)

    # Temp dir for staging .v files during simulation (no CLAUDE.md here)
    sim_workdir = Path("/tmp/rtl-baseline-sim")
    sim_workdir.mkdir(parents=True, exist_ok=True)

    if not SYSTEM_PROMPT_FILE.exists():
        sys.exit(
            f"ERROR: {SYSTEM_PROMPT_FILE} not found.\n"
            "This file contains the systemic RTL assistant instructions."
        )
    system_prompt = SYSTEM_PROMPT_FILE.read_text(encoding="utf-8")

    write_header = not results_csv.exists()
    total = len(problems)
    passed_count = 0

    with results_csv.open("a", newline="", encoding="utf-8") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=CSV_FIELDS)
        if write_header:
            writer.writeheader()

        for i, prob in enumerate(problems, 1):
            name = prob["name"]
            print(f"\n[{i}/{total}] {name}  model={model}", flush=True)

            spec_text = prob["prompt"].read_text(encoding="utf-8")

            # Build the full prompt: system context + spec + task instruction
            full_prompt = (
                system_prompt
                + "\n\n---\n\n## Hardware Specification\n\n"
                + spec_text
                + "\n\n"
                + TASK_INSTRUCTION
            )

            # --- Call claude (runs from /tmp — no project CLAUDE.md loaded) ---
            print("  Calling claude...", flush=True)
            claude_ok, duration, raw_jsonl, stderr, assistant_text, peak_tok, total_tok = (
                call_claude_zeroshot(full_prompt, timeout, model)
            )

            # --- Extract Verilog files ---
            verilog_files = extract_verilog_files(assistant_text)
            files_extracted = len(verilog_files)
            print(f"  Files extracted: {files_extracted} {list(verilog_files.keys())}", flush=True)

            sim_result: dict = {"passed": False, "mismatches": -1, "total": -1, "error": ""}
            staged: list[Path] = []

            if not verilog_files:
                err_snippet = stderr.strip()[:200].replace("\n", " ") if stderr else ""
                sim_result["error"] = f"no verilog extracted from response; stderr: {err_snippet}"
            else:
                # Stage all files in sim_workdir for iverilog
                for fname, src in verilog_files.items():
                    p = sim_workdir / fname
                    p.write_text(src, encoding="utf-8")
                    staged.append(p)
                print(f"  Simulating {[f.name for f in staged]}...", flush=True)
                sim_result = run_simulation(prob["test"], prob["ref"], staged, sim_workdir)

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

            # --- Save artifacts ---
            dest = generated_dir / name
            dest.mkdir(parents=True, exist_ok=True)

            for fname, src in verilog_files.items():
                (dest / fname).write_text(src, encoding="utf-8")

            (dest / "claude_trace.jsonl").write_text(raw_jsonl, encoding="utf-8")
            (dest / "claude_trace.txt").write_text(
                format_trace(raw_jsonl)
                + (f"\n\n=== stderr ===\n{stderr}" if stderr.strip() else ""),
                encoding="utf-8",
            )
            (dest / "response.txt").write_text(assistant_text, encoding="utf-8")

            # --- Write CSV row ---
            writer.writerow(
                {
                    "problem": name,
                    "model": model,
                    "passed": int(passed),
                    "mismatches": sim_result["mismatches"],
                    "total_samples": sim_result["total"],
                    "duration_s": f"{duration:.2f}",
                    "claude_exit_ok": int(claude_ok),
                    "files_extracted": files_extracted,
                    "peak_input_tokens": peak_tok,
                    "total_input_tokens": total_tok,
                    "error": sim_result["error"][:300],
                }
            )
            csvfile.flush()

            # --- Clean up staged temp files ---
            for p in staged:
                p.unlink(missing_ok=True)

    print(f"\n{'='*60}")
    print(f"Results: {passed_count}/{total} passed  (pass@1 = {passed_count / total:.3f})")
    print(f"CSV: {results_csv}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Baseline zero-shot Verilog generation benchmark via a single Claude session.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help="Claude model ID to use",
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
        default=120,
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

    dataset_dir = args.dataset_dir.resolve()
    if not dataset_dir.exists():
        sys.exit(f"ERROR: dataset dir not found: {dataset_dir}")

    problems = find_problems(dataset_dir)
    if not problems:
        sys.exit(f"ERROR: no problems found in {dataset_dir}")

    start_idx = max(0, args.start_from - 1)
    selected = problems[start_idx:]
    if args.limit is not None:
        selected = selected[: args.limit]

    print(f"Found {len(problems)} problems, running {len(selected)} "
          f"(start={args.start_from}, limit={args.limit})")
    print(f"Model: {args.model}  claude_workdir: {CLAUDE_WORKDIR}")

    run_benchmark(
        problems=selected,
        model=args.model,
        generated_dir=args.generated_dir.resolve(),
        results_csv=args.results_csv.resolve(),
        timeout=args.timeout,
    )


if __name__ == "__main__":
    main()
