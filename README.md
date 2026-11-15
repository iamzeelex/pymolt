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
# or with the optional MCP server (for clients without a shell):
uv tool install "pymolt[mcp]"
```

One path, in order. `pymolt status .` shows at any time where the project is and which command is next; every command also prints that next command.

```bash
cd your-legacy-project
pymolt setup .                                         # manifest, tool, base and target Python
pymolt assess .                                        # resolve the target, write requirements-target.txt
pymolt capture baseline --mode tests -- pytest tests/  # before any code change; cannot be redone later
pymolt plan .                                          # API changes, codemods and predicted risks; shows the diff, writes no source
pymolt apply .                                         # writes exactly that plan
pymolt capture post-migration --mode tests -- pytest tests/   # in the target environment
pymolt verify .                                        # PASS / FAIL / INCONCLUSIVE
```

`pymolt rollback .` undoes the last `apply`. Instead of `plan` + `apply` you can change the code by hand or hand the project to an AI agent; `verify` compares the two captures the same way (`status` tracks only migrations made with `apply`).

`verify` gives you three numbers that no resolver can: how much of the dependency surface touched by the plan's API changes your tests actually exercised, how much stayed **BLIND**, and which calls changed behaviour. Its verdict is deliberately tri-state: `PASS`, `FAIL`, or `INCONCLUSIVE`. Missing, stale, opaque, sampled, or non-comparable evidence can never become a green result.

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
| plan | `pymolt plan .` | The API changes between base and target, the codemods for them and the predicted risks per call site, as a plan with the diff. Writes no source. |
| apply | `pymolt apply .` | Writes exactly the plan; refuses it if a planned file changed since. Keeps rollback data. |
| capture | `pymolt capture post-migration -- pytest tests/` | The same recording in the target environment. |
| verify | `pymolt verify .` | Compares the captures: confirmed · BLIND · trust % · what changed. |

**Where is the contact map in this?** `pymolt scan .` lists it: calls, attribute reads, decorators, bases, context managers, constants and protocol operations that cross into a dependency. It is the static denominator of every coverage number. The plan's API changes focus it automatically after `apply`; full-surface totals remain visible.

---

## Built for AI Agents

Agents drive the CLI. The seven report commands (`status`, `scan`, `setup`, `assess`, `capture`, `plan`, `verify`) take `--agent`, which prints the compact view: the summary, true counts, capped lists with the path of the full report, and flags that say whether a list is complete. `--json` prints the full payload.

```bash
pymolt assess . --agent
pymolt verify . --json
```

The Claude Code plugin (`plugin/`) ships a skill and an agent that use these commands. It starts no MCP server.

Give your agent the contract commands in particular. It can already resolve dependencies and read manifests on its own; what it cannot do is tell you whether the migration changed behaviour, or even count its own dependency contact surface correctly.

### MCP server (optional)

For clients without a shell (Claude Desktop, ChatGPT and similar), PyMolt also runs as a Model Context Protocol server. It is a thin adapter over the same view layer: each tool returns exactly what `pymolt <command> --agent` prints for the same project, and has no logic of its own. Install the extra (it pins `mcp<2`) and add the server to your client's config (e.g. `claude_desktop_config.json`):

```bash
uv tool install "pymolt[mcp]"
```

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

The 7 tools have the names of the CLI commands. None of them writes source: `plan` saves the plan, and a person applies it with `pymolt apply`. Lists that do not fit are cut, with the true total stated; the full report goes to `.pymolt/agent/<command>.json`.

| Tool | What the agent gets |
|---|---|
| `status` | Where the project stands, whether its environments exist, and the single next tool. |
| `scan` | Project roots, dependency edges, Python-version divergence, and every call site into a dependency, resolved through imports and scopes. Import greps undercount this severalfold. |
| `setup` | With no choices, the valid manifests, tools and Python versions; with them, the saved configuration. |
| `assess` | Base vs target resolution: which versions move, which conflict, what enters the manual zone, how to build the target environment, plus optional risk scoring. An input to the contract, never the conclusion. |
| `capture` | Runs *your* test or app command with a boundary tracer injected (zero edits to the project) and keeps it as the `baseline` or `post-migration` capture. Runs a process on your machine, so it requires `PYMOLT_MCP_ALLOW_EXEC=1` in the server's environment. |
| `plan` | The API changes, the codemods for the target manifest and the predicted risks, as a saved plan and unified diffs. |
| `verify` | The verdict: PASS / FAIL / INCONCLUSIVE, confirmed / BLIND coverage, trust %, which predicted risks were hit, and exactly which dependency calls changed result, changed raise, or disappeared. |

---

## Behavioural Contract Verification

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

## Codemods and API changes

`pymolt plan` finds which APIs your code uses changed between the base and the target versions of each dependency, and rewrites what it can with **LibCST** codemods.

- **Computed locally**: the API changes come from the two environments, only for the symbols your project touches: removed, moved, signature, default and deprecated. Successor hints come from the warnings the baseline run recorded, the old version's docstrings and the knowledge shipped with pymolt. With `plan --online`, intermediate releases are read from PyPI as well; only package names and versions leave the machine.
- **Environments**: the old side is the baseline capture's interpreter, the new side your target environment. The from-versions are the ones the baseline capture actually ran (it records them), not what an unpinned manifest resolves to today; the plan states each correction. If there is none, pymolt builds a throwaway one under `.pymolt/cache/envs/` from uv's cache. Without `--online` it downloads nothing, and a package it cannot build is reported as inconclusive, never as "no changes".
- **Predicted risks**: the plan lists the risks per call site, as "fixed by the plan" or "needs attention", for example a default that changed on a parameter your call leaves unset. A default that moves to or from `None` or a sentinel is decided at call time, so it is medium unless the site's own baseline warning names it. `verify` then reports each prediction as hit, prevented by the plan (the rewrite kept the behaviour), miss or not exercised.
- **Formatting kept**: indentation and comments stay as written.
- **Templates**: a codemod is a match/rewrite template, e.g. `Series_1.append(Series_2)` → `pd.concat([Series_1, Series_2])`.
- **Checked locally**: pymolt re-checks every rule before it goes into a plan; rules it cannot check are listed for manual review.
- **All or nothing**: `apply` prepares and parses every changed file before the first write; a failed apply restores them all.
- **Remote Axiom (optional)**: a remote Axiom service is used only if you configure one, with `--endpoint <url>` or `PYMOLT_AXIOM_ENDPOINT` (token in `PYMOLT_API_TOKEN`). Only package names and versions are sent, never code.

Evidence: on 22 API changes in flask, werkzeug and pandas, the local computation reaches recall 0.95 and precision 1.00, against 0.55 and 0.63 for Axiom's cached data. See [bench/delta_parity/RESULTS.md](bench/delta_parity/RESULTS.md).

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
| `pymolt plan [dir]` | API changes, codemods and predicted risks as a plan with the diff; `--online` reads intermediate releases from PyPI, `--endpoint URL` uses a remote Axiom, `--patch FILE` exports the diff. Writes no source. |
| `pymolt apply [dir]` | Writes only the saved plan; validates hashes and retains rollback data. |
| `pymolt verify [dir]` | `PASS` / `FAIL` / `INCONCLUSIVE`, confirmed / BLIND / trust %, and behavioural changes; also golden snapshots, canary policy, shadow classification, opt-in rollback. `--baseline`/`--post-migration` compare other recordings (not saved). |
| `pymolt rollback [dir]` | Restores the sources of the last apply; protects later edits unless `--force`. |

**Tools**

| Command | Description |
|---|---|
| `pymolt mcp` | Starts the optional stdio Model Context Protocol server (needs `pymolt[mcp]`). |
| `pymolt succession [dir]`, `pymolt forks REPO` | Experimental: framework succession (`--plan` for `apply`) and fork triage. |

Commands removed in this version print their replacement: `doctor` → `status`; `contract capture/trace/boundary/export-watcher` → `capture`; `contract report/diff` → `verify`; `contract map` → `scan`; `migrate`, `codemods` → `plan` + `apply`; `env hint` → `assess`; `axiom` (API changes are computed locally by `plan`; for a remote provider use `plan --endpoint URL` or `PYMOLT_AXIOM_ENDPOINT`); `auth`, `config`, `login`, `logout` → `PYMOLT_API_TOKEN` (sent only to an explicitly configured endpoint) or `plan`.

---

## Glossary

One word per thing, in the CLI, the MCP tools and these docs.

| Term | Meaning |
|---|---|
| **base** / **target** | The project as it is today (base Python, base dependencies) and as it will be after the migration. |
| **manifest** | The file that declares dependencies: `requirements.txt`, `pyproject.toml`, `environment.yml`… setup picks one; assess writes the **target manifest** (`requirements-target.txt` or `environment-target.yml`). |
| **capture** | The calls your code made into its dependencies while a command ran, recorded without changing the code. |
| **baseline** | The capture taken before the migration. Nothing else is called baseline. |
| **post-migration** | The capture taken after the migration, in the target environment. |
| **call site** | A place in your code that calls into a dependency. `scan` lists them (the contact map). |
| **BLIND** | A call site that no capture exercised. |
| **codemod** | A rewrite for one dependency version change, from the local API-change facts or the knowledge shipped with pymolt. |
| **API change** | A symbol your code uses that is removed, moved, or changed in signature, default or deprecation status between base and target. `plan` computes them locally. |
| **plan** | The API changes, the codemods applied to your files as a diff, and the predicted risks, saved for `apply`. |
| **verdict** | `PASS`, `FAIL` or `INCONCLUSIVE`, from `verify`. |
| **base tier** | How far the base dependencies can be trusted: gold (pinned), silver (tests pass), bronze (unvalidated), none. JSON key: `baseline_tier`. |
| **Axiom** | Optional remote provider of API changes and codemods; used only if configured with `--endpoint`. |

---

## License

PyMolt is open-source software licensed under the [Apache License, Version 2.0](LICENSE).
