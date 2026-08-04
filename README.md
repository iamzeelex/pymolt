# PyMolt (pymolt)

> **⚠️ Personal Project — Early Development**
>
> This project is currently in early development and is maintained as a personal experiment. It is **not intended for production use** and may contain bugs, incomplete features, or breaking changes.

🐍 **PyMolt** is a Python migration CLI tool designed to make dependency upgrades and codebase migrations predictable, visible, and controllable.

The migration funnel is **scan → setup → assess → contract** (plus **codemods**), each runnable
from both a Typer CLI and a Textual TUI, locally and offline-first (the only network steps —
`assess --risk` and codemods — are opt-in and cached).

## Installation

PyMolt is a Python CLI (Python 3.12+). The one-liner installs it as an isolated tool via
[`uv`](https://docs.astral.sh/uv/) — uv provisions a matching Python for you, so you don't need
3.12 already, and nothing lands in your system Python:

```bash
curl -fsSL https://pymolt.zeelex.me/install.sh | sh
```

Or install it yourself with any Python tool installer:

```bash
uv tool install pymolt      # recommended
pipx install pymolt
pip install --user pymolt
```

Then:

```bash
pymolt --version
pymolt scan .               # or just `pymolt` to open the TUI cockpit
```

## Project Structure

PyMolt uses a structured, modular design targeting a single navigable dependency graph substrate layered with static analysis, dependency resolution, migration-risk scoring, codemods, and behavioral verification.

```text
pymolt/
├── core/            # The Substrate (Graph, Node, Edge, Serialization)
├── discovery/       # Surface map: monorepo roots, Dockerfile/tox/nox, version evidence
├── ingestion/       # World-as-found (uv/conda/pip inputs, lock-first, name maps, config)
├── inventory/       # Dependency edge classification + code-usage intersection
├── setup/           # Manifest / toolset / base+target Python / container selection
├── assess/          # Lock-first resolve, baseline vs target compare, feasibility
├── risk/            # Migration risk: CVEs (OSV), wheels/compilation, abandonment
├── codemods/        # Fetch patterns/rules from Axiom Graph, apply with LibCST
├── verify/          # Behavioral contract: static contact map × dynamic trace, diff
└── interfaces/      # CLI (Typer) and TUI (Textual) entry points
```

## Commands

| Command | What it does |
|---|---|
| `pymolt status [dir]` | **Where this migration stands**, and the one command to run next. Reads only what is on disk (config, captures, target manifest) — runs nothing, writes nothing. Flags evidence that is *empty* or *stale* (captured against a manifest you have since changed), because that is worse than evidence that is missing. |
| `pymolt scan [dir]` | **Phase 1 — the primary as-is scan.** Gathers everything scattered, in one offline pass: every Python surface (monorepo roots, Dockerfiles with multi-stage / ARG / `COPY --from`, tox/nox), where the Python version **diverges** across sources, and the dependency **edges** (dev/extras, `-r`/`-c`, private indexes, git/url/local). No target, no resolution. |
| `pymolt setup [dir]` | Configure the migration: pick the manifest, toolset, base/target Python, container — writes `.pymolt/env_config.json`. |
| `pymolt assess [dir]` | Resolve the dependency graph **lock-first**, compare baseline vs target Python, flag conflicts/manual-zone, rank risk, and write a pinned target manifest (with hashes). Flags: `--target-python`, `--risk`, `--json`, `--no-cache`, `--no-hashes`. |
| `pymolt codemods [dir]` | Fetch codemod patterns/rules for the upgraded dependencies from Axiom Graph and apply them with LibCST (binding-aware, dry-run by default; review each change). Requires an account ([pymolt.zeelex.me](https://pymolt.zeelex.me)) — a **recipe** permanently unlocks one package jump (canonical pair, e.g. `pandas 1.x → 2.x`). Priced by delivered weight (public formula: renames ×1, behavioral rewrites ×3): S ≤5 wt = $5, M 6–25 wt = $10, L >25 wt = $25. The metadata quote is a hard cap; billing follows what survives local re-verification, and an empty bundle costs nothing. First recipe free — any class. Re-fetches are free forever; the CLI shows class & price and asks before spending. Only dependency names/versions cross the wire — never your code. |
| `pymolt contract …` | Behavioral contract: static contact map × dynamic trace, with a guided baseline → post-migration diff (see below). |
| `pymolt` / `pymolt ui [dir]` | Open the interactive TUI cockpit — every phase, same engine, with review cards and guided capture. |
| `pymolt forks OWNER/REPO` | **Strategic**: rank the live/ported successor forks of a dead repo (recency, stars, divergence, an "already ported?" signal). Offline by default; `--online` hits the GitHub API and caches. |
| `pymolt succession [dir]` | **Strategic**: find where a dead framework went (e.g. `keras → tensorflow.keras`). In-place shims are applied via LibCST (dry-run by default); a transplant (`keras → torch`) is shown as a plan, never auto-applied. |
| `pymolt env hint [dir]` | Print a recipe for the target environment — a derived `Dockerfile.pymolt-target` and the `uv` commands — plus the post-migration capture command. Builds nothing. |
| `pymolt login` / `logout` | Store or remove the API token used by `codemods` ([pymolt.zeelex.me](https://pymolt.zeelex.me) → Account → API tokens). `PYMOLT_API_TOKEN` overrides it. |
| `pymolt mcp` | Run the stdio MCP server — the funnel as agent tools (see below). |

### Migration workflow

```bash
# 1. As-is scan: what do I have? (surfaces, version divergence, dependency edges) — all in one pass
pymolt scan .

# 2. Configure (manifest / toolset / base + target Python)
pymolt setup .

# 3. Assess feasibility against the target Python: lock-first resolve, compare, rank risk
pymolt assess . --target-python 3.12 --risk      # add --json for CI

# 4. Confirm behaviour is unchanged across the version bump
pymolt contract boundary
```

**Risk Assessment (`assess --risk`)** is opt-in and network-backed (cached under
`.pymolt_cache/`): it pulls **CVEs** (OSV.dev, as a before/after-migration delta),
**wheel/compilation** status (PyPI — `sdist-only` means a C build) and **abandonment**
(last-release recency), scoring each package into a HIGH/MEDIUM/LOW `RiskTier`.

### Cases

- **"I inherited a service I don't know."** A migration is often done by someone with no context
  (only docs / README / commits). `scan` collects all the scattered facts in one offline pass —
  every Python surface, where the version **diverges** (e.g. `Dockerfile` says 3.6 but CI says
  3.11), and the dependency edges — each with provenance and a confidence level. You understand
  the project before touching it.
- **"Will it even run on the new Python?"** `setup` picks the target; `assess` resolves
  **lock-first**, compares baseline vs target, isolates the blocker if it can't, and ranks risk
  (CVE / needs-compilation / abandoned).
- **"Will behaviour break?"** Resolution says it *installs*; `contract` says whether it still
  *behaves*. `map` (static) + `trace` (dynamic) + `report` show what's confirmed, what's BLIND,
  and `--probe-python` actively re-invokes contacts under the new version to flag changes.
- **"In CI / for a tool."** Every command takes `--json`; resolution and risk lookups are cached
  and offline-by-default.

## For AI agents (Claude Code, ChatGPT, local models)

PyMolt is built to be driven by agents: every report command takes `--json`, output is
deterministic, and every fact carries provenance + a confidence level — a fact
with receipts can be trusted without re-verification.

**The output contract** (what you can rely on when parsing):

- **stdout carries only the answer** — the rendered report, or the `--json` payload.
  Warnings, progress and errors go to **stderr**, so `pymolt … --json | jq` never
  chokes on a diagnostic and never silently swallows one.
- **a failure under `--json` is still JSON**: `{"ok": false, "error": …, "hint": …}`
  on stdout. One shape to parse, whatever happened.
- **exit codes mean something**:

  | code | meaning |
  |---|---|
  | `0` | ran; the answer is positive or informational |
  | `1` | ran; the answer is **negative** — behavior changed (`contract diff`), or a prompt was declined. CI branches on this |
  | `2` | wrong invocation or inputs — no such directory, no manifest, missing required option. Nothing was attempted |
  | `3` | the environment failed — a service is down, docker is missing, an optional extra isn't installed |

- **reads stay reads**: `pymolt assess` writes a pinned target manifest by design;
  pass `--no-write` when you only want the answer and must not touch the tree
  (the JSON always discloses the path it wrote in `target_manifest_path`). A frontier model can do a
migration without PyMolt; it will just burn ~100× the tokens rediscovering the
repo and trust its memory of an API that moved since training. PyMolt feeds any
model — a local 7B or a frontier one — the same verified facts.

Paste this into your repo's `AGENTS.md` / `CLAUDE.md`:

```markdown
## Python migrations
This repo uses PyMolt for migration facts — do not rediscover them manually.
- Repo surfaces / versions / dependency edges: `pymolt scan . --json`
  (never read Dockerfiles/lockfiles/tox configs by hand for this).
- Feasibility + risk on a target Python:
  `pymolt assess . --target-python <X.Y> --risk --json --no-write` — real resolver,
  live CVE data; never guess package versions from memory. Drop `--no-write` when
  you actually want the pinned `requirements-target.txt` written.
- Behavioral ground truth across a dependency bump:
  `pymolt contract report . --json`. After editing code, re-run
  `pymolt contract capture --when post-migration --mode tests -- <test cmd>`
  and re-check the report until no `result_changed` remains.
- Codemod recipes (Axiom Graph) cost money — always show the quote and get
  explicit human approval before a slot is consumed.
```

The MCP server ships in the CLI — `pymolt mcp` exposes scan / setup / assess /
contract / codemods-preview as native tools for any MCP client:

```bash
uv tool install 'pymolt[mcp]'          # or: pipx install 'pymolt[mcp]'
claude mcp add pymolt -- pymolt mcp    # Claude Code
# ChatGPT, Cursor, any MCP client: stdio command = `pymolt mcp`
```

A Claude Code skill lives in `integrations/claude-code/skills/pymolt-migrations/`,
and a machine-readable summary at <https://pymolt.zeelex.me/llms.txt>.

## Behavioral Verification (`verify/`)

Static analysis reasons about *declared* surface. `verify/` answers the question it cannot:
**does the code still behave the same after a dependency version changes?** It observes the
actual calls crossing from your code into a target dependency under two versions and diffs
them, emitting typed, honesty-marked facts (it decides nothing — an engineer reads the verdict).

**The tool runs entirely _outside_ the target project.** Nothing is added to your codebase. The
boundary tracer is injected via `sitecustomize`/`.pth` + environment variables, and the
injectable runtime is **pure standard library, Python 3.6+** (no `pymolt`, no `pydantic`), so it
loads in any interpreter — including old ones inside a container.

### How it works

- **Injected runtime** (`boundary_tracer`, `_normalize`, `_backend`, `_sinks`, `_wrap`) — loaded
  *inside* the target app's interpreter. Records every call into the target dependency
  (`qualname`, bound args, normalized return / raised type) **plus the call site in your code**
  (`where`: `file`, `line`, `func`) to a streaming JSONL artifact. Two observation backends:
  - `setprofile` (default, 3.6–3.11) — complete coverage, but taxes *every* call in the process
    (≈10× on hot paths) — best for tests/staging, not always-on prod.
  - `wrap` (`--wrap sym1,sym2`) — monkeypatches *only* the named dependency symbols — module
    functions, classes (via `__init__`), and methods (`Class.method`, incl. static/class methods)
    — with transparent recorders, installed via an import hook so it survives `from dep import
    name`. Overhead is paid only on the wrapped symbols (≈1× on the rest of the process) and it
    captures Python `raise` faithfully — the prod-grade path. The symbol list pairs with `verify
    gaps`. Symbols it can't patch (C/extension-type methods) are surfaced as honesty `skipped`.
  - `hybrid` (`--wrap … --backend hybrid`) — `wrap` for the listed symbols plus `setprofile` for
    completeness (the C-remainder and any dynamic/aliased usage), de-duplicated. Pays setprofile's
    process tax; the audit/completeness mode. `sys.monitoring` (3.12+) is a further extension point.
- **Host core** (`cascade`, `coverage`, `golden_master`, `diff`, `models`) — consumes the
  artifacts and folds them into a typed `BoundaryDiff` / `VerifyReport`. The cascade runs
  cheapest-first (tests → golden master → boundary trace) with coverage-driven scope.

A `BoundaryDiff` categorizes contacts as `disappeared` / `result_changed` / `raise_changed`
(→ `BEHAVIOR_CHANGED`), `appeared` (info), and `skipped_opaque` (comparison honestly declined
→ `NEEDS_HUMAN`).

### CLI

```text
pymolt contract map DIR [--json]                 # STATIC: where our code calls into third-party deps (cheap, no runtime)
pymolt contract capture --when W --mode M -- CMD # guided capture: name the trace baseline|post-migration (see below)
pymolt contract export-watcher DEST              # write the standalone stdlib bundle (no deps)
pymolt contract trace --out F -- CMD             # run CMD with the watcher injected, → JSONL (raw, unnamed)
pymolt contract diff OLD NEW [--contract] [--json] [--full]   # fold two recordings → BoundaryDiff (exit 1 if changed)
pymolt contract boundary                         # interactive: capture both versions + diff
pymolt contract report DIR [--trace T] [--against OLD] [--json]   # unified report (see below)
```

### The guided flow: named captures (baseline / post-migration)

`capture` is the migration-semantic front door over `trace`: instead of tracking raw JSONL
paths yourself, each capture is **named** for its place in the migration and persisted to
`.pymolt/contract_state.json` — `report` then needs no flags at all.

```bash
# 1. BEFORE migrating: capture the baseline (run your suite, or your app)
pymolt contract capture --when baseline --mode tests -- pytest tests/

# 2. Migrate (assess → codemods → your edits), until the code runs again.

# 3. AFTER: capture the post-migration behavior the same way
pymolt contract capture --when post-migration --mode tests -- pytest tests/

# 4. The report auto-diffs baseline → post-migration (no paths to remember):
pymolt contract report .
```

**Captures are never destroyed.** A baseline records an environment that stops
existing the moment you migrate — it cannot be retaken later at any price. So
re-capturing a filled slot *displaces* the old recording into
`.pymolt/contract_traces/archive/` (named for when it was taken) rather than
overwriting it, and every capture is stamped with a fingerprint of the world it
recorded (manifest contents, base Python, toolset, container). When `report`
later finds that fingerprint no longer matches the project, it says so above the
verdict instead of quietly diffing against a recording of a different world.
`pymolt status` shows the same staleness at a glance.

Three capture modes: `--mode tests` (pymolt runs your test command and waits), `--mode command`
(pymolt runs your app; Ctrl+C keeps whatever was captured), and `--mode attach` (for a process
YOU run — a server you want to click around in: it prints a ready `PYTHONPATH=… PYMOLT_TRACE_*=…`
line to prepend to your own command, then `--collect` finalizes whatever it wrote). Re-capturing
an already-filled slot asks before overwriting (`--force` for scripts/CI). The TUI's Contract
panel drives this same flow with slot cards and a guided modal — same state file, interchangeable.

### The contract report

`contract report` folds the two axes into one honest picture:

- **Coverage of the contract** — the static contact map (`map`: every dependency symbol your
  code *could* call — the denominator) versus a dynamic trace (what actually ran — the
  numerator). Each symbol is **confirmed** (static ∧ observed), **BLIND** (static, never
  observed → a sandbox candidate), or **dynamic-only** (observed but the static map missed it →
  static under-approximation via `getattr`/monkeypatch). `trust` = confirmed / static.
- **Sandbox probe** (optional `--probe-python TARGET_VENV/bin/python`) — actively re-invokes each
  captured contact whose inputs are reconstructible **under the target dependency version** and
  reports per symbol: `stable`, `CHANGED`, `error`, or `opaque-inputs` (inputs the normalized
  capture can't rebuild — those need a `dill` snapshot or a fork from live state).
- **Version diff** (optional `--against`) — two recordings (old vs new dependency version) folded
  into a `BoundaryDiff`; a behavior change is only trustworthy over the area the dynamics covered.

When `--trace` is omitted, `report` auto-sources from the named captures in
`.pymolt/contract_state.json`: post-migration becomes the trace, baseline becomes `--against`
(both captured → the before/after diff comes for free). Explicit flags always override.

The boundary tracer only sees behavior where code actually runs — you cannot fabricate behavior
for code that never executed, but the **BLIND** list makes the unexercised contacts explicit:
what to write tests for, exercise under the Mode-B watcher, or probe in isolation with the
sandbox. This subsumes the old standalone `gaps` command — blind zones are now a section here.

```bash
# the contract sub-flow: static map → dynamic trace → unified report (coverage + probe + diff)
pymolt contract map .                                            # 1. STATIC denominator (no runtime)
pymolt contract trace --target flask --out new.jsonl -- new-venv/bin/python app.py   # 2. DYNAMIC numerator
pymolt contract report . --trace new.jsonl --probe-python new-venv/bin/python \
                         --against old.jsonl                     # 3. fold: confirmed/BLIND + probe + version diff
```

**The static ↔ dynamic division of labour** (why both): static is the cheap *denominator* — every
contact that **could** run, found without executing anything, plus reachability. Dynamic is the
deep but **partial** witness — only what **did** run, with real values. `gaps` = static − dynamic
(the BLIND zone). The sandbox then *actively* runs the unreached/uncertain points in isolation —
**re-invoke under the new version** (replays a captured contact in the target venv) or, for points
you can only reach from a live process state, **fork from live state** (CoW: the child runs the
blind branch with the real in-scope objects; crash/hang/mutation stays in the child).

(The boundary tracer aggregates by `(qualname, inputs)`, so a path exercised twice increments a
count rather than duplicating data — tested areas aren't recorded redundantly.)

### Interaction contract: `diff --contract`

`diff` defaults to comparing concrete values, which flags volatile data (a venv path, a
User-Agent, a timestamp) as a change. `--contract` instead compares the **interaction shape** —
did it return or raise, the result's type/structure — re-keying contacts by `(qualname,
input-shape)`. A function that returned a `str` still returning a `str` is contract-stable even
if the string differs; a `dict{a:int}` becoming `dict{a:int,b:int}` or an `int` becoming a
`str` is flagged. This is the migration-grade "does behavior still hold?" view; concrete-value
comparison stays available for cases where the data itself is the contract.

`trace` options:

```text
--target/-t   dependency (far) side: 'all'/'*' (every installed dependency, DEFAULT),
              'flask' (one), or 'flask,werkzeug' (several)
--source/-s   YOUR code (near side, caller): e.g. 'flasgger'. Default: auto-detect
--exclude/-x  comma-separated top-levels to skip (e.g. a non-editable app): -x flasgger
--include-internal   also record internal dependency<->dependency calls (full audit)
--container/-c  run CMD inside an ALREADY-RUNNING container via `docker exec` (no docker run)
--workdir/-w    working dir inside the container (default: image WORKDIR)
```

**Only the boundary `your code -> dependency` is recorded** — internal dependency↔dependency
traffic (e.g. `flask -> werkzeug`) is *not*, since that is not your contract. The near side
(your code) is auto-detected (anything not an installed dependency and not stdlib), or named
explicitly with `--source` when auto-detection is ambiguous (e.g. a test harness around a
library). `--target` defaults to **all installed dependencies**, so one run captures your
project's whole-environment boundary at once; `--include-internal` widens it to everything for
a deep audit.

### Example — tracing `flask` across two versions (zero edits to the app)

```bash
# capture the dependency boundary under the old and new dependency versions
pymolt contract trace --target flask --out old.jsonl -- /path/to/old-venv/bin/python app.py
pymolt contract trace --target flask --out new.jsonl -- /path/to/new-venv/bin/python app.py

# diff them
pymolt contract diff old.jsonl new.jsonl
```

For an isolated / legacy interpreter (e.g. a Python 3.6 devcontainer), drop the standalone
bundle on `PYTHONPATH` and let the watcher activate from the environment — no pymolt install
required in the target:

```bash
pymolt contract export-watcher ./bundle
PYTHONPATH=./bundle PYMOLT_TRACE_TARGET=flask \
PYMOLT_TRACE_OUT=/tmp/trace-{pid}.jsonl \
  python -m pytest tests/        # the target app/suite is never modified
```

Environment variables (watcher / Mode B): `PYMOLT_TRACE_TARGET` (`all`/`*`, one prefix, or a
comma list; absent ⇒ no-op), `PYMOLT_TRACE_SOURCE` (near side / your code; default auto-detect),
`PYMOLT_TRACE_EXCLUDE` (comma-separated top-levels to skip), `PYMOLT_TRACE_INTERNAL` (`1` to
include dep↔dep traffic), `PYMOLT_TRACE_OUT` (`{pid}` template), `PYMOLT_TRACE_BACKEND`
(`auto`|`setprofile`|`monitoring`). See [`docs/TESTING.md`](docs/TESTING.md) for end-to-end
scenarios against real fixtures.
