# Manual testing scenarios

Hands-on commands to exercise pymolt against real projects. The sample projects under
`tests/artifacts/` (git submodules, fetched as below) have deliberately different shapes:

| Project    | Shape | Good for testing |
|------------|-------|------------------|
| `flasgger` | requirements + dev + Dockerfile + tox + `.python-version` + nested `demo_app/` + named contract captures (create via `pymolt contract capture`) | scan (monorepo, version divergence, edges), contact map, **contract report** |
| `pyfolio`  | `setup.py` / `setup.cfg` + conda | scan of a setup.py project |
| `blaze`    | many `requirements-*.txt` + `etc/requirements_*.txt` + conda `blaze_env.yml` | scan (edge inventory at scale), assess |
| `odo`      | `setup.py` + one `etc/requirements_*.txt` per backend + conda recipe | scan (extras per backend), assess |
| `zipline`  | `setup.py` + 17 Cython `.pyx` + pip-compiled locks (`etc/requirements_locked.txt`, `etc/requirements_py36_locked.txt`) + Dockerfiles; Python 2.7–3.6, pandas 0.18 / numpy 1.11; unmaintained since Quantopian closed, a maintained `-reloaded` fork exists | lock-first ingestion, compiled deps and wheels (`assess --risk`), legacy-interpreter capture, pandas API changes, fork ranking |
| `alphalens` | `setup.py` + tox; Python 2.7–3.5; pandas-heavy, sibling of `pyfolio` (`-reloaded` fork exists) | pandas API changes: plan codemods, predicted risks, verify on a real test suite |
| `vibora`   | `setup.py` + `requirements.txt` + 14 Cython `.pyx`; Python 3.6 only, asyncio web framework, archived | 3.6-only capture, compiled extensions, a migration with no successor |
| `hyper`    | `setup.py` + tox; HTTP/2 client capped at `h2<3`, `hyperframe<4`; Python 2.7–3.5, archived | narrow version caps, removed protocol-library APIs |
| `theano`   | `setup.py` + `requirements.txt` + conda recipe + C/Cython sources; Python 2.7–3.8; discontinued in 2017 (successors Aesara, PyTensor) | framework succession, numpy C-API changes |
| `lasagne`  | `setup.py` + `requirements.txt` (`Theano>=0.8.2`) | a library built on a dead framework: succession through a dependency |

The artifacts are shallow submodules pinned to each project's last commit (`shallow = true` in
`.gitmodules`), about 150 MB in all on a fresh fetch. Fetch all of them, or just the one you need:

```bash
git submodule update --init                          # all artifacts
git submodule update --init tests/artifacts/zipline  # one
```

They are marked `ignore = dirty`: running pymolt writes `.pymolt/` and target manifests into an
artifact, and that never shows up as a change in this repo.

## Setup (once)

```bash
cd /Users/zeelex/Documents/projects/python_migrator
source .venv/bin/activate        # `pymolt` lives in this venv
```

Every report command accepts `--json` (pipe to `jq`). Caches live under
`<project>/.pymolt/cache/` — delete to reset.

---

## A — `scan`: the as-is recon (offline, no resolution)

```bash
pymolt scan tests/artifacts/flasgger
```
Expect: **3 project roots** (`.`, `demo_app`, `etc/flasgger_package`); a **⚠ version
divergence** (`.python-version`=3.6.1 vs `Dockerfile`=3.6 vs `tox`=3.6…3.11); **27 edges**
(pypi:25, local:1), groups dev:20 / main:7.

```bash
pymolt scan tests/artifacts/pyfolio      # setup.py project -> 1 root + a note (setup.py edges not yet classified)
pymolt scan tests/artifacts/blaze        # many requirements-* -> roots `.`/`etc`, ~140 / ~64 edges
pymolt scan tests/artifacts/flasgger --json | jq '.surfaces.project_roots[].path'
```

---

## B — `contract map`: the static denominator (where our code touches third-party code)

```bash
pymolt contract map tests/artifacts/flasgger
```
Expect: **≈60 files**, **≈230 contacts** across ≈19 deps (flask, click, werkzeug, …).
Counts drift with the submodule state — an order-of-magnitude check, not a pin
(`tests/verify/test_flasgger_artifact.py` holds the floors automatically).
pymolt's own injected watcher bundle (`pymolt_trace`) is filtered out.

```bash
pymolt contract map tests/artifacts/pyfolio --json | jq '.by_dep | keys'
```

---

## C — `contract report`: static × dynamic (capture a trace first)

No trace is committed with the artifact — capture one yourself (any command that
imports flasgger will do; its own pytest suite is the richest source):

```bash
pymolt contract capture --when baseline --mode tests \
    --project-dir tests/artifacts/flasgger -- python -c "import flasgger"
pymolt contract report tests/artifacts/flasgger        # auto-sources the capture
```
Expect: a narrow (import-time) capture → **confirmed** small, **trust** low —
that's honest. Any **dynamic-only** rows are symbols the static map *missed*
(flasgger's heavy decorators / metaclasses → static under-approximation):
exactly why static is the *map* and dynamic is the *confirmation*.

```bash
pymolt contract report tests/artifacts/flasgger --json \
  | jq '{static:.static_targets, confirmed, blind, dynamic_only, trust}'
```

---

## D — sandbox probe (`--probe-python`): re-invoke captured contacts under a version

The probe needs the dependency **installed in the target venv**. The artifacts' deps aren't in
this venv, so on flasgger the probes report `error`/`opaque-inputs`. To see a real
`stable`/`CHANGED`, here is a self-contained run using this venv (which has `pyyaml`):

```bash
T=$(mktemp -d); mkdir -p "$T/app"; : > "$T/app/__init__.py"
printf 'import yaml\ndef load():\n    return yaml.safe_load("a: 1")\ndef dump():\n    return yaml.dump({})\n' > "$T/app/m.py"
printf '{"q":"yaml.safe_load","in":{"bound":{"stream":"a: 1"}},"result":{"__dict__":[["a",1]]},"t":"return"}\n' > "$T/trace.jsonl"
pymolt contract report "$T" --trace "$T/trace.jsonl" --probe-python "$(which python)"
rm -rf "$T"
```
Expect: `yaml.safe_load` → **confirmed, probe: stable**; `yaml.dump` → **BLIND** (no inputs);
trust 0.5. Change the `result` in `trace.jsonl` to anything else → the probe reports **CHANGED**.
On a real migration, point `--probe-python` at a venv that has the **new** dependency version.

---

## E — `assess`: will it resolve on the new Python + risk

> Requires `uv` on PATH and (for `--risk`) network access (OSV/PyPI, cached). Can be slow.

```bash
pymolt assess tests/artifacts/blaze --target-python 3.12 --json \
  | jq '{quality:.resolution_quality, target_resolved, error:.target_error}'
pymolt assess tests/artifacts/flasgger --target-python 3.12 --risk     # CVE / wheels / abandonment
pymolt assess tests/artifacts/flasgger --target-python 3.12 --no-cache # force a fresh re-resolve
```

---

Re-capturing a filled slot prompts before overwriting (`--force` skips).
