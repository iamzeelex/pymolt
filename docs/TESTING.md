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
pymolt scan tests/artifacts/pyfolio      # setup.py project -> 1 root, edges from setup.py (source_file: setup.py)
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

## D — probe (`--probe-python`): re-invoke captured contacts under a version

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

## G — the work order: plan, apply, record, rollback, accept (about 15 minutes)

The toy of `bench/e2e_pandas` (the `change` scenario behind the README example) moves from
pandas 1.5.3 on Python 3.11 to pandas 2.2.3 on 3.12. Its plan has a step for each executor: a
pin and a rewrite pymolt applies (`Series.append` → `pd.concat`), and a behaviour change for a
person (`DataFrame.mean` on a frame with a text column returns a mean in 1.5 and raises in 2.x;
the app catches it, so its test stays green). Needs `uv`, and network for the first env build.
Two steps need a person at a terminal; they are marked *A person, on a terminal*.

Build the two environments (once), write the toy, pin its manifest so that the pin is a pymolt
step, and take the baseline. Attestations go under `$XDG_STATE_HOME`: point it at the scratch
dir.

```bash
python bench/e2e_pandas/run.py setup           # bench/e2e_pandas/.envs/{3.11,3.12}
E=$PWD/bench/e2e_pandas/.envs; T=/tmp/pymolt-rc/toy; rm -rf /tmp/pymolt-rc
export XDG_STATE_HOME=/tmp/pymolt-rc/state XDG_CONFIG_HOME=/tmp/pymolt-rc/config
python -c 'import sys; sys.path.insert(0, "bench/e2e_pandas"); import run, pathlib
run.make_project(pathlib.Path(sys.argv[1]), change=True)' "$T"
cd "$T"
printf 'pandas==1.5.3\nrequests\n' > requirements.txt
printf 'pandas==2.2.3\nrequests\n' > requirements-target.txt
pymolt setup . --manifest requirements.txt --tool uv --base-python 3.11 --target-python 3.12 \
    --target-env "$E/3.12"
pymolt capture baseline --mode tests --project-dir . -- \
    "$E/3.11/bin/python" -m pytest -q -p no:cacheprovider
```
Expect: `run.py setup` prints `3.11: python/pandas 3.11.x 1.5.3` and `3.12: … 2.2.3`; the
capture says **captured 15 events**, `dropped=0`, and `next → pymolt plan .`.

**Plan, and the agent gate.**

```bash
pymolt plan .
pymolt status .
pymolt apply . --agent
```
Expect:
- `plan`: the verified codemod `$S.append($OTHER, ignore_index=$IGN) → pd.concat(…)`, a
  **certain** risk at `toyapp/core.py:13` (fixed by the plan), a **high** one at
  `toyapp/core.py:24` (`DataFrame.mean`), and a Steps table of **7 steps: 2 by pymolt, 5 for a
  person**, with an empty depends-on column:

  | step | who | claim | what |
  |---|---|---|---|
  | `env-1` | pymolt | intended | pin `requirements.txt` |
  | `env-2` | person | none | build the target environment |
  | `mech-1` | pymolt | preserve | `Series.append` → `pd.concat` |
  | `beh-1` | person | preserve | `DataFrame.mean`: default `numeric_only` None → False (effort large) |
  | `beh-2`…`beh-4` | person | preserve | `Series` `copy`, `DataFrame.groupby` `axis`, `GroupBy.sum` `numeric_only` |

  It writes `.pymolt/PLAN.md` and no source.
- `status`: `7 step(s): 1 present, 2 planned, 4 planned (person)` (`env-2` is present: setup
  names an existing target env), next `pymolt apply .`.
- `apply --agent`: exit **2**, `"ok": false`, no person approved this plan for agents; the hint
  names `pymolt apply . --approve-agent`.

*A person, on a terminal (optional):* `pymolt apply . --approve-agent` prints the following and
waits. Type anything but the nonce: exit **2**, `the nonce was not typed back`, nothing written.
Off a terminal it is refused the same way (`stdin is not a terminal`).

<!-- docs-check:off -->
```text
Approve plan <id> for agents: they may apply its 2 pymolt step(s) (env-1, mech-1) and roll back one step at a time. A re-plan or any change to the plan voids it.
To attest this as a person, type <6 hex chars>:
```
<!-- docs-check:on -->

**Read a step's brief, then dry-run.**

```bash
pymolt apply . --step beh-1 --brief
pymolt apply . --dry-run
```
Expect:
- the brief opens `# Step beh-1: … (tier high)` and `Who: person. Claim: preserve.`, and names
  `toyapp/core.py`, `average()`, line 24 (`return df.mean().to_dict()`), with the baseline's
  `FutureWarning` as a pitfall;
- the dry run: `nothing was written`, `Would apply: env-1, mech-1`, `Files: 2`, the diff of
  `requirements.txt` (`pandas==1.5.3` → `pandas==2.2.3`) and of `toyapp/core.py`
  (`return pd.concat([s1, s2], ignore_index=True)`), `compile on the target 3.12: 1 file(s) ok`.
  Both files are unchanged.

**Apply one step, then the rest.**

```bash
pymolt apply . --step mech-1
pymolt status .
pymolt apply .
```
Expect: `Applied plan <id>: mech-1` with `compile_target ok` and `smoke_import ok`; `status`:
`1 applied, 1 present, 1 planned, 4 planned (person)`, next `pymolt apply .` for `env-1`. Then
`Applied plan <id>: env-1`: `requirements.txt` pins `pandas==2.2.3`, and the next command is
`pymolt apply --step beh-1 --record`.

**Record a person's edit** (`beh-1`). pandas 2.2 treats `numeric_only=None` like `False` and
still raises, so the fix that keeps the old outcome is `numeric_only=True`. Then decline the
other three behaviour steps: their outcome does not change, and verify judges them anyway.

```bash
pymolt apply . --step beh-1 --record
pymolt rollback . --step mech-1                # refused while the record is open
perl -pi -e 's/df\.mean\(\)/df.mean(numeric_only=True)/' toyapp/core.py
pymolt apply . --step beh-1 --record --done
for s in beh-2 beh-3 beh-4; do pymolt apply . --step $s --skip --reason "outcome kept"; done
pymolt status .
```
Expect:
- `Record open`, `3 file(s) snapshotted`, `beh-1` in progress;
- the rollback: exit **2**, `step beh-1 … is being recorded: rollback would write into its
  record`;
- `--record --done`: `Recorded step beh-1 … 1 file(s)`, `compile_target ok`, exit 0. On a
  terminal it first asks the person to attest the edit (below); typed back, the panel says
  `attested (<id>)`. Otherwise the note says `recorded as an unattested person`;
- after the skips, `status`: `2 applied, 1 recorded, 1 present, 3 skipped`, next the
  post-migration capture.

```text
Record step beh-1 (`DataFrame.mean`: default `numeric_only` None → False) of plan <id> as made by a person: claim preserve. Files: toyapp/core.py.
To attest this as a person, type <6 hex chars>:
```

**Capture after, verify the whole surface and one step.**

```bash
pymolt capture post-migration --mode tests --project-dir . -- \
    "$E/3.12/bin/python" -m pytest -q -p no:cacheprovider
pymolt verify .
pymolt verify . --step beh-1; echo "exit $?"
pymolt status .
```
Expect: **15 events**, `backend=monitoring`. **VERDICT: PASS**, `3 edited sites: 3 equal`;
calibration `0 hit, 2 prevented by the plan`: `Series.append` (certain) and `DataFrame.mean`
(high). `--step beh-1`: `verified (epoch 3)`, `risk …: prevented (fixed)`, exit **0**.
`status`: `1 applied, 2 verified, 1 present, 3 skipped`, `Done: the verdict is PASS.`

**Roll back the person's fix, then a pymolt step, and apply it again.**

```bash
pymolt rollback . --step beh-1
pymolt status .
pymolt rollback . --step mech-1 --cascade
pymolt status .
pymolt apply . --step mech-1
```
Expect:
- `Steps: beh-1`, `Files restored: 1`; line 24 reads `return df.mean().to_dict()` again. The
  post-migration capture recorded the line the rollback took away, so it is set aside
  (`Post-migration capture: set aside, kept at …/contract_traces/archive/…`). `status`:
  `1 applied, 1 verified, 1 present, 1 rolled back, 3 skipped`, next the post-migration capture;
- no step depends on `mech-1`, so `--cascade` rolls back `mech-1` alone: `Steps: mech-1`, and
  line 13 reads `s1.append(…)` again. `status`: `1 applied, 1 present, 2 rolled back, 3
  skipped`, post-migration `not captured`;
- `Applied plan <id>: mech-1`, `smoke_import ok`.

**The break, and a person accepting it as intended.**

```bash
pymolt capture post-migration --mode tests --project-dir . --force -- \
    "$E/3.12/bin/python" -m pytest -q -p no:cacheprovider
pymolt verify .; echo "exit $?"
pymolt verify . --step beh-1; echo "exit $?"
pymolt verify . --accept 86395ab0c8 < /dev/null; echo "exit $?"
```
Expect:
- **14 events**;
- **VERDICT: FAIL**, exit **1**, with two failing findings at `toyapp/core.py:24`:
  `DataFrame.mean`, `kind changed` (it returned a Series and now raises `TypeError`),
  `[finding 86395ab0c8]`; and `Series.to_dict`, `presence changed`, `[finding a197e6f0c1]`.
  Calibration `1 hit`: `DataFrame.mean` (high);
- `--step beh-1`: `rolled_back`, `not in effect, so this verify does not judge it`, exit **4**;
- the accept off a terminal: exit **2**, `verify --accept is a person's attested act: stdin is
  not a terminal`.

*A person, on a terminal:* `pymolt verify . --accept 86395ab0c8` prints the following; type the
nonce. Accept `a197e6f0c1` the same way. `pymolt verify .` then lists both as accepted, with who
and when, and fails on neither; `pymolt verify . --strict` reviews them again.

```text
Accept finding 86395ab0c8: pandas.core.frame.DataFrame.mean at toyapp/core.py:24, [<before>] -> [<after>] (1 entry), as an intended change (ad-hoc step x-86395ab0c8). It expires when the outcome or either capture changes.
To attest this as a person, type <6 hex chars>:
```

Clean up:

```bash
cd - && rm -rf /tmp/pymolt-rc && unset XDG_STATE_HOME XDG_CONFIG_HOME
```

---

Re-capturing a filled slot prompts before overwriting (`--force` skips).
