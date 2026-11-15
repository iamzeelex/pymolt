# pymolt legacy evaluation: theano

Project: `tests/artifacts/theano` (Theano 1.0.5, discontinued 2017; successors Aesara, PyTensor). Branch `rework/cli-first-local-delta`.
Logs: `/private/tmp/claude-501/-Users-zeelex-Documents-projects-python-migrator/b5b734e8-1757-4fe9-b0ed-03fd9bc6c401/scratchpad/eval/theano/`.
Test subset: `theano/scalar/tests` (31 tests, 4 modules). Base python:3.6 (linux/amd64), target host Python 3.12.

## 1. Scorecard

| # | Phase | Outcome | Wall | Key numbers | Honest message |
|---|---|---|---|---|---|
| 0 | scan | ok (slow) | 80 s (268 s under cProfile) | 380 files, 7272 contacts, 30 deps, 2 roots; 5/5 spot-checks true | partly (see D6, D7) |
| 1 | setup (requirements.txt, container, 3.6 to 3.12) | ok | ~2 s | saved config; lists other runners' containers | yes (D9) |
| 2 | assess | degraded | 77 s | BRONZE; 10 upgrades, 1 added, 0 conflicts; `requirements-target.txt` written (hashes) | partly (D4) |
| 3 | capture baseline | ok | 44 s captured vs 12 s uncaptured warm (3.7x) | 830 events, backend setprofile, 0 dropped, no activation failures, coverage_pct null | yes |
| 4 | build 3.12 env | ok (deps) / project not importable | seconds | `.venv-target`: numpy 2.5.4, scipy 1.18.1, cython 3.3.1; `import theano` fails (`numpy.distutils` removed) | n/a |
| 5 | plan (offline and `--online`) | degraded | 88 s / 83 s | 0 files, 11 dependency moves, 11 of 11 INCONCLUSIVE, 0 risks | partly (D1, D2) |
| 6 | apply | ok (empty) | <1 s | "Files changed: 0"; rolled back at end | misleading (D8) |
| 7 | capture post-migration | failed (correctly refused) | 1 s | pytest exit 2, "capture not accepted as post-migration evidence" | yes |
| 8 | verify | INCONCLUSIVE (exit 4) | ~90 s | static 871, confirmed 6, BLIND 865, dynamic-only 17, trust 3%; no calibration | no (D3) |
| + | succession | ok, empty | 1 s | "No known succession path"; Aesara/PyTensor never named | honest but useless (D5) |

Subset baseline in container: 31 passed. The first uncaptured run took 89 s because it compiled C modules; warm it takes 12 s. The captured run was warm, so compare it with the warm figure.

## 2. Defects

### Project's own breakage (not pymolt)
- `theano/gof/cmodule.py:23` does `import numpy.distutils`. numpy 2.x and Python 3.12 removed it, so `import theano` fails on the target. The versions pymolt chose are legitimate; Theano is the problem.
- `versioneer.py:342` uses `configparser.SafeConfigParser`, removed in 3.12, so `uv pip install .` and `uv pip compile` fail on the sdist.
- In the submodule, 3.12's versioneer finds the shallow git repo (no tags), returns `0+untagged...` and `theano/version.py` raises IndexError at `int(short_version.split('.')[2])`. This is an artifact of the shallow submodule. Workaround: `GIT_DIR=/nonexistent` when running the target python.
- Running pytest inside `tests/artifacts/*` on the host picks up the parent repo's `tests/conftest.py` (needs `pymolt`). Workaround: `--confcutdir=.`.
- `nose` is declared and used by Theano's tests. It is unchanged in the target manifest (1.3.7) and is broken on 3.10+.

### pymolt defects

**D1 (major): API deltas are never computed for a container baseline, so plan is blind on exactly the legacy case.** Every package reports `INCONCLUSIVE risk=unknown`.
Repro: `pymolt plan tests/artifacts/theano --target-manifest requirements-target.txt --agent`, also with `--online` (no change).
```
numpy 1.19.5 -> 2.5.4 could not be computed: no old interpreter: the capture's interpreter
(/usr/local/bin/python) is not runnable here (container capture?); ... no Python 3.6 interpreter found
for the base environment; `uv venv -p` failed: Invalid version request: Python <3.7 is not supported
```
The base env is unbuildable on the host, and the running container that holds the real base env is not used. As a result `numpy.distutils` (imported by Theano) and `np.bool`/`np.int` style removals are never predicted. The headline says "Predicted risks: 0 need attention ... (evidence partial)". That is easy to read as "no risks" when 11 of 11 deltas are unknown.
Location: `pymolt/verify/identity_resolver.py:463` and the delta engine's old-env builder.

**D2 (major): `--online` does nothing for the inconclusive deltas** (same 11 INCONCLUSIVE, same explanation). The brief expected it to help; it did not.

**D3 (major): verify trust is 3% for a container baseline, and the reason is wrong.**
Repro: `pymolt verify tests/artifacts/theano --agent`
```
VERDICT: INCONCLUSIVE (contract: no baseline trace is available for a version diff)
Static 871, confirmed 6, BLIND 865, dynamic-only 17. Contract trust: 3%.
static identity: 2/871 static path(s) resolved ... 869 unresolvable
the trace's call sites lie outside the project root; executed files were matched by path suffix
```
(a) The baseline trace exists; the missing side is post-migration. The hint line ("Only one side is recorded") is right, but the verdict reason is false.
(b) 830 events produced 6 confirmed contacts. Container `/work` paths and identities resolved under pymolt's own 3.12 make the static-to-dynamic match almost empty. The same effect gave 0 of 132 on lasagne.

**D4 (minor): contradictory fixation and a misleading example in the assess verdict.**
`source_fixation: "pinned"` in the agent JSON, while the same output warns "requirements.txt is not fully pinned" (the manifest is entirely unpinned). The BRONZE detail text cites "`tensorflow>=1.3` -> 2.x", an example from nowhere near this project. Also, `nose` and `reindent` (unmaintained, nose broken on 3.12) are carried through with no flag. Location: `pymolt/assess/service.py`.

**D5 (major, product gap): succession has no knowledge of Theano, nose or the Aesara/PyTensor path.**
Repro: `pymolt succession tests/artifacts/theano`
```
Frameworks detected: cython, nose, numpy, parameterized, reindent, requests, scipy, six, sympy
No known succession path for these frameworks.
```
"Frameworks" is just every manifest edge, so scipy and six are listed as frameworks. The curated knowledge has no entry for theano, aesara, pytensor or nose (`grep -ri "aesara\|pytensor" pymolt` finds nothing). The output is honest ("No known") but gives no hint that the project is itself the dead framework. Since this repo is Theano, nothing can detect it from the edges.

**D6 (minor): scan takes 80 s, nearly all in libcst.** Profile (cProfile, 260 s): 99% of the time is in `build_contact_map`, serial `MetadataWrapper(...).visit()` per file (380 files): `QualifiedNameProvider`/scope analysis, 0.53 s per file on average, with the largest files (`tensor/basic.py`, 7000 lines) the worst. No parallelism or per-file cache. Plan (88 s) and verify (~90 s) each re-run the same contact map, so one project pays this three times. Location: `pymolt/verify/contact_map.py:383-404`. Acceptable for a one-off scan, painful for the plan/verify loop; a process pool or a content-hash cache would cut it.

**D7 (minor): scan noise.**
- `doc/requirements.txt` is a Sphinx include file; its 4 "edges" are RST directives (`.. |CompilerName| replace:: ...`) classified as `kind: local`. Root `doc` therefore reports 4 junk edges.
- `setup.cfg` is offered as a manifest (it is flake8 config).
- `setup.py` `install_requires` (numpy, scipy, six) is not edge-classified ("follow-up" note), so the real runtime requirements are missing.
- `distutils` (18 contacts) and `imp` (1) appear as dependencies only because pymolt runs on 3.12 and `sys.stdlib_module_names` lacks them. That is useful by accident for a 3.12 target (they are exactly the removed modules), but it depends on the host interpreter and is not labelled "stdlib removed in 3.12". The `imp` contact is real (`theano/tests/breakpoint.py:121`); `distutils.spawn.find_executable` (`configdefaults.py:532`) is real.
- Vendored versioneer yields `cx_Freeze.dist.build_exe` (2 contacts); it is noise, harmless.
- `lazylinker_ext` (Theano's own generated C module) and `scan_perform` show up as dependencies.

**D8 (minor): empty plan flows to "Migration applied".** `status` says next -> `pymolt apply .` for a plan with 0 files; `apply` prints "Migration applied / Files changed: 0" and marks migration done. Nothing was migrated; the target env is not even configured (status shows "target environment: no target environment configured").

**D9 (minor): setup/status cosmetics.**
- `setup` lists every running python:3.6 container "bind-mounting project", including other projects' containers (vibora, zipline, hyper, alphalens); none mounts this project.
- `contract_state.json` `env_note: "baseline -> container pymolt-eval-"` and `status` print the container name truncated (should be `pymolt-eval-theano`).
- `setup` modifies the project's tracked `.gitignore` (adds `.pymolt/env_config.json`) but not the rest of `.pymolt/`, so the tree stays dirty.

**D10 (major): capture stores a cwd-relative trace path.** When run from the parent repo with a relative `--project-dir tests/artifacts/theano`, `contract_state.json` holds `trace_path: "tests/artifacts/theano/.pymolt/contract_traces/baseline.jsonl"`. `plan` then resolves it against the project dir:
```
could not create migration plan: cannot bind the plan to baseline trace: .../tests/artifacts/theano/tests/artifacts/theano/.pymolt/contract_traces/baseline.jsonl
```
The failure came after 98 s of scanning. Workaround: rewrite the paths in `.pymolt/contract_state.json` to absolute. Location: `pymolt/verify/service.py` (slot persistence).

**D11 (minor): rejected capture hides the reason.** `capture not accepted as post-migration evidence: command exited with code 2` does not say why (ImportError); the user must open the diagnostic jsonl. Rejecting it is the right behaviour.

## 3. What worked well
- Setup/assess container path: it resolved the base set (numpy 1.19.5, scipy 1.5.4, ...) in the live container, wrote a hashed `requirements-target.txt`, and printed the exact `uv venv`/`uv pip install` commands. That target env built in seconds.
- The assess verdict never claims the code works: "BRONZE ... migration unproven", "A resolve is not evidence the code still works".
- Capture: container execution worked first time (`backend=setprofile`, 0 dropped, 830 events, fingerprints stamped). A failing post-migration run is refused rather than recorded as evidence.
- verify gives INCONCLUSIVE (exit 4) with the correct next step rather than a false PASS.
- Scan spot-checks (5 of 5 true): `imp.find_module` (`theano/tests/breakpoint.py:121`), `distutils.spawn.find_executable` (`theano/configdefaults.py:532`), `mkl.get_version_string` (`configdefaults.py:1257`), `keyring.get_password` (`theano/misc/gh_api.py:27`), `cx_Freeze.dist.build_exe` (`versioneer.py:1563`).
- The Python-2 stdlib note ("ConfigParser -> configparser ... not ported to Python 3 yet") is a useful honest flag.

## 4. Workarounds and effort
- Baseline env (not pymolt's job): `pip install numpy scipy six sympy nose parameterized pytest cython requests` plus `pip install -e .` in python:3.6. Resolved to numpy 1.19.5 / scipy 1.5.4 / sympy 1.9. About 3 min, no trouble. g++ present; tests compile C at runtime (first run 89 s, warm 12 s).
- Target env: `uv venv .venv-target --python 3.12 && uv pip install -r requirements-target.txt` plus pytest. Seconds. Theano cannot be installed or imported there (project breakage above), so phases 6-8 could not complete.
- Manifest: `requirements.txt` chosen (the dev list). `setup.py` `install_requires` was not usable (not edge-classified), and no `requirements-pymolt-eval.txt` was needed here.
- `contract_state.json` trace paths made absolute by hand (D10).
- Target runs need `GIT_DIR=/nonexistent` and `--confcutdir=.`.
- No Aesara/PyTensor port attempted (out of scope).

## 5. Final state
- Containers left running: `pymolt-eval-theano` (python:3.6, project at `/work`, Theano installed editable plus test deps).
- Target env: `tests/artifacts/theano/.venv-target` (3.12) in place.
- Artifact: `pymolt rollback` run (0 files, nothing to restore); `.gitignore` restored with `git checkout`. Remaining untracked pymolt output: `.pymolt/`, `requirements-target.txt`. No source file differs from HEAD.
