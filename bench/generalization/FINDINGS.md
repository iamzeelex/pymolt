# Generalization matrix: findings (2026-11-15)

Run: `bench/generalization/results/wave2/` (pymolt `wave2/fixers-and-successors`, 3829b01). Ten toys: pandas (the reference), pydantic, SQLAlchemy, Django, Jinja2, click, PyYAML, urllib3, networkx and stdlib. Every toy's ground truth was checked by running both versions. The hypotheses come from `SPEC.md`.

## Verdict: there is a skew, and it sits in the runtime evidence layer, not in API detection

Only pandas got the expected verdicts in both scenarios.

The **delta engine generalizes**. Leaving out BEHAVIOR entries, which no API comparison can find, it detected:

| kind | detected |
|---|---|
| MOVED | 7/7 |
| REMOVED | 22/30 |
| SIGNATURE | 7/11 |
| DEPRECATED | 6/14 (pydantic's capture failed, and it holds most of the misses) |

Imports on Django needed no settings (S3 not observed). The stdlib Python fixers rewrote 4/4 correctly.

The **runtime evidence layer did not generalize.** That covers the tracer, the value renderers, verify's pairing, and warning capture. The pandas toy happened to avoid every weak spot:
- its dependency API is pure Python;
- its return values are DataFrames and Series, which have structural renderers;
- the plan, not a person, made the edits;
- its breaks happen inside the callee;
- its test framework produces stable inputs.

## pymolt defects found (with evidence)

| id | Defect | Evidence (results/wave2/…) | Severity |
|---|---|---|---|
| G1 | **Compiled (Cython/C) dependency APIs are invisible to the tracer.** Compiled pydantic 1.10 gives "capture produced zero events"; vibora (ujson) also had 0 events in the legacy run. | `pydantic/equiv/capture_baseline.log` | high |
| G2 | **Stdlib calls are not traced.** Python-version migrations get no runtime evidence (the stdlib baseline held one pytest event). Replacing a dependency call with a stdlib one (`click.get_terminal_size` → `shutil.get_terminal_size`, `ChainMap`) reads as DISAPPEARED, a false FAIL. | `stdlib/*/verify.json`, `click/equiv/verify.json` | high |
| G3 | **verify can't pair an edited call when its input or symbol changes at the same site.** `Retry.__init__(method_whitelist=…)` *disappeared* and `Retry.__init__(allowed_methods=…)` *appeared* at the same `client.py:13`, both returning normally. The same holds for attribute renames and manual replacements. Equivalent hand migrations FAIL (click, networkx, SQLAlchemy, urllib3). | `urllib3/equiv/verify.json` | high |
| G4 | **A call that fails while binding its arguments is invisible.** PyYAML 6's `yaml.load(s)` raises TypeError before the callee runs, so it is recorded as DISAPPEARED rather than "now raises TypeError". That understates signature breaks and loses calibration hits. | `pyyaml/change/verify.json` (`settings.py:56`) | medium |
| G5 | **Generic objects compare by type only (S1 confirmed).** click's `Result.exit_code` change (0 → 2) went unseen. SQLAlchemy is 75% opaque. A relocated class gives a false CHANGED (`declarative_base` returns a `Base` now defined in `orm.decl_api`). Structural renderers exist only for numpy/pandas, stdlib types and dataclasses. | `click/change`, `sqlalchemy/*` | high |
| G6 | **Test-framework calls are recorded as dependency calls.** `_pytest.monkeypatch.syspath_prepend(<tmp path>)` gets a new input every run, so pairing fails and the equivalent stdlib migration FAILs. | `stdlib/equiv/verify.json` | medium |
| G7 | **Warnings raised from C are not captured (S2 confirmed).** That is 0/4 C-level warnings observable post-migration, and 0/1 on networkx's old side. PyYAML's **Python-level** `YAMLLoadWarning` was also missed (0/2): cause unknown. | `pyyaml`, `stdlib`, `networkx` E | medium |
| G8 | **Successor search:** renames to a *different* object (`force_text` → `force_str`, `Graph.node` → `nodes`, `.dict()` → `model_dump`, `contextfunction` → `pass_context`) are not found when the old version gives no deprecation prose. The same-object ranking also prefers an incidental re-export (`jinja2.compiler.escape`) over the object's own home (`markupsafe.escape`, its `__module__`). | `A.per_entry` successors | medium |
| G9 | Generator and iterator results are recorded as "generator", so behaviour inside a stream is invisible (urllib3 `stream()` chunk sizes, `yaml.load_all`). | `urllib3/change` | low (known design limit) |
| G10 | Django's delta old side built throwaway envs instead of reusing the baseline interpreter, unlike PyYAML. Unexplained. | `F.delta_sides` | low |
| G11 | Old sdists need build constraints: PyYAML 5.4.1 on 3.11 needs `Cython<3`. pymolt's throwaway envs would hit this. | env build logs | low |

## Not pymolt (fix in the bench)
- **Django toy:** a multi-line manual edit no longer matched after pymolt's own `ugettext` rewrite. `six` stayed, so the post capture was red. Split the edit.
- **Runner:** A should leave out BEHAVIOR entries; they belong to verify. `expected_changed` should also count DISAPPEARED-at-site and raise-now as detection, at least until G4 is fixed. C needs per-rewrite attribution.

## Proposed fix order (a generality wave before Wave 3)

Acceptance is this matrix: for every toy, equiv gives PASS and change gives FAIL with the expected symbol CHANGED or now raising, plus no regression on pandas or the six legacy projects.

1. G6 (exclude test-runner packages from the boundary by default) and the bench fixes. Small.
2. G3 + G4: verify pairs calls at the same site, symbol or line, even when their input differs, comparing outcomes; binding failures are recorded as raises.
3. G5: a generic, side-effect-free structural renderer covering instance `__dict__` (no properties or descriptors run), `__slots__`, attrs, namedtuple and pydantic fields. Moved classes compare by name and shape, not by module path.
4. G2: during a Python move, trace the stdlib modules the static stdlib sites touch.
5. G1: record compiled calls as identity plus outcome, through sys.monitoring `CALL`/`C_RETURN`/`C_RAISE`, setprofile `c_call`, or wrappers on module-level C functions.
6. G7: capture C-level warnings through an observer filter, the way `catch_warnings(record=True)` works, while keeping the program's own filter decisions; and debug the PyYAML miss.
7. G8: rank same-object successors by the object's `__module__` home, and mine new-side docstrings, `__deprecated__` and release notes for rename successors.

G9–G11 come later.

## Known limits after Wave G
- **Keyword arguments forwarded through `**attrs`:** a keyword the new callable only forwards (click `argument(autocompletion=)`) is invisible to a signature comparison.
- **Dotted names in strings:** a name only spelled inside a string (jinja2 `extensions=["jinja2.ext.with_"]`) is not a static contact.
- **Implicit C-slot calls on 3.9-3.11 compiled types:** `row["name"]` on SQLAlchemy 1.3 `RowProxy` dispatches through a C slot that no tracer observes.
- **Attributes of a replaced function's result:** `ArgSpec.keywords` is reached through `inspect.getargspec(...)`, which is itself gone, so nothing is probed for its result.
