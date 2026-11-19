# Manual testing scenarios

Hands-on commands to exercise pymolt against real projects. The sample projects under
`tests/artifacts/` (git submodules, fetched as below) have deliberately different shapes:

| Project    | Shape | Good for testing |
|------------|-------|------------------|
| `flasgger` | requirements + dev + Dockerfile + tox + `.python-version` + nested `demo_app/` + named captures (create via `pymolt capture`) | scan (monorepo, version divergence, edges, contact map), **verify** |
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

Run every command from the root of your clone of this repo.

```bash
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

## B — the contact map in `scan`: the static denominator (where our code touches third-party code)

```bash
pymolt scan tests/artifacts/flasgger      # the "Calls into dependencies" section
```
Expect: **≈60 files**, **≈230 contacts** across ≈19 deps (flask, click, werkzeug, …).
Counts drift with the submodule state — an order-of-magnitude check, not a pin
(`tests/verify/test_flasgger_artifact.py`, marked `integration`, holds the floors whenever the
submodule is initialised).
pymolt's own injected watcher bundle (`pymolt_trace`) is filtered out.

```bash
pymolt scan tests/artifacts/pyfolio --json | jq '.contact_map.by_dep | keys'
```

---

## C — `verify`: static × dynamic (capture a trace first)

No trace is committed with the artifact — capture one yourself (any command that
imports flasgger will do; its own pytest suite is the richest source). A capture
needs a setup, because it records the environment it ran in:

```bash
pymolt setup tests/artifacts/flasgger --manifest requirements.txt --tool uv \
    --base-python 3.6 --target-python 3.12
pymolt capture baseline --mode tests \
    --project-dir tests/artifacts/flasgger -- python -c "import flasgger"
pymolt verify tests/artifacts/flasgger        # one side captured: coverage, no diff yet
```
Expect: a narrow (import-time) capture → **confirmed** small, **coverage** low
(most contacts are in code the import never runs), **recall** near 100% — that's honest. Any **dynamic-only** rows are symbols the static map *missed*
(flasgger's heavy decorators / metaclasses → static under-approximation):
exactly why static is the *map* and dynamic is the *confirmation*.

```bash
pymolt verify tests/artifacts/flasgger --json \
  | jq '.contract | {static:.static_targets, confirmed, blind, dynamic_only, coverage, recall,
                     coverage_granularity}'
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
pymolt verify "$T" --post-migration "$T/trace.jsonl" --probe-python "$(which python)"
rm -rf "$T"
```
Expect: `yaml.safe_load` → **confirmed, probe: stable**; `yaml.dump` → **BLIND** (no inputs);
coverage 100% and recall 50% at file granularity (a hand-written trace has no executed-code
record, so both contacts in `app/m.py` count as run). Change the `result` in `trace.jsonl` to anything else → the probe reports **CHANGED**.
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

## F — the flask boundary across two Flask releases (the Python 3.6 devcontainer)

Flasgger is a Flask extension: its contract with Flask runs while requests are handled, so
trace flasgger's own pytest suite under two Flask releases that both install on 3.6 (1.0.4 and
2.0.3, both in its `tox.ini`). The run needs `tests/.devcontainer` (python:3.6 with flasgger's
requirements); Flask 1.0.4 does not install on the host's Pythons. pymolt stays on the host;
the container gets only the pure-stdlib watcher bundle.

On the host, write the bundle where the container sees it (the devcontainer mounts `tests/`
at `/workspace/tests`):

```bash
python -m pymolt.verify.export_watcher tests/artifacts/flasgger/.pymolt/watcher
```

In the container (`/workspace/tests/artifacts/flasgger`):

```bash
export PYTHONPATH=/workspace/tests/artifacts/flasgger/.pymolt/watcher
export PYMOLT_TRACE_TARGET=flask
pip install 'flask==1.0.4'
PYMOLT_TRACE_OUT=/workspace/tests/artifacts/flasgger/.pymolt/old-{pid}.jsonl python -m pytest -q tests/
pip install 'flask==2.0.3'
PYMOLT_TRACE_OUT=/workspace/tests/artifacts/flasgger/.pymolt/new-{pid}.jsonl python -m pytest -q tests/
cat .pymolt/old-*.jsonl > .pymolt/old.jsonl     # one file per process: merge them
cat .pymolt/new-*.jsonl > .pymolt/new.jsonl
```

Back on the host:

```bash
pymolt verify tests/artifacts/flasgger \
    --baseline tests/artifacts/flasgger/.pymolt/old.jsonl \
    --post-migration tests/artifacts/flasgger/.pymolt/new.jsonl --json \
  | jq '.contract | {diff, diff_clean}'
```
Expect: both recordings fold without error and hold flask boundary calls (the watcher survived
Werkzeug's and Flask's C-level calls), and the pinned pair shows at least one behavioural
change at the flask boundary: a non-zero `diff` count and `diff_clean: false`.

---

Re-capturing a filled slot prompts before overwriting (`--force` skips).
