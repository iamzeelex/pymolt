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

One path, in order. `pymolt status .` shows at any time where the project is and which command is next; every command also prints that next command.

```bash
cd your-legacy-project
pymolt setup .                                         # manifest, tool, base and target Python
pymolt assess .                                        # resolve the target, write requirements-target.txt
pymolt capture baseline --mode tests -- pytest tests/  # before any code change; cannot be redone later
pymolt plan .                                          # the codemods as a plan; shows the diff, writes no source
pymolt apply .                                         # writes exactly that plan
pymolt capture post-migration --mode tests -- pytest tests/   # in the target environment
pymolt verify .                                        # PASS / FAIL / INCONCLUSIVE
```

`pymolt rollback .` undoes the last `apply`. Instead of `plan` + `apply` you can change the code by hand or hand the project to an AI agent; `verify` compares the two captures the same way (`status` tracks only migrations made with `apply`).

`verify` gives you three numbers that no resolver can: how much of the Axiom-impacted dependency surface your tests actually exercised, how much stayed **BLIND**, and which calls changed behaviour. Its verdict is deliberately tri-state: `PASS`, `FAIL`, or `INCONCLUSIVE`. Missing, stale, opaque, sampled, or non-comparable evidence can never become a green result.

---

## How It Works: The Funnel Feeds The Contract

The two captures bracket the code change. Everything before them prepares it:

```
 scan → setup → assess → capture baseline → plan → apply → capture post-migration → verify
 what    which   resolve   before any         the code change    in the target          what
 is      Python  the       code change        (or by hand)       environment            changed
 there   & tool  target
```

| Step | Command | What it does |
|---|---|---|
| scan | `pymolt scan .` | Manifests, Python-version divergence, dependency edges, and every call site into a dependency. Offline. |
| setup | `pymolt setup .` | Manifest, resolution tool, base and target Python. Options or prompts. |
| assess | `pymolt assess . --risk` | Lock-first resolution of base vs target, CVE deltas, the pinned target manifest, and how to build the target environment. |
| capture | `pymolt capture baseline -- pytest tests/` | Records the calls your code makes into its dependencies. Needs setup. The one step you cannot do later. |
| plan | `pymolt plan .` | The verified codemods as a plan, with the diff. Writes no source. |
| apply | `pymolt apply .` | Writes exactly the plan; refuses it if a planned file changed since. Keeps rollback data. |
| capture | `pymolt capture post-migration -- pytest tests/` | The same recording in the target environment. |
| verify | `pymolt verify .` | Compares the captures: confirmed · BLIND · trust % · what changed. |

**Where is the contact map in this?** `pymolt scan .` lists it: calls, attribute reads, decorators, bases, context managers, constants and protocol operations that cross into a dependency. It is the static denominator of every coverage number. Axiom delta paths focus it automatically after `apply`; full-surface totals remain visible.

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

The tools have the names of the CLI commands. None of them writes source: `plan` saves the plan, and a person applies it with `pymolt apply`.

| Tool | What the agent gets |
|---|---|
| `status` | Where the project stands, whether its environments exist, and the single next tool. |
| `scan` | Project roots, dependency edges, Python-version divergence, and every call site into a dependency, resolved through imports and scopes. Import greps undercount this severalfold. |
| `setup` | With no choices, the valid manifests, tools and Python versions; with them, the saved configuration. |
| `assess` | Base vs target resolution: which versions move, which conflict, what enters the manual zone, how to build the target environment — plus optional risk scoring. An input to the contract, never the conclusion. |
| `capture` | Runs *your* test or app command with a boundary tracer injected — zero edits to the project — and keeps it as the `baseline` or `post-migration` capture. |
| `plan` | The codemods for the target manifest as a saved plan and unified diffs. |
| `verify` | The verdict: PASS / FAIL / INCONCLUSIVE, confirmed / BLIND coverage, trust %, and exactly which dependency calls changed result, changed raise, or disappeared. |

---

## Behavioral Contract Verification

Static type checkers catch signature changes. They cannot tell you that a method started returning an empty generator instead of `None`, that a call now raises `KeyError` where it used to raise nothing, or that a dependency call your code used to make simply stopped happening.

PyMolt ships a zero-dependency runtime tracer that attaches through standard library hooks (`sys.setprofile` / import wrappers) — **no code changes, no decorators, nothing added to your requirements**. It runs inside your app's interpreter, which may well be Python 3.6, and it is pure stdlib on purpose.

1. Run your existing test suite (or the app itself) under the baseline Python to record real interaction shapes.
2. Upgrade dependencies, plan and apply codemods, edit code.
3. Re-run the same command under the target Python.
4. `pymolt verify .` diffs the two and folds every configured oracle into one verdict.

What comes back combines three coverage numbers with a tri-state verdict:

- **confirmed** — dependency calls your run actually exercised, on both sides.
- **BLIND** — calls in the static contact map that your run never touched. This is the honest measure of how much your test suite does *not* cover. A green suite with high BLIND proves very little, and PyMolt says so instead of pretending otherwise.
- **trust %** — confirmed against the static denominator `scan` lists.

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
- **Free Public Cloud Hub**: Recipes are fetched on demand from Axiom Cloud Hub. Custom/private on-prem instances are supported via `pymolt axiom endpoint <url>`, `--endpoint <url>` or `PYMOLT_AXIOM_ENDPOINT`.

---

## Commands Summary

**Migration**, in the order you run them:

| Command | Description |
|---|---|
| `pymolt status [dir]` | Where the project stands, whether its environments exist, and the next command. |
| `pymolt scan [dir]` | Manifests, Python-version divergence, dependency edges and call sites into dependencies. |
| `pymolt setup [dir]` | Manifest, tool, base and target Python: `--manifest`, `--tool`, `--base-python`, `--target-python`, `--container`, `--target-env`, or prompts. |
| `pymolt assess [dir]` | Base vs target resolution, CVE deltas, pinned target manifest, target-environment commands. |
| `pymolt capture baseline\|post-migration -- <cmd>` | Runs your command and keeps only a valid recording; failed/empty captures remain diagnostic artifacts. |
| `pymolt plan [dir]` | The codemods as a plan with the diff; `--patch FILE` exports it. Writes no source. |
| `pymolt apply [dir]` | Writes only the saved plan; validates hashes and retains rollback data. |
| `pymolt verify [dir]` | `PASS` / `FAIL` / `INCONCLUSIVE`, confirmed / BLIND / trust %, and behavioural changes; also golden snapshots, canary policy, shadow classification, opt-in rollback. `--baseline`/`--post-migration` compare other recordings (not saved). |
| `pymolt rollback [dir]` | Restores the sources of the last apply; protects later edits unless `--force`. |

**Tools**

| Command | Description |
|---|---|
| `pymolt axiom login\|logout\|status\|endpoint\|clear-cache` | Axiom access: token, endpoint, local cache. |
| `pymolt mcp` | Starts the stdio Model Context Protocol server for AI coding agents. |
| `pymolt succession [dir]`, `pymolt forks REPO` | Experimental: framework succession (`--plan` for `apply`) and fork triage. |

Commands removed in this version print their replacement: `doctor` → `status`; `contract capture/trace/boundary/export-watcher` → `capture`; `contract report/diff` → `verify`; `contract map` → `scan`; `migrate`, `codemods` → `plan` + `apply`; `env hint` → `assess`; `auth`, `config`, `login`, `logout` → `axiom`.

---

## License

PyMolt is open-source software licensed under the [Apache License, Version 2.0](LICENSE).
Axiom Cloud Hub recipes are provided under the Business Source License 1.1 with free public community access.
