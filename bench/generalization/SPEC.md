# Generalization matrix: is pymolt skewed toward pandas and numpy?

Every acceptance gate so far was pandas-centred: the e2e toy, delta parity (pandas + flask), and four of the six legacy projects in the numpy/pandas world. This bench measures the same funnel on migrations from **other ecosystems**, each with a **verified ground truth**, so a skew shows up as numbers rather than impressions.

## Hypotheses from the code audit (2026-11-15)

| id | Hypothesis | Evidence |
|---|---|---|
| S1 | Value renderers are structural only for numpy/pandas (`verify/_normalize.py:185-193`) and stdlib types. Other libraries' objects (pydantic models, attrs, SQLAlchemy rows, namedtuples?) compare by type only, so verify catches fewer behaviour changes there. | code |
| S2 | Runtime warning capture wraps the Python-level `warnings.warn` only. Warnings raised from C (`PyErr_WarnEx`) bypass it. | **confirmed:** `datetime.utcnow()` on 3.12 warns from C and the wrapper did not see it |
| S3 | The probe has one context (Flask's request context). A framework with side effects at import time (Django needs settings) may raise on import, and its deltas end up in `import_errors` / INCONCLUSIVE. | code |
| S4 | Tier and noise calibration was tuned on pandas: many DEFAULT changes, and FutureWarning everywhere. Libraries that warn differently, or not at all, may get fewer correct high-tier predictions, or more noise. | design |
| S5 | Knowledge (codemod rules, succession) is per-ecosystem and small. That is coverage, not a mechanism skew, but it shapes the results. | data |

## Toy format: `bench/generalization/toys/<name>/toy.json`

```json
{
  "name": "pyyaml", "ecosystem": "serialization",
  "old": {"python": "3.11", "requirements": ["PyYAML==5.4.1", "pytest"]},
  "new": {"python": "3.12", "requirements": ["PyYAML==6.0.2", "pytest"]},
  "manifest": ["PyYAML"],
  "target_manifest": ["PyYAML==6.0.2"],
  "files": {"app/__init__.py": "", "app/core.py": "...", "tests/test_core.py": "..."},
  "manual_migration": [{"file": "app/core.py", "old": "yaml.load(s)", "new": "yaml.safe_load(s)"}],
  "scenarios": {
    "equiv":  {"edits": [], "expected_verdict": "pass"},
    "change": {"edits": [{"file": "app/core.py", "append": "..."}, {"file": "tests/test_core.py", "append": "..."}],
               "expected_verdict": "fail", "expected_changed": ["yaml.dump"]}
  },
  "ground_truth": [
    {"symbol": "yaml.load", "kind": "SIGNATURE", "param": "Loader", "site": "app/core.py:12",
     "breaks": "TypeError: load() missing 1 required positional argument: 'Loader'",
     "successor": null, "fixable": "manual", "source": "PyYAML 6.0 changelog"}
  ],
  "expected_warnings": [{"symbol": "yaml.load", "category": "YAMLLoadWarning", "from_c": false}],
  "notes": "..."
}
```

- `manifest` is the unpinned `requirements.txt`, as in the pandas e2e. The baseline capture's record supplies the real old versions.
- `manual_migration`: edits applied **after** `pymolt apply`. They stand for the person finishing what pymolt could not fix automatically, so the equivalent scenario ends with equal behaviour.
- `ground_truth[].kind` is one of `REMOVED | MOVED | SIGNATURE | DEFAULT | DEPRECATED | BEHAVIOR`, defined as below (Wave G round 4). A label is only changed with recorded evidence (identity and existence checks run in both environments) in the entry's `verified`.

  | kind | definition | the test that decides it |
  |---|---|---|
  | `MOVED` | the **same object** is reachable under a new spelling | in the old environment `old_spelling is new_spelling`; in the new one the old spelling is gone and the new one resolves. Example: django 2.2 `ugettext is gettext`; networkx 2.3 `Graph.node is Graph.nodes` |
  | `REMOVED` | the **spelling is gone**. A successor, if any, is a rename to a *different* object | old spelling exists, new does not, and the successor is not the same object in the old environment. Example: jinja2 `contextfunction` -> `pass_context`; `imp.reload` -> `importlib.reload` |
  | `SIGNATURE` | the call **as written binds differently or fails**: a removed or renamed parameter, a parameter that lost its default (now required), a changed kind | `inspect.signature` differs in a way an existing call notices |
  | `DEFAULT` | a **default value changed** and calls still bind | the same parameters, a different default |
  | `DEPRECATED` | it **still works and warns** in the new version | the old spelling resolves in the new environment, calling it emits a deprecation warning |
  | `BEHAVIOR` | the **same API, a different result** or raise. Also: a consequence of switching to a successor with a different return type (`numpy.matrix.A1` after `to_numpy_array`, which still exists in numpy) | the name resolves on both sides; outputs differ |

  The scorer counts a member-level REMOVED under a removed module (`django.utils.six.string_types`) as detecting that module's removal.
- Every ground-truth entry is **verified by running it** in the old env and the new env directly (plain Python, no pymolt). Record the observed old/new behaviour in `verified`.

## Metrics per toy (computed by `run.py`)

| key | what | from |
|---|---|---|
| A delta | per ground-truth entry: detected with the right kind, or missed; facts at touched symbols not in the ground truth (FP, judged by re-running); successor correct | `plan` JSON `api_impacts` vs ground truth |
| B prediction | the change scenario's breaking call predicted, at which tier; calibration hit / prevented / miss / not exercised / unpredicted | `plan` risks and `verify` calibration |
| C rewrites | proposed; correct (equiv tests pass after apply + manual); wrong | `plan` diffs and test runs |
| D verdict | equiv gives PASS, change gives FAIL, with `expected_changed` among the CHANGED symbols; share of facts compared at exact level vs type level (S1) | `verify` JSON fact diff |
| E warnings | expected warnings recorded on the baseline facts, split Python-level vs C-level (S2) | baseline capture facts |
| F probe | `import_errors` / INCONCLUSIVE deltas per package (S3) | `plan` JSON deltas |

Plus wall time per phase. The scorecard has one row per toy, and adds pandas (`bench/e2e_pandas`) as the reference row.

## Toys
| toy | ecosystem | old → new | old python | why it's here |
|---|---|---|---|---|
| pydantic | validation / web | 1.10 → 2.x | 3.11 | renames with deprecation shims (`.dict()` → `model_dump`), `Field(regex=)`, validators, `Config` |
| sqlalchemy | ORM / DB (sqlite) | 1.3 → 2.0 | 3.9 | `engine.execute` removed, `select([...])`, Row objects (S1) |
| django | web framework | 2.2 → 4.2/5.x | 3.9 | settings at import time (S3), `url()` → `re_path`, `ugettext`, `force_text` |
| jinja2 | templating | 2.11 → 3.1 (+ MarkupSafe) | 3.9 | `jinja2.Markup` / `escape` → `markupsafe` (a cross-package same-object successor), `contextfunction` → `pass_context` |
| click | CLI | 7.1 → 8.x | 3.11 | `get_terminal_size` removed, parameter callback/autocompletion changes |
| pyyaml | serialization | 5.4 → 6.0 | 3.11 | `yaml.load` without a Loader (a new required parameter), `YAMLLoadWarning` |
| urllib3 | HTTP (no network) | 1.26 → 2.x | 3.11 | `Retry(method_whitelist=)`, `HTTPResponse.getheaders`, objects with attributes (S1) |
| networkx | graphs | 2.x → 3.x | 3.9 | `G.node`, `to_numpy_matrix`, `nx.info` removed |
| stdlib | Python itself | stdlib 3.8 → 3.12 | 3.8 | asyncio `loop=` removal (stdlib SIGNATURE), `imp`, `distutils.version`, collections ABCs, `unittest` aliases; plus a **C-level** deprecation (S2) |

Toy apps are small (one module, about 5–15 dependency calls, real usage patterns) with a pytest suite. Each runs in under 30 s.
