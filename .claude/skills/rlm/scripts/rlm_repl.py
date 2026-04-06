#!/usr/bin/env python3
"""Persistent mini-REPL for RLM-style Verilog hardware generation in Claude Code.

This script provides a stateful Python environment across invocations by
saving a pickle file to disk. It is intentionally small and dependency-free.

Commands:
  exec    - Execute Python code with persisted state
  verify  - Run iverilog syntax/elaboration check on a Verilog file
  status  - Show current state summary
  reset   - Delete the current state file

Helpers injected into the exec environment:
  - buffers: list[str] for storing intermediate results
  - verify_verilog(file_path) -> dict
  - call_codev(prompt, server_url, model="zhuyaoyu/CodeV-R1-RL-Qwen-7B") -> str

Security note:
  This runs arbitrary Python via exec. Treat it like running code you wrote.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import pickle
import re
import subprocess
import sys
import textwrap
import traceback
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any, Dict, List, Tuple


DEFAULT_STATE_PATH = Path(".claude/rlm_state/state.pkl")
DEFAULT_MAX_OUTPUT_CHARS = 8000

CODEV_SYSTEM_PROMPT = (
    "You are a helpful assistant. The assistant first thinks about the reasoning "
    "process in the mind and then provides the user with the answer. The reasoning "
    "process and answer are enclosed within <think> </think> and<answer> </answer> "
    "tags, respectively, i.e., <think> reasoning process here </think>"
    "<answer> answer here </answer>.  Now the user asks you to write verilog code. "
    "After thinking, when you finally reach a conclusion, enclose the final verilog "
    "code in ```verilog ``` within <answer> </answer> tags. i.e., <answer> ```verilog\n"
    " module top_module(in, out, ...) ... ``` </answer>."
)


class RlmReplError(RuntimeError):
    pass


def _ensure_parent_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _load_state(state_path: Path) -> Dict[str, Any]:
    if not state_path.exists():
        return {"version": 1, "buffers": [], "globals": {}}
    with state_path.open("rb") as f:
        state = pickle.load(f)
    if not isinstance(state, dict):
        raise RlmReplError(f"Corrupt state file: {state_path}")
    return state


def _save_state(state: Dict[str, Any], state_path: Path) -> None:
    _ensure_parent_dir(state_path)
    tmp_path = state_path.with_suffix(state_path.suffix + ".tmp")
    with tmp_path.open("wb") as f:
        pickle.dump(state, f, protocol=pickle.HIGHEST_PROTOCOL)
    tmp_path.replace(state_path)


def _truncate(s: str, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    if len(s) <= max_chars:
        return s
    return s[:max_chars] + f"\n... [truncated to {max_chars} chars] ...\n"


def _is_pickleable(value: Any) -> bool:
    try:
        pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
        return True
    except Exception:
        return False


def _filter_pickleable(d: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    kept: Dict[str, Any] = {}
    dropped: List[str] = []
    for k, v in d.items():
        if _is_pickleable(v):
            kept[k] = v
        else:
            dropped.append(k)
    return kept, dropped


# ---------------------------------------------------------------------------
# Core Verilog helpers (also injected into exec environment)
# ---------------------------------------------------------------------------

def verify_verilog(file_path: str) -> Dict[str, Any]:
    """Run ``iverilog -t null`` on *file_path* and return a result dict.

    Returns:
        {
            "success": bool,
            "returncode": int,
            "stdout": str,
            "stderr": str,
        }

    A ``"success": true`` result means the file compiled without errors.
    If ``"success": false``, pass ``"stderr"`` to the Coder sub-agent for
    correction.
    """
    result = subprocess.run(
        ["iverilog", "-t", "null", file_path],
        capture_output=True,
        text=True,
    )
    return {
        "success": result.returncode == 0,
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def call_codev(
    prompt: str,
    server_url: str,
    model: str = "zhuyaoyu/CodeV-R1-RL-Qwen-7B",
) -> str:
    """Call CodeV via vllm's OpenAI-compatible API and return only the Verilog code.

    The function strictly parses the model's response to extract code within
    the ``\`\`\`verilog`` block inside the ``<answer>`` tags.  The ``<think>``
    reasoning block is discarded to prevent compiler errors.

    Args:
        prompt:     Natural-language hardware specification.
        server_url: Base URL of the vllm server (e.g. "http://localhost:8000").
        model:      Model name served by vllm.

    Returns:
        The raw Verilog source as a string (no markdown fences, no tags).

    Raises:
        ValueError: If the model response cannot be parsed.
        urllib.error.URLError: If the vllm server is unreachable.
    """
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": CODEV_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.0,
        "max_tokens": 4096,
    }

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{server_url.rstrip('/')}/v1/chat/completions",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    with urllib.request.urlopen(req) as resp:
        response = json.loads(resp.read().decode("utf-8"))

    full_text = response["choices"][0]["message"]["content"]

    # Extract content inside <answer> tags only (discards <think> reasoning).
    answer_match = re.search(r"<answer>(.*?)</answer>", full_text, re.DOTALL)
    if not answer_match:
        raise ValueError(
            "No <answer> tags found in CodeV response.\n"
            f"Raw output (first 500 chars):\n{full_text[:500]}"
        )

    answer_content = answer_match.group(1)

    # Extract the Verilog code block inside the answer.
    code_match = re.search(r"```verilog\s*(.*?)\s*```", answer_content, re.DOTALL)
    if not code_match:
        raise ValueError(
            "No ```verilog block found within <answer> tags.\n"
            f"Answer content:\n{answer_content[:500]}"
        )

    return code_match.group(1).strip()


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

def cmd_verify(args: argparse.Namespace) -> int:
    result = verify_verilog(args.file)
    if result["success"]:
        print(f"OK: {args.file} compiled cleanly.")
    else:
        print(f"FAIL: {args.file} has errors (returncode={result['returncode']}).")
    if result["stdout"]:
        sys.stdout.write(result["stdout"])
    if result["stderr"]:
        sys.stderr.write(result["stderr"])
    return 0 if result["success"] else 1


def cmd_status(args: argparse.Namespace) -> int:
    state = _load_state(Path(args.state))
    buffers = state.get("buffers", [])
    g = state.get("globals", {})

    print("RLM REPL status")
    print(f"  State file : {args.state}")
    print(f"  Buffers    : {len(buffers)}")
    print(f"  Persisted vars: {len(g)}")
    if args.show_vars and g:
        for k in sorted(g.keys()):
            print(f"    - {k}")
    return 0


def cmd_reset(args: argparse.Namespace) -> int:
    state_path = Path(args.state)
    if state_path.exists():
        state_path.unlink()
        print(f"Deleted state: {state_path}")
    else:
        print(f"No state to delete at: {state_path}")
    return 0


def cmd_exec(args: argparse.Namespace) -> int:
    state_path = Path(args.state)
    state = _load_state(state_path)

    buffers: List[str] = state.setdefault("buffers", [])
    if not isinstance(buffers, list):
        buffers = []
        state["buffers"] = buffers

    persisted: Dict[str, Any] = state.setdefault("globals", {})
    if not isinstance(persisted, dict):
        persisted = {}
        state["globals"] = persisted

    code = args.code
    if code is None:
        code = sys.stdin.read()

    # Build execution environment: persisted vars + injected helpers.
    env: Dict[str, Any] = dict(persisted)
    env["buffers"] = buffers
    env["verify_verilog"] = verify_verilog
    env["call_codev"] = call_codev

    injected_keys = {"__builtins__", "buffers", "verify_verilog", "call_codev"}

    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()

    try:
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            exec(code, env, env)  # noqa: S102
    except Exception:
        traceback.print_exc(file=stderr_buf)

    # Pull back possibly mutated buffers.
    maybe_buffers = env.get("buffers")
    if isinstance(maybe_buffers, list):
        state["buffers"] = maybe_buffers

    # Persist any new variables (excluding injected keys).
    to_persist = {k: v for k, v in env.items() if k not in injected_keys}
    filtered, dropped = _filter_pickleable(to_persist)
    state["globals"] = filtered

    _save_state(state, state_path)

    out = stdout_buf.getvalue()
    err = stderr_buf.getvalue()

    if dropped and args.warn_unpickleable:
        msg = "Dropped unpickleable variables: " + ", ".join(dropped)
        err = err + ("\n" if err else "") + msg + "\n"

    if out:
        sys.stdout.write(_truncate(out, args.max_output_chars))
    if err:
        sys.stderr.write(_truncate(err, args.max_output_chars))

    return 0


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="rlm_repl",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=textwrap.dedent(
            """\
            Persistent mini-REPL for RLM Verilog hardware generation.

            Examples:
              python rlm_repl.py verify path/to/top_module.v
              python rlm_repl.py exec -c "result = verify_verilog('top.v'); print(result)"
              python rlm_repl.py exec <<'PY'
              code = call_codev('implement a 4-bit adder', 'http://localhost:8000')
              print(code)
              PY
              python rlm_repl.py status
              python rlm_repl.py reset
            """
        ),
    )
    p.add_argument(
        "--state",
        default=str(DEFAULT_STATE_PATH),
        help=f"Path to state pickle (default: {DEFAULT_STATE_PATH})",
    )

    sub = p.add_subparsers(dest="cmd", required=True)

    p_verify = sub.add_parser("verify", help="Run iverilog check on a Verilog file")
    p_verify.add_argument("file", help="Path to the .v file to verify")
    p_verify.set_defaults(func=cmd_verify)

    p_status = sub.add_parser("status", help="Show current state summary")
    p_status.add_argument(
        "--show-vars", action="store_true", help="List persisted variable names"
    )
    p_status.set_defaults(func=cmd_status)

    p_reset = sub.add_parser("reset", help="Delete the current state file")
    p_reset.set_defaults(func=cmd_reset)

    p_exec = sub.add_parser("exec", help="Execute Python code with persisted state")
    p_exec.add_argument(
        "-c",
        "--code",
        default=None,
        help="Inline code string. If omitted, reads code from stdin.",
    )
    p_exec.add_argument(
        "--max-output-chars",
        type=int,
        default=DEFAULT_MAX_OUTPUT_CHARS,
        help=f"Truncate stdout/stderr to this many chars (default: {DEFAULT_MAX_OUTPUT_CHARS})",
    )
    p_exec.add_argument(
        "--warn-unpickleable",
        action="store_true",
        help="Warn on stderr when variables could not be persisted",
    )
    p_exec.set_defaults(func=cmd_exec)

    return p


def main(argv: List[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        return int(args.func(args))
    except RlmReplError as e:
        sys.stderr.write(f"ERROR: {e}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
