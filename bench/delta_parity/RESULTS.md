# Delta parity: results

Host: macOS 27.2.1 arm64. Repo at `0a629e3` plus this uncommitted harness, branch
`exp/agent-surface-and-delta`, run 2026-11-14. Cold intermediate-fetch cache; warm uv cache.
Raw data: `results/flask_parity.json`, `results/pandas_parity.json`, `results/last_run.txt`.
Method and rule: [README.md](README.md).

## Outcome

**The decision rule passes: retire Axiom Graph as a runtime service.**

| | local recall | Axiom recall | local precision | Axiom precision | local time per library |
|---|---|---|---|---|---|
| flask + werkzeug, API changes (9) | **0.89** | 0.22 | **1.00** | 0.22 | 1.6 s for both (incl. 1.2 s cold intermediate fetch) |
| flask + werkzeug, use-impacting (4) | **0.75** | 0.25 | **1.00** | 0.14 | — |
| pandas, API changes (13) | **1.00** | 0.77 | 1.00 | 1.00 (tie) | 1.9 s |
| pandas, use-impacting (2) | **1.00** | 0.50 | 1.00 | 1.00 (tie) | — |
| **pooled, API (22)** | **0.95** (21/22) | 0.55 (12/22) | **1.00** (0 FP) | 0.63 (7 FP) | < 2 s each, rule is < 10 s |
| **pooled, use-impacting (6)** | **0.83** | 0.33 | **1.00** | 0.25 | |

- **Recall:** local ≥ Axiom everywhere.
- **Precision:** local is higher pooled and on flask. Pandas is a tie at 1.00, and Axiom only
  reaches it there through a forwarding join that needs the local probe (see caveat 3).
- **Time:** every library finishes in well under 10 s.
- **Hints:** local recovered the correct successor for all 3 removed symbols. Axiom's
  checkpoints have none.
- **Overturn condition:** no project-relevant change found by Axiom was out of reach of the
  two endpoints plus one intermediate release.

## flask 2.0.3 + werkzeug 2.0.0 (py3.9) → flask 3.0.0 + werkzeug 3.0.0 (py3.12)

The setup:
- 55 touched paths: 49 from `pymolt contract map tests/artifacts/flasgger`, plus 6 instance
  methods/overrides flasgger uses. They collapse to 47 dependency-owned units.
- Axiom: flask checkpoint 15 steps / 280 changes; werkzeug 20 unique steps / 141 changes.
  **15 of the 20 werkzeug steps (2.1.1 → 3.0.0) are empty**, so they were recorded as
  analysed with zero changes.

Units with any finding:

| unit | ground truth (use evidence) | local | Axiom |
|---|---|---|---|
| `flask.Markup` | REMOVED (`Markup(...)`: ok → AttributeError) | REMOVED ✓, hint `markupsafe.Markup` (from 2.3.3) | — (miss) |
| `flask.json.JSONEncoder` | REMOVED (import: ok → ImportError) | REMOVED ✓, hint `Flask.json` (from 2.2.5) | REMOVED ✓ (hint text `deprecated:: 2.`) |
| `flask.__version__` | DEPRECATED (works, DeprecationWarning "removed in Flask 3.1") | DEPRECATED ✓ | REMOVED ✗ |
| `flask.request.json` | BEHAVIOR (non-JSON body: `None` → `UnsupportedMediaType` 415) | — (miss) | — (miss) |
| `flask.abort` | SIGNATURE (`status` → `code`; now `flask.helpers.abort`) | ✓ | — (miss) |
| `flask.request.headers.get` | SIGNATURE (`as_bytes` removed, werkzeug 3.0) | ✓ | — (miss; empty werkzeug steps) |
| `flask.views.MethodView.dispatch_request` | SIGNATURE (`*args` removed; flasgger's override still works) | ✓ | ✓ |
| `flask.Flask` | BASES (`Scaffold` → sansio `App`) | ✓ | — (miss) |
| `flask.Blueprint` | BASES (`Scaffold` → sansio `Blueprint`); `Blueprint(...)` works | ✓ | SIGNATURE ✗ (`Blueprint.__init__` "removed") |
| `flask.Flask.route` | none (`app.route` works in 3.0) | — | REMOVED ✗ |
| `flask.Flask.register_blueprint` | none (works) | — | REMOVED ✗ |
| `flask.Blueprint.add_url_rule` | none (works) | — | REMOVED ✗ |
| `flask.current_app.url_map` | none (`iter_rules()` works) | — | REMOVED ✗ |
| `flask.current_app.view_functions` | none (works) | — | REMOVED ✗ |

All six of Axiom's false `REMOVED` claims fall on members that Flask 3.0 moved into Sans-IO
base classes. Use proves each one still works. Without the 6 grep-derived instance methods
(static map only): local P 1.00 / R 0.88, Axiom P 0.20 / R 0.12.

## pandas 1.5.3 (py3.11) → 2.2.3 (py3.12), toy app symbols

14 touched paths → 19 units (public and defining spellings are separate units, so the
`Series` constructor change counts three times on both sides). Axiom: the 1.5.3 → 2.0.0
step of `pandas__1.3.5__2.0.0.jsonl` (3,617 changes).

| unit | ground truth | local | Axiom |
|---|---|---|---|
| `Series.append` | REMOVED (`s.append(...)` → AttributeError) | ✓, hint `pandas.concat` from the old env's own warning | ✓ (hint text `deprecated:: 1.`) |
| `DataFrame.mean` | SIGNATURE (`numeric_only` None → False) + BEHAVIOR (`{'v': 2.0}` → TypeError) | both ✓ (BEHAVIOR predicted by the baseline FutureWarning) | SIGNATURE ✓ only via the forwarding join to `NDFrame.mean`; BEHAVIOR miss |
| `DataFrameGroupBy` | SIGNATURE (`squeeze`, `mutated` removed) | ✓ | miss |
| `Series.to_dict` | SIGNATURE (`into` keyword-only) | ✓ | miss (possibly post-2.0.0, see window caveat) |
| 9 more (`Series`, `concat`, `groupby`, `GroupBy`, `GroupBy.sum`, …) | SIGNATURE (copy / axis / observed / numeric_only defaults) | all ✓ | all ✓ |

`local_any_static_warn` (the looser DEPRECATED rule) adds 5 false positives here. Those are
argument-conditional warnings in class bodies. The shipped strict rule has none.

## Intermediate fetch (flask, cold cache)

| symbol | releases tried (newest first) | last release with it | mined prose | cost |
|---|---|---|---|---|
| `flask.Markup` | 2.3.3 ✓ | 2.3.3 | "'flask.Markup' is deprecated … Import 'markupsafe.Markup' instead." | 0.65 s, 4 requests, 156 KB |
| `flask.json.JSONEncoder` | 2.3.3 ✗, 2.2.5 ✓ | 2.2.5 | "'JSONEncoder' is deprecated … Use 'Flask.json' to provide an alternate JSON implementation instead." | 0.54 s, 6 requests, 136 KB |

- The total of 292 KB includes 89 KB of PyPI release JSON. Only package names and
  versions were sent.
- Bisection was tried first and failed. Flask's history is not monotonic: 2.3.0 dropped
  `flask.Markup` by accident and 2.3.1 restored it as a deprecated alias (flask CHANGES).
  The search is now newest-patch-per-minor first.

## Timing (seconds, wall, including interpreter start)

| step | flask+werkzeug | pandas |
|---|---|---|
| probe, old env | 0.18 | 0.90 (mostly `import pandas`) |
| probe, new env | 0.13 | 0.66 |
| baseline-run warnings (old env) | 0.08 | 0.31 |
| diff | 0.002 | 0.001 |
| intermediate fetch (cold) | 1.19 | 0 (old env already warns) |
| **local total** | **1.58** | **1.86** |
| not counted: contact map (tier-1 input) | 2.3 | — |
| not counted: env build | 0.55 / 0.37 (warm uv cache; cold not measured) | prebuilt |

Axiom's own compute time is not recorded in the checkpoints, so it was not measured.

## Caveats

1. **Shared mechanism.** For the API-level categories, ground truth and the local probe both
   use introspection, so local's API precision is partly by construction. The independent
   checks are use-based, and they refute all six Axiom false removals, confirm every local
   removal, and supply the one behaviour change (`request.json` → 415) that both static
   approaches miss. That change is tier 1's job.
2. **Checkpoints, not the live service.** The Axiom side is the cached checkpoint data. Its
   propose stage (not cached) would add codemods for removals it *detected*, such as
   `append → concat`, which an earlier local run of Axiom's own engine reproduced. It
   cannot help where the removal was never detected (`flask.Markup`), nor with the 15
   empty werkzeug steps. A fresh service run might do better on werkzeug.
3. **Matching is generous to Axiom.** Changes match through public, owner, receiver,
   defining, `__init__` and forwarding-target spellings. Without the forwarding join,
   pandas Axiom recall is 9/13. The join itself needs the local probe.
4. **Pandas window mismatch.** Axiom's knowledge ends at 2.0.0 while the target is 2.2.3, so
   its two pandas SIGNATURE misses may be post-2.0 changes. `Series.to_dict`'s keyword-only
   signature comes from a `deprecate_nonkeyword_arguments` decorator: positional calls still
   work with a FutureWarning, so introspection overstates it.
5. **Small sample.** Two projects, three libraries, 22 API changes. The `DEPRECATED` rule was
   tightened after seeing pandas; the looser variant is reported alongside.
6. **Range requests barely help small wheels.** Flask wheels are about 100 KB, and a 64 KB
   tail plus one member is not much smaller. The technique matters for large wheels such as
   pandas and TF, which this run did not measure.
