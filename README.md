# Claude Code RLM — Verilog Hardware Generation

A Recursive Language Model (RLM) setup for Claude Code that generates and verifies synthesisable Verilog RTL from natural language hardware specifications.

Based on the original [Brainqub3](https://brainqub3.com/) RLM implementation and the RLM paper:

> **Recursive Language Models**
> Alex L. Zhang, Tim Kraska, Omar Khattab — MIT CSAIL
> [arXiv:2512.24601](https://arxiv.org/abs/2512.24601)

---

## Architecture

| RLM Role | Implementation | Model |
|---|---|---|
| Root LM (orchestrator) | Main Claude Code conversation | Claude Sonnet |
| Summarizer sub-LM | `summarizer` subagent | Claude Haiku |
| Coder sub-LM (haiku mode) | `coder` subagent | Claude Haiku |
| Coder sub-LM (codev mode) | `call_codev()` in REPL | CodeV-R1-RL-Qwen-7B via vllm |
| External environment | Persistent Python REPL (`rlm_repl.py`) | Python 3 |
| Verifier | `verify_verilog()` / `rlm_repl.py verify` | Icarus Verilog |

### Generation pipeline

```
Spec
 │
 ▼
[Root Agent] ── decompose spec ──► architectural plan
 │
 ▼
[Summarizer] ── scan existing .v files ──► Module Contract (ports, params, timing)
 │
 ▼
[Coder: haiku or codev] ── generate RTL ──► TopModule.v
 │
 ▼
[Verifier: iverilog] ── syntax/elaboration check
 │  ▲
 │  └── error? feed back to Coder (recursive correction)
 ▼
[Root Agent] ── wire sub-modules ──► top-level wrapper + final verify
```

---

## Prerequisites

- [Claude Code](https://claude.ai/claude-code) CLI installed and authenticated
- Python 3
- Icarus Verilog (`iverilog` and `vvp` on `PATH`)
- *(codev mode only)* A running [vllm](https://github.com/vllm-project/vllm) server serving `zhuyaoyu/CodeV-R1-RL-Qwen-7B`

---

## Usage

### Interactive (single module)

```bash
cd rtl-code-gen-with-rlm
claude
```

Then inside the session:

```
/rlm spec=path/to/spec.txt mode=haiku
```

or for CodeV:

```
/rlm spec=path/to/spec.txt mode=codev server_url=http://localhost:8000
```

### Benchmark (automated, verilog-eval dataset)

```bash
# Run all 156 problems in haiku mode
python benchmark.py --mode haiku

# Run 10 problems in CodeV mode
python benchmark.py --mode codev --server-url http://localhost:8000 --limit 10

# Resume from problem 42
python benchmark.py --mode haiku --start-from 42

# Full options
python benchmark.py --help
```

Results are written to `results.csv`. Generated files and full session traces are saved to `generated/<problem_name>/`.

---

## Repository structure

```
.
├── CLAUDE.md                              # Instructions read by Claude during every session
├── benchmark.py                           # Automated benchmark runner
├── generated/                             # Per-problem outputs (created at runtime)
│   └── <ProbXXX_name>/
│       ├── TopModule.v                    # Copy of the generated Verilog
│       ├── claude_trace.txt               # Human-readable session trace
│       └── claude_trace.jsonl             # Raw stream-json events (NDJSON)
├── results.csv                            # pass@1 results (created at runtime)
└── .claude/
    ├── agents/
    │   ├── rlm-subcall.md                 # Summarizer sub-agent (Haiku)
    │   └── coder.md                       # Coder sub-agent (Haiku)
    └── skills/
        └── rlm/
            ├── SKILL.md                   # /rlm skill definition
            └── scripts/
                └── rlm_repl.py            # Persistent REPL: verify_verilog, call_codev
```

---

## Important: paths with spaces

If your project directory contains spaces (e.g. `Winter 2026/Project/`), always invoke
`rlm_repl.py` using its **relative path** from the workdir:

```bash
# Correct — relative path, no quoting issues
python3 .claude/skills/rlm/scripts/rlm_repl.py verify TopModule.v

# Wrong — absolute paths with spaces cause Python to misparse the script path
python3 /home/user/Winter 2026/Project/rtl-code-gen-with-rlm/.claude/...
```

This rule is enforced in `CLAUDE.md` so the agent never constructs absolute paths.

---

## Security warning

**This project is not intended for production use.**

When running `benchmark.py` or any `claude --dangerously-skip-permissions` invocation:
- Run inside an isolated project directory only
- Never point the workdir at a folder containing credentials or sensitive data
- The `--dangerously-skip-permissions` flag allows Claude to execute commands without confirmation

---

## License

See [LICENSE](LICENSE) for details.
