# PyMolt

**Prove your Python migration didn't change what your code does.**

Upgrading Python or a major dependency is easy to *start* and almost impossible to *finish* with confidence. The resolver goes green, the test suite goes green, and you still don't know whether some call now returns an empty generator where it used to return `None`.

PyMolt answers that question. It records what your code actually calls into its dependencies — before the migration and after — and reports exactly what changed: results, raised exceptions, calls that stopped happening altogether. No decorators, no code changes, no instrumentation of your app.

It also does the parts around it (surface scan, lock-first resolution, LibCST codemods), because a contract needs a migration to measure. But those are the means. The proof is the product.

---

## Why this exists

**A dependency resolve that succeeds is not a migration that works.** `uv pip compile` tells you a set of versions co-exists. It says nothing about behaviour. Neither does a passing test suite, unless your tests happen to cover every dependency call — and PyMolt will tell you what fraction they actually cover.

This matters more now that AI agents do migrations. We measured it ([`bench/RESULTS.md`](bench/RESULTS.md)): a coding agent with shell access resolves dependencies for Python 3.12 perfectly well on its own — it builds a venv, runs `pip check`, and gets there in about 45 seconds. But asked for the dependency contact surface of the same project, the same agent reported **35–38 call sites where there were 227**, because grepping imports finds modules, not the symbols your code actually calls. With PyMolt it got the exact number, in a fifth of the time and a thirteenth of the context.

That gap is the product: PyMolt is for the questions your agent cannot answer by reading files.

---

## Quick Start

Install via `uv` or `pipx`:

```bash
uv tool install pymolt
# or with MCP server support:
uv tool install "pymolt[mcp]"
```

**Record the contract before you change anything** — this is the step that cannot be recovered later:

```bash
cd your-legacy-project
pymolt doctor .
pymolt contract capture --when baseline --mode tests -- pytest tests/
```

Then migrate however you like — by hand, with `pymolt assess` + `pymolt codemods`, or by handing the project to an AI agent. When the target environment runs, record the other side and compare:

```bash
pymolt plan .                 # freeze the exact reviewed codemod plan
pymolt apply .                # apply that plan; retains durable rollback data
pymolt rollback .             # restore the last applied plan if needed
pymolt migrate .              # end-to-end read-only preview shortcut
pymolt migrate . --patch migration.patch  # export the exact unified diff
pymolt migrate . --write      # requires the valid baseline above
pymolt contract capture --when post-migration --mode tests -- pytest tests/
pymolt verify .               # one verdict across traces, replay, golden/canary evidence
```

`verify` gives you three numbers that no resolver can: how much of the Axiom-impacted dependency surface your tests actually exercised, how much stayed **BLIND**, and which calls changed behaviour. Its verdict is deliberately tri-state: `PASS`, `FAIL`, or `INCONCLUSIVE`. Missing, stale, opaque, sampled, or non-comparable evidence can never become a green result.

> Prefer one command? `pymolt migrate` runs the preparation phases end-to-end. It resolves, previews codemods and shows diffs — but it cannot prove anything on its own, and it will tell you so. The contract still needs a baseline recorded *before* the first edit.

---

## How It Works: The Funnel Feeds The Contract

The contract brackets the migration. Everything else happens inside the brackets:

```
   contract capture ──────── the migration ───────► contract capture
      (baseline)                                     (post-migration)
          │          scan → setup → assess → codemods         │
          │       surfaces   target   resolve   LibCST        │
          │       & edges     Python  baseline  rewrites      │
          │                   & tool  vs target               │
          └───────────────────────┬───────────────────────────┘
                                  ▼
                          pymolt verify
              confirmed · BLIND · trust % · what changed
```

```bash
# 0. BASELINE — record first. This is the only step you cannot do later.
pymolt contract capture --when baseline --mode tests -- pytest tests/

# 1. SCAN: monorepo surfaces, Dockerfiles, Python-version divergence — offline
pymolt scan .

# 2. SETUP: pick target Python (3.12 / 3.13) and resolver toolset (uv, pip, conda)
pymolt setup .

# 3. ASSESS: lock-first resolution of baseline vs target, CVE deltas, compile risk
pymolt assess . --target-python 3.12 --risk

# 4. PLAN/APPLY: freeze the AST result, then apply exactly what was reviewed
pymolt plan .                 # dry-run, hash-bound plan with unified diff
pymolt apply .                # baseline-gated, durable and transactional
# `codemods .` remains a focused preview/apply shortcut for one codemod phase.

# 5. PROVE IT: record the other side and diff the two contracts
pymolt contract capture --when post-migration --mode tests -- pytest tests/
pymolt verify .
```

Steps 1–4 are the migration. Step 0 and step 5 are the reason to trust it.

**Where is the contact map in this?** `pymolt contract map .` is the static denominator — calls, attribute reads, decorators, bases, context managers, constants and protocol operations that cross into a dependency. Axiom delta paths focus this map automatically after `apply`; full-surface totals remain visible.

---

## Built for AI Agents (MCP Server)

PyMolt isn't just a CLI — it's a first-class Model Context Protocol (MCP) server. If you use Cursor, Claude Desktop, Windsurf, or Google Antigravity, your agent gets the migration funnel as tools.

Give your agent the contract tools in particular. It can already resolve dependencies and read manifests on its own; what it cannot do is tell you whether the migration changed behaviour — or even count its own dependency contact surface correctly.

### Configure in your MCP Client

Add PyMolt to your agent config (e.g. `~/.cursor/mcp.json` or `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "pymolt": {
      "command": "pymolt",
      "args": ["mcp"]
    }
  }
}
```

### Available MCP Tools

**The ones your agent cannot do for itself** — these need LibCST scope resolution or recorded runtime traces, and no amount of file reading substitutes:

| Tool | What the agent gets |
|---|---|
| `contract_map` | Every call site into a dependency, resolved through imports and scopes. Import greps undercount this severalfold. |
| `contract_capture` | Runs *your* test or app command with a boundary tracer injected — zero edits to the project — and stores it as the `baseline` or `post-migration` slot. |
| `contract_report` | The verdict: confirmed / BLIND coverage, trust %, and exactly which dependency calls changed result, changed raise, or disappeared. |

**Preparation** — faster and more reproducible than driving the tools by hand, and they carry honesty markers (fixation, resolution quality, baseline tier) that a hand-run loses:

| Tool | What the agent gets |
|---|---|
| `status` | Where the project stands in the funnel, and the single next command. |
| `scan` | Project roots, dependency edges and Python-version divergence, without parsing Dockerfiles by hand. |
| `setup_options` / `setup_apply` | Valid target Python versions and package managers, then the pinned configuration. |
| `assess` | Baseline vs target resolution: which versions move, which conflict, what enters the manual zone — plus optional risk scoring (CVEs, C-wheel compilation, abandonment). An input to the contract, never the conclusion. |
| `codemods_preview` / `codemods` | Unified diffs of AST rewrites; `write=True` applies them. |
| `env_hint` | Dockerfile recipes and `uv` commands for building the target environment. |
| `migrate` | Runs the preparation phases end-to-end in one call. |

---

## Behavioral Contract Verification

Static type checkers catch signature changes. They cannot tell you that a method started returning an empty generator instead of `None`, that a call now raises `KeyError` where it used to raise nothing, or that a dependency call your code used to make simply stopped happening.

PyMolt ships a zero-dependency runtime tracer that attaches through standard library hooks (`sys.setprofile` / import wrappers) — **no code changes, no decorators, nothing added to your requirements**. It runs inside your app's interpreter, which may well be Python 3.6, and it is pure stdlib on purpose.

1. Run your existing test suite (or the app itself) under the baseline Python to record real interaction shapes.
2. Upgrade dependencies, apply codemods, edit code.
3. Re-run the same command under the target Python.
4. `pymolt verify .` diffs the two and folds every configured oracle into one verdict.

What comes back combines three coverage numbers with a tri-state verdict:

- **confirmed** — dependency calls your run actually exercised, on both sides.
- **BLIND** — calls in the static contact map that your run never touched. This is the honest measure of how much your test suite does *not* cover. A green suite with high BLIND proves very little, and PyMolt says so instead of pretending otherwise.
- **trust %** — confirmed against the static denominator from `contract map`.

Plus the behavioural verdict: which calls changed result, changed raise, or disappeared. `PASS` means no change was observed on a valid, comparable and fully covered contract surface; `FAIL` means a comparable oracle found a change; `INCONCLUSIVE` identifies the exact missing evidence.

The contract also tracks **provenance**: each capture is stamped with its environment, workload, privacy profile, sampling rate and validity. Failed tests, empty traces and malformed artifacts are retained under `contract_traces/diagnostics/`, but never replace active evidence. Re-capturing displaces a valid old recording rather than deleting it.

Live and attach captures default to `--privacy shape`, which records types and structure without scalar values. Test-suite captures default to `values` for exact local comparison. Use `--sample-rate` for high-volume observation; sampled evidence remains `INCONCLUSIVE` for disappearance claims.

For canaries, pass `--policy policy.json --metrics metrics.json`; gates cover error rate, p95/p99 latency, output-shape mismatch, dependency-call drift and dropped events. `--auto-rollback` is explicit opt-in and executes only when a mandatory gate fails. `--shadow shadow.json` classifies read/idempotent operations and suppresses unsafe or unknown writes. Comparator profiles are `--profile exact`, `shape`, or `custom --comparator module:callable`; node-level cascade output can be added with `--cascade report.json`, and golden snapshots with `--golden-before/--golden-after`.

CI exit codes are stable: `0` = passed policy, `1` = incompatibility or failed captured command, `2` = invalid invocation, `3` = environment/service failure, `4` = insufficient evidence.

---

## AST Codemods & Axiom Cloud Hub

Regex replacements break code; large language models frequently hallucinate non-existent API flags. PyMolt uses **LibCST (Concrete Syntax Trees)** paired with **Axiom Cloud Hub** recipes.

- **Non-destructive AST edits**: Preserves your formatting, indentation, and comments exactly as written.
- **Typed Metavariables**: Declarative match/rewrite templates (e.g., `Series_1.append(Series_2)` → `pd.concat([Series_1, Series_2])`).
- **Client Verification**: PyMolt locally re-verifies every rule before applying it to your repository.
- **Transactional apply**: every changed file is prepared and parsed before the first replacement; an interrupted or failed mixed rule/pattern batch rolls all replacements back.
- **Free Public Cloud Hub**: Recipes are fetched on demand from Axiom Cloud Hub. Custom/private on-prem instances are supported via `--endpoint <url>` or `PYMOLT_ENDPOINT`.

---

## Commands Summary

**Proof**

| Command | Description |
|---|---|
| `pymolt verify [dir]` | Unified persisted `PASS` / `FAIL` / `INCONCLUSIVE`; supports exact/shape/custom, golden snapshots, canary policy, safe shadow classification and opt-in rollback. |
| `pymolt contract map [dir]` | Static contact map: every call site into a dependency (the coverage denominator). |
| `pymolt contract capture --when baseline\|post-migration -- <cmd>` | Runs your command and promotes only a valid trace; failed/empty captures remain diagnostic artifacts. |
| `pymolt contract report [dir]` | `PASS` / `FAIL` / `INCONCLUSIVE`, confirmed / BLIND / trust %, and behavioral changes. |
| `pymolt contract diff OLD NEW` | Fold two recordings into a BoundaryDiff directly. |

**Preparation**

| Command | Description |
|---|---|
| `pymolt status [dir]` | Where the project stands in the funnel, and the exact next command. |
| `pymolt doctor [dir]` | Read-only preflight for environments, detected workload, evidence validity and migration receipt. |
| `pymolt scan [dir]` | Monorepo surfaces, Dockerfiles and lockfiles: version divergence and edges. |
| `pymolt setup [dir]` | Interactive setup for target Python, package manager and manifest. |
| `pymolt assess [dir]` | Baseline vs target resolution, CVE deltas, pinned target manifest. |
| `pymolt plan [dir]` | Freeze the exact hash-bound codemod result for review and later apply. |
| `pymolt apply [dir]` | Apply only the saved plan; validates hashes and retains durable rollback data. |
| `pymolt rollback [dir]` | Atomically restore the latest applied plan; protects later edits unless `--force`. |
| `pymolt codemods [dir]` | Focused exact unified preview by default; `--patch FILE` exports it and `--write` uses the durable plan path. |
| `pymolt migrate [dir]` | End-to-end exact preview; `--patch FILE` exports it, while `--write` uses the durable plan path and requires a valid baseline. |
| `pymolt env hint [dir]` | Generates a target `Dockerfile` and `uv` environment commands. |

**Plumbing**

| Command | Description |
|---|---|
| `pymolt mcp` | Starts the stdio Model Context Protocol server for AI coding agents. |
| `pymolt auth login` | Authenticates the CLI for Axiom Cloud Hub recipe access. |

---

## License

PyMolt is open-source software licensed under the [Apache License, Version 2.0](LICENSE).
Axiom Cloud Hub recipes are provided under the Business Source License 1.1 with free public community access.
