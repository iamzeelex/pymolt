# Wave 2: Python fixers, same-object successors, the static old side, and the cold scan

Status: in progress, 2026-11-15. Base: `wave1/runner-and-stdlib` (9ef1336).

| Track | Source |
|---|---|
| 2a fixers | design P3 in `docs/design/python-version-breakage.md` |
| 2b same-object successor search | found by the Wave 1 evaluation (alphalens `pandas.util.testing`) |
| 2c static old side | griffe over the old release's source, old side only (`bench/legacy_eval/KEY_ISSUES.md`) |
| 2d cold scan | B6: theano's cold scan took 221 s |

## Acceptance

Re-run `bench/legacy_eval/run.py`:
- **hyper:** the plan fixes 6 of 8 certain python risks, in 3 files:
  - `hyper/http11/connection.py:13` and `:393`, `hyper/common/headers.py:13`, and `test/test_import.py` (`imp.reload` → `importlib.reload`);
  - `import collections.abc` is added where an attribute is rewritten;
  - `ssl.wrap_socket` stays manual;
  - no other file changes;
  - after apply, `import hyper` on 3.12 gets past hyper's own code (the next failure is in hyperframe, a dependency, which Wave 3 handles).
- **vibora:** `vibora/templates/ast.py:1` is fixed. `asyncio.Task.current_task()` → `asyncio.current_task()` is fixed, flagged `runs_on_base: false`, since 3.7 is newer than the 3.6 base.
- **alphalens:** `pandas.util.testing.assert_frame_equal` / `assert_series_equal` get the successor `pandas.testing.*` (the same object on the old side), and the plan rewrites the test imports.
- **Minimal diff:** every changed line holds a flagged spelling. The changed-file count is reported next to pyupgrade's 45 and Ruff's 43 on hyper.
- **Re-compile:** every rewritten file compiles on the target, and on the base runner when the successor existed there.
- **Static old side:** with no container running, dependency deltas come back `partial` (basis static) instead of INCONCLUSIVE. On alphalens and zipline, a static fact never contradicts a container-probed fact. In particular there are no false REMOVED from unresolved bases.
- **Theano's cold scan** is at most the old 80 s on an idle machine, and the warm scan is unchanged.
- The pandas e2e (4/4), delta accuracy (P 1.00, R 0.95), the full suite and the 3.6 parse checks are unchanged.

## Contracts

### Codemod patterns (2a owns `codemods/models.py`, `codemods/apply.py`, `delta/provider.py`)
`CodemodPattern` gains three fields:
- `requires_import: str | None = None`: a module the rewritten code needs, for example `collections.abc`;
- `scope: str = "dependency"`: `"python"` for stdlib fixes;
- `runs_on_base: bool | None = None`: whether the rewritten code also runs on the base Python.

Kinds:
- `rewrite-import`, for `from collections import Iterable` → `from collections.abc import Iterable`, splitting mixed imports;
- `rewrite-attr`, for `collections.Iterable` → `collections.abc.Iterable`, plus LibCST `AddImportsVisitor` for `requires_import`;
- new `replace-attr-path`, for a cross-module rename such as `imp.reload` → `importlib.reload` or `asyncio.Task.current_task` → `asyncio.current_task`. It adds the new import, and `RemoveImportsVisitor` drops an import that becomes unused.

Rules:
- the existing kinds behave as before;
- a guarded fallback site is never rewritten;
- binding-aware, as today: `import numpy as collections` is not touched.

### Python fix recipes (2a)
`pymolt/pyversion/fixes.py::python_patterns(report, sites) -> list[CodemodPattern]` turns a python `DeltaReport` plus `core.stdlib.changes_for(...).fix` into patterns:
- `fix: advisory` gives no pattern, and the site stays manual;
- `same_object` or `successor_since <= base` gives `runs_on_base: true`.

`build_migration_plan` feeds these into the same preview as dependency patterns. The plan records `fixed_by_plan` for python risks through the existing `_fix_for`.

### Same-object successors (2b owns `delta/probe.py`'s identity search and `delta/engine.py`'s diff and successor logic)
- **Old side:** for a touched spelling, the probe records `id()` and looks for the same object under candidate public spellings in the **old** interpreter. Candidates are:
  - the package's top-level exports;
  - public submodules one level down, such as `pandas.testing`, `pandas.errors` and `pandas.api.types`;
  - knowledge hints.

  The probe returns `same_object_as: [spelling, …]`. It stays stdlib only and 3.6-compatible.
- **Engine:** when a spelling vanished on the new side and a `same_object_as` candidate exists on the new side, it emits `MOVED` with that `successor`. It sets `detail.successor_importable = True` and `detail.successor_same_signature = True` (identity implies the same signature), so the existing provider `_pattern` produces the rewrite.

### Static old side (2c owns `delta/static_side.py` (new), `delta/acquire.py` (new), and in `delta/envs.py` the final fallback of `resolve_old`; in `delta/engine.py` it edits only side selection, while 2b edits only diffing)
- It returns **probe-shaped results** for the touched symbols, built from the old release's wheel or sdist with griffe (already a dependency). The sources are uv's cache, else a fetch (allowed only under `--online` or assess's network policy). The results hold: existence, kind, parameters (names, kinds, literal defaults only), owner, and `warnings.warn`/docstring deprecation prose.
- Meta `{"basis": "static", "source": "<wheel|sdist> <file>"}`.
- **Traps** (verified on flask 2.0.3 → 3.0.0):
  - an unresolved base class or namespace subpackage means *unknown*, never absent;
  - follow aliases and re-exports regardless of `__all__`;
  - PEP 562 `__getattr__` names come from `delta/fetch.py`'s existing scope reader;
  - compare a default value only when both sides are literals.
- The engine uses static results only when no old interpreter is reachable. Facts then carry `basis {"old": "static"}`, and the report quality is at most `partial`.
- Extraction caps every read, refuses `..` members and unpacks zips safely.

### Cold scan (2d owns `verify/contact_map.py`'s file walk and parse scheduling)
- Parse cache misses in a process pool sized by CPU count, falling back to serial for small projects or when spawning fails.
- Output is unchanged and deterministic: the same sort order and the same cache files.
- `PositionProvider` stays.
- Measure on `tests/artifacts/theano`: cold and warm, before and after.

## File ownership

| WP | Owns |
|---|---|
| 2a | `codemods/models.py`, `codemods/apply.py`, `codemods/service.py`, `delta/provider.py`, `pyversion/fixes.py` (new) and its use in `pyversion/plan.py`, `migration_plan.py`, `knowledge/stdlib_changes.json` (`fix` fields only), the plan view and render (`views.py` / `commands.py`, plan functions only), and tests |
| 2b | `delta/probe.py` (the identity search), and in `delta/engine.py` the diff and successor functions (`diff`, `_group_facts`, `_local_hint`, `_successor_candidates`), plus tests |
| 2c | `delta/static_side.py`, `delta/acquire.py`, the `resolve_old` fallback in `delta/envs.py`, side selection in `delta/engine.py` (`run_probe` callers, `_side_ok`, compute), and tests |
| 2d | `verify/contact_map.py` (the walk and pool only), plus tests |
