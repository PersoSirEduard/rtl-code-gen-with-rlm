#!/usr/bin/env python3
"""Zero-Footprint RLM REPL for Verilog hardware generation in Claude Code.

Architecture
============
All state lives in a persistent ``workbench`` dictionary (pickled to disk).
The root agent is blinded from generated Verilog — source lives exclusively in
``workbench[key]["source"]`` and on disk.  Only compact metadata dicts flow
back to the root agent's context.

Workbench layout
----------------
  workbench["prompt"]           Systemic context loaded from prompt.txt at startup.
  workbench["server_url"]       Optional: override vllm server URL for codev mode.
  workbench[<target_key>]       {"source": str} entry created by sub_llm /
                                generate_rtl / read.

Commands
--------
  exec    – Execute Python code inside the persisted workbench environment.
  verify  – Run iverilog syntax/elaboration check on a Verilog file.
  status  – Show a compact workbench key summary.
  reset   – Delete the current state file.

Functions injected into exec
----------------------------
  workbench                           dict  – Persistent state dictionary.
  sub_llm(input, target_key)          dict  – Call Claude Haiku; store text output.
  generate_rtl(spec, mode, target_key) dict – Generate Verilog; store source.
  write(filename, source_key)         dict  – Flush workbench source to disk.
  read(filename, target_key)          dict  – Load file from disk into workbench.
  extract_verilog(text)               str   – Parse ```verilog``` block from text.
  verify_verilog(file_path)           dict  – iverilog -t null check.
  call_codev(prompt, server_url)      str   – Direct CodeV API call (raw Verilog).

Security note
-------------
  This runs arbitrary Python via exec.  Treat it like running code you wrote.
"""

from __future__ import annotations

import argparse
import io
import json
import pickle
import re
import subprocess
import sys
import tempfile
import textwrap
import traceback
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any, Dict, List


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_STATE_PATH = Path(".claude/rlm_state/state.pkl")
DEFAULT_MAX_OUTPUT_CHARS = 2000
DEFAULT_PROMPT_FILE = Path("prompt.txt")

HAIKU_MODEL = "claude-haiku-4-5-20251001"

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

RTL_GEN_INSTRUCTION = (
    "Generate a complete, synthesisable Verilog module that satisfies the hardware "
    "specification below.  Return ONLY the Verilog source code inside a "
    "```verilog ... ``` code block.  Do not include any text, explanation, or "
    "commentary outside the code block.\n\n"
)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class RlmReplError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def _ensure_parent_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _load_state(state_path: Path) -> Dict[str, Any]:
    if not state_path.exists():
        return {"version": 2, "workbench": {}}
    try:
        with state_path.open("rb") as f:
            state = pickle.load(f)
        if not isinstance(state, dict):
            raise RlmReplError(f"Corrupt state file: {state_path}")
        # Migrate v1 state (buffers/globals) → v2 (workbench)
        if "workbench" not in state:
            state = {"version": 2, "workbench": {}}
        return state
    except (pickle.UnpicklingError, EOFError, KeyError) as exc:
        sys.stderr.write(f"WARNING: Could not load state ({exc}); starting fresh.\n")
        return {"version": 2, "workbench": {}}


def _save_state(state: Dict[str, Any], state_path: Path) -> None:
    _ensure_parent_dir(state_path)
    tmp = state_path.with_suffix(state_path.suffix + ".tmp")
    with tmp.open("wb") as f:
        pickle.dump(state, f, protocol=pickle.HIGHEST_PROTOCOL)
    tmp.replace(state_path)


def _truncate(s: str, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    if len(s) <= max_chars:
        return s
    return s[:max_chars] + f"\n... [truncated to {max_chars} chars] ...\n"


# ---------------------------------------------------------------------------
# Verilog extraction
# ---------------------------------------------------------------------------

def extract_verilog(text: str) -> str:
    """Extract the first ```verilog ... ``` block from *text*.

    Falls back to the raw text if no fenced block is found (so that
    ``verify_verilog`` can report the parse failure rather than silently
    discarding output).
    """
    match = re.search(r"```verilog\s*(.*?)\s*```", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    return text.strip()


# ---------------------------------------------------------------------------
# Core helpers (also injected into exec environment)
# ---------------------------------------------------------------------------

def verify_verilog(file_path: str) -> Dict[str, Any]:
    """Run ``iverilog -t null`` on *file_path* and return a result dict.

    Returns::

        {"success": bool, "returncode": int, "stdout": str, "stderr": str}

    ``"success": true`` means the file compiled without errors.
    Pass ``"stderr"`` back into ``sub_llm`` for recursive correction.
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
    """Call CodeV via vllm's OpenAI-compatible API; return extracted Verilog.

    Strips ``<think>`` reasoning and markdown fences automatically.
    Raises ``ValueError`` if the response cannot be parsed.
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

    answer_match = re.search(r"<answer>(.*?)</answer>", full_text, re.DOTALL)
    if not answer_match:
        raise ValueError(
            "No <answer> tags found in CodeV response.\n"
            f"Raw output (first 500 chars):\n{full_text[:500]}"
        )

    answer_content = answer_match.group(1)
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
    wb = state.get("workbench", {})

    print("RLM Workbench status")
    print(f"  State file : {args.state}")
    print(f"  Keys       : {len(wb)}")

    # Prompt: check workbench first, then fall back to the prompt.txt file on disk.
    # The file exists (written by benchmark) even before the first exec, so this
    # gives an accurate picture regardless of whether exec has been called yet.
    if "prompt" in wb:
        print(f"  prompt     : loaded in workbench ({len(wb['prompt'])} chars)")
    else:
        prompt_file = Path(args.prompt_file)
        if prompt_file.exists():
            disk_len = prompt_file.stat().st_size
            print(f"  prompt     : ready on disk, not yet loaded ({disk_len} bytes) — will load on first exec")
        else:
            print(f"  prompt     : MISSING — {prompt_file} not found")

    data_keys = [k for k in wb if k not in {"prompt", "server_url"}]
    print(f"  Data keys  : {len(data_keys)}")

    if args.show_keys and data_keys:
        for k in sorted(data_keys):
            entry = wb[k]
            if isinstance(entry, dict) and "source" in entry:
                print(f"    [{k}]  source = {len(entry['source'])} chars")
            else:
                print(f"    [{k}]  = {str(entry)[:80]}")

    return 0


def cmd_reset(args: argparse.Namespace) -> int:
    state_path = Path(args.state)
    if state_path.exists():
        state_path.unlink()
        print(f"Deleted state: {state_path}")
    else:
        print(f"No state to delete at: {state_path}")
    return 0


def cmd_exec(args: argparse.Namespace) -> int:  # noqa: C901
    state_path = Path(args.state)
    state = _load_state(state_path)
    workbench: Dict[str, Any] = state.setdefault("workbench", {})

    # ------------------------------------------------------------------
    # Initialise systemic context from prompt.txt on first exec
    # ------------------------------------------------------------------
    if "prompt" not in workbench:
        prompt_file = Path(args.prompt_file)
        if prompt_file.exists():
            workbench["prompt"] = prompt_file.read_text(encoding="utf-8")
        else:
            workbench["prompt"] = ""
            sys.stderr.write(
                f"WARNING: {args.prompt_file} not found. "
                "workbench['prompt'] initialised to empty string.\n"
            )

    # ------------------------------------------------------------------
    # Injected helper functions — closures over `workbench`
    # ------------------------------------------------------------------

    def sub_llm(input_string: str, target_key: str = "last_result") -> Dict[str, Any]:
        """Concatenate ``workbench['prompt']`` + *input_string*, call Claude Haiku,
        and store the text output in ``workbench[target_key]['source']``.

        Args:
            input_string: The task or question to send to the model.
            target_key:   Key under which the response is stored in workbench.

        Returns:
            ``{"key": target_key, "length": <int>}``
        """
        full_prompt = workbench.get("prompt", "") + "\n\n" + input_string
        result = subprocess.run(
            [
                "claude",
                "--dangerously-skip-permissions",
                "--output-format", "text",
                "--model", HAIKU_MODEL,
                "-p", full_prompt,
            ],
            capture_output=True,
            text=True,
            timeout=300,
            # Run outside the project directory so claude does NOT load CLAUDE.md
            # and does NOT try to orchestrate the RLM workflow itself.
            cwd=tempfile.gettempdir(),
        )
        output = result.stdout.strip()
        if result.returncode != 0 and not output:
            output = (
                f"ERROR (rc={result.returncode}): {result.stderr.strip()[:400]}"
            )
        if not isinstance(workbench.get(target_key), dict):
            workbench[target_key] = {}
        workbench[target_key]["source"] = output
        return {"key": target_key, "length": len(output)}

    def generate_rtl(
        spec: str,
        mode: str = "haiku",
        target_key: str = "last_gen",
    ) -> Dict[str, Any]:
        """Generate a synthesisable Verilog module from *spec*.

        Modes:
            haiku  – Calls Claude Haiku; extracts ```verilog``` block from output.
            codev  – Calls CodeV via vllm (reads ``workbench['server_url']`` or
                     falls back to ``http://localhost:8000``).

        The raw Verilog string is stored in ``workbench[target_key]['source']``.

        Returns:
            ``{"key": target_key, "module": <name>, "lines": <int>}``
        """
        if mode == "haiku":
            full_prompt = (
                workbench.get("prompt", "")
                + "\n\n"
                + RTL_GEN_INSTRUCTION
                + spec
            )
            result = subprocess.run(
                [
                    "claude",
                    "--dangerously-skip-permissions",
                    "--output-format", "text",
                    "--model", HAIKU_MODEL,
                    "-p", full_prompt,
                ],
                capture_output=True,
                text=True,
                timeout=300,
                # Run outside the project directory so claude does NOT load CLAUDE.md
                # and does NOT try to orchestrate the RLM workflow itself.
                cwd=tempfile.gettempdir(),
            )
            if result.returncode != 0 and not result.stdout.strip():
                raise RlmReplError(
                    f"claude CLI failed (rc={result.returncode}): "
                    f"{result.stderr.strip()[:400]}"
                )
            verilog = extract_verilog(result.stdout)

        elif mode == "codev":
            server_url = workbench.get("server_url", "http://localhost:8000")
            verilog = call_codev(spec, server_url)

        else:
            raise RlmReplError(f"Unknown mode: {mode!r}. Choose 'haiku' or 'codev'.")

        if not isinstance(workbench.get(target_key), dict):
            workbench[target_key] = {}
        workbench[target_key]["source"] = verilog

        module_match = re.search(r"\bmodule\s+(\w+)", verilog)
        module_name = module_match.group(1) if module_match else "unknown"
        lines = verilog.count("\n") + 1
        return {"key": target_key, "module": module_name, "lines": lines}

    def write(filename: str, source_key: str) -> Dict[str, Any]:
        """Write ``workbench[source_key]['source']`` to *filename* on disk.

        Returns:
            ``{"written": filename, "chars": <int>}``
        """
        entry = workbench.get(source_key)
        if not isinstance(entry, dict) or "source" not in entry:
            raise RlmReplError(
                f"workbench[{source_key!r}]['source'] not found. "
                "Run generate_rtl() or sub_llm() first."
            )
        source = entry["source"]
        Path(filename).write_text(source, encoding="utf-8")
        return {"written": filename, "chars": len(source)}

    def read(filename: str, target_key: str) -> Dict[str, Any]:
        """Read *filename* from disk into ``workbench[target_key]['source']``.

        Returns:
            ``{"read": filename, "chars": <int>}``
        """
        content = Path(filename).read_text(encoding="utf-8")
        if not isinstance(workbench.get(target_key), dict):
            workbench[target_key] = {}
        workbench[target_key]["source"] = content
        return {"read": filename, "chars": len(content)}

    # ------------------------------------------------------------------
    # Build execution environment
    # ------------------------------------------------------------------
    _injected = {
        "workbench", "sub_llm", "generate_rtl", "write", "read",
        "extract_verilog", "verify_verilog", "call_codev", "__builtins__",
    }

    env: Dict[str, Any] = {
        "workbench": workbench,
        "sub_llm": sub_llm,
        "generate_rtl": generate_rtl,
        "write": write,
        "read": read,
        "extract_verilog": extract_verilog,
        "verify_verilog": verify_verilog,
        "call_codev": call_codev,
    }

    code = args.code
    if code is None:
        code = sys.stdin.read()

    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()

    try:
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            exec(code, env, env)  # noqa: S102
    except Exception:
        traceback.print_exc(file=stderr_buf)

    # ------------------------------------------------------------------
    # Persist workbench — drop any non-pickleable values the user added
    # ------------------------------------------------------------------
    updated_wb = env.get("workbench", workbench)
    if isinstance(updated_wb, dict):
        clean: Dict[str, Any] = {}
        for k, v in updated_wb.items():
            try:
                pickle.dumps(v, protocol=pickle.HIGHEST_PROTOCOL)
                clean[k] = v
            except Exception:
                stderr_buf.write(
                    f"\nWARNING: workbench[{k!r}] is not pickleable; dropped.\n"
                )
        state["workbench"] = clean

    _save_state(state, state_path)

    out = stdout_buf.getvalue()
    err = stderr_buf.getvalue()

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
            Zero-Footprint RLM REPL for Verilog hardware generation.

            All state lives in a persistent workbench dict.  Generated Verilog is
            stored in workbench[key]["source"] and written to disk via write().
            The root agent only ever sees compact metadata — never raw Verilog.

            Quick reference
            ---------------
            # Phase 1 — planning
            python3 .claude/skills/rlm/scripts/rlm_repl.py exec -c "
            meta = sub_llm('Decompose this spec: <spec>', target_key='decomp')
            print(meta)
            "

            # Phase 2 — RTL generation
            python3 .claude/skills/rlm/scripts/rlm_repl.py exec -c "
            meta = generate_rtl('<spec>', mode='haiku', target_key='top')
            print(meta)
            "

            # Phase 3 — write & verify
            python3 .claude/skills/rlm/scripts/rlm_repl.py exec -c "
            print(write('TopModule.v', 'top'))
            print(verify_verilog('TopModule.v'))
            "

            # Phase 4 — recursive fix
            python3 .claude/skills/rlm/scripts/rlm_repl.py exec -c "
            err = verify_verilog('TopModule.v')['stderr']
            src = workbench['top']['source']
            fix_prompt = 'Fix this Verilog:\\n' + src + '\\nErrors:\\n' + err
            meta = sub_llm(fix_prompt, target_key='top')
            workbench['top']['source'] = extract_verilog(workbench['top']['source'])
            print(meta)
            "

            # Inspect state
            python3 .claude/skills/rlm/scripts/rlm_repl.py status --show-keys
            python3 .claude/skills/rlm/scripts/rlm_repl.py reset
            """
        ),
    )
    p.add_argument(
        "--state",
        default=str(DEFAULT_STATE_PATH),
        help=f"Path to state pickle (default: {DEFAULT_STATE_PATH})",
    )

    sub = p.add_subparsers(dest="cmd", required=True)

    # verify
    p_verify = sub.add_parser("verify", help="Run iverilog check on a Verilog file")
    p_verify.add_argument("file", help="Path to the .v/.sv file to verify")
    p_verify.set_defaults(func=cmd_verify)

    # status
    p_status = sub.add_parser("status", help="Show current workbench summary")
    p_status.add_argument(
        "--show-keys", action="store_true",
        help="List each workbench key with its source size",
    )
    p_status.add_argument(
        "--prompt-file",
        default=str(DEFAULT_PROMPT_FILE),
        help=f"prompt.txt path to check on disk when not yet loaded in workbench "
             f"(default: {DEFAULT_PROMPT_FILE})",
    )
    p_status.set_defaults(func=cmd_status)

    # reset
    p_reset = sub.add_parser("reset", help="Delete the current state file")
    p_reset.set_defaults(func=cmd_reset)

    # exec
    p_exec = sub.add_parser("exec", help="Execute Python with persisted workbench")
    p_exec.add_argument(
        "-c", "--code",
        default=None,
        help="Inline code string. If omitted, reads from stdin.",
    )
    p_exec.add_argument(
        "--max-output-chars",
        type=int,
        default=DEFAULT_MAX_OUTPUT_CHARS,
        help=f"Truncate stdout/stderr to this many chars (default: {DEFAULT_MAX_OUTPUT_CHARS})",
    )
    p_exec.add_argument(
        "--prompt-file",
        default=str(DEFAULT_PROMPT_FILE),
        help=f"Systemic context file loaded into workbench['prompt'] on first exec "
             f"(default: {DEFAULT_PROMPT_FILE})",
    )
    p_exec.set_defaults(func=cmd_exec)

    return p


def main(argv: List[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except RlmReplError as exc:
        sys.stderr.write(f"ERROR: {exc}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
