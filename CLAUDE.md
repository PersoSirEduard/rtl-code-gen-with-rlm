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

- **haiku** – Invoke the `coder` subagent (Claude Haiku). Pass the Module Contract and the hardware specification. The agent returns a complete Verilog module.
- **codev** – Call `call_codev(prompt, server_url)` inside the REPL. This sends the specification to `zhuyaoyu/CodeV-R1-RL-Qwen-7B` running via vllm and returns only the extracted Verilog code (reasoning and tags are stripped automatically).

### 4. Verification (ALWAYS required after writing any .v file)

After writing **any** `.v` file, immediately verify it:

```bash
python3 .claude/skills/rlm/scripts/rlm_repl.py verify <file.v>
```

Or within a REPL `exec` block:

```python
result = verify_verilog('<file.v>')
print(result)
```

A clean compilation returns `{"success": true, ...}`. Never skip this step.

### 5. Recursive Correction (Feedback Loop)

If verification fails:
1. The Root Agent reads the compiler error output.
2. Optionally, ask the `summarizer` subagent to compare the error against the Module Contract to identify the mismatched interface.
3. Ask the `coder` subagent (or call `call_codev()`) to fix the specific failing lines, providing the full error log.
4. Write the corrected file and run verification again.
5. Repeat until `"success": true`.

### 6. Integration & Final Assembly (Root Agent)

Once all sub-modules pass verification, generate the top-level wrapper module that instantiates and wires all sub-modules according to the initial architectural plan. Verify the top-level file as well before declaring the task complete.
