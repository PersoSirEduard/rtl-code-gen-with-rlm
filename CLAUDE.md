# Project instructions

## RLM mode for hardware generation

This repository implements a **Zero-Footprint Recursive Language Model (RLM)** for Verilog hardware generation.

Components:
- **Skill**: `/rlm` in `.claude/skills/rlm/`
- **Persistent REPL**: `.claude/skills/rlm/scripts/rlm_repl.py`
- **Systemic context + spec**: `prompt.txt` (written per-run by the benchmark as `system_prompt.txt` + problem spec; loaded automatically into `workbench["prompt"]` on first exec so every `sub_llm` / `generate_rtl` call has the full spec in context)

Run the `/rlm` skill when asked to implement hardware.

**ALWAYS use relative path**: `python3 .claude/skills/rlm/scripts/rlm_repl.py`

---

## Role: Strategic Programmatic Architect

The root agent is a **Strategic Programmatic Architect**. It orchestrates all hardware generation through the REPL workbench — it never generates Verilog itself and never reads raw `.v` files into its context.

The workbench is the single source of truth. All LLM outputs, generated code, and intermediate results live in `workbench[key]["source"]`. Only compact metadata dicts (`{"key": ..., "lines": ..., "module": ...}`) are printed back to the root agent's context.

---

## Four-Phase Workflow

### Phase 1 — Holistic Planning

Do not generate any code. Do not use the `Read` tool on any file. The hardware
specification is already in `workbench["prompt"]` — loaded from `prompt.txt` on first
exec. Issue a **single** `sub_llm` call that produces the full implementation plan:

```bash
python3 .claude/skills/rlm/scripts/rlm_repl.py exec -c "
meta = sub_llm(
    'Based on the hardware specification in your context, produce a concise '
    'implementation plan covering:\n'
    '1. Sub-module decomposition: name, paradigm '
    '(combinatorial/sequential/behavioral/structural), one-sentence description\n'
    '2. Port maps: for each module list all ports with name, direction, width, '
    'clock domain, reset polarity\n'
    '3. Architectural patterns: clock domains, reset strategy, shared signals\n\n'
    'Be structured and concise. This plan drives all subsequent RTL generation.',
    target_key='plan'
)
print(meta)
"
```

`workbench['plan']['source']` now contains the full architectural plan.

### Phase 2 — Programmatic Execution

For each sub-module, concatenate `workbench['plan']['source']` with a focused task
string and call `generate_rtl`. Use arbitrary Python to parse or slice plan sections
between calls.

```bash
python3 .claude/skills/rlm/scripts/rlm_repl.py exec -c "
spec = (
    workbench['plan']['source'] + '\n\n'
    'Generate the <ModuleName> module only, as described in the plan above.'
)
meta = generate_rtl(spec, mode='haiku', target_key='<module_key>')
print(meta)
"
```

Repeat for each sub-module. Use distinct `target_key` values so all sources persist simultaneously in the workbench.

**Mode selection**:
- `mode='haiku'` — Claude Haiku via the CLI. Uses `workbench['prompt']` as systemic context.
- `mode='codev'` — CodeV-R1 via vllm. Set `workbench['server_url']` before calling.

### Phase 3 — Module Verification

After generating each module, immediately write it to disk and verify:

```bash
python3 .claude/skills/rlm/scripts/rlm_repl.py exec -c "
print(write('ALU.v', 'alu'))
result = verify_verilog('ALU.v')
print({'success': result['success'], 'stderr_len': len(result['stderr'])})
"
```

Never skip verification. Never `Read` the `.v` file back into chat — use `result['stderr']` only.

### Phase 4 — Recursive Debugging

If verification fails, pipe the error log and workbench source back into `sub_llm`. Use Python to build the correction prompt programmatically:

```bash
python3 .claude/skills/rlm/scripts/rlm_repl.py exec -c "
result = verify_verilog('ALU.v')
err = result['stderr']
src = workbench['alu']['source']
meta = sub_llm(
    'Fix the following Verilog module. Return only the corrected module in a '
    '```verilog``` block. Fix only the failing lines; do not restructure unrelated logic.\n\n'
    'Source:\n' + src + '\n\nCompiler errors:\n' + err,
    target_key='alu'
)
workbench['alu']['source'] = extract_verilog(workbench['alu']['source'])
print(meta)
"
```

Then re-run Phase 3. Repeat until `"success": true`.

---

## Context Constraints (STRICT)

- **Never `Read` a generated `.v` file into chat.** Source is accessible only via `workbench[key]['source']` inside exec blocks.
- **All printed output from exec is capped at 2000 characters** by the REPL. Print only metadata dicts and short diagnostic strings.
- **Never generate a Verilog code block in the main conversation.** All RTL generation goes through `generate_rtl()` or `sub_llm()`.
- Check workbench state at any time with:
  ```bash
  python3 .claude/skills/rlm/scripts/rlm_repl.py status --show-keys
  ```
