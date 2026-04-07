# Project instructions

## RLM mode for hardware generation

This repository includes a "Recursive Language Model" (RLM) setup for Claude Code, specialised for programmatic hardware description generation:
- Skill: `rlm` in `.claude/skills/rlm/`
- Subagents: `summarizer` and `coder` in `.claude/agents/`
- Persistent Python REPL: `.claude/skills/rlm/scripts/rlm_repl.py`

Run the `/rlm` skill when asked to implement hardware.

## Verilog Workflow

When asked to implement hardware, follow these steps in order.

### 1. Decomposition (Root Agent)

Analyse the hardware specification. Identify:
- Required sub-modules (e.g. ALU, control unit, register file)
- Global signal hierarchy (clocks, resets, bus protocols)
- Design paradigm for each module (combinatorial, sequential, behavioral, or structural)

Produce a written architectural plan before generating any code.

### 2. Context Distillation (Summarizer Sub-Agent)

Invoke the `summarizer` subagent on any existing Verilog source files and documentation relevant to the new component.

It returns a **Module Contract** containing:
- Port definitions (name, direction, width)
- Parameters and their default values
- Timing constraints (clock domains, reset polarity)
- Interface protocols the new module must honour

### 3. RTL Generation (Coder Sub-Agent)

Choose a coder mode based on user preference or task complexity:

- **haiku** – Invoke the `coder` subagent (Claude Haiku). Pass the Module Contract, hardware specification, and the target `.v` file path. The subagent writes the file directly to disk and returns only a JSON metadata summary (file, module name, line count, port count).
- **codev** – Execute `call_codev(prompt, server_url)` inside a REPL `exec` block. The REPL writes the file to disk and prints only metadata. Reasoning and markdown fences are stripped automatically before writing.

**RLM Execution Rules**:
- The Root Agent is **FORBIDDEN** from generating Verilog code blocks in its main response.
- The Root Agent is **FORBIDDEN** from reading generated `.v` files — all Verilog stays on disk and in the REPL state. Only metadata (file path, line/port counts) flows back to the root agent.
- ALL generated `.v` files **MUST** be written directly to the working directory root (e.g. `TopModule.v`), never inside subdirectories.
- **ALWAYS use the relative path** `python3 .claude/skills/rlm/scripts/rlm_repl.py` — never construct an absolute path. Absolute paths break when the project directory contains spaces.
- After each generation step, store the returned metadata in the REPL (`generated_files` list in globals + `buffers`). Use `rlm_repl.py status --show-vars` to inspect REPL state at any time.

### 4. Verification (ALWAYS required after writing any .v file)

After writing **any** `.v` file, immediately verify it:

```bash
python3 .claude/skills/rlm/scripts/rlm_repl.py verify TopModule.v
```

Or within a REPL `exec` block:

```python
result = verify_verilog('<file.v>')
print(result)
```

A clean compilation returns `{"success": true, ...}`. Never skip this step.

### 5. Recursive Correction (Feedback Loop)

If verification fails:
1. The Root Agent reads the compiler error output (stderr only — never the Verilog source).
2. Optionally, ask the `summarizer` subagent to compare the error against the Module Contract to identify the mismatched interface.
3. Ask the `coder` subagent (or re-run `call_codev()` in the REPL) to fix the specific failing lines, providing the full error log and the same target file path to overwrite.
4. The subagent/REPL writes the corrected file directly. Store the correction metadata in the REPL state.
5. Re-run verification. Repeat until `"success": true`.

### 6. Integration & Final Assembly (Root Agent)

Once all sub-modules pass verification, generate the top-level wrapper module that instantiates and wires all sub-modules according to the initial architectural plan. Verify the top-level file as well before declaring the task complete.
