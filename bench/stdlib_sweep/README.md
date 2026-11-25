# stdlib_sweep

Repo-only generator for `pymolt/knowledge/stdlib_lifetimes.json` (never hand-edited, never shipped logic: only its output ships).

## What it produces

- `modules`: lifetimes from typeshed `stdlib/VERSIONS` (vendored snapshot `typeshed_VERSIONS.txt`, see `typeshed_VERSIONS.meta.json`) plus the Python 2 table in `pymolt/core/stdlib.py`. `[first, last]`, `last: null` = still present.
- `names`: dotted names whose presence **changes** across the swept interpreters, with `present: [first, last]` (and `gaps` when a name vanishes and returns). A name already present on the oldest interpreter swept (3.6) starts at its module's first version.
- `stable`: per module, the names present on every swept interpreter where the module imports (kept compact: they carry no lifetime information beyond "always").
- `submodules`: modules found by the walk that typeshed lists only through a parent (so `present_in('distutils.sysconfig', v)` can answer from the parent's lifetime).
- `pythons`: the exact interpreter versions swept; `source`, `date`.

## How the names are found

Two passes over real interpreters (3.6 and 3.7 from the `python:3.6` / `python:3.7` images, 3.8-3.14 from uv; all on linux/amd64 so platform-specific names do not look like removals):

1. `collect` imports every stdlib top-level module (typeshed + `sys.stdlib_module_names`) and, for packages, every public submodule, and records public names (`dir()` entries that are not modules, plus `__all__`). It also goes one class level deep for the classes in `CLASSES` (e.g. `unittest.TestCase`, `logging.Logger`, `ssl.SSLContext`).
2. `check` asks `hasattr` on **every** interpreter for the union of all names seen anywhere. This is not optional: on 3.7-3.9 the `collections` ABC aliases are served by module `__getattr__` and invisible to `dir()`, so a `dir()`-only sweep finds zero `collections.*` removals.

Modules that fail to import for a reason other than being absent (a missing `_tkinter`, say) make their names *unknown* on that interpreter, not removed. Modules that are re-exported as attributes are dropped. Skipped on purpose: `antigravity`, `this`, `idlelib`, `turtledemo`, `test*`, `encodings.*`, `lib2to3.fixes`, private modules.

## Re-running

```bash
# needs docker (images are pulled on first use) and uv; ~5 minutes
uv run --python 3.12 python bench/stdlib_sweep/sweep.py            # writes pymolt/knowledge/stdlib_lifetimes.json
uv run --python 3.12 python bench/stdlib_sweep/sweep.py --only 3.11 3.12   # dev: prints counts, writes nothing
uv run --python 3.12 python bench/stdlib_sweep/sweep.py --native   # 3.8+ via the host uv (skewed on macOS)
```

Docker use is `docker run --rm` one-shots (a named volume `pymolt_stdlib_sweep_uv` caches the uv-downloaded interpreters).

To add a Python version: append it to `UV` in `sweep.py` and re-run. To refresh typeshed: `curl -sSf https://raw.githubusercontent.com/python/typeshed/main/stdlib/VERSIONS -o bench/stdlib_sweep/typeshed_VERSIONS.txt`, update the meta file, re-run.

The sweep prints where typeshed and the interpreters disagree (platform-only modules such as `msvcrt`/`nt`/`winreg` on Linux, optional extensions): typeshed stays the authority for module lifetimes.

## Checks

`tests/core/test_stdlib_sweep.py` cross-checks `pymolt/knowledge/stdlib_changes.json` (curated) against this file, and, where interpreters are available, against a live `hasattr` on them.
