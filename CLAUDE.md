# Project instructions

## RLM mode for hardware generation

This repository implements a **Zero-Footprint Recursive Language Model (RLM)** for Verilog generation.

Components:
- **Skill**: `/rlm` in `.claude/skills/rlm/` (see SKILL.md for the full pipeline + exec templates)
- **Persistent REPL**: `.claude/skills/rlm/scripts/rlm_repl.py`
- **Role context**: `system_prompt.txt` → `workbench["system"]` (role only, no spec). Used by `sub_llm`.
- **Full context**: `prompt.txt` (role + hardware spec) → `workbench["prompt"]`. Used by `generate_rtl`.

Run `/rlm` when the user asks to implement hardware. Always use the relative REPL path: `python3 .claude/skills/rlm/scripts/rlm_repl.py`.

---

## Role: Strategic Programmatic Architect

The root agent orchestrates everything via REPL exec blocks. It never generates Verilog directly and never reads `.v` files into its context. The workbench is the single source of truth; only compact metadata dicts flow back to the root context.

---

## Pipeline

```
Plan → Contract → Code Gen → Static Verify → Port Verify
   ↑       ↑          ↑            (mechanical)     ↑
   └───────┴──────────┴─────── SRLM loop ───────────┘
                  (when confidence < 80 AND uncertainties != [])
```

1. **Plan** — holistic: modules, paradigms, ports, behaviors, plain-English description with domain words. Phase 1's prompt includes an **adversarial checklist** that forces the model to scan for known ambiguity shapes (polarity of "previous"/"prior" qualifiers, threshold operators, direction-specific inputs, reset polarity, invalid encodings, Moore-vs-Mealy timing) before scoring confidence — anything ambiguous MUST be listed as an uncertainty.
2. **Contract** — per-module: interface, expected logic (boolean equations VERBATIM if the spec gives them, else transition / truth / operation tables), constraints. Phase 2's prompt includes its own **adversarial checklist** for transcription failures (dropped equations, incomplete transition tables, polarity carryover, OR-collapsed direction inputs, interface drift, missing CONSTRAINTS).
3. **Code Generation** — `generate_rtl` with the contract as authoritative spec; the original hardware spec still wins on any conflict. Phase 3's prompt carries an **adversarial checklist** targeting code-gen failures (EQUATIONS not implemented verbatim, polarity flips, threshold off-by-one, reset edge/polarity errors, output reg/wire, port-width drift, transition completeness, initial state).
4. **Static Verification** — `iverilog -g2012 -t null`. Pure mechanical.
5. **Port Verification** — regex-extract declared ports vs SUMMARY's planned ports. Gated fix-call only on mismatch.

Phases 1, 2, 3 (and 5-fix when triggered) are **gated** — they emit a `summary` + `output` block and go through the gating rule. Phase 4 (Static) is mechanical.

## Output contract (every gated call)

Each gated call returns two fenced blocks:

- ```` ```summary ```` — JSON `{confidence_score, uncertainties, summary}`
- ```` ```output ```` — content (plan / contract / Verilog)

Auxiliary single-pass calls (relevance check, prosecutor, contract extraction) do NOT use this format — their output is plain text.

## Confidence calibration (uniform across all gated calls)

| Range | Meaning |
|---|---|
| 95–100 | Every spec clause maps to a specific implementation choice |
| 80–94 | Minor stylistic choices only, no semantic ambiguity |
| 60–79 | At least one ambiguous clause (must appear in uncertainties) |
| <60 | Structural uncertainty (paradigm, primitive, contradiction) |

If `uncertainties` is non-empty, `confidence_score` MUST be ≤ 79.

## Gating rule

`confidence ≥ 80 AND uncertainties == []` → accept; otherwise → SRLM loop.

## SRLM loop (cap: 2 iterations per phase)

The SRLM only fires when the gating rule escalates. The **prosecutor is the
single spec-compliance authority** — there are no separate "audit" phases
running unconditionally; the adversarial checklists in the gated prompts
surface uncertainties up front, gating triggers the SRLM, and the prosecutor
inside the loop is the final spec check.

1. **Research** — DB search keyed by uncertainty terms + SUMMARY structural keywords + description domain words. Domain words are score-only, never `hard_filter`.
2. **Relevance check** — single-pass `sub_llm` produces a per-uncertainty status table + `RELEVANCE: useful | partial | not_useful` + `RELEVANCE_NOTE`. If `not_useful`, discard refs (or refine search keys, cap 1 retry).
3. **Regenerate** — re-run the original gated call with prior uncertainties + (when relevant) the refs prepended.
4. **Prosecutor (spec-aware)** — single-pass `sub_llm` scores the regenerated artefact **directly against the original spec**. No OLD comparison — a NEW-vs-OLD diff misses errors both share (inverted polarity, etc.). Verdict: `spec_compliant | minor_deficiencies | major_deficiencies` + a `DEFICIENCIES` list (each citing a spec clause and what the artefact does instead) + a `REFINED_STRATEGY` of concrete fixes.
   - `spec_compliant` → keep, exit.
   - `minor_deficiencies` → keep, propagate the list as advisory uncertainties to the next phase.
   - `major_deficiencies` → if budget remains, regenerate using the prosecutor's `REFINED_STRATEGY` as dominant context (it outranks the research agent's references). On exhaustion, accept the current artefact and propagate the deficiencies as blocking uncertainties.

## Uncertainty propagation

The root agent propagates unresolved uncertainties forward as `prior_uncertainties` in the next phase's prompt. The model must explicitly say whether it resolved each one in the new diagnostic.

## Don't overthink

Calls go to Claude Haiku. Be direct: no "let me think step by step", no exploratory tangents. Commit to the most defensible answer; doubt belongs in `uncertainties`.

## Two-Reading Test (the core uncertainty rule)

Every adversarial checklist in every gated phase reduces to one rule applied to each natural-language claim:

> Could a careful engineer reading the SAME sentence implement the OPPOSITE behavior and still claim spec compliance? If yes, list it as an uncertainty.

Convention alone is not enough — only a **waveform, truth table, explicit equation, or worked example** in the spec pins an interpretation. Phase-specific examples in SKILL.md include MSB-first/LSB-first shift direction, "more than N" vs "at least N" thresholds, "previous higher/lower" polarity, direction-specific inputs (bump_left vs bump_right), reset polarity / edge ambiguity, hybrid paradigm labels, signed/unsigned arithmetic, sign-extension, latch inference, and blocking vs non-blocking assignment — but the test applies to every claim, not only those matching a specific pattern.

---

## Context Constraints (STRICT)

- **NEVER `Read` ANY file** while running `/rlm` — not `prompt.txt`, not `system_prompt.txt`, not generated `.v` files. The skill's frontmatter explicitly excludes the `Read` tool. The hardware spec is auto-loaded by the REPL into `workbench['prompt']` on the first exec call; from there it lives only inside sub-LLM calls. The root agent reasons over metadata only.
- Never print workbench source content; print only metadata dicts.
- Exec stdout capped at 2000 chars.
- Never generate Verilog in the main conversation.
- Always escape backticks (`` \`\`\` ``) inside `bash -c "..."` blocks.
- Never skip Phase 4 after writing any `.v` file.
- Check workbench state with `python3 .claude/skills/rlm/scripts/rlm_repl.py status --show-keys` AFTER Phase 1 has run.
