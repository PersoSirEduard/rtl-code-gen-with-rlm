---
name: rlm
description: Run the Zero-Footprint RLM hardware generation workflow. Plan → Contract → Code → Static Verify → Port Verify, with a self-reflective RLM loop that escalates only on low confidence + uncertainties.
allowed-tools:
  - Bash
---

# rlm — Zero-Footprint Recursive Language Model

Use this Skill when the user asks you to implement hardware described in natural language.

## Mental model

- **Root Agent** = orchestrator. **NEVER** uses the `Read` tool — the `Read` tool is not in this skill's allowed-tools and the root agent must not see the hardware specification at all. The spec (`prompt.txt`) is auto-loaded into `workbench["prompt"]` by the REPL on the first `exec` call; from there it lives only inside sub-LLM calls. The root agent reasons over compact metadata only (paradigm, port lists, confidence scores, uncertainty strings).
- **`workbench`** = persistent dict, single source of truth for all artefacts. Pickled to disk between exec calls.
- **`workbench["system"]`** = role instructions only (`system_prompt.txt`). Prepended to every `sub_llm` call.
- **`workbench["prompt"]`** = role + hardware spec (`prompt.txt`). Prepended to every `generate_rtl` call. Pass it explicitly inside an exec block when a `sub_llm` call needs the spec (only Phase 1 planning does).
- **`sub_llm(input, target_key)`** = one Claude Haiku call. Stores content in `workbench[target_key]["source"]`, diagnostic in `workbench[target_key]["diagnostic"]`. Returns `{key, length, confidence, uncertainties}`.
- **`generate_rtl(spec, target_key)`** = one RTL generation via Haiku. Same content + diagnostic extraction.
- **`write(file, key)` / `read(file, key)`** — REPL helpers (functions, not tools) that move data between workbench and disk inside exec blocks.
- **`verify_verilog(file)`** — `iverilog -g2012 -t null` check.

The skill takes no arguments — `prompt.txt` is preloaded by the REPL. **Do not open it, do not Read it, do not echo its contents in any tool call.** The first thing the orchestrator should do is run the Phase 1 exec block.

---

## Output contract (every gated call)

Every gated `sub_llm` / `generate_rtl` call MUST emit exactly two fenced blocks, in this order, with no preamble or text outside them:

````
```summary
{
  "confidence_score": <integer 0-100>,
  "uncertainties": ["<concrete ambiguity, missing-info, design gap>", ...],
  "summary": "<1-sentence description of the approach>"
}
```

```output
<the actual content — plan body, contract, Verilog module, etc.>
```
````

The REPL parses the body of the first ```summary``` block as JSON into `workbench[key]["diagnostic"]` and returns `confidence` + `uncertainties`. The body of the first ```output``` block is stored in `workbench[key]["source"]`.

Single-pass auxiliary calls (relevance check, prosecutor, contract extraction) do **not** require this format — their output goes directly into `["source"]` and the diagnostic stays empty.

## Confidence calibration (uniform across all gated calls)

| Range | Meaning |
|---|---|
| Range | Meaning | `uncertainties` |
|---|---|---|
| **95–100** | Every spec clause maps to a specific implementation choice with zero interpretive guess. Quote-line traceable. | MUST be `[]` |
| **80–94** | Only minor stylistic choices (variable names, unconstrained encoding widths). No semantic ambiguity. | MUST be `[]` |
| **60–79** | At least one genuinely ambiguous clause (off-by-one thresholds, "more than 20" vs "≥ 20", unspecified reset, contract/spec mismatch). | MUST be non-empty and list the ambiguity |
| **Below 60** | Structural uncertainty (missing primitive, contradictory clauses, uncertain paradigm). | MUST be non-empty |

**HARD CONSTRAINT — read before responding.** This is a contract with the
orchestrator, not a guideline. The two fields must agree:

- `uncertainties` non-empty → `confidence_score` ≤ 79. ALWAYS. No exceptions.
- `uncertainties == []` → `confidence_score` ≥ 80.
- A response with e.g. `"confidence_score": 88, "uncertainties": ["..."]` is
  **malformed**. The orchestrator will detect and clamp the score to 79
  automatically — so dishonest 95 ≡ 79 in practice. Be calibrated.

**Self-check before emitting.** Count entries in `uncertainties`. If > 0, set
score in [40, 79]. If 0, set score in [80, 100]. Adjust before output.

## Don't overthink

Calls go to Claude Haiku. Be direct: no exploratory reasoning, no "let me think step by step", no alternative interpretations inline. Pick the most defensible answer and commit. Doubt belongs in `uncertainties` — and remember that listing doubt forces `confidence_score ≤ 79`, so only list real concerns, not hedges. If a clause is unambiguous, do NOT invent uncertainty just to look thorough; pick the answer and ship 90+.

## Two-Reading Test (the core uncertainty rule)

Every adversarial checklist in every gated phase boils down to one rule applied to every natural-language claim in the spec (and, for downstream phases, in the upstream artefact):

> **Could a careful engineer reading the SAME sentence implement the OPPOSITE behavior and still claim spec compliance?**
>
> If yes — even if your reading is the *conventional* one — the claim is ambiguous and MUST be listed as an uncertainty (forcing `confidence_score ≤ 79`).

Only a **waveform, truth table, explicit equation, or worked example** in the spec pins an interpretation. Convention alone does NOT — engineers disagree on conventions, and "MSB-first" / "previous level higher" / "more than 20" are textbook examples.

The phase-specific checklists below give *example shapes* that almost always fail the test. They are illustrative, not exhaustive — the test applies to every claim, not only those matching a specific pattern.

---

## Gating rule

After every gated call:

- **`confidence_score >= 80` AND `uncertainties == []`** → accept, proceed.
- **Otherwise** (`confidence < 80` AND `uncertainties != []`, or `confidence is None`) → enter the **SRLM loop**.

`confidence is None` means the diagnostic block didn't parse — treat as low confidence and escalate.

## Self-reflective RLM loop (SRLM)

Cap: **2 iterations per phase**. The orchestrator drives the loop; the model just answers.

### SRLM-1 — Research (DB lookup)

Build a keyword list combining:
- **Uncertainty terms** — concrete noun phrases extracted from each uncertainty.
- **Structural keywords from SUMMARY** — `circuit_type` and `behaviors`.
- **Domain words from `SUMMARY.description`** — `lemming`, `traffic-light`, etc. Score-only, never `hard_filter`.

Token-set matching with `[-_\s]+` normalization. Top 5 entries. If `hard_filter` empties the pool, fall back to no filter automatically.

```bash
python3 .claude/skills/rlm/scripts/rlm_repl.py exec -c "
import json, os, re
db_path = '../verilog-db/'

# Pass-1 hard filter — structural / circuit_type tokens only. Empty to skip.
hard_filter = ['<circuit_type-token>', '<behavior-token>']
# Pass-2 scoring — uncertainty + behaviors + domain words.
score_terms = ['<term-1>', '<term-2>']

def _tok(kw):
    return [t for t in re.split(r'[-_\s]+', kw.lower()) if t]
def _match(text, kw):
    return all(t in text for t in _tok(kw))

all_entries = []
for f in os.listdir(db_path):
    if not f.endswith('.json'):
        continue
    with open(os.path.join(db_path, f)) as j:
        e = json.load(j)
    if not e.get('verilog_code'):
        continue
    text = (e.get('module_name','') + ' ' + e.get('description','')).lower()
    all_entries.append((text, e))

def _filter(active):
    if not active:
        return [e for _, e in all_entries]
    return [e for t, e in all_entries if all(_match(t, k) for k in active)]

pool = _filter(hard_filter)
fallback = False
if hard_filter and not pool:
    pool = _filter([])
    fallback = True

scored = []
for e in pool:
    text = (e.get('module_name','') + ' ' + e.get('description','')).lower()
    s = sum(1 for k in score_terms if _match(text, k))
    if s or not score_terms:
        scored.append((s, e))
scored.sort(key=lambda x: (-x[0], x[1].get('token_count', 9999)))
top = [e for _, e in scored[:5]]
workbench['srlm_refs_<key>'] = json.dumps(top)
print({'pool_size': len(pool), 'top_n': len(top), 'fallback': fallback,
       'descriptions': [e.get('description','')[:120] for e in top]})
"
```

If the descriptions look irrelevant or `top_n == 0`, retry SRLM-1 once with refined keywords (broader or different) before proceeding.

### SRLM-2 — Relevance check

Single-pass `sub_llm` (no diagnostic format) verifies whether each DB hit addresses the uncertainties. Strict: a structurally close ref that doesn't address the specific uncertainty is `not_addressed`.

```bash
python3 .claude/skills/rlm/scripts/rlm_repl.py exec -c "
import json, re
hits = json.loads(workbench['srlm_refs_<key>'])
uncertainties = [<verbatim list of uncertainty strings>]
prompt = 'Verify whether the references address the uncertainties. Be strict: a structurally close ref that does not address the specific concern is not_addressed.\n\n'
prompt += 'Uncertainties:\n' + '\n'.join(f'  {i+1}. {u}' for i, u in enumerate(uncertainties)) + '\n\n'
prompt += 'References:\n'
for i, h in enumerate(hits, 1):
    prompt += f'\n--- Ref {i}: {h.get(\"module_name\",\"?\")} ---\n'
    prompt += f'Description: {h.get(\"description\",\"\")}\n'
    prompt += f'Code:\n{h.get(\"verilog_code\",\"\")[:3000]}\n'
prompt += (
    '\nFor EACH uncertainty output exactly:\n'
    '  Uncertainty <n>: <verbatim>\n'
    '    Status: resolved | partial | not_addressed\n'
    '    Note: <1-2 sentences citing Ref N and the specific mechanism>\n\n'
    'Then exactly:\n'
    '  RELEVANCE: useful | partial | not_useful\n'
    '  RELEVANCE_NOTE: <1 sentence — what the refs collectively offer and what they miss>\n\n'
    'Be direct, do not overthink.'
)
sub_llm(prompt, target_key='srlm_check_<key>')
text = workbench['srlm_check_<key>']['source']
v = re.search(r'RELEVANCE:\s*(useful|partial|not_useful)', text, re.I)
n = re.search(r'RELEVANCE_NOTE:\s*([^\n]+)', text)
verdict = v.group(1).lower() if v else 'partial'
note = n.group(1).strip() if n else ''
workbench['srlm_check_<key>']['verdict'] = verdict
workbench['srlm_check_<key>']['note'] = note
print({'verdict': verdict, 'note': note})
"
```

- `not_useful` → discard the refs. Either retry SRLM-1 with refined keywords (cap 1 retry per loop iteration) or proceed to SRLM-3 with no ref context.
- `useful` / `partial` → proceed to SRLM-3 with the refs.

### SRLM-3 — Regenerate

Re-run the original gated call with prior uncertainties + (optionally) the
relevance-check body as ref context.

```bash
python3 .claude/skills/rlm/scripts/rlm_repl.py exec -c "
verdict = workbench.get('srlm_check_<key>', {}).get('verdict', 'partial')
prior_uncertainties = [<verbatim list>]

prior_block = 'Prior concerns — you MUST address each one explicitly:\n'
for i, u in enumerate(prior_uncertainties, 1):
    prior_block += f'  {i}. {u}\n'

ref_block = ''
if verdict in ('useful', 'partial'):
    note = workbench.get('srlm_check_<key>', {}).get('note', '')
    ref_block = (
        f'\nReference context (relevance: {verdict} — {note})\n'
        '(Background — spec is still authoritative.)\n\n'
        + workbench['srlm_check_<key>']['source'] + '\n'
    )

augmented = prior_block + ref_block + '\n' + '<original prompt verbatim>'
# Re-issue the original call (sub_llm for plan/contract, generate_rtl for code):
meta = sub_llm(augmented, target_key='<key>')   # OR generate_rtl(...)
print({'confidence': meta['confidence'], 'uncertainties': meta['uncertainties']})
"
```

### SRLM-4 — Prosecutor (spec-aware)

Single-pass `sub_llm` (no diagnostic format). The prosecutor is the **only**
spec-compliance check in the pipeline — it fires whenever the gating ladder
escalates and the SRLM has produced a regenerated artefact. It scores the
artefact directly against the original spec (no OLD comparison) and emits a
refined strategy if deficiencies remain.

```bash
python3 .claude/skills/rlm/scripts/rlm_repl.py exec -c "
import re
spec = workbench['prompt']
artefact = workbench['<key>']['source']
prior = [<verbatim uncertainty list>]
prompt = (
    'You are the PROSECUTOR. Score the artefact below against the ORIGINAL SPEC. '
    'You are not comparing it to a previous attempt — you are checking spec '
    'compliance. Identify spec clauses that are NOT faithfully implemented, '
    'including subtle issues: polarity inversions (e.g. did_rise vs did_fall), '
    'off-by-one operators (>= vs >), missed branches, OR-collapsed inputs that '
    'should be direction-specific, contract/spec mismatches, and any prior '
    'uncertainty that was glossed over without resolution.\\n\\n'
    'Spec:\\n' + spec + '\\n\\n'
    'Prior uncertainties (re-check whether the artefact actually addresses these):\\n' +
    '\\n'.join(f'  {i+1}. {u}' for i, u in enumerate(prior)) + '\\n\\n'
    'Artefact:\\n' + artefact + '\\n\\n'
    'Output exactly:\\n'
    '  JUDGEMENT: spec_compliant | minor_deficiencies | major_deficiencies\\n'
    '  SCORE: <integer 0-100, overall fidelity to the spec>\\n'
    '  DEFICIENCIES:\\n'
    '    - <verbatim spec clause>: <what the artefact does instead>\\n'
    '    - ... (use \"(none)\" if fully compliant)\\n'
    '  REFINED_STRATEGY:\\n'
    '    <bullet points of concrete corrections — one per deficiency, naming the '
    'spec clause and the specific fix (\"change `dfr=1` in S_L1_RISE to `dfr=0`; '
    'spec says dfr asserts when previous level was higher\")>\\n\\n'
    'Be strict. \"It looks reasonable\" is not acceptable — every spec clause must '
    'have a specific implementation line you can point to. If you cannot, that is '
    'a deficiency.\\n'
    'Calibration:\\n'
    '  spec_compliant      = no deficiencies, every clause has a faithful implementation.\\n'
    '  minor_deficiencies  = 1-2 deficiencies that are stylistic or unspecified-edge-case.\\n'
    '  major_deficiencies  = at least one semantic mismatch (polarity, operator, missing branch).\\n'
    'Be direct, do not overthink.'
)
sub_llm(prompt, target_key='prosecutor_<key>')
text = workbench['prosecutor_<key>']['source']
m = re.search(r'JUDGEMENT:\\s*(spec_compliant|minor_deficiencies|major_deficiencies)', text, re.I)
judgement = m.group(1).lower() if m else 'minor_deficiencies'
workbench['prosecutor_<key>']['judgement'] = judgement
print({'judgement': judgement})
"
```

Decision:

- **`spec_compliant`** → keep NEW. Exit loop.
- **`minor_deficiencies`** → keep NEW. Parse the `DEFICIENCIES` list and propagate
  each one as a new `prior_uncertainty` to the next phase — they are advisory,
  not regenerated against, but downstream phases see them.
- **`major_deficiencies`** → if iteration budget remains, regenerate SRLM-3 using
  the prosecutor's `REFINED_STRATEGY` as the dominant context (replacing the
  research agent's references, since the prosecutor has more authority here).
  If budget is exhausted, accept the current NEW artefact and propagate
  `DEFICIENCIES` as blocking uncertainties so downstream phases at least try
  to compensate.

### Uncertainty propagation

If the loop exits with unresolved uncertainties (either accepted with `still_present` or capped out), the orchestrator carries them forward into the next phase's prompt as a "Prior concerns — must address" block. The model is responsible for explicitly saying whether it resolved each one in its new diagnostic.

---

## Pipeline phases

The pipeline is **Plan → Contract → Code Gen → Static Verify → Port Verify**.
Phases 1–3 are gated — they emit a `summary` + `output` block, the gating rule
fires when `confidence < 80 AND uncertainties != []`, and the SRLM loop then
runs (research → relevance → regenerate → prosecutor) where the **prosecutor
is the single spec-compliance authority**. Each gated phase carries an
**adversarial checklist** in its prompt so the model surfaces the canonical
failure modes (polarity inversions, off-by-one operators, OR-collapsed
direction-specific inputs, etc.) as uncertainties rather than committing
silently. Phase 4 (Static) is mechanical. Phase 5 is mechanical with a gated
fix-call on mismatch.

### Phase 1 — Plan (gated)

Holistic. Initial research: identifies modules, paradigm, circuit type, behaviors, plain-English description (with domain words), ports.

**Important — start here, do not Read any file first.** The spec is already in `workbench['prompt']`; the REPL loads it from `prompt.txt` on the first `exec` call. The root agent's first action for every problem must be the Phase 1 exec block below — not a `Read` of `prompt.txt` or any other source. (`Read` is not even an allowed tool for this skill.)

```bash
python3 .claude/skills/rlm/scripts/rlm_repl.py exec -c "
import json, re
spec = workbench['prompt']
meta = sub_llm(
    spec + '\n\n'
    'TASK: produce a holistic implementation plan for the hardware spec above.\n\n'
    'Plan content requirements:\n'
    '1. Sub-module decomposition: name, paradigm, one-sentence description.\n'
    '2. Port maps for each module: name, direction, width, clock domain, reset polarity.\n'
    '3. Architectural patterns: clock domains, reset strategy, shared signals.\n'
    '4. SUMMARY JSON as the FIRST line of the output block:\n'
    '   SUMMARY: {\"modules\": [{\"name\": \"TopModule\", '
    '\"paradigm\": \"combinational|sequential|fsm-mealy|fsm-moore\", '
    '\"circuit_type\": \"<single-hyphenated-primitive>\", '
    '\"behaviors\": [\"<3-5 hyphenated structural properties>\"], '
    '\"description\": \"<plain English with domain words>\", '
    '\"ports\": [{\"name\": \"clk\", \"dir\": \"input\", \"width\": 1}]}]}\n\n'
    'Field rules:\n'
    '  paradigm — fsm-moore (registered outputs from state) | fsm-mealy (output depends on state + inputs) | sequential (counters, shift-regs, no named states) | combinational.\n'
    '  circuit_type — most specific primitive label, hyphenated. NEVER application words.\n'
    '  behaviors — HOW it works structurally (registered-output, async-reset, etc.). NEVER domain words.\n'
    '  description — plain English with the actual application/object name (lemming, traffic-light, ps2-keyboard, etc.). Used ONLY for opportunistic DB lookups.\n\n'
    'ADVERSARIAL CHECK — apply the TWO-READING TEST to every spec claim before scoring confidence.\n'
    '\n'
    'For EACH natural-language claim in the spec, ask: \"could a careful engineer reading the SAME sentence implement the OPPOSITE behavior and still claim spec compliance?\" If yes — even if your reading is the conventional one — the claim is ambiguous and MUST be listed as an uncertainty (forces score ≤ 79). Convention alone is NOT enough; only a waveform, truth table, equation, or worked example pins the interpretation.\n'
    '\n'
    'EXAMPLE SHAPES that almost always fail the test (illustrative, not exhaustive — the test applies to every claim, not just these patterns):\n'
    '  - Directional / temporal qualifiers — \"MSB-first\" / \"LSB-first\" / \"shifted in\" (does the FIRST bit become the MSB via shift LEFT, or does each bit LAND at the MSB via shift RIGHT?); \"previous higher/lower\" / \"was rising/falling\"; \"after N cycles\" (does the entry cycle count?).\n'
    '  - Threshold operators — \"more than 20\" vs \"at least 20\", \">\" vs \">=\", counter saturation (at N-1 vs N), boundary inclusion.\n'
    '  - Direction-specific inputs — bump_left vs bump_right, push vs pop, up vs down, set vs reset: do NOT collapse to (a | b) if the spec assigns each a distinct effect.\n'
    '  - Reset — \"reset\" alone is ambiguous: active-high vs active-low, synchronous vs asynchronous, target state on reset.\n'
    '  - Edge polarity — \"edge-triggered\" alone is ambiguous (rising vs falling); each clk/reset-derived signal needs explicit polarity.\n'
    '  - Operation conventions without qualifier — \"shift register\" without left/right, \"counter\" without up/down, \"FIFO\" without read/write side.\n'
    '  - Default / unspecified behavior — \"if both signals are 1\", \"on invalid encoding\", \"at power-on\", \"on overflow\" (saturate vs wrap), \"on underflow\".\n'
    '  - Output timing convention — Moore (registered, 1-cycle latency) vs Mealy (combinational, immediate). Hybrid hyphenated labels (\"mealy-augmented-moore\") are a sign of confusion — flag it.\n'
    '  - Number representation — signed vs unsigned, two\\'s complement vs sign-magnitude, sign-extension vs zero-extension on width changes; bit-order / endianness for multi-byte values.\n'
    '  - Pipeline latency — \"output appears after N cycles\": ambiguous about when the pipeline starts (input-register latching cycle? next cycle?).\n'
    '\n'
    'For EACH item that applies to the spec: if the spec resolves it explicitly (quote-line in the plan body), confidence stays high. If genuinely ambiguous, add an uncertainty AND drop confidence to ≤ 79. Do NOT commit silently. The conventional reading is NOT a free pass — convention varies by community.\n\n'
    'OUTPUT FORMAT — exactly two fenced blocks, no preamble:\n\n'
    '\`\`\`summary\n'
    '{\"confidence_score\": <0-100>, \"uncertainties\": [...], \"summary\": \"<1-sentence approach>\"}\n'
    '\`\`\`\n\n'
    '\`\`\`output\n'
    'SUMMARY: {<JSON above>}\n'
    '<plan body>\n'
    '\`\`\`\n\n'
    'CONFIDENCE — HARD CONTRACT, not a guideline:\n'
    '  - uncertainties non-empty  =>  confidence_score in [40, 79]. ALWAYS.\n'
    '  - uncertainties == []      =>  confidence_score in [80, 100].\n'
    'These two fields MUST agree. A response with e.g. confidence=88 and a non-empty '
    'uncertainties list is malformed; the orchestrator auto-clamps it to 79, so '
    'dishonesty has no upside.\n'
    'Rubric for picking the score itself:\n'
    '  95-100: every clause maps to a specific choice, quote-line traceable.\n'
    '  80-94 : minor stylistic choices only, no semantic ambiguity.\n'
    '  60-79 : one ambiguous clause (must be listed in uncertainties).\n'
    '  <60   : structural doubt (must be listed).\n'
    'SELF-CHECK before emitting: count uncertainties. >0 => score ≤ 79. =0 => score ≥ 80.\n'
    'Only list real concerns — do not invent doubt to look thorough. If a clause is '
    'unambiguous, commit and ship 90+.\n\n'
    'Be direct — no exploratory reasoning, no \"let me think\", no alternative interpretations. Commit.',
    target_key='plan'
)
src = workbench['plan']['source']
m = re.search(r'SUMMARY:\s*(\{.*)', src)
summary = {}
if m:
    raw = m.group(1)
    for suffix in ['', '}', ']}', ']}}']:
        try:
            summary = json.loads(raw + suffix)
            break
        except json.JSONDecodeError:
            pass
print({'summary': summary, 'confidence': meta['confidence'], 'uncertainties': meta['uncertainties']})
"
```

Apply the gating rule. If escalating, the SRLM loop fires (Research → Relevance
→ Regenerate → Prosecutor) using the plan as the artefact under judgement.
Otherwise proceed directly to Phase 2.

### Phase 2 — Contract (gated, per module)

Per-module. Defines the **interface** (full port table), **expected logic** (boolean equations VERBATIM if the spec gives them; else transition / truth / operation tables), and **constraints** (clock edge, reset polarity, encoding width).

```bash
python3 .claude/skills/rlm/scripts/rlm_repl.py exec -c "
paradigm = '<paradigm from summary>'
plan_body = workbench['plan']['source']

contract_prompts = {
    'fsm-mealy': (
        'Extract the complete Mealy FSM behavioral contract. Output BOTH sections:\n\n'
        'INTERFACE: full port list with name, direction, width, clock, reset.\n\n'
        'EQUATIONS: copy any boolean equations the spec gives VERBATIM (e.g. '
        '`q = state ^ a ^ b`). If none, write `EQUATIONS: (none)`.\n\n'
        'TRANSITION TABLE: (state, input1=v, ...) -> next_state, output1=v, ... '
        'Enumerate ALL combinations. Do NOT skip rows.\n\n'
        'CONSTRAINTS: clock edge, reset polarity, encoding.'
    ),
    'fsm-moore': (
        'Extract the complete Moore FSM behavioral contract. Output ALL sections:\n\n'
        'INTERFACE: full port list.\n\n'
        'EQUATIONS: copy any spec equations VERBATIM. If none, `EQUATIONS: (none)`.\n\n'
        'TRANSITIONS: (state, input1=v, ...) -> next_state, one per line. Enumerate ALL combinations.\n\n'
        'OUTPUTS: state -> output1=v, output2=v, one per state.\n\n'
        'CONSTRAINTS: clock edge, reset polarity, encoding.'
    ),
    'combinational': (
        'Extract the complete combinational contract. Output BOTH:\n\n'
        'INTERFACE: full port list.\n\n'
        'EQUATIONS: copy any spec boolean equations VERBATIM, one output per line. If none, `EQUATIONS: (none)`.\n\n'
        'TRUTH TABLE: every input combination -> output values, one row per line.'
    ),
    'sequential': (
        'Extract the complete sequential contract. Output BOTH:\n\n'
        'INTERFACE: full port list.\n\n'
        'EQUATIONS: copy any spec / register-update equations VERBATIM. If none, `EQUATIONS: (none)`.\n\n'
        'OPERATION TABLE:\n'
        '  reset=1            -> <register assignments>\n'
        '  enable=1, load=1   -> <register assignments>\n'
        '  else               -> <register assignments>\n'
    ),
}

prompt_body = contract_prompts.get(paradigm, contract_prompts['combinational'])
full_prompt = (
    plan_body + '\n\n' + prompt_body + '\n\n'
    'ADVERSARIAL CHECK — apply the TWO-READING TEST to every CLAUSE in plan and spec before scoring confidence.\n'
    '\n'
    'For EACH plan/spec claim, ask: \"could a careful engineer reading the SAME claim transcribe it into the OPPOSITE contract entry and still claim faithfulness?\" If yes, the contract has a transcription gap and MUST be listed as an uncertainty (forces score ≤ 79).\n'
    '\n'
    'EXAMPLE TRANSCRIPTION FAILURES (illustrative, not exhaustive — the test applies to every claim):\n'
    '  - EQUATIONS not verbatim — copy spec/plan boolean equations CHARACTER-FOR-CHARACTER. Do not simplify, expand, distribute, or paraphrase. If plan says next_state = a&b | a&c | b&c, do not collapse to a&b. If plan gives no equation, write \"EQUATIONS: (none)\".\n'
    '  - Transition table incomplete — enumerate ALL state × input combinations. A 1-bit state with 2 inputs needs 8 rows. Do NOT skip rows with \"same as above\" or omit values.\n'
    '  - Output table coverage — every state in the encoding MUST have an output row; default branches do NOT substitute.\n'
    '  - Polarity carryover — if plan says \"dfr asserts when previous level was higher\", the contract OUTPUTS section asserts dfr in FALLING states, not RISING. Inversion-prone.\n'
    '  - Direction-specific inputs OR-collapsed — if plan distinguishes bump_left vs bump_right (push/pop, up/down, set/reset), each gets its OWN transition column; never collapse to (a | b).\n'
    '  - Shift/serial-load direction — if plan says \"MSB-first\" or \"LSB-first\", flag both shift directions in CONSTRAINTS (left vs right, where data lands). Two opposite circuits.\n'
    '  - Interface fidelity — port names, widths, directions in INTERFACE must match SUMMARY EXACTLY. Adding/renaming/widening ports is a blocking transcription bug.\n'
    '  - CONSTRAINTS completeness — explicit clock edge (posedge/negedge), reset polarity (active-high/low, sync/async), reset target state, and number representation (signed/unsigned, 2''s complement) must ALL appear. Blanks are uncertainties.\n'
    '  - Threshold off-by-one in transitions — \"more than N\" vs \"≥ N\" must match how the contract''s counter saturates / triggers.\n'
    '  - Default / unspecified rows — \"on invalid encoding\" or \"if both control signals are 1\" must be either explicitly resolved by the plan or flagged.\n'
    '\n'
    'For EACH item: if plan/spec resolves it, confidence stays high. If the contract drops or contradicts, add an uncertainty AND drop score to ≤ 79.\n\n'
    'OUTPUT FORMAT — exactly two fenced blocks, no preamble:\n\n'
    '\`\`\`summary\n'
    '{\"confidence_score\": <0-100>, \"uncertainties\": [...], \"summary\": \"<1-sentence>\"}\n'
    '\`\`\`\n\n'
    '\`\`\`output\n'
    '<the contract sections above, no extra prose>\n'
    '\`\`\`\n\n'
    'CONFIDENCE — HARD CONTRACT, not a guideline:\n'
    '  - uncertainties non-empty  =>  confidence_score in [40, 79]. ALWAYS.\n'
    '  - uncertainties == []      =>  confidence_score in [80, 100].\n'
    'These two fields MUST agree. A response with e.g. confidence=88 and a non-empty '
    'uncertainties list is malformed; the orchestrator auto-clamps it to 79, so '
    'dishonesty has no upside.\n'
    'Rubric for picking the score itself:\n'
    '  95-100: every clause maps to a specific choice, quote-line traceable.\n'
    '  80-94 : minor stylistic choices only, no semantic ambiguity.\n'
    '  60-79 : one ambiguous clause (must be listed in uncertainties).\n'
    '  <60   : structural doubt (must be listed).\n'
    'SELF-CHECK before emitting: count uncertainties. >0 => score ≤ 79. =0 => score ≥ 80.\n'
    'Only list real concerns — do not invent doubt to look thorough. If a clause is '
    'unambiguous, commit and ship 90+.\n\n'
    'Be direct, do not overthink. Doubt belongs in uncertainties.'
)
meta = sub_llm(full_prompt, target_key='contract_<module_key>')
print({'contract_key': 'contract_<module_key>', 'length': meta['length'],
       'confidence': meta['confidence'], 'uncertainties': meta['uncertainties']})
"
```

Apply the gating rule. If escalating, the SRLM loop fires (the prosecutor scores
the regenerated contract against the spec). Otherwise proceed directly to
Phase 3.

### Phase 3 — Code Generation (gated, per module)

`generate_rtl` with the contract as the authoritative spec — but the original hardware spec still wins on any conflict.

```bash
python3 .claude/skills/rlm/scripts/rlm_repl.py exec -c "
contract = workbench['contract_<module_key>']['source']
spec = (
    'Behavioral contract (a faithful summary of the spec — NOT a replacement):\n'
    + contract + '\n\n'
    'Task: Generate the <ModuleName> module only.\n\n'
    'Authority order: (1) the original hardware spec is authoritative; (2) the contract is a guide; '
    '(3) if you detect a contract/spec conflict, do NOT silently follow the contract — flag it as '
    'a concrete uncertainty and implement what the spec describes.\n'
    'Equations under EQUATIONS in the contract or directly in the spec are particularly authoritative — '
    'implement verbatim, do not simplify.\n\n'
    'Implement using a case statement, explicit equations, or truth table. Not ad-hoc guessed boolean '
    'expressions.\n\n'
    'ADVERSARIAL CHECK — apply the TWO-READING TEST to every clause in the contract (and any spec text the contract references) before scoring confidence.\n'
    '\n'
    'For EACH contract clause, ask: \"could a careful engineer reading the SAME clause emit the OPPOSITE Verilog and still claim contract compliance?\" If yes, the implementation choice is ambiguous and MUST be in uncertainties (forces score ≤ 79). The conventional / most-common-in-textbooks Verilog idiom is NOT a free pass.\n'
    '\n'
    'EXAMPLE CODE-GEN FAILURES (illustrative, not exhaustive — the test applies to every clause):\n'
    '  - EQUATIONS not verbatim — if the contract''s EQUATIONS section gives a boolean equation, implement it CHARACTER-FOR-CHARACTER. Do not simplify next_state = a&b|a&c|b&c to a&b. Do not collapse q = state^a^b to a^b.\n'
    '  - Direction-specific branches OR-collapsed — bump_left vs bump_right, push vs pop, up vs down, set vs reset: implement each as a separate branch, never (a | b).\n'
    '  - Polarity flips — if contract OUTPUTS table says dfr=1 in FALL states, dfr=1 must appear there in code. Do not swap to RISE.\n'
    '  - Shift / serial-load direction — \"MSB-first\" needs the right concat: shift LEFT means `q <= {q[N-2:0], data}` (data lands at LSB, oldest bit at MSB after N cycles); shift RIGHT means `q <= {data, q[N-1:1]}` (data lands at MSB, oldest bit at LSB). These are OPPOSITE circuits — verify which one the contract+spec mandate.\n'
    '  - Threshold operators — \">= 20\" is NOT \"> 20\". \"more than 20 cycles\" with a counter that increments after entering the state means counter == 20 ⇔ 21 cycles spent. Match the spec wording exactly; counter saturation matters.\n'
    '  - Reset edge / polarity — `always @(posedge clk, posedge reset)` is async; `always @(posedge clk) if(reset)` is sync. Active-low uses `if (!reset)` or `if (reset_n)`. Wrong choice breaks every testbench cycle.\n'
    '  - Output reg vs wire — Moore registered outputs typically `output reg`; combinational Mealy can be `output` + `assign` or `output reg` + `always_comb`. Match the CONSTRAINTS section.\n'
    '  - Port name / direction / width drift — declare ports EXACTLY as SUMMARY/contract specifies. No renames, extra ports, missing ports, or [2:0] vs [3:0] width drift.\n'
    '  - Transition completeness — every state in the contract TRANSITIONS table must have a case branch; a `default` does not substitute for missing rows.\n'
    '  - Initial state / reset target — reset to the state the contract names (e.g. S_LOW vs S_HIGH); do not default to 0 if the contract says otherwise.\n'
    '  - Number representation — signed vs unsigned arithmetic; sign-extension vs zero-extension on width changes; `+` on `reg` defaults to unsigned unless declared `signed`.\n'
    '  - Blocking vs non-blocking — `=` in always_ff blocks creates simulation/synthesis mismatch. Use `<=` for sequential, `=` for combinational.\n'
    '  - Latch inference — incomplete `if`/`case` in `always @(*)` creates latches. Provide every output an unconditional default.\n'
    '\n'
    'For EACH item: if the contract resolves the choice, confidence stays high. If you have to guess, add an uncertainty AND drop score to ≤ 79. Do NOT silently pick the conventional idiom.\n\n'
    'OUTPUT FORMAT — exactly two fenced blocks, no preamble:\n\n'
    '\`\`\`summary\n'
    '{\"confidence_score\": <0-100>, \"uncertainties\": [...], \"summary\": \"<1-sentence>\"}\n'
    '\`\`\`\n\n'
    '\`\`\`output\n'
    'module <ModuleName> (...);\n'
    '  ...\n'
    'endmodule\n'
    '\`\`\`\n\n'
    'CONFIDENCE — HARD CONTRACT, not a guideline:\n'
    '  - uncertainties non-empty  =>  confidence_score in [40, 79]. ALWAYS.\n'
    '  - uncertainties == []      =>  confidence_score in [80, 100].\n'
    'These two fields MUST agree. The orchestrator auto-clamps any score ≥ 80 with '
    'a non-empty uncertainties list down to 79 — dishonesty has no upside.\n'
    'Rubric for picking the score:\n'
    '  95-100: every spec clause maps to a specific RTL line, quote-line traceable.\n'
    '  80-94 : minor stylistic choices only (signal naming, encoding width when unconstrained).\n'
    '  60-79 : at least one ambiguous clause — operator off-by-one, unspecified reset, '
    'contract/spec mismatch. MUST be listed in uncertainties.\n'
    '  <60   : structural doubt (paradigm, primitive, contradictory clauses). MUST be listed.\n'
    'SELF-CHECK before emitting: count uncertainties. >0 => score ≤ 79. =0 => score ≥ 80.\n'
    'Only list real ambiguities — do not pad uncertainties to look thorough. If every clause '
    'is unambiguous, commit and ship 90+.\n\n'
    'Be direct, do not overthink. Pick the most defensible implementation and commit.'
)
meta = generate_rtl(spec, target_key='<module_key>')
print({'module': '<ModuleName>', 'confidence': meta['confidence'],
       'uncertainties': meta['uncertainties'], 'lines': meta['lines']})
"
```

Apply the gating rule. If escalating, the SRLM loop fires (the prosecutor scores
the regenerated code against the spec). Otherwise proceed directly to Phase 4.

### Phase 4 — Static Verification (mechanical, NOT gated)

```bash
python3 .claude/skills/rlm/scripts/rlm_repl.py exec -c "
print(write('<ModuleName>.v', '<module_key>'))
result = verify_verilog('<ModuleName>.v')
print({'success': result['success'], 'stderr': result['stderr'][:300]})
"
```

- Pass → proceed to Phase 5.
- Fail → classify the error:
  - **Syntactic** (wire/reg type, missing semicolon, port typo, bracket imbalance) → one inline regex fix in an exec block, then if still failing one `sub_llm` fix call. No SRLM, no DB.
  - **Design / structural** (missing always block, wrong sensitivity, incompatible primitive) → enter SRLM with the compiler error as the uncertainty payload.

### Phase 5 — Port Verification (gated only on mismatch)

Regex-extract declared ports from the generated source, diff against SUMMARY's `ports` list.

```bash
python3 .claude/skills/rlm/scripts/rlm_repl.py exec -c "
import re
src = workbench['<module_key>']['source']
gen = set(re.findall(
    r'\b(?:input|output|inout)\s+(?:(?:wire|reg|logic)\s+)?(?:signed\s+)?(?:\[\d+:\d+\]\s+)?(\w+)',
    src
))
plan = {p['name'] for p in <planned_ports_list>}
missing, extra = plan - gen, gen - plan
print({'port_ok': not missing and not extra,
       'missing': sorted(missing), 'extra': sorted(extra)})
"
```

- `port_ok: True` → done.
- `port_ok: False` → fix call (a `sub_llm` invocation that itself goes through the gating rule, since it produces code with a confidence + uncertainties).

---

## Guardrails

- **NEVER `Read` ANY file** — not `prompt.txt`, not `system_prompt.txt`, not generated `.v` files. The `Read` tool is not in this skill's `allowed-tools` for that exact reason. The hardware spec is auto-loaded by the REPL into `workbench['prompt']`; sub-LLM calls inject it. The root agent must reason over metadata only.
- **Never print workbench source content.** Print only metadata dicts and short diagnostic strings.
- **All exec stdout is capped at 2000 chars.**
- **Never generate Verilog code blocks in the main conversation.** Generation goes through `generate_rtl()` or `sub_llm()`.
- **Always escape backticks inside `bash -c "..."` blocks** — write `` \`\`\` `` for a triple-backtick fence inside a Python string. Templates above are already escaped; keep them that way when copying.
- **Never skip Phase 4** after writing any `.v` file.
- Use `status --show-keys` to inspect the workbench **after** Phase 1 has run:
  ```bash
  python3 .claude/skills/rlm/scripts/rlm_repl.py status --show-keys
  ```
