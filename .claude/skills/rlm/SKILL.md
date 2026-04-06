---
name: rlm
description: Run the RLM hardware generation workflow to produce verified Verilog from a natural language specification. Orchestrates decomposition, context distillation, RTL generation, and iterative verification.
allowed-tools:
  - Read
  - Write
  - Edit
  - Grep
  - Glob
  - Bash
  - Agent
---

# rlm (Recursive Language Model – Verilog Hardware Generation)

Use this Skill when the user asks you to implement hardware described in natural language.

## Mental model

- **Root Agent** (main Claude Code conversation) = architect and orchestrator.
- **`summarizer` subagent** = context distillation; reads existing Verilog and docs, returns Module Contracts.
- **`coder` subagent** = RTL generation in haiku mode.
- **`call_codev()` in REPL** = RTL generation in codev mode (hardware-specialist LLM).
- **`verify_verilog()` in REPL** = Icarus Verilog syntax/elaboration check.
- **Persistent REPL** (`rlm_repl.py`) = stateful Python environment for tool calls and state.

## Inputs

Accept these from `$ARGUMENTS` or ask the user:
- `spec=<description or file path>` (required): the hardware specification.
- `mode=haiku|codev` (optional, default `haiku`): coder backend.
- `server_url=<url>` (required when mode is `codev`): base URL of the vllm server.

---

## Step-by-step procedure

### Step 1 – Decompose the specification

Read the hardware spec and produce a written architectural plan:
- List all sub-modules with their paradigm (combinatorial / sequential / behavioral / structural).
- Identify global signals: clocks, resets, bus protocols, data widths.
- Determine port hierarchy and dependencies between sub-modules.

Do not generate any code yet.

### Step 2 – Distil context (Summarizer sub-agent)

For each sub-module, check whether related Verilog files already exist in the project:

```bash
python3 .claude/skills/rlm/scripts/rlm_repl.py status
```

If existing `.v` files are relevant, invoke the `summarizer` subagent:
- Pass the file path(s) and a description of what the new module must interface with.
- The subagent returns a **Module Contract** (ports, parameters, timing constraints).

Store the Module Contract in the REPL state:

```bash
python3 .claude/skills/rlm/scripts/rlm_repl.py exec -c "
buffers.append('''<module_contract_text>''')
"
```

### Step 3 – Generate RTL (Coder sub-agent)

#### haiku mode

Invoke the `coder` subagent with:
- The Module Contract from Step 2.
- The relevant portion of the hardware specification.
- Any constraints (clock domain, reset polarity, target paradigm).

The subagent returns complete Verilog source. Write it to a `.v` file.

#### codev mode

Call `call_codev()` in the REPL:

```bash
python3 .claude/skills/rlm/scripts/rlm_repl.py exec <<'PY'
spec = """<hardware specification text>"""
verilog_code = call_codev(spec, server_url="http://localhost:8000")
print(verilog_code)
PY
```

Write the returned code to a `.v` file. The function automatically strips `<think>` reasoning and markdown fences.

### Step 4 – Verify (ALWAYS required)

After writing **any** `.v` file:

```bash
python3 .claude/skills/rlm/scripts/rlm_repl.py verify <file.v>
```

A zero exit code and `"success": true` means the file is clean. A non-zero exit code means errors were found — proceed to Step 5.

You can also verify inside an exec block and capture the result for the feedback loop:

```bash
python3 .claude/skills/rlm/scripts/rlm_repl.py exec -c "
result = verify_verilog('path/to/module.v')
buffers.append(str(result))
print(result)
"
```

### Step 5 – Recursive correction (if verification fails)

1. Read `result['stderr']` to identify the exact errors.
2. Optionally invoke the `summarizer` subagent with the error log and the Module Contract to identify the mismatch.
3. Invoke the `coder` subagent (or `call_codev()`) with:
   - The original spec.
   - The current (broken) Verilog.
   - The full compiler error log.
   - A clear instruction to fix only the failing lines.
4. Overwrite the `.v` file with the corrected code.
5. Re-run Step 4. Repeat until clean.

### Step 6 – Integrate all sub-modules

Once every sub-module passes verification:
1. Generate a top-level wrapper module that instantiates all sub-modules.
2. Wire ports according to the architectural plan from Step 1.
3. Verify the top-level file (Step 4).
4. Report success to the user with the list of generated files.

---

## Guardrails

- Never skip verification after writing a `.v` file.
- Never paste large raw Verilog into the main conversation — reference file paths instead.
- Subagents cannot spawn other subagents; all orchestration stays in the main conversation.
- Keep generated files and REPL state under the project directory.
- In codev mode, `call_codev()` is the only safe way to invoke the model — do not pass raw model output directly to iverilog.
