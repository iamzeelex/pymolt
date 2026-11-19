# Python-version breakage: detect, rate, fix, calibrate

Status: design, 2026-11-15. Base: `rework/cli-first-local-delta`. Scope: OSS.

Sources:
- the legacy evaluation in `bench/legacy_eval/` (hyper, vibora, theano, lasagne, alphalens, zipline);
- two independent drafts (Opus, Sonnet), merged here;
- re-checks noted inline.

## Problem

pymolt is meant to make dependency **and** Python-version migrations enumerable. On real 3.6 → 3.12 projects it sees nothing on the Python side:
- **hyper**: imports fail at `from collections import Iterable, Mapping`.
- **vibora**: `from collections import Callable` breaks 13 of 17 test files, and its only pin (`cython==0.28.3`) cannot build on 3.12.
- **theano**: needs `imp`, `distutils` and `numpy.distutils`.

`assess` calls vibora "GOLD, resolves", and `plan` says "0 need attention". Separately, the API-change probe has no container support. Every 3.6 container baseline (4 of 4 projects) therefore got 100% INCONCLUSIVE dependency deltas. That is how alphalens's `pandas.util.testing` removal was missed.

## Evidence

All of the following was measured.

- **Real 3.6.15:** `collections.Iterable` exists (`__module__ == "collections.abc"`) and does **not** warn. `import imp` warns. Runtime warnings therefore cannot be the primary detector for 3.6 baselines; the ABC warning only arrived in 3.7.
- **Minimal 3.12:** `import collections; collections.abc.Iterable` raises `AttributeError`. A rewrite to `collections.abc.X` must add `import collections.abc`.
- **Name sweep 3.8 → 3.14:** 84 module members disappear. A `dir()`-only sweep finds **zero** `collections.*` removals, because 3.7–3.9 serve the ABC aliases through module `__getattr__`. The sweep must start from 3.6 and check with `hasattr`.
- **typeshed `stdlib/VERSIONS`:** module lifetimes as `name: X.Y-A.B` (`imp: 3.0-3.11`, `distutils: 3.0-3.11`, `asynchat: 3.0-3.11`). It has no name-level lifetimes.
- **Ruff 0.15.17, `--select UP --target-version py312`** (re-checked):
  - UP035 rewrites only the `from collections import X` form;
  - it misses `collections.Iterable` used as an attribute (hyper `connection.py:393`, hyperframe), `imp`, `distutils`, `inspect.getargspec` and `ssl.wrap_socket`;
  - it also rewrites `except ImportError:` compatibility fallbacks.
- **pyupgrade `--py312-plus`** changed 45 files in hyper and 37 in vibora. Ruff `UP` changed 43 and 29. That is mostly cosmetic churn.
- **Prototype site scan:**
  - hyper: 7 unguarded removed sites (6 fixable in 3 files; `ssl.wrap_socket` is manual);
  - vibora: 1 (`vibora/templates/ast.py:1`);
  - lasagne: 0 unguarded, 2 guarded fallbacks;
  - theano: about 10 unguarded (`imp`, `distutils`) plus guarded compat imports.
  - A rule like "inside any `if`/`try`" is wrong in both directions: hyper `connection.py:393` is an `elif isinstance(...)`, not a guard.
- **Target compile check** (`compile()` of every `.py` on 3.12) takes 0.04–0.58 s per project. It found 0 SyntaxErrors and some invalid-escape SyntaxWarnings: vibora 3, theano 8, lasagne 7.
- **hyper after a pyupgrade/Ruff rewrite** still fails, now inside **hyperframe 3.2.0** (`flags.py`: `collections.MutableSet`). That version is pinned by hyper's `hyperframe<4`. Resolving succeeds; importing does not.
- **Host-dependent stdlib membership:** `contact_map.py:400`, `report.py:582` and `gaps.py:74` use pymolt's own `sys.stdlib_module_names` (3.12). That is why `imp` and `distutils` show up as "dependencies".

## Decisions

| # | Decision | Why |
|---|---|---|
| D1 | **Python is a pseudo-migration in the delta engine.** It gets one `DeltaReport` with `subject: "python"` and `package: "python"`, with from/to set to the probed interpreter versions, and runs through the same probe, diff, provider, predict and calibrate path. | On 3.6 `collections.Iterable` *is* `collections.abc.Iterable`, so the engine's existing MOVED-with-successor logic yields the rewrite. No second pipeline. |
| D2 | **The target side is always probed.** Use the target env if one exists, else the bare target interpreter (the stdlib needs no dependencies). The base side is probed through a runner (container or host) when reachable, else it comes from shipped data. | Absence on the target is what breaks code. Only a real env shows shims, such as setuptools' `distutils`. |
| D3 | **Lifetimes ship as data; existence is computed on the spot.** Three sources: module lifetimes from typeshed `VERSIONS`; name lifetimes from a scripted release-time sweep of real 3.6–3.14 interpreters (dir() union plus `hasattr`); and a curated table of about 50 successors and fixes. | The data gives "removed in X.Y" labels, offline base-side evidence, and the exposure view in `scan`. |
| D4 | **Tiers are capped by evidence.** `certain` needs absence probed on the target, presence on the base (probe or data) and an unguarded site. Data-only absence caps at `high`. Guarded, dead-on-target and pre-existing sites are **listed, not rated**. | Honesty markers: the tier says what was observed. |
| D5 | **Stdlib sites come from the contact-map pass.** Same LibCST parse, plus positions, guard facts and `reached_at` (import or call). There is a per-file content-hash cache. Stdlib membership uses the union over all versions, never the host's list. | One parse. The cache also removes theano's 80 s × 3 rescans. |
| D6 | **Python risks need no baseline capture.** | vibora never got one, and its breakage is one import line. |
| D7 | **LibCST is the only applier. Ruff is not a runtime dependency; it is a dev-only differential oracle in tests.** Removals with no same-meaning 1:1 successor become advisories with a hint (`imp.find_module`, `distutils.version`, `getargspec`). | Measured: Ruff covers about one removal family in eight and rewrites fallbacks. Two appliers mean two preview and rollback stories. The existing transformers are binding-aware and golden-tested. |
| D8 | **Minimal diffs.** A plan only changes lines that contain a flagged removed spelling. It never modernizes: no f-strings, no `super()`, no Python-2 cleanup. | Migration diffs must be reviewable. pyupgrade's 45 files hide the 3 that matter. |
| D9 | **A container runner adapter** (`docker exec -i <name> <exe> -B -c <probe>`, request on stdin, nothing copied in). Each capture slot and the setup config record a structured `RunnerSpec`. Every old-side probe uses it: dependency deltas, the stdlib base side, import observation and identity resolution. Timeouts are ×4 under emulation. Paths are mapped through `mounts`. | All four evaluated projects had a running 3.6 container and 100% INCONCLUSIVE deltas, `/work/...` risk sites and truncated names. |
| D10 | **Resolves ≠ installs ≠ imports, split by subject.** The dependency rungs live in **assess**. The project's own imports are observed by a new **`capture --mode imports`**. Plan predicts and never executes project code. The probe reports "module raises on import" as `import_errors`, never as REMOVED. | Roles stay clean: capture observes, plan predicts, verify judges. Without the split, hyperframe's crash would read as API removals. |
| D11 | **verify gets an import layer:** a base-vs-target module import diff (regressed / pre-existing / fixed). It feeds the verdict, so hyper becomes FAIL with a cause instead of INCONCLUSIVE. A rejected tests capture also keeps its parsed exception and frames (`rejection`). | It works when tests cannot even collect, and every "exit code 2" becomes evidence. |
| D12 | **A moved name is `certain` when the site's own spelling vanished** (all python sites, and static dependency spelling sites). | Today `_rate` (`verify/predict.py:356`) rates `from collections import Iterable` `low`. |
| D13 | **Python-2 leftovers are pre-existing breakage, not migration risks.** The compile check reports them on both sides. | They aren't caused by the version jump. |
| D14 | **Stdlib warnings at the user's own sites** (unclaimed warnings that originate in the stdlib) are recorded as `site_warnings`. They corroborate a site, but never form the primary detector and never lift data-only evidence to `certain`. | It's useful on 3.7–3.9 bases, and 3.6 doesn't warn (verified). |

## Data and schema

- **`pymolt/knowledge/stdlib_lifetimes.json`** is *generated* by a repo-only `bench/stdlib_sweep/`: `python:3.6`/`3.7` images, and 3.8–3.14 via uv.
  - It holds `modules` (VERSIONS plus the Python-2 table from `core/legacy_stdlib.py`) and `names: {"collections.Iterable": {"present": ["3.0","3.9"]}, …}`.
  - It keeps public, defined and `__getattr__`-served names, and drops re-exported modules.
  - Each file carries `source` and `date`.
- **`pymolt/knowledge/stdlib_changes.json`** is *curated*, about 50 entries: `{id, symbols, kind, deprecated_in, warns_since, removed_in, successor, same_object, successor_since, fix: rewrite-import|rewrite-attr|replace-attr-path|advisory, hint, shimmed_by, source}`.
  - Examples: collections ABCs (3.10, `rewrite-*`); `imp.reload` → `importlib.reload` (`replace-attr-path`); `imp.find_module` (advisory); `distutils` (3.12, `shimmed_by: setuptools>=60`); unittest aliases (3.12); `xml.etree.cElementTree` (3.9); `configparser.SafeConfigParser` (3.12); `inspect.getargspec` (3.11, advisory); `ssl.wrap_socket` (3.12, advisory); PEP 594 modules (3.13).
  - CI cross-checks the table against every interpreter uv can provide.
- **`pymolt.delta/1` changes are additive.** Readers ignore unknown keys, and `PROBE_VERSION` is bumped.
  - Report: `subject` (default `"dependency"`), `sides` (`probe:<interpreter>` or `knowledge:<file>@<date>`), `import_errors[]`.
  - Fact: `basis` per side (`probe` | `knowledge` | `import-statement`), `removed_in`, `deprecated_in`, `warns_since`, `origin` (`stdlib` | `site-packages:<dist>`).
  - The kind set is unchanged.
- **Sibling schemas:**
  - `pymolt.compile/1`: `{file, line, kind, message, python, base}`;
  - `pymolt.imports/1`: per module `{status: ok|failed|timeout|not-built|skipped, error, first_project_frame, origin_owner: project|stdlib|dist:<name>, cause}`.
- **Precedence per touched spelling:**
  - absent on the target: target probe, else data;
  - present on the base: base probe, else data, else "the code imported it";
  - successor: curated, then validated by a second target-probe pass.

## Python tiers

These rules sit in `verify/predict.py` beside the dependency table.

| tier | rule |
|---|---|
| certain | absent in the probed target, present on base, unguarded; a removed keyword passed explicitly; a target SyntaxError where the base compiles |
| high | the same with absence from data only (no target interpreter); a base presence resting only on an import statement |
| medium | present on target only through a third-party shim (`distutils` via setuptools); a site under a broad `except`/`pass` (silently degrades); a SIGNATURE change on a passed parameter |
| low | deprecated but still present on target (`Logger.warn`); removed after the target (lookahead); a target SyntaxWarning (invalid escape) |
| listed, not rated | guarded fallback, version-dead on target, pre-existing (absent or failing on base too) |

**Guards are syntactic and precise.**
- A guard is a `try` whose handler catches `ImportError`, `ModuleNotFoundError` or `AttributeError`; a broad `except` counts as broad.
- An `if` counts only when it tests `sys.version_info`/`sys.version`, a `PY2`/`PY3`/`six.PY*`-style name, or `hasattr(<stdlib module>, "<name>")`.
- Raw guard facts are cached per file and evaluated against the target only at rating time.

## Flow and touch points

- **scan**
  - New `pymolt/core/stdlib.py` (`is_stdlib(top)` over the version union, plus `lifetime(name)`), absorbing `core/legacy_stdlib.py`. It replaces the host lists at `verify/contact_map.py:400`, `verify/report.py:582` and `verify/gaps.py:74`.
  - `_ContactVisitor` (`contact_map.py:198`) adds `PositionProvider` and sends stdlib names from `_emit` (`:268`) to a new `ContactMap.stdlib_sites`; `Contact` gains `line`.
  - There is a per-file cache under `.pymolt/cache/contacts/`.
  - `run_scan` gains `python_exposure` (data only): sites per removal version, guarded and pre-existing totals, and `ceiling`, the highest Python the code runs on unchanged.
  - The unparsed `.pyx` files are stated.
- **setup** stores a `RunnerSpec` from `docker inspect` (full id, name, image, platform, workdir, mounts). It lists only containers that mount the project. It also fixes the 12-character name in `status.py:103`.
- **assess**
  - Keeps the uv stderr (`assess/service.py:198`) and explains it through `pyversion/explain.py` ("has no attribute", "cannot import name" → lifetimes lookup).
  - A new `assess/target_check.py` runs after `write_target_manifest`:
    1. it builds or reuses the manifest-keyed env via `delta/envs.py:179 throwaway_env`, with the same key `resolve_new` uses, so plan reuses it;
    2. on a failed install it names the culprit pin with per-pin `--no-deps` retries (capped);
    3. it imports each resolved dist's top-level modules;
    4. it runs a control import in the base runner when one is reachable, giving regression vs pre-existing.
  - It persists `.pymolt/target_check.json`, and `compatibility_verdict` (`:340`) claims only the rungs it checked.
  - Default on, with `--no-install-check`. Over MCP it needs `PYMOLT_MCP_ALLOW_EXEC`, since sdist builds execute code.
- **capture**
  - New `--mode imports`, with no command. It runs a 3.6-safe `pyversion/_compat_probe.py` in the slot's env: the base via the runner, post-migration in the target env.
  - It compiles every `.py` and imports package submodules only: never loose scripts, `setup.py`, `build.py`, `conftest.py` or `__main__`. It uses `-B` and a per-module timeout; a missing extension is `not-built`.
  - Results go to `ContractState.imports.{baseline,post_migration}` (`verify/models.py:243`).
  - A rejected tests capture stores `rejection` (exception and frames, from the last 4 KB of the log).
  - `_backend.py:698 _capture_warning` collects `site_warnings`.
- **plan**
  - `build_migration_plan` (`codemods/service.py:588`) adds the python move.
  - `pyversion/facts.py::python_delta` uses `compute_deltas` for `python` with both sides (`probe.py:665` `roots` = the touched stdlib tops), or a single-sided classifier (target probe plus data, `partial`).
  - `delta/envs.py`:
    - `resolve_old` (`:265`) tries runner candidates first;
    - `resolve_new` (`:325`) falls back to the bare target interpreter for `python`.
  - `engine.run_probe` (`:211`) runs through the runner.
  - The probe records `import_errors` (`probe.py:500`).
  - The provider emits `rewrite-attr` with `requires_import` (`delta/provider.py:78`). `RewriteAttrCodemod` (`apply.py:343`) is followed by LibCST `AddImportsVisitor`/`RemoveImportsVisitor`. A new `replace-attr-path` kind does cross-module renames.
  - Every rewritten file is re-compiled on the target, and on the base runner when `successor_since <= base`; the plan says `runs_on_base`.
  - `predict_python_risks` sits beside `_add_static_risks` (`predict.py:530`). `PredictedRisk` (`:103`) gains `scope`, `reached_at`, `guard` and `fix`.
  - Python risks are predicted even with no baseline (`migration_plan.py:325`).
  - Dependency `import_errors` and `target_check.json` failures become `certain` `scope: dependency-import` risks, naming the constraint that pins them (hyperframe `<4`).
- **apply**
  - The gate is unchanged, except D15 below.
  - The "next" hint names the target interpreter and `--mode imports`.
- **verify**
  - `pyversion/observe.py::import_diff` loads the imports slots.
  - `calibrate_python` assigns:
    - **hit**: the module fails at that site, or the cause names the spelling;
    - **prevented**: fixed by the plan, and the module now imports past the line;
    - **miss**: the module imports and the site wasn't fixed;
    - **not exercised**: masked by an earlier failure, a call-time site, or a module that isn't built.
  - Unexplained failures are **unpredicted**.
  - `_stamp_verdict` (`service.py:1948`): a regressed import, or a target failure explained by a removal fact when the base wasn't observed, is FAIL. Clean imports without a tests comparison are INCONCLUSIVE, citing "imports: M/M on 3.12".

## Surface

There are no new commands; all output goes through `views.py`, and MCP returns the same.
- **`scan`** gives `data.python_exposure`, e.g. "7 sites use stdlib names removed by 3.12 (data only; `pymolt plan` confirms and fixes); runs unchanged up to 3.9".
- **`assess`** gives `data.target_ladder {resolves, installs, imports}` plus failures with causes, e.g. "resolves on 3.12; cython==0.28.3 does not build there (collections.Iterable, removed 3.10)".
- **`plan`** gives `data.python {from, to, quality, sides}`, and its risks carry `scope`, e.g. "Python 3.6→3.12: 7 risks (6 certain); 6 fixed by the plan in 3 files; needs attention: ssl.wrap_socket (manual)".
  - **"0 need attention" is never printed** while any delta is inconclusive. The line says instead: "dependency API changes unknown for 11 of 11 packages".
  - Flag: `--no-python`.
- **`verify`** gives `data.imports {regressed, pre_existing, fixed, base_observed}`, and its calibration rows carry `scope`.
- **`status`** shows "python 3.6→3.12: 7 predicted, 6 fixed, imports: not observed".
- **Offline:**
  - scan, the compile check, data facts and container probes stay local;
  - target envs come from uv's cache, or from the network in assess or with `--online`;
  - with no target interpreter, facts are data-only and capped at `high`.

## Acceptance

1. **hyper** (3.6 container → 3.12).
   - **scan:** the 7 unguarded sites and `ceiling 3.9`. Python-2 names are pre-existing or guarded, never dependencies.
   - **assess:** `installs ✓, imports ✗ hyperframe 3.2.0`, with the cause and the `<4.0` constraint.
   - **plan,** offline, with or without the container:
     - 6 certain sites fixed in 3 files, with `import collections.abc` added;
     - `ssl.wrap_socket` certain and manual;
     - the hyperframe import risk certain;
     - no other file changed.
   - The rewrites compile on 3.6 and 3.12.
   - **After apply**, `capture post-migration --mode imports` and `verify` give FAIL "imports regressed: hyperframe 3.2.0". The 3 import-time risks are prevented, hyperframe is a hit, and nothing is unpredicted.
2. **vibora.**
   - **assess:** `installs ✗ cython==0.28.3` with its cause.
   - **plan** with no baseline: 1 certain site, `vibora/templates/ast.py:1`, fixed. The invalid escapes are low. The `.pyx` skip is stated.
   - **After apply,** with Cython 0.29.37 built on 3.12, 17/17 subset test files collect (today 4/17).
3. **theano**, with the container running:
   - the numpy delta is not inconclusive;
   - `numpy.distutils` (`theano/gof/cmodule.py:23`) is certain;
   - `imp` (`theano/tests/breakpoint.py`) is certain and manual; `configparser.SafeConfigParser` is fixed;
   - `distutils` is medium where setuptools shims it;
   - guarded compat imports are listed with 0 risks.
   - With the container stopped: `numpy.distutils` is high, and the python delta is partial with a reason.
4. **lasagne.**
   - assess names Theano's sdist build failure and its cause, and does not label numpy/scipy/six as conflicts.
   - plan: 0 python risks, 2 guarded sites listed. Theano is named as the blocker.
5. **alphalens**, with the container runner:
   - the pandas 0.25.3 → 2.x delta is not inconclusive;
   - `pandas.util.testing` is REMOVED at the test sites, with its successor.
6. **pandas toy:**
   - the python delta is complete with 0 risks and 0 rewrites;
   - the verdicts (PASS / FAIL / PASS / FAIL), the calibration and delta accuracy (P 1.00, R 0.95) are unchanged;
   - a warm plan takes at most +2 s.
7. **New `bench/e2e_stdlib`** (3.8 → 3.12, host):
   - at least 25 labelled sites: attr, from and import forms; fallbacks in both orders; version guards; broad except; the shim on and off; a removed keyword; unittest aliases; escapes.
   - **Gates:**
     - precision 1.00 on certain/high, recall ≥ 0.95;
     - every fix compiles and imports on both versions;
     - every changed line contains a flagged spelling, and the changed-file count is reported next to pyupgrade's and Ruff's;
     - the Ruff differential test agrees on the `from collections` cases.
8. The injected runtime and the probes parse at `feature_version=(3, 6)`. The full suite passes. No network is used beyond assess's resolve and install, or `--online`.

## Phases

**Agreed order (owner, 2026-11-15):** the waves in `bench/legacy_eval/KEY_ISSUES.md`.
1. Wave 0 makes the instrument honest (P0) and scripts the evaluation.
2. Wave 1 runs two parallel tracks: 1a is the container runner (P4), 1b is stdlib detection (P1–P2).
3. Wave 2 adds the fixers (P3), plus a static old side read from the old release's source with griffe.
4. Wave 3 covers imports and installs (P5, P6).
5. Wave 4 covers knowledge and search (P7, plus the Theano and nose succession).

Each wave ends with a re-run of the scripted evaluation. The phase sizes below still apply.

| phase | content | size |
|---|---|---|
| P0 | Honesty and plumbing fixes from the evaluation (the ranked backlog is in `bench/legacy_eval/SUMMARY.md`): `plan --agent` crashing with a traceback on a resolver error, post-migration capture running outside the configured target env, relative `--project-dir` paths, verify's "no baseline" reason, assess stderr and blanket "conflict", subdirectory manifests in setup, one `setup.py` parser for scan and assess, empty-capture hints, name truncation, the setup container list, the no-op apply message | 2–3 d |
| P1 | `core/stdlib.py`, the lifetimes sweep plus VERSIONS, the curated changes table, `stdlib_sites` with guards, `reached_at`, lines and the per-file cache, scan exposure and `ceiling` | 3 d |
| P2 | The python pseudo-migration (target probe plus data), compile check, `predict_python_risks`, the D12 rule, plans without a baseline, wording | 3–4 d |
| P3 | Fixers: `rewrite-attr` plus import add/remove, `replace-attr-path`, the curated rules, dual-version re-compile, the minimal-diff gate, the Ruff differential test | 3 d |
| P4 | Container runner: `RunnerSpec`, envs/engine/probe through the runner, cache stamps, emulation timeouts, path mapping, `import_errors` | 4–5 d |
| P5 | `--mode imports`, the imports slots, the verify import layer and verdict, python calibration, `rejection` parsing, `site_warnings` | 4 d |
| P6 | Assess ladder: `target_check.py`, the explainer, culprit naming, the control run, dependency-import risks in plan | 3 d |
| P7 | Later: NumPy 1.24/2.0 facts, an `--online` search for the first target-importable release (hyperframe), traceback calibration of call-time sites, Python-2 report polish | S–M |

- **Order:** P0 → P1, then the critical path P2 → P3 → P5. P4 and P6 can run in parallel after P1.
- **Delivery:** P0–P3 alone make hyper and vibora honest and fixed with no environment. P4 unblocks every container baseline, dependency deltas included.

## Open questions for the owner

- **Q1. Policy change.** May `apply` accept an imports-mode baseline for plans whose changes are all python-scope (same-object renames)? vibora cannot produce a trace baseline, since its tests call no dependency. Dependency rewrites would still need a trace baseline.
- **Q2.** Should assess's install and import ladder be on by default (honest, but it builds an env), or opt-in, stated as "installs: not checked"?
- **Q3.** `async`/`await` as identifiers are valid on 3.6 and a SyntaxError from 3.7. The compile check finds them, but LibCST cannot parse them. Should they be manual, or get a text edit at the error location?
- **Q4.** Later: should static dependency sites adopt "tier = breakage if executed, `reached_at` separate" instead of BLIND → low? It changes existing calibration.

## Risks

- **Import observation runs project import-time code.** Mitigations: package modules only, `-B`, timeouts, an explicit capture mode, and the MCP exec gate.
- **Guard heuristics set the precision.** e2e_stdlib and the six artifacts are the regression set.
- **The data goes stale with each Python release.** The sweep is scripted, and CI cross-checks against real interpreters.
- **Emulated containers may time out on heavy imports.** That is reported as partial, never as "no changes".
