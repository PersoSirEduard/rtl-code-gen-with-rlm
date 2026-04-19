---
name: rlm
description: Run the Zero-Footprint RLM hardware generation workflow to produce verified Verilog from a natural language specification. Orchestrates holistic planning, programmatic RTL generation, verification, and recursive debugging — all through the persistent workbench REPL.
allowed-tools:
  - Read
  - Bash
---

# rlm (Zero-Footprint Recursive Language Model — Verilog Hardware Generation)

Use this Skill when the user asks you to implement hardware described in natural language.

## Mental model

- **Root Agent** = Strategic Programmatic Architect. Orchestrates all phases via REPL exec blocks. Never generates Verilog directly. Never reads `.v` files into its context.
- **`workbench`** = persistent dict (pickled to disk). Single source of truth for all LLM outputs, generated code, and intermediate results.
- **`workbench["system"]`** = RTL assistant role instructions only (from `system_prompt.txt`). Loaded on first exec. Prepended automatically to every `sub_llm` call.
- **`workbench["prompt"]`** = role instructions + hardware spec (from `prompt.txt`). Loaded on first exec. Prepended automatically to every `generate_rtl` call.
- **`sub_llm(input, target_key)`** = general-purpose Claude Haiku call. Prepends `workbench["system"]` (role only — **no hardware spec**). Pass the spec explicitly in `input` when the call needs it (Phase 1 planning). Stores text output in `workbench[target_key]["source"]`.
- **`generate_rtl(spec, mode, target_key)`** = RTL generation. Prepends `workbench["prompt"]` (role + spec) automatically. Stores raw Verilog in `workbench[target_key]["source"]`. Never prints the Verilog.
- **`write(filename, source_key)`** / **`read(filename, target_key)`** = move data between workbench and disk.
- **`verify_verilog(file_path)`** = iverilog syntax/elaboration check.

## Inputs

Accept these from `$ARGUMENTS` or ask the user:
- `mode=haiku|codev` (optional, default `haiku`): RTL generation backend.
- `server_url=<url>` (required when `mode=codev`): base URL of the vllm server.

The hardware specification is **not** passed as an argument. It is already loaded into
`workbench["prompt"]` from `prompt.txt` (written by the benchmark before each session,
or populated manually for ad-hoc runs). Do not ask the user for the spec.

---

## Step-by-step procedure

### Phase 1 — Holistic Planning

Do not generate any code yet. Do not use the `Read` tool on any file.
**Do not call `status` before this step** — workbench keys are loaded lazily on the
first `exec` call.

`sub_llm` only prepends the role instructions (`workbench["system"]`), not the hardware
spec. For planning you must pass the spec explicitly inside the exec block by reading it
from `workbench["prompt"]` and concatenating it with the planning task:

```bash
python3 .claude/skills/rlm/scripts/rlm_repl.py exec -c "
spec = workbench['prompt']
meta = sub_llm(
    spec + '\n\n'
    'Produce a concise implementation plan covering:\n'
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

`print(meta)` returns only `{"key": "plan", "length": <int>}`.
**The plan text stays in the workbench — never print it back to the root context.**

Immediately after, extract module names in the same exec block so Phase 2 knows what to generate without ever printing the plan:

```bash
python3 .claude/skills/rlm/scripts/rlm_repl.py exec -c "
meta = sub_llm(
    workbench['plan']['source'] + '\n\n'
    'List only the Verilog module names from this plan as a compact JSON array. '
    'Example: [\"Adder\", \"TopModule\"]. Output nothing else.',
    target_key='modules'
)
print(workbench['modules']['source'])
"
```

This prints a short JSON array like `["TopModule"]` or `["ALU", "Ctrl", "TopModule"]`. This is the **only** workbench source value you may print — it is compact extracted metadata, not plan content. Use these names to drive Phase 2.

---

### Phase 2 — Programmatic Execution

For each sub-module identified in the plan, feed `workbench['plan']['source']` into
`generate_rtl` along with a focused task string. Use Python string concatenation freely
to build the prompt — including slicing or reformatting plan sections as needed.

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

**For codev mode**, set `server_url` first:
```bash
python3 .claude/skills/rlm/scripts/rlm_repl.py exec -c "
workbench['server_url'] = 'http://localhost:8000'
spec = workbench['plan']['source'] + '\n\nGenerate the <ModuleName> module.'
meta = generate_rtl(spec, mode='codev', target_key='<module_key>')
print(meta)
"
```

Repeat for each sub-module with a unique `target_key` (e.g. `'alu'`, `'ctrl'`, `'regfile'`).

---

### Phase 3 — Module Verification

After generating each module, immediately write it to disk and verify. Never skip this step.

```bash
python3 .claude/skills/rlm/scripts/rlm_repl.py exec -c "
print(write('<ModuleName>.v', '<module_key>'))
result = verify_verilog('<ModuleName>.v')
print({'success': result['success'], 'stderr_len': len(result['stderr'])})
"
```

A `"success": true` response means the file is clean. Proceed to the next sub-module or to final integration. A `"success": false` response means proceed to Phase 4.

---

### Phase 4 — Recursive Debugging

Feed the error log and workbench source back into `sub_llm` using Python string concatenation. Use `extract_verilog` to parse the corrected module out of the text response.

```bash
python3 .claude/skills/rlm/scripts/rlm_repl.py exec -c "
result = verify_verilog('<ModuleName>.v')
err = result['stderr']
src = workbench['<module_key>']['source']
meta = sub_llm(
    'Fix the following Verilog module. Return only the corrected module in a '
    '```verilog``` block. Fix only the failing lines; do not restructure unrelated logic.\n\n'
    'Source:\n' + src + '\n\nCompiler errors:\n' + err,
    target_key='<module_key>'
)
workbench['<module_key>']['source'] = extract_verilog(workbench['<module_key>']['source'])
print(meta)
"
```

Then write the corrected source to disk and re-run Phase 3. Repeat until `"success": true`.

---

### Final Integration

Once every sub-module passes verification, generate the top-level wrapper:

```bash
python3 .claude/skills/rlm/scripts/rlm_repl.py exec -c "
top_spec = (
    workbench['plan']['source'] + '\n\n'
    'Generate the top-level wrapper module that instantiates and wires all '
    'sub-modules according to the plan above.'
)
meta = generate_rtl(top_spec, mode='haiku', target_key='top')
print(meta)
"
```

Verify the top-level file (Phase 3). Report the list of generated files to the user.

---

## Guardrails

- **Never read a `.v` file into the root agent's context.** Use `workbench[key]['source']` inside exec blocks for any source inspection.
- **Never print workbench source content to the root context.** This means no `print(plan[:N])`, no `print(src)`, no slice of plan/spec/Verilog strings. The only permitted print of a workbench source value is `print(workbench['modules']['source'])` — the compact JSON module-name list extracted at the end of Phase 1. Everything else must stay inside the exec block.
- **Never generate a Verilog code block in the main conversation.** All RTL generation goes through `generate_rtl()` or `sub_llm()`.
- **All exec stdout is capped at 2000 chars.** Print only metadata dicts and short diagnostic strings.
- **Never skip Phase 3** after writing any `.v` file.
- In codev mode, `generate_rtl(..., mode='codev')` is the only safe way to call the model — it strips `<think>` reasoning and markdown fences before storing the source.
- Use `status --show-keys` to inspect the workbench **after** Phase 1 has run. Do not call `status` as a first action — `workbench["prompt"]` is loaded lazily on first `exec` and `status` before exec will show "not yet loaded" even though `prompt.txt` is ready on disk:
  ```bash
  python3 .claude/skills/rlm/scripts/rlm_repl.py status --show-keys
  ```
