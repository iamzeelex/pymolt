# Delta accuracy: results

Host: macOS 27.2.1 arm64. Repo at `0a629e3` plus this uncommitted harness, branch
`exp/agent-surface-and-delta`, run 2026-11-14. Cold intermediate-fetch cache; warm uv cache.
Raw data: `results/flask_accuracy.json`, `results/pandas_accuracy.json`, `results/last_run.txt`.
Method: [README.md](README.md).

Engine: `run.py` now runs the shipped `pymolt.delta` engine, not the prototype's internal predictor. The numbers below were reproduced with it: pooled recall 0.95 and precision 1.00 (flask 0.89 / 1.00, pandas 1.00 / 1.00). The measured tables are unchanged.

## Outcome

**Pooled over both pairs, the local engine finds 21 of 22 API changes with no false
positive, in under 2 s per library.**

| | recall | precision | time per library |
|---|---|---|---|
| flask + werkzeug, API changes (9) | 0.89 | 1.00 | 1.6 s for both (incl. 1.2 s cold intermediate fetch) |
| flask + werkzeug, use-impacting (4) | 0.75 | 1.00 | — |
| pandas, API changes (13) | 1.00 | 1.00 | 1.9 s |
| pandas, use-impacting (2) | 1.00 | 1.00 | — |
| **pooled, API (22)** | **0.95** (21/22) | **1.00** (0 FP) | < 2 s each |
| **pooled, use-impacting (6)** | **0.83** | **1.00** | |

- **Time:** every library finishes in under 2 s, excluding the environment build.
- **Hints:** local recovered the correct successor for all 3 removed symbols.

## flask 2.0.3 + werkzeug 2.0.0 (py3.9) → flask 3.0.0 + werkzeug 3.0.0 (py3.12)

The setup: 55 touched paths, 49 from `pymolt contract map tests/artifacts/flasgger` plus 6
instance methods/overrides flasgger uses. They collapse to 47 dependency-owned units.

Units with any finding:

| unit | ground truth (use evidence) | local |
|---|---|---|
| `flask.Markup` | REMOVED (`Markup(...)`: ok → AttributeError) | REMOVED ✓, hint `markupsafe.Markup` (from 2.3.3) |
| `flask.json.JSONEncoder` | REMOVED (import: ok → ImportError) | REMOVED ✓, hint `Flask.json` (from 2.2.5) |
| `flask.__version__` | DEPRECATED (works, DeprecationWarning "removed in Flask 3.1") | DEPRECATED ✓ |
| `flask.request.json` | BEHAVIOR (non-JSON body: `None` → `UnsupportedMediaType` 415) | — (miss) |
| `flask.abort` | SIGNATURE (`status` → `code`; now `flask.helpers.abort`) | ✓ |
| `flask.request.headers.get` | SIGNATURE (`as_bytes` removed, werkzeug 3.0) | ✓ |
| `flask.views.MethodView.dispatch_request` | SIGNATURE (`*args` removed; flasgger's override still works) | ✓ |
| `flask.Flask` | BASES (`Scaffold` → sansio `App`) | ✓ |
| `flask.Blueprint` | BASES (`Scaffold` → sansio `Blueprint`); `Blueprint(...)` works | ✓ |

Without the 6 grep-derived instance methods (static map only): local P 1.00 / R 0.88.

## pandas 1.5.3 (py3.11) → 2.2.3 (py3.12), toy app symbols

14 touched paths → 19 units (public and defining spellings are separate units, so the
`Series` constructor change counts three times).

| unit | ground truth | local |
|---|---|---|
| `Series.append` | REMOVED (`s.append(...)` → AttributeError) | ✓, hint `pandas.concat` from the old env's own warning |
| `DataFrame.mean` | SIGNATURE (`numeric_only` None → False) + BEHAVIOR (`{'v': 2.0}` → TypeError) | both ✓ (BEHAVIOR predicted by the baseline FutureWarning) |
| `DataFrameGroupBy` | SIGNATURE (`squeeze`, `mutated` removed) | ✓ |
| `Series.to_dict` | SIGNATURE (`into` keyword-only) | ✓ |
| 9 more (`Series`, `concat`, `groupby`, `GroupBy`, `GroupBy.sum`, …) | SIGNATURE (copy / axis / observed / numeric_only defaults) | all ✓ |

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

## Caveats

1. **Shared mechanism.** For the API-level categories, ground truth and the local probe both
   use introspection, so local's API precision is partly by construction. The independent
   checks are use-based: they confirm every local removal and supply the one behaviour
   change (`request.json` → 415) that the static probe misses. That change is tier 1's job.
2. **`Series.to_dict` is overstated.** Its keyword-only signature comes from a
   `deprecate_nonkeyword_arguments` decorator: positional calls still work with a
   FutureWarning, so introspection overstates it.
3. **Small sample.** Two projects, three libraries, 22 API changes. The `DEPRECATED` rule was
   tightened after seeing pandas; the looser variant is reported alongside.
4. **Range requests barely help small wheels.** Flask wheels are about 100 KB, and a 64 KB
   tail plus one member is not much smaller. The technique matters for large wheels such as
   pandas and TF, which this run did not measure.
