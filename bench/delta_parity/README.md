# Delta parity: local scoped API delta vs Axiom Graph

Can PyMolt produce the API-change facts a migration needs locally and on the spot, from
the two environments it already builds and scoped to the symbols the project touches, at
least as well as the Axiom Graph service? This harness answers that for concrete version
pairs and writes the evidence to `results/`. The latest numbers are in [RESULTS.md](RESULTS.md).

## What it compares

| side | what it is |
|---|---|
| **local** | `probe.py` runs inside the old and the new interpreter and reports, per touched symbol: existence, owner, signature, bases, `warnings.warn` prose (own body, or the PEP 562 `__getattr__` branch that serves the name), warnings raised on access, and the forwarding target. Old vs new are then diffed. It adds the warnings the baseline run emits (`calls_*.py` in the **old** env only), which is what tier 1's baseline capture records. For symbols that are removed with no local successor hint, an **intermediate fetch** scans PyPI releases between old and new, newest first. It range-requests one module file per wheel (versions only cross the wire) and mines its deprecation prose. |
| **axiom** | the cached Axiom Graph checkpoints on this machine (`~/Library/Caches/axiom_graph/checkpoints/<pkg>__<from>__<to>.jsonl`, override with `AXIOM_CHECKPOINTS`). Changes and transitions are matched to the same symbols through every spelling: public path, defining owner, receiver class path (how griffe lists inherited members), `defined_as`, `X.__init__`, and, to be generous, the callee a method forwards to. |
| **ground truth** | introspection in both envs, **plus use**: `calls_<pair>.py` exercises each symbol the way the project does (flasgger's argument shapes, the toy app's calls) in both envs. A removal counts only if use also breaks. A behaviour change is any use whose outcome differs. |

Categories scored: `REMOVED`, `SIGNATURE` (parameter removed, kind or default changed, new
required parameter), `BASES`, `DEPRECATED` (still there but warns), and `BEHAVIOR` (use
outcome differs). The "use" view keeps only the categories that change what the project's
code does: `REMOVED`, `DEPRECATED`, `BEHAVIOR`.

Pairs:
- `flask`: the flasgger fixture's flask/werkzeug contact symbols. These are the static
  contact map plus the instance methods flasgger calls or overrides (listed in `run.py`).
  Versions are flask 2.0.3 + werkzeug 2.0.0 (py3.9) → flask 3.0.0 + werkzeug 3.0.0 (py3.12),
  exactly the pairs Axiom's checkpoints cover.
- `pandas`: the e2e toy app's 14 baseline symbols, pandas 1.5.3 (py3.11) → 2.2.3 (py3.12),
  reusing `bench/e2e_pandas/.envs`. The only Axiom checkpoint is 1.3.5 → 2.0.0; its
  1.5.3 → 2.0.0 step is used (a window mismatch, see RESULTS.md).

## Run

```bash
python bench/e2e_pandas/run.py setup                  # once, for the pandas envs
.venv/bin/python bench/delta_parity/run.py            # both pairs
.venv/bin/python bench/delta_parity/run.py --pair flask --offline   # no intermediate fetch
```

- `uv` must be on PATH. The flask envs are built into `.cache/envs/` (gitignored) on the
  first run. They are ~2 MB of wheels from PyPI and are reused afterwards.
- The intermediate fetch needs network. Release JSON and fetched module files are cached
  in `.cache/pypi/`; delete it to measure a cold fetch.
- Outputs:
  - `results/<pair>_parity.json`: per-unit ground truth, local and axiom, plus scores, hints
    and timing;
  - `results/<pair>_symbols.json`;
  - `results/last_run.txt`, if you redirect the run's stdout there.

## Decision rule (fixed before the run)

Retire the Axiom service as a runtime dependency if, on project-relevant changes, all three
hold:
1. local recall ≥ Axiom recall;
2. local precision > Axiom precision;
3. local takes < 10 s per library, excluding the env build tier 1 does anyway.

It is overturned if Axiom finds project-relevant changes that neither the two endpoints nor
one intermediate release can supply.

## Honesty notes

- For the API-level categories, ground truth and the local probe share a mechanism
  (introspection in the env). The independent evidence is **use**: it refutes or confirms
  every `REMOVED` claim, and it is the only thing that finds `BEHAVIOR` changes.
- The Axiom side is the cached checkpoint data (structural changes, deprecation hints,
  transitions). It is not the service's propose-stage codemods, which are not cached.
- The local `DEPRECATED` rule counts only unconditional evidence (PEP 562 branch, or a
  warning on access). A looser variant that counts every body warning naming the symbol is
  scored too (`local_any_static_warn`). It was added after the pandas run showed that most
  such warnings are argument-conditional.
