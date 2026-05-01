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
  workbench                                   dict – Persistent state dictionary.
  sub_llm(input, target_key)                  dict – Single Haiku call; parses
                                                     ```summary``` JSON and
                                                     ```output``` body; stores under
                                                     workbench[target_key].
  generate_rtl(spec, target_key)              dict – Single Verilog generation; same
                                                     summary + output extraction.
  write(filename, source_key)                 dict – Flush workbench source to disk.
  read(filename, target_key)                  dict – Load file from disk into workbench.
  extract_verilog(text)                       str  – Parse ```verilog``` block from text.
  verify_verilog(file_path)                   dict – iverilog -g2012 -t null check.

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
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_STATE_PATH = Path(".claude/rlm_state/state.pkl")
DEFAULT_MAX_OUTPUT_CHARS = 2000
DEFAULT_PROMPT_FILE = Path("prompt.txt")
DEFAULT_SYSTEM_FILE = Path("system_prompt.txt")

# HAIKU_MODEL = "claude-haiku-4-5-20251001"
HAIKU_MODEL = "claude-sonnet-4-6"

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
        ["iverilog", "-g2012", "-t", "null", file_path],
        capture_output=True,
        text=True,
    )
    return {
        "success": result.returncode == 0,
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def _extract_assistant_result(raw_jsonl: str) -> str:
    """Pull the final assistant text from a stream-json NDJSON stream.

    Prefers the `result` event's `result` field; falls back to concatenating
    all assistant `text` content blocks.
    """
    for raw_line in raw_jsonl.splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "result" and event.get("subtype") == "success":
            return (event.get("result") or "").strip()

    parts: List[str] = []
    for raw_line in raw_jsonl.splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "assistant":
            for block in event.get("message", {}).get("content", []):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
    return "\n".join(parts).strip()


def _format_trace(raw_jsonl: str) -> str:
    """Render stream-json NDJSON as a human-readable trace.

    Mirrors the format used by `benchmark_rlm.format_trace`: thinking blocks,
    text chunks, tool calls/results, and the final result summary.
    """
    out: List[str] = []
    for raw_line in raw_jsonl.splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            out.append(f"[RAW] {raw_line}")
            continue

        etype = event.get("type", "")
        if etype == "system" and event.get("subtype") == "init":
            out.append(f"[SESSION INIT] session_id={event.get('session_id', '?')}")
        elif etype == "assistant":
            for block in event.get("message", {}).get("content", []):
                btype = block.get("type", "")
                if btype == "text":
                    text = block.get("text", "").strip()
                    if text:
                        out.append(f"[ASSISTANT]\n{text}\n")
                elif btype == "thinking":
                    thinking = block.get("thinking", "").strip()
                    if thinking:
                        out.append(f"[THINKING]\n{thinking}\n")
                elif btype == "tool_use":
                    tool_name = block.get("name", "?")
                    tool_input = json.dumps(
                        block.get("input", {}), ensure_ascii=False
                    )
                    if len(tool_input) > 600:
                        tool_input = tool_input[:600] + "…"
                    out.append(f"[TOOL CALL] {tool_name}\n{tool_input}\n")
        elif etype == "user":
            for block in event.get("message", {}).get("content", []):
                if block.get("type") == "tool_result":
                    content = block.get("content", "")
                    if isinstance(content, list):
                        content = " ".join(
                            c.get("text", "") for c in content if isinstance(c, dict)
                        )
                    content = str(content).strip()
                    if len(content) > 800:
                        content = content[:800] + "…"
                    out.append(
                        f"[TOOL RESULT] (id={block.get('tool_use_id', '?')})\n{content}\n"
                    )
        elif etype == "result":
            summary = f"[SESSION RESULT] subtype={event.get('subtype', '')}"
            usage = event.get("usage")
            if usage:
                summary += f"  tokens={usage}"
            out.append(summary)
            result_text = (event.get("result") or "").strip()
            if result_text:
                out.append(f"  final_result: {result_text[:400]}")
    return "\n".join(out)


def _call_haiku(prompt: str, log_path: Optional[Path] = None) -> str:
    """Single Claude Haiku call returning the assistant's final text.

    Always uses ``--output-format stream-json --verbose`` so thinking and
    intermediate steps are captured. When ``log_path`` is provided, the raw
    NDJSON is written to ``<log_path>.jsonl`` and a human-readable rendering
    to ``<log_path>.txt``. Logging failures are silenced.
    """
    result = subprocess.run(
        [
            "claude",
            "--dangerously-skip-permissions",
            "--output-format", "stream-json",
            "--verbose",
            "--model", HAIKU_MODEL,
            "-p", prompt,
        ],
        capture_output=True,
        text=True,
        timeout=600,
        cwd=tempfile.gettempdir(),
    )
    raw = result.stdout

    if log_path is not None:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.with_suffix(".jsonl").write_text(raw, encoding="utf-8")
            log_path.with_suffix(".txt").write_text(
                _format_trace(raw), encoding="utf-8"
            )
        except OSError:
            pass

    output = _extract_assistant_result(raw)
    if result.returncode != 0 and not output:
        output = f"ERROR (rc={result.returncode}): {result.stderr.strip()[:400]}"
    return output


_SUMMARY_FENCE_RE = re.compile(r"```summary\s*(\{.*?\})\s*```", re.DOTALL)
_OUTPUT_FENCE_RE = re.compile(r"```output\s*(.*?)\s*```", re.DOTALL)


def _parse_diagnostic(text: str) -> Dict[str, Any]:
    """Extract the diagnostic JSON from the first ```summary``` fenced block.

    Returns ``{}`` if no summary block is present or its body cannot be parsed
    as JSON. Progressive suffix repair (``}``, ``]}``, ``]}}``) handles
    models that occasionally drop closing brackets on long lines.

    Calibration enforcement: if the model returns a non-empty
    ``uncertainties`` list together with ``confidence_score >= 80`` (a
    rubric violation — uncertain output cannot be 80+), we clamp the score
    to 79 so the gating rule sees a consistent signal. The original score
    is preserved in ``raw_confidence_score`` for debugging.
    """
    m = _SUMMARY_FENCE_RE.search(text)
    if not m:
        return {}
    raw = m.group(1).strip()
    diag: Dict[str, Any] = {}
    for suffix in ["", "}", "]}", "]}}"]:
        try:
            diag = json.loads(raw + suffix)
            break
        except json.JSONDecodeError:
            continue
    if not diag:
        return {}

    uncertainties = diag.get("uncertainties") or []
    score = diag.get("confidence_score")
    if uncertainties and isinstance(score, (int, float)) and score >= 80:
        diag["raw_confidence_score"] = score
        diag["confidence_score"] = 79
    return diag


def _extract_content(text: str) -> str:
    """Return the body of the first ```output``` fenced block, verbatim.

    Falls back to the full response text when the model omits the output
    fence so downstream callers (``extract_verilog``, plan parsing) can
    still attempt to recover something useful.
    """
    m = _OUTPUT_FENCE_RE.search(text)
    if m:
        return m.group(1).strip()
    return text.strip()


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

    data_keys = [k for k in wb if k != "prompt"]
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

    # Per-call sub_llm log directory + monotonic sequence counter.
    # Each Haiku call writes <NNNN_target_key[_pI]>.{jsonl,txt} so the
    # thinking + tool-call trace can be replayed for debugging.
    log_dir = state_path.parent / "sub_llm_logs"

    def _alloc_log(target_key: str) -> Path:
        seq = state.get("sub_llm_seq", 0) + 1
        state["sub_llm_seq"] = seq
        # Sanitize target_key for filesystem (replace any path separators)
        safe = re.sub(r"[^\w.-]", "_", target_key)
        return log_dir / f"{seq:04d}_{safe}"

    # ------------------------------------------------------------------
    # Initialise systemic context on first exec.
    #
    # workbench["system"] — role instructions only (system_prompt.txt).
    #                       Used as the base context for sub_llm calls so
    #                       that the hardware spec never leaks into planning
    #                       or debugging turns unless the orchestrator
    #                       explicitly passes it in the input string.
    #
    # workbench["prompt"] — system_prompt.txt + hardware spec (prompt.txt).
    #                       Used exclusively by generate_rtl so the sub-LLM
    #                       has the full spec when producing Verilog.
    # ------------------------------------------------------------------
    if "system" not in workbench:
        system_file = Path(args.system_file)
        if system_file.exists():
            workbench["system"] = system_file.read_text(encoding="utf-8")
        else:
            workbench["system"] = ""
            sys.stderr.write(
                f"WARNING: {args.system_file} not found. "
                "workbench['system'] initialised to empty string.\n"
            )

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

    def sub_llm(
        input_string: str,
        target_key: str = "last_result",
    ) -> Dict[str, Any]:
        """Single Claude Haiku call. Stores content in
        ``workbench[target_key]['source']`` and the parsed diagnostic JSON in
        ``workbench[target_key]['diagnostic']``.

        When the response has no ```summary``` / ```output``` fences, the full
        text is treated as content and the diagnostic is left empty — so this
        helper still works for summarisation-style calls (relevance check,
        prosecutor, contract extraction).
        """
        full_prompt = workbench.get("system", "") + "\n\n" + input_string
        log_path = _alloc_log(target_key)
        raw = _call_haiku(full_prompt, log_path=log_path)
        diagnostic = _parse_diagnostic(raw)
        content = _extract_content(raw)

        if not isinstance(workbench.get(target_key), dict):
            workbench[target_key] = {}
        workbench[target_key]["source"] = content
        workbench[target_key]["diagnostic"] = diagnostic

        return {
            "key": target_key,
            "length": len(content),
            "confidence": diagnostic.get("confidence_score"),
            "uncertainties": diagnostic.get("uncertainties", []),
        }

    def generate_rtl(
        spec: str,
        target_key: str = "last_gen",
    ) -> Dict[str, Any]:
        """Generate a synthesisable Verilog module from *spec* via Claude Haiku.

        Expects a ```summary``` + ```output``` response. The output body is
        stored as the Verilog source.
        """
        full_prompt = (
            workbench.get("prompt", "")
            + "\n\n"
            + RTL_GEN_INSTRUCTION
            + spec
        )
        log_path = _alloc_log(target_key)
        raw = _call_haiku(full_prompt, log_path=log_path)
        diagnostic = _parse_diagnostic(raw)
        verilog = extract_verilog(_extract_content(raw))

        if not isinstance(workbench.get(target_key), dict):
            workbench[target_key] = {}
        workbench[target_key]["source"] = verilog
        workbench[target_key]["diagnostic"] = diagnostic

        module_match = re.search(r"\bmodule\s+(\w+)", verilog)
        module_name = module_match.group(1) if module_match else "unknown"
        lines = verilog.count("\n") + 1
        return {
            "key": target_key,
            "module": module_name,
            "lines": lines,
            "confidence": diagnostic.get("confidence_score"),
            "uncertainties": diagnostic.get("uncertainties", []),
        }

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
        "extract_verilog", "verify_verilog",
        "__builtins__",
    }

    env: Dict[str, Any] = {
        "workbench": workbench,
        "sub_llm": sub_llm,
        "generate_rtl": generate_rtl,
        "write": write,
        "read": read,
        "extract_verilog": extract_verilog,
        "verify_verilog": verify_verilog,
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
        help=f"Full context file (system + spec) loaded into workbench['prompt'] "
             f"for generate_rtl (default: {DEFAULT_PROMPT_FILE})",
    )
    p_exec.add_argument(
        "--system-file",
        default=str(DEFAULT_SYSTEM_FILE),
        help=f"Role-instructions-only file loaded into workbench['system'] "
             f"for sub_llm (default: {DEFAULT_SYSTEM_FILE})",
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
