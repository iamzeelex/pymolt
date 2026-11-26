# Changelog

All notable changes to pymolt. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[PEP 440](https://peps.python.org/pep-0440/) as described in
[docs/RELEASING.md](docs/RELEASING.md).

## [0.2.0] - 2026-11-26

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
  or when the smoke import finds a module that imported before and fails after. Running `apply`
  again on an applied plan exits 0 and writes nothing.
- After `apply`, `verify` judges the whole recorded surface, as it does without a plan: a
  changed call anywhere fails, and PASS needs every static symbol a capture can record exercised,
  not only those on the plan's API paths. Under `contract` in `verify --json`, `confirmed`, `blind`,
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
  tool's `data` differs from 0.1.0. What the agent may do is set in the server command:
  `pymolt mcp --root DIR` bounds the project directories (it replaces `PYMOLT_MCP_ROOT`), and
  `--allow-run` adds the `capture` tool (it replaces `PYMOLT_MCP_ALLOW_EXEC`). Without it the
  agent runs no program. `capture` lost `deployment_id`, `request_id` and `correlation_id`.
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

- The `PLAN.md` header says when the plan was made (UTC), by which pymolt version, and from which
  baseline capture (its time and its name, such as `20261120T060620Z-b3e1a2c0`).

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
- Compiled calls and calls that fail to bind are recorded. Plain public Cython functions of
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
- An INCONCLUSIVE verdict says whether it needs a person (`needs_review`: a review item) or
  evidence (`needs_evidence`: BLIND symbols a test can still reach, a fact that could not be
  compared, a capture missing, stale or unlike the other, noise no baseline replicate measured).
  The BLIND table says per kind what settles its rows; a `--probe-python` replay never does.
  `verification.json` (schema 2) keeps `inconclusive_kind` and every reason with its kind and what
  settles it (`inconclusive_causes`); `verify` prints what settles it, and `status` names it. A
  schema-1 file reads with no kind. Both kinds exit 4.
- Floats within 1024 ulps at each value's own precision (array elements too), never more than
  2^-10 relative, are numeric drift, stamped in the verdict ("k value(s) equal within
  floating-point rounding") and in `numeric_drift`; an input that differs only within rounding is
  the same input. `verify --float-ulps N` sets the bound (0 is exact); `--strict` turns each such
  pair into a review item.
- A FAIL names the plan steps whose sites failed (`failing_steps` in `verification.json` and
  `--agent`), says how to bring a rolled-back one back, and gives a person's two options: fix the
  code and capture again, or accept an intended change with `pymolt verify DIR --accept ID` on a
  terminal. `status` no longer suggests rolling back the whole run on a FAIL.

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
- `apply` imports the changed modules in the target environment before and after the write
  (the smoke import), and undoes the write when one that imported before fails after. It is on
  by default; `--no-smoke` turns it off, and the receipt then reads "not checked: --no-smoke".
  Over MCP it runs only on a server started with `--allow-run`.
- `apply` lists the plan's advisories and manual items with their files and lines.
- `rollback --step` undoes one step by the inverse of its effect and keeps later edits in the
  same files; `--cascade` rolls back the steps that depend on it first.
- An append-only apply ledger, and the step texts kept once by hash in `.pymolt/blobs/`, with a
  `.pymolt/.gitignore` for them. The project's own `.gitignore` is not touched.
- `plan` names successors from the library's own text and data: deprecation-helper arguments and
  closure cells (the helpers are listed in `knowledge/deprecation_helpers.json`), warning helpers,
  the new version's errors, and the callee the old body forwards to. A warning counts as evidence
  about a symbol only when it names it or matches its own code. Each fact's `succession` gives its
  trust, sources, caveats, candidates and rejected names. Several candidates are listed for an
  agent, never chosen.
- `rename-keyword`, `rename-member` and `forward-template` codemods, made only when the library's
  code proves the rewrite and the probe's summary of the body is complete. An unknown decorator,
  a helper handed the arguments, a call chained on the result, or an alias beside the forward
  leaves the successor named for an agent. A rewrite touches one call in the statement's own
  header, reached through its import on every path, and keeps the order arguments are evaluated
  in. They run as basis `library-proof` steps (claim `preserve`); a member rename is gated by the
  receivers the baseline recorded.

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
  `verify --auto-rollback`, which rolls back the whole run, is refused under `--agent`, like
  `rollback --agent` without `--step`.
- MCP `apply` and `rollback` tools, which write only on a plan a person approved for agents;
  `apply` runs its import check only under `--allow-run`. Every MCP tool refuses arguments it
  does not take, and `capture`, `apply` and `rollback` carry the `destructiveHint` annotation.
- Text that comes from a dependency, the code or a capture is fenced and marked untrusted in
  step briefs, `PLAN.md` and the agent views.

#### Docs

- The README covers what pymolt checks and how to use it, with an example from a real run, and
  warns that capturing a service under load costs throughput.
- Reference pages: `docs/commands.md`, `docs/verify.md`, `docs/plan.md`, `docs/agents.md`,
  `docs/glossary.md`, and `docs/limits.md` (what pymolt cannot do yet, measured overhead, the
  projects it was tried on).
- `docs/RELEASING.md` (versions, branches, release steps) and this changelog.
- The README lists the libraries pymolt is tested on, with the benchmark's totals, and
  `docs/limits.md` gives each library's numbers and why a migration that keeps its behaviour is
  not PASS.

#### Packaging and CI

- CI runs the test suite on Python 3.12 and checks the built package on every pull request.
- The release workflow runs on `v*` tags only. It refuses a tag that is not canonical PEP 440,
  differs from `pymolt.__version__` or is not on `main` or `release/X.Y`, and marks rc, alpha and
  beta releases as pre-releases.

### Changed

- CI runs the injected tracer's tests on Python 3.6 and 3.7 as well (3.8 and 3.11 ran already),
  checks pyflakes errors with ruff, installs from the lock file (`--locked`) and pins every
  GitHub Action by commit. The package no longer claims Python 3.13 (14 tests fail there; see
  docs/limits.md), and `install.sh` installs on Python 3.12. `pytest` skips the `integration`
  tests unless asked (`-m integration`).

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

- `verify`'s golden, canary and shadow options (`--golden-before`, `--golden-after`, `--policy`,
  `--metrics`, `--auto-rollback`, `--shadow`) are hidden until 0.3: no command makes their input
  files yet. They still work for a script that writes those files.
- A dynamic-only symbol (one the static map missed) no longer blocks PASS when it is settled as
  a static symbol would be: its facts compared equal, are calls its line did not make (compiled
  code calling back, an implicit hook), or are calls an edit made with no partner (unverified
  edited sites; a new raise there still fails). A new call a hand edit introduced kept every
  equivalent migration with such an edit INCONCLUSIVE.
- A dependency use no capture can record no longer blocks PASS: one no capture can observe (an
  attribute read, a constant such as `yaml.SafeLoader`, a chain through a return value), and one
  whose line ran while the watcher recorded no call there (a numpy ufunc, `polars.col`, an
  implicit `__float__`). The verdict lists them ("N dependency use(s) cannot be observed by a
  capture"), and `verify --agent` gives them as `unobservable_uses` (owner decisions). A BLIND
  symbol a test can still reach keeps blocking.
- `verify` compares a dependency object by its type and the attributes your code reads (old names
  on the old side, new names on the new one); unread state that differs is counted as drift.
  Values (builtins, containers, records your project declares, the opaque types of data packages
  such as numpy and pandas) compare by content, as before. Places that read attributes by a
  computed name are listed (`dynamic_attribute_reads` in `--agent`), not blocking; `--strict`
  compares full state. This needs both captures' source snapshots: without them, or while a
  project file does not parse here, objects compare in full, stamped in `quality.objects`.
- A class change inside one top-level package is drift when your code never checks the class or
  its package (`isinstance`, `issubclass`, `match`, `type(x) is C`, singledispatch; aliases and
  base classes resolved) and reads nothing off the result the capture did not render. A class
  change across packages is a review item until MRO tables arrive. Relocated structures and raises
  compare equal; a type an edited line produced is judged with that edit, along its data chain.
- A result is compared by what its statement observes of it: discarded (an object by its type,
  unless the call handed back its receiver or an argument), unpacked or iterated (elements, not the
  container type), `int()`/`float()`/`bool()`/`len()`, or a truth test. `--strict` compares the
  full result.
- Below line granularity, a contact that counts as ran only because its function or file ran may
  not have run: coverage reads "at most" and recall "at least" (`coverage_bounded`), in `verify`,
  `--agent` and `status`.
- `verify` takes the interpreter of a workload as replaceable (`python3.11 -m pytest` and the
  target env's `python -m pytest` are one workload), and "different commands" names the baseline's
  command.
- The baseline is judged against the environment the migration started from. Bumping the pins
  of the same manifest keeps both the baseline and the plan valid; another manifest file, base
  Python, toolset or container does not.
- A rollback that restored files sets the post-migration capture aside, and `verify` names it; a
  rollback that restored no file keeps it.
- The test runner is never traced, and its symbols (`pytest.fixture`, `pytest.raises`) are left
  out of the verdict, the counts and both numbers, with a note that counts them. A
  `@pytest.fixture` used to keep a clean diff INCONCLUSIVE.
- A step whose entries could not be compared, or may be run-to-run noise no baseline replicate
  measured, is `needs_evidence` (it was `needs_review`), and its judgement lists them
  (`uncomparable`, `noise`) with what settles them. `status` then names the baseline replicate
  for noise, and for the rest the test or capture that settles it.

#### Plan and apply

- `plan` takes the from-versions from what the baseline capture ran, not from re-resolving an
  unpinned manifest.
- A codemod rule that is not verified, or that the check downgraded, is never applied; the plan
  lists it as manual work with each file and line it matched.
- A rolled-back plan applies again without a new `plan` while its files are back at their
  planned text.
- A successor named only by text is never rewritten, even with an equal signature (urllib3's
  `get_host(url)` → `parse_url(url)` returns another thing): it is an agent step.
- Plan schema 2 additions: `symbol.params`, `why.successor`, `do.manual.candidates`, `param` on
  impacts, basis `library-proof`. Ledger entries of a successor step get `param_map`,
  `member_map` and `materialized`.
- A behaviour step for a default that is `None` or a sentinel on either side is `medium` effort,
  not `large` (such a default is decided at call time), and its title shows a sentinel as "a
  sentinel" instead of `<object object>` or `<no_default>`.
- `plan`'s next command is `status`'s, printed for the dir you gave; `plan --agent` returns it
  as `data.next` (the same shape as apply's), and `PLAN.md` renders it with the dir as `.`. With
  no baseline it is the baseline capture, and the human output prints the "no baseline" warning.

#### Assess, scan and setup

- `scan` and `assess` read `install_requires` and `extras_require` from `setup.py` and
  `setup.cfg` with one static parser; `scan` used to skip `setup.py`. Files missing from the
  contact-map cache are parsed in parallel.
- `assess` ends with the uv commands that build the target environment, and a Dockerfile
  derived from the project's when it has one.
- `setup` without a terminal takes the detected default for each missing choice and names it.
  The default target Python is the one the prompt offers, not the interpreter pymolt runs on.
- `setup` offers only containers that mount the project, and says when it edits `.gitignore`.
- When no manifest is chosen, `scan`, `setup`, `assess` and `plan` pick the same default: a lock,
  then a canonical manifest, never a docs, dev or CI variant or a sub-root while something better
  exists (blaze: `requirements-strict.txt`, where assess took `requirements-rtd.txt`). A
  `setup.cfg` that declares no dependencies is no longer offered as a manifest.
- Help and messages say what each command does. The project's current dependencies are the
  "base"; "baseline" means only the pre-migration capture. JSON keys (`baseline_tier`,
  `baseline_version`, ...) are unchanged.

#### Agents and MCP

- The Claude Code plugin drives the CLI with `--agent` and starts no MCP server.
- The `mcp` extra is pinned to `mcp>=1.2,<2`.

#### State files

- `.pymolt/contract_state.json` is schema 2: its slot paths are relative to the project root.
  A schema-1 file is read as before and saved as schema 2. pymolt 0.1 does not check the
  schema, so do not go back to 0.1 on a project 0.2 has captured.
- `status` shows a damaged or newer state file as a problem of its phase (`problems` in `--json`
  and `--agent`), with no next command and the file's remedy as the reason.
- A command that updates a state file while another pymolt does waits for it (up to 60 s,
  `.pymolt/state.lock`), then exits 3 naming the process that holds the lock.

### Removed

- `verify --cascade FILE`: it read a per-node cascade report that nothing produces. The
  `pymolt.verify` re-exports `run_cascade`, `verify_node`, `CascadeDeps`, `build_coverage_map`,
  `blind_spots`, `CoverageMap`, `NodeRef`, `NodeVerifyResult`, `TestOutcome`, `VerifyReport`,
  `Verdict`, `EvidenceLevel`, `TraceScope` and `TestStatus` go with it. `rollback --cascade` is
  unchanged.

### Fixed

- Every printed next command names the project dir as you gave it (relative to the current
  directory when it lies inside it), and capture commands add `--project-dir`: `pymolt scan
  tests/artifacts/flasgger` suggested `pymolt setup .`, and `status` printed `apply --step ID
  --record` with no dir. The MCP `status` and `apply` tools follow the same rule.
- pymolt's own parsing of the project's files no longer prints `SyntaxWarning: invalid escape
  sequence` on stderr; the compile check still reports each one as a finding.

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
- The tracer no longer keeps the program's objects alive. Its caches held the callables it had
  seen: a builtin method bound to a dict, list or dependency object kept that receiver alive,
  as did a callable instance, a closure, a `functools.partial`, a decorator's wrapped method
  and, during the incremental settle, a recorded call's frame. Their `__del__` ran late or
  never and weak references to them stayed alive. Its per-class caches also held each class the
  program made at run time (one per request, one per mock) and with it the class's namespace:
  23 of locust's tests failed under capture on 3.12. The same facts are recorded.
- The tracer's wrapper replaces only a plain function: a fused Cython function, a coroutine,
  generator or async-generator function, a callable object (an `lru_cache` wrapper, a
  `functools.partial`) and a callable that does not bind in a class keep their own object and are
  listed in `skipped`. Any project that reached scipy's `gaussian_kde` (seaborn's `kdeplot`) could
  not be captured, and `inspect.iscoroutinefunction` turned False. A wrapped function that C calls
  with no Python frame above (an atexit callback, a thread's target) no longer raises into the
  program.
- Whether code is the project's is decided per namespace, not per module name: a script run as
  `__main__` inside `python -m pytest` (streamlit's AppTest) recorded none of its calls under
  `sys.monitoring`.
- Code a dependency generates with `exec` is no longer taken for the project's. Such code runs
  as `<string>` in a namespace with no `__file__`, and is now placed by the module its
  `__name__` names: SQLAlchemy 2.0's mapped-class `__init__` and decorated methods made every
  call they made inside SQLAlchemy a contact, and an equivalent 1.3 to 2.0 migration failed on
  71 APPEARED entries. `python -c`, `<stdin>`, the REPL and notebook cells (`__main__`) stay
  the user's.
- A refused capture's hint fits the side: a failing post-migration run may be what the migration
  broke (`--accept-failing` keeps it so verify compares it), a failing replicate may be flaky. A
  run the test runner stopped before any test ran is named so ("N test file(s) could not be
  collected", a usage error, no test selected), and no `--accept-failing` is offered for it.
- After a baseline capture the next step names `pymolt plan`, then the post-migration capture.
- gevent: the trace runtime stays off a monkeypatched `threading`, stdlib functions are never
  wrapped in place, and gevent-patched stdlib modules are not traced.
- A forked child writes its own trace file, with its own writer and counters; it used to fill a
  queue nobody drained and overwrite the parent's metadata.
- Container captures keep the command's output as the command log, and container paths
  (`/work/...`) map back to the project.
- Capture paths and the target environment are resolved against the project, not the working
  directory, and the target venv's `python` is not resolved through its symlink.
- `capture --mode tests` runs the test suite once. When the `coverage` package was importable,
  it ran the suite a second time as `coverage run -m pytest` with the command's first word
  dropped, on the host even for a container capture, and wrote `.coverage` and `coverage.json`
  into the project. The second coverage figure it gave (`coverage_pct` in `status` and the
  capture view) is gone: coverage is `verify`'s, the contacts that ran.

#### Verify

- A review the measured baseline replicate did not explain no longer says to capture a replicate:
  it asks a person to review it (fix, or `verify --accept`).
- `verify --agent` says how many failing findings its summary leaves out ("and N more"), so an
  agent does not read the first three as all of them.
- A deprecation that says "Instead access X directly" names X as the successor.
- The post-migration capture after `apply` adds the plan's symbols to what it records instead
  of narrowing to them, which produced false DISAPPEARED entries.
- The static contact map skips hidden, virtualenv and build directories, `*.egg-info` and
  nested checkouts.
- `verify` names the side that is missing, and a refused `capture` returns `ok: false` with a
  hint that matches the cause.
- An operator between two dependency objects (`Version(a) > Version(b)`) no longer leaves the
  reflected method (`__lt__`) BLIND as ran but not recorded, which made a capture pair with no
  code change INCONCLUSIVE with recall below 100%. Python calls one of the two methods: either
  recorded at the statement confirms both, and the pair counts once in coverage and recall.
- A person-attested `representation` expectation whose values `verify` cannot see (compared by
  type only, or recorded as opaque) makes the change not comparable, needing evidence (capture
  both sides with `--privacy values`). It failed, while the same change without the
  attestation was a review item: attesting made the verdict worse with nothing against the
  claim. A visible violation still fails.
- A diff in which no fact compared equal is INCONCLUSIVE, needing evidence: a test that
  exercises those calls with stable values. It passed when every difference was noise a
  replicate measured, a call explained away as implicit, or an unverified edited site, though
  nothing showed the behaviour unchanged.
- A compiled call that newly raises after a move from Python 3.6–3.11 to 3.12+ fails when the
  baseline's tracer recorded that callable as a compiled call elsewhere: that tracer sees builtin
  C functions, so the baseline's "no raise" is evidence. It was not comparable, as for a Cython
  function the older tracer cannot see (which stays so).
- Outcomes are matched with their counts, so one changed call in a loop, a call more or fewer,
  or a raise count that moved fails, for a fact with one outcome too; clock readings keep their
  counts. In a workload whose values vary between runs a moved count is a review a baseline
  replicate settles. Scalar floats in a loop pair by sorted order, with no cap on distinct values.
- A coroutine's marker is uncomparable (needs evidence: its awaited result is not recorded)
  instead of equal; a value or a record that became a coroutine fails.
- Call sites are identified across interpreters and library moves: PEP 709 comprehensions,
  inherited constructors, a relocated decorator closure, a callee that moved and binds its
  argument differently, calls compiled code made while the project's call ran, and numpy
  dispatchers (dropped on both sides and counted, never under `--strict`).
- A src-layout project's records are values; an unpacked library namedtuple compares its fields;
  a read attribute present on one side only, a private attribute read by name, and a discarded
  object of another kind are compared; calls on dependency records pair per record; an added
  `None` default is counted drift (a review under `--strict`), and a decorator's wrapper named
  differently is drift only when each side's callable is the decorated function, a closure or a
  class the library made. A project file that does not parse here turns the read-set projections
  off, stamped, and a deeply nested file no longer crashes verify.
- An input change on an unedited line with a visible cause is judged by that cause: differing
  values that are string literals a hand edit changed, and a call through a name a hand edit
  re-bound, are judged as that edit (a new value is a review, a new raise fails); a parameter
  bound to `None` on one side only is an added default (drift); an interpreter's own paths
  (`certifi.where()`) are masked like the project root.
- A proven keyword rename's inputs are compared in the old call's terms (`shift(periods=1)` →
  `shift(n=1)`), and only for the rewritten call.
- A post-migration capture whose recorded code changed since (a hand revert) is stale:
  INCONCLUSIVE, needs evidence, naming the files (`stale_sources`) and the capture to take again.
  It used to PASS, and `status` showed the saved PASS next to a capture it marked stale.
- A recording named with `--baseline` or `--post-migration` needs its event-loss counters, from
  its `<recording>.meta.json` (the one `pymolt capture` writes, or the watcher's, several gathered
  under `processes`): two hand-written traces gave PASS. A `--probe-python` replay that changed
  FAILs with one recording too, since it compares with the recording it replays.
- With no capture at all, the verdict has one reason, "no captures", settled by capturing the
  baseline, then post-migration.
- Captures taken in different checkouts of the project compare: each capture's source snapshot
  names the root it was taken in, and call sites are made relative to it. A failing entry from a
  capture without a source snapshot taken elsewhere is uncomparable ("site-unplaced"), never a
  FAIL.
- A relative path in a container capture's executed record no longer stops the `/work/...` paths
  mapping: zipline and locust read coverage 0%.
- Observed call sites are printed relative to the project, as in the other tables.
- A trace without call sites gets one granularity note (symbol), not a second one about files.
- A review that no baseline replicate can settle (two recordings named on the command line) no
  longer says to capture a replicate: it says to record the before side twice and compare.

#### Plan and apply

- `rollback --step` of a recorded agent or person step sets aside the post-migration capture
  that recorded the edit, as for a pymolt step. It was kept, and `status` went on showing its
  stale verdict.
- `apply --dry-run` prints the diff as it is; a `[` in it showed as `\[`.
- The brief of a step for a changed default no longer promises that passing the old value keeps
  the old outcome (pandas 2 reads `numeric_only=None` as `False`): it says to check it.
- Standard-library membership comes from the bundled lifetimes of every Python version, never
  from the interpreter pymolt runs on. On 3.12, `imp`, `distutils` and `asynchat` used to count
  as third-party in the delta's touched symbols, the behaviour checks, the default `--stdlib`
  set and the noise rule for stdlib packages traced on one side.
- `plan`'s compile check runs on the base interpreter too when that interpreter is on the host,
  so a target SyntaxError the base also has (a Python 2 leftover) is marked as already there and
  one the move introduced is marked as new. It used to estimate the base from its grammar only.
- A rename that also moves a symbol to another module (`flask.Markup` → `markupsafe.Markup`)
  moves only that name: `from flask import Markup, Flask` becomes `from flask import Flask` plus
  `from markupsafe import Markup`, where it used to move `Flask` too.
- A resolver failure in `plan` exits 2, with a JSON error under `--json` and `--agent`, instead
  of a traceback.
- `apply` says that an empty plan changed no file.
- A step recorded again after the post-migration capture is `not_in_capture`, and `status`
  says to capture post-migration again. It showed `recorded`: the new record is diffed from the
  step's first before-text, which the capture did not have either, so the step looked overtaken
  by a later edit, as if the capture held it.
- `status` after `apply` and a new `plan` describes the new plan. The plan phase said "applied
  by migration run" for the earlier plan; it now shows the saved plan and names the earlier
  run, and the next command applies the new plan's steps.
- `plan` reads setup's manifest relative to the project, not the working directory: run from
  elsewhere (as agents and the MCP server do) it failed, or read another file with the same name.
- A risk is rated by the name its site writes. A call that resolves through its receiver to a
  method that still exists (`df.groupby` after pandas moved it off `NDFrame`, `pd.Timedelta`,
  `.str.upper()`) is no longer `certain: removed` with a step that loosened verify there; the
  delta probes the receiver's spelling, and REMOVED and MOVED facts carry `vanished_at`. A call
  whose own spelling is unknown is `low`, unverified, with a check to run.
- A Python-version risk is rated only for a call its statement makes: calls made by compiled code,
  a resumed generator or a private implementation class (uvloop's `ensure_future(loop=...)` under
  `asyncio.sleep(0)`) no longer make a site `certain`, and `self` or `cls` is never a missing
  required parameter.
- A curated rule (`DataFrame.append` → `pd.concat`) is dropped only when the delta probed one of
  its symbols and found it still there, not when the delta never probed it.
- A compile finding is planned once, as its compile step, not also as a risk step.
- The `PLAN.md` header says how many dependency deltas are inconclusive, which packages, the
  reason and what settles it (such as starting a stopped container and planning again).
- The apply panel counts what this apply wrote and names each manifest pin it applied; it printed
  the run's totals.
- `status` reports the self-check per step in effect (compile on the target and the base, smoke
  import), not the latest invocation's.
- `status` and `apply` suggest the post-migration capture with the baseline's workload, mode and
  options, run by the target environment's interpreter; a step's brief names the record commands
  or `setup DIR --target-env PATH` instead of a bare capture that exits 2.
- A refusal because of who asks or how (the `--agent` limits, the approval gate, options that do
  not go together) is no longer printed as a plan conflict with a re-plan hint; an agent's refusal
  says a person runs the same command on a terminal. Exit codes are unchanged (2).
- A successor the old version's warning names is checked in the new version, so it is named when
  it exists there; a candidate the new version was not asked about is listed as "was not probed
  in the new version", where the plan said it "does not exist in the new version".
- `plan --online` reads a module from an intermediate release's wheel with a cap on what it
  inflates (4 MB) and on the sizes it accepts; a wheel past them is skipped with a note.

#### Assess, scan and setup

- A failed `assess` resolve keeps the resolver's error output and marks only the packages it
  names as conflicts; it marked every package. Source fixation is read from the manifest.
- `assess` keeps a base Python taken from setup's guess marked as assumed.
- `status` recognises a migration made by hand; it called the post-migration capture stale.
- `succession` says what its knowledge covers instead of "no known path".
- A base version nothing observed is `unknown` (`baseline_version: "unknown"`, status
  `base-unknown`), never a `>=` floor or `0.0.0`: assess showed `flask 0.10 -> 3.1.3`, the delta
  engine fetched flask 0.10 as the old side, and `--risk` claimed CVEs cleared. Only an exact pin
  is a version; a lock, the base interpreter or container, or a baseline capture settles the rest.
- When the project's own build fails under the target Python (theano's `versioneer.py`), assess
  resolves the dependencies it reads from the file without running it, stamped
  `target_basis: static-read`; otherwise it says the build is the project's and hints at a fix
  (`target_hint`). After a failed target resolve the CLI ends on the way forward.
- A base manifest that does not resolve is reported with uv's first error line, the culprits and a
  hint, in the CLI and over MCP (exit code 3).
- Releases compare by PEP 440 (`3.2` is `3.2.0`), and a base dependency whose marker is false on
  the target Python is `excluded`, not `removed`.
- pip-compile output: inline comments are dropped from the requirements, `# via` provenance marks
  transitive entries, and a declared-only base is stamped with what its declarations pin.
- `assess --risk` never rates the base pins as the target: with no target graph it rates nothing
  and says why (`not_rated`).
- Texts: the target manifest's header names `assess`; the legacy hint names `--base-python`; a
  Python read from the project is "(project config)"; scan warns once per `git://` package; a
  failed throwaway-env install reads as one line; the BRONZE example comes from the project's own
  requirements.
- `scan` says "not ported to Python 3" only for an unguarded Python-2 import; one behind a
  fallback guard is noted as a fallback.

#### State files

- A damaged state file in `.pymolt/` (unreadable, not JSON, or not what pymolt expects) or one
  written by a newer pymolt is refused instead of read as missing: every command that needs it
  exits 2, naming the file, what is wrong and what to do (JSON under `--json` and `--agent`), and
  nothing overwrites it. A damaged `contract_state.json` used to be read as empty, so the next
  `capture` replaced it and the other slots lost their recordings; a damaged receipt or ledger
  made `apply` and `verify` act as if no run existed. `capture` refuses before it runs the
  command, `plan` and `verify` before they compute anything. Covered: `env_config.json`,
  `contract_state.json`, `migration_plan.json`, `migration_state.json`, `apply_ledger.json`,
  `verification.json`, and each run's `state.json`, `plan.json` and `approval.json` (see
  [docs/commands.md](docs/commands.md#damaged-or-newer-state-files)).
- Every file pymolt writes is written atomically (a temporary file, fsynced, then renamed): a
  crash or a full disk no longer leaves a torn `env_config.json`, `PLAN.md`, target manifest,
  `--agent` report or cache.
- Two pymolt commands on one project no longer lose each other's update: a capture's slot
  update of `contract_state.json`, `setup`, the saved plan and verdict, the assess overrides
  and the post-migration capture a `rollback` sets aside take turns under one lock. Two
  captures of different slots at once used to drop one of them.
- A plan, a receipt and its frozen plans name the baseline capture and
  `.pymolt/migrations/<run>/plan.json` by their path in the project, so a copied project (another
  worktree, a CI workspace) keeps them: apply refused "the baseline capture was replaced", and
  verify lost the plan's predictions. An absolute path an earlier 0.2 build stored is read through
  this checkout's `.pymolt/`.
- After a FAIL at the sites of a rolled-back person's or agent's step, `verify` and `status` say
  to make the change again with `pymolt apply . --step ID --record`. They said
  `pymolt apply . --step ID`, which refuses a step pymolt does not write.
- Text in square brackets is no longer dropped from the printed output: `scan` showed
  `requests[socks]==2.31.0` as `requests==2.31.0`, and `pymolt mcp` without the MCP extra said to
  install `pymolt` instead of `pymolt[mcp]`. The same fix covers the other commands' tables, paths,
  commands and messages, and the Codex entry in `pymolt mcp --help`.
- `verify --accept` marks an outcome it shortens in its prompt with `…`: it cut the old and new
  outcomes at 120 characters without saying so, so the old value of a long result seemed missing.
- The install commands in the README, the plugin's README and the MCP docs ask for Python 3.12
  (`uv tool install --python 3.12`, `pipx install --python python3.12`). Without it uv and pipx
  take the first Python on `PATH`, which may be 3.13, where pymolt is not tested.

## [0.1.0] - 2026-09-26

First public release on PyPI.

[0.2.0]: https://github.com/iamzeelex/pymolt/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/iamzeelex/pymolt/releases/tag/v0.1.0
