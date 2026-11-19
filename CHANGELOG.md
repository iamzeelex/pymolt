# Changelog

All notable changes to pymolt. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[PEP 440](https://peps.python.org/pep-0440/) as described in
[docs/RELEASING.md](docs/RELEASING.md).

## [Unreleased]

### Breaking

- The CLI has 12 commands instead of 31. A removed command prints its replacement and exits
  with code 2:
  - `doctor`: use `status`, which now shows the environment checks (`checks` in `--json`).
  - `contract capture`, `contract trace`, `contract boundary`, `contract export-watcher`: use
    `capture`.
  - `contract report`, `contract diff`: use `verify`, with `--baseline` and `--post-migration`
    for recordings other than the captures. Two equal recordings with no static contacts are
    INCONCLUSIVE, where `contract diff` said OK.
  - `contract map`: use `scan` (`contact_map` in `--json`).
  - `migrate`, `codemods`: use `plan`, then `apply`.
  - `env hint`: use `assess`, which ends with the commands that build the target environment
    (`target_hint` in `--json`).
- `capture` takes the side as an argument: `pymolt capture baseline -- CMD` replaces
  `pymolt contract capture --when baseline -- CMD`.
- `capture` refuses to run before `setup`, because such a capture records no environment. Run
  `pymolt setup` first; `status` marks an older capture without an environment as stale.
- Renamed options. The old spellings still parse in 0.2 but are no longer listed:
  `assess --source-manifest` is `--manifest`, `assess --current-python` is `--base-python`,
  `assess -c` is `--upgrade-constraint`, and `plan --target-file` is `--target-manifest`.
- `succession --write` is removed: save the in-place codemods with `succession --plan`, then
  run `pymolt apply`.
- `plan` and `succession` compute everything on this machine and contact no service. The
  `login`, `logout`, `auth` and `config` commands are gone with no replacement, and so are the
  options and settings that named a service: a service URL, a saved endpoint and a sign-in
  token.
- `apply` also writes the target's pins into a requirements manifest: each `name==version` line
  without `--hash` for a package the target pins at another version. See the change with
  `apply --dry-run`; leave it out with `apply --step ID --skip --reason TEXT`; `rollback`
  restores the lines.
- `apply` exits 1, and leaves nothing written, when a changed file that compiled before no
  longer compiles on the target Python (or on the base, where the change claims to run there),
  or when `apply --smoke` finds a module that imported before and fails after. Running `apply`
  again on an applied plan exits 0 and writes nothing.
- After `apply`, `verify` judges the whole recorded surface, as it does without a plan: a
  changed call anywhere fails, and PASS needs every static symbol exercised, not only those on
  the plan's API paths. Under `contract` in `verify --json`, `confirmed`, `blind`,
  `dynamic_only` and `static_targets` count the whole surface; the plan's paths are in the
  `impacted_*` keys. A migration that passed with 0.1.0 can now be INCONCLUSIVE or FAIL.
- Some differences that failed with 0.1.0 are now review items, so the verdict is INCONCLUSIVE
  (exit code 4): a different value on a line you edited by hand, an input that changed with no
  visible cause on an unedited line, and a difference at a call that reads an aspect of the
  environment that differs between the two captures. A new raise and a changed raise type
  still fail. In CI, treat exit code 4 as not passed.
- `verify --json` no longer has `contract.trust`, `trust_*` or `all_trust`, and there is no
  alias. Read `coverage` and `recall`, with `coverage_granularity` and the `contacts_*` counts.
  `impacted_trust` is replaced by `impacted_coverage` and `impacted_recall`; the identity
  diagnostics are `identity_tiers`.
- The MCP server has 9 tools instead of 12, named after the CLI commands: `setup_options` and
  `setup_apply` are `setup`, `contract_map` is `scan`, `contract_capture` is `capture`,
  `contract_report` is `verify`, `codemods_preview`, `codemods` and `migrate` are `plan`, and
  `env_hint` is `assess`. Each tool returns what `pymolt <command> --agent` prints, so every
  tool's `data` differs from 0.1.0. The new `apply` and `rollback` tools write nothing unless
  the server runs with `PYMOLT_MCP_ALLOW_WRITE=1`.
- The migration plan is schema 2 (`"schema_version": 2`), a list of ordered steps. pymolt reads
  a 0.1.0 plan as one step, `v1`; pymolt 0.1.0 cannot read a schema-2 plan. Run `pymolt plan`
  again to get steps.
- `.pymolt/migration_state.json` is schema 2: every run and step, with the 0.1.0 fields kept as
  a summary of the latest run. A 0.1.0 file still loads; its first save keeps a copy in
  `.pymolt/archive/`.
- `.pymolt/contract_state.json` stores `trace_path`, `metadata_path`, `command_log` and
  `sources_path` relative to the project root. Resolve them against the project directory.
  Absolute paths written by 0.1.0 still load.

### Added

#### Capture and tracer

- `capture baseline --replicate` runs the baseline's command again on the same code and
  environment; `--runs 2` records the baseline and its replicate in one go. `verify` uses the
  replicate to measure run-to-run noise.
- During a Python move, captures also record calls into the standard-library modules the
  project imports. `capture --stdlib` names the modules, or `none`.
- `capture --accept-failing` keeps a run whose command exited non-zero, if it recorded calls,
  stamped red.
- `capture --json` and `capture --agent`.
- Each recorded call carries its canonical callee, whatever name the code used, and the
  arguments the caller passed; a parameter left at its default is recorded by name only.
- Also recorded: the project lines that ran (3.12+) or the functions that started (3.6–3.11),
  the deprecation-family warnings each call issued (including warnings raised from C), the key
  order of each dict, and, per process, the runtime environment, without the hostname. A
  different key order is a review item unless it is shown to be deterministic.
- Compiled calls and calls that fail to bind are recorded. Public Cython functions of
  dependency modules are wrapped on every Python and recorded with arguments and results; other
  compiled calls past 2,000 per call site are counted.
- Datetimes, decimals, UUIDs, enums, dataclasses, numpy and pandas objects, and plain objects
  (by their fields) are rendered so that they compare.
- Exception types under `sys.setprofile` (3.6–3.11): from a one-off `sys.settrace` exception
  event in the frame that receives the raise. pymolt leaves `sys.settrace` alone while coverage
  or a debugger owns it.
- Long runs (`--mode command`, `--mode attach`) aggregate in the traced process with bounded
  memory: every call is counted, and values are kept for the first 32 calls per call site and
  for `--sample-rate` of the rest. On 3.12+ they watch incrementally from the first call.
- On 3.12+, a line of the project that runs for the first time after the 30 s settle re-opens
  the watcher for dunder, property and generator contacts (`PYMOLT_TRACE_INCREMENTAL_REOPEN`;
  `0` turns it off).

#### Verify

- A fact diff that names every changed call, with its call site and its outcome before and
  after: CHANGED, REWRITTEN, DISAPPEARED, APPEARED, DRIFT or UNCOMPARABLE.
- Captures from different Python versions or tracers compare: an untyped raise matches any
  exception type, C-level raise facts only one tracer records are left out, and a decorator's
  calls are compared within its decorated statement, whether attributed to the `def` line
  (before 3.11) or to each `@` line.
- Coverage and recall over contacts, stamped with their granularity (`line`, `function`,
  `file`, `symbol`). Each contact is recorded, ran but not recorded, not run, or unobservable.
- Run-noise handling: values from entropy sources, clocks, the environment and inputs are
  masked; a call site whose inputs never recur is judged as one group; a baseline replicate
  decides what counts as noise. Reports lead with findings, and `verify --verbose` also shows
  what was explained away.
- Edited lines are judged by who edited them: a line `pymolt apply` wrote or a recorded step
  covers is judged by the step's claim (`preserve` or `intended`), any other edited line by the
  hand-edit rules.
- `verify --accept ID`: a person on a terminal accepts a failing or review finding as intended.
  The attestation is kept outside the project with an HMAC, and expires when the outcome, the
  statement or either capture changes. `verify --strict` reviews accepted changes again.
- `verify --step ID` prints one step's judgement and exits by it: 0 verified, 1 failed, 4 needs
  review or evidence, 2 an unknown step.
- `verify --baseline` and `--post-migration` compare other recordings. A verdict on another
  post-migration recording is saved with what it compared, and `status` does not count it.
- `verify` scores each risk the plan predicted as hit, prevented, accepted, miss or not
  exercised, per risk and per step, and lists the failing changes no prediction covered.
- An "Impact focus" line in the `verify` report: the plan's changed API paths and the static
  symbols on them.

#### Plan and apply

- A local API-change engine. It probes the symbols the project touches in the old and new
  interpreters (the old one inside the baseline's container when there is one) and reads the old
  release from its wheel or sdist when no interpreter is reachable. Curated knowledge ships with
  pymolt. `plan --online` also reads intermediate releases from PyPI (package names and
  versions only).
- Successors: a name that now points to the same object under another spelling is a move with a
  rewrite, not a removal; renames are read from deprecation messages; a public name that now
  names another class is reported.
- Python-version breakage: call sites of stdlib names removed or moved between the base and
  target Python, from shipped lifetime data and a probe of the target, with guarded fallbacks
  left alone, and a compile check of every file on the target. `plan` fixes attribute paths,
  adding the import (`collections.Iterable` → `collections.abc.Iterable`), and moved names
  (`imp.reload` → `importlib.reload`).
- `plan` rates the risk at each call site (certain, high, medium, low), marks the risks the plan
  fixes, and predicts Python-version risks even without a baseline. Risk ids contain no line
  numbers (`pymolt.risks/2`), so an edit that moves lines keeps them.
- The plan is a list of steps (env, mechanical, signatures, behaviour). Each has an executor
  (`pymolt`, `agent` or `person`), a claim, `depends_on`, anchors that name statements by
  content, how it is verified and its effort. `plan` writes `.pymolt/PLAN.md` (`--plan-md` for
  another path) and a brief per step.
- `apply --step`, `--through`, `--dry-run`, `--skip --reason` and `--brief`. A step whose file
  moved on is re-based, a step already made by hand is `present`, and a stale step is refused
  with each statement it lost.
- `apply --step ID --record`, then `--record --done` or `--record --abort`, records an agent's or
  a person's edit as a step, with `--by`, `--actor`, `--claim`, `--expect` and `--pair`.
- `apply --smoke` imports the changed modules in the target environment before and after the
  write, and undoes the write when one that imported before fails after.
- `apply` lists the plan's advisories and manual items with their files and lines.
- `rollback --step` undoes one step by the inverse of its effect and keeps later edits in the
  same files; `--cascade` rolls back the steps that depend on it first.
- An append-only apply ledger, and the step texts kept once by hash in `.pymolt/blobs/`, with a
  `.pymolt/.gitignore` for them. The project's own `.gitignore` is not touched.

#### Assess, scan and setup

- `scan` lists every place the code touches each dependency (calls, attribute reads,
  decorators, base classes, context managers, constants, protocol operations), the stdlib
  names it uses, and the newest Python it runs on unchanged.
- `setup --manifest`, `--tool`, `--base-python`, `--target-python`, `--container` and
  `--target-env`, so setup runs without prompts. With `--json` or `--agent` and no choices it
  lists the valid values and saves nothing.
- `setup` accepts a manifest in a sub-root that `scan` finds, such as a lock file in `etc/`.
- `status` shows whether the base and target environments exist, and the status of each plan
  step.

#### Agents and MCP

- `--agent` on `status`, `scan`, `setup`, `assess`, `capture`, `plan`, `verify`, `apply` and
  `rollback`: a compact JSON view with capped lists, flags that say whether each list is
  complete, and `full_report_path`. With `--json` or `--agent` no command asks a question or
  takes a default.
- `apply --approve-agent`: a person on a terminal approves a plan for agents. `apply --agent`
  then writes its pymolt steps, and `rollback --step ID --agent` rolls one back. A re-plan or
  any change to the plan voids the approval.
- MCP `apply` and `rollback` tools, which need `PYMOLT_MCP_ALLOW_WRITE=1` and an approved plan;
  their smoke import needs `PYMOLT_MCP_ALLOW_EXEC=1`. They refuse arguments they do not take.
- Text that comes from a dependency, the code or a capture is fenced and marked untrusted in
  step briefs, `PLAN.md` and the agent views.

#### Docs

- The README covers what pymolt checks and how to use it, with an example from a real run, and
  warns that capturing a service under load costs throughput.
- Reference pages: `docs/commands.md`, `docs/verify.md`, `docs/plan.md`, `docs/agents.md`,
  `docs/glossary.md`, and `docs/limits.md` (what pymolt cannot do yet, measured overhead, the
  projects it was tried on).
- `docs/RELEASING.md` (versions, branches, release steps) and this changelog.

#### Packaging and CI

- CI runs the test suite on Python 3.12 and checks the built package on every pull request.
- The release workflow runs on `v*` tags only. It refuses a tag that is not canonical PEP 440,
  differs from `pymolt.__version__` or is not on `main` or `release/X.Y`, and marks rc, alpha and
  beta releases as pre-releases.

### Changed

#### Capture and tracer

- On Python 3.12+, `--backend auto` traces with `sys.monitoring` instead of `sys.setprofile`.
- `--mode command` and `--mode attach` keep values for 10% of calls by default, plus the first 32
  calls of each call site (0.1.0 kept all). Calls are still counted in full, so a call that
  stopped happening is detected exactly.
- Trace events are version 2 (`"v": 2`): they add the canonical callee (`sym`, `symq`, `own`) and
  `in.defaulted`, and every v1 field keeps its meaning. A 0.1.0 trace still loads, but its
  symbols match by name only: capture new baselines with 0.2.
- When the trace writer of a test-suite capture falls behind, the program waits instead of
  dropping events (`PYMOLT_TRACE_BACKPRESSURE=drop` restores dropping;
  `PYMOLT_TRACE_STALL_SECONDS`, default 10, bounds a stalled writer). The capture reports the
  time spent waiting and warns from 1 s.
- The first Ctrl+C during a capture gives the command 10 s to exit, a second one stops it, and
  the capture is kept either way.

#### Verify

- A dynamic-only symbol whose facts all compared equal no longer blocks PASS.
- The baseline is judged against the environment the migration started from. Bumping the pins
  of the same manifest keeps both the baseline and the plan valid; another manifest file, base
  Python, toolset or container does not.
- A rollback that restored files sets the post-migration capture aside, and `verify` names it; a
  rollback that restored no file keeps it.
- The test runner is never traced, and its contacts are left out of coverage.

#### Plan and apply

- `plan` takes the from-versions from what the baseline capture ran, not from re-resolving an
  unpinned manifest.
- A codemod rule that is not verified, or that the check downgraded, is never applied; the plan
  lists it as manual work with each file and line it matched.
- A rolled-back plan applies again without a new `plan` while its files are back at their
  planned text.

#### Assess, scan and setup

- `scan` and `assess` read `install_requires` and `extras_require` from `setup.py` and
  `setup.cfg` with one static parser; `scan` used to skip `setup.py`. Files missing from the
  contact-map cache are parsed in parallel.
- `assess` ends with the uv commands that build the target environment, and a Dockerfile
  derived from the project's when it has one.
- `setup` without a terminal takes the detected default for each missing choice and names it.
  The default target Python is the one the prompt offers, not the interpreter pymolt runs on.
- `setup` offers only containers that mount the project, and says when it edits `.gitignore`.
- Help and messages say what each command does. The project's current dependencies are the
  "base"; "baseline" means only the pre-migration capture. JSON keys (`baseline_tier`,
  `baseline_version`, ...) are unchanged.

#### Agents and MCP

- The Claude Code plugin drives the CLI with `--agent` and starts no MCP server.
- The `mcp` extra is pinned to `mcp>=1.2,<2`.

### Fixed

#### Capture and tracer

- Version probes in Python 2.7–3.5 interpreters run; those interpreters and containers were
  reported as absent.
- A watcher that cannot start in the target interpreter (Python 2.7) is reported and recorded
  on the capture, and the verdict cannot be PASS. It used to run untraced while the capture
  looked valid.
- Under `sys.setprofile`, a raise is no longer recorded as `return None`, and each event costs
  less. Temporary `pymolt-trace-*` directories are deleted once merged.
- Recording a value runs none of its code: dict, list and set subclasses are read from what
  they store, and proxies such as werkzeug's `LocalProxy` are recorded as their own type.
  Flask's test suite failed under capture before.
- gevent: the trace runtime stays off a monkeypatched `threading`, stdlib functions are never
  wrapped in place, and gevent-patched stdlib modules are not traced.
- A forked child writes its own trace file, with its own writer and counters; it used to fill a
  queue nobody drained and overwrite the parent's metadata.
- Container captures keep the command's output as the command log, and container paths
  (`/work/...`) map back to the project.
- Capture paths and the target environment are resolved against the project, not the working
  directory, and the target venv's `python` is not resolved through its symlink.

#### Verify

- The post-migration capture after `apply` adds the plan's symbols to what it records instead
  of narrowing to them, which produced false DISAPPEARED entries.
- The static contact map skips hidden, virtualenv and build directories, `*.egg-info` and
  nested checkouts.
- `verify` names the side that is missing, and a refused `capture` returns `ok: false` with a
  hint that matches the cause.

#### Plan and apply

- A resolver failure in `plan` exits 2, with a JSON error under `--json` and `--agent`, instead
  of a traceback.
- `apply` says that an empty plan changed no file.

#### Assess, scan and setup

- A failed `assess` resolve keeps the resolver's error output and marks only the packages it
  names as conflicts; it marked every package. Source fixation is read from the manifest.
- `assess` keeps a base Python taken from setup's guess marked as assumed.
- `status` recognises a migration made by hand; it called the post-migration capture stale.
- `succession` says what its knowledge covers instead of "no known path".

## [0.1.0] - 2026-09-26

First public release on PyPI.

[Unreleased]: https://github.com/iamzeelex/pymolt/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/iamzeelex/pymolt/releases/tag/v0.1.0
