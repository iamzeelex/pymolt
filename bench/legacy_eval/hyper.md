# Legacy evaluation: hyper

Project: `tests/artifacts/hyper` (HTTP/2 client, Python 2.7-3.5 era, archived). Branch `rework/cli-first-local-delta`. Logs: `/private/tmp/claude-501/-Users-zeelex-Documents-projects-python-migrator/b5b734e8-1757-4fe9-b0ed-03fd9bc6c401/scratchpad/eval/hyper/`.

Test subset (same on both sides): `test_http20 test_http11 test_headers test_abstraction test_hyper test_import test_windowmanager test_cli`, with 3 baseline-failing tests deselected (see D3). Baseline uncaptured: 222 passed, 1 skipped in 2.4 s (3 failed, 222 passed without the deselect).

## 1. Scorecard

| # | Phase | Outcome | Wall | Key numbers | Honest message |
|---|-------|---------|------|-------------|----------------|
| 0 | scan | ok (spot-checked) | 2 s | 0 edges, 238 contacts / 46 files / 15 deps | yes (it says setup.py is "not yet edge-classified") |
| 1 | setup | ok after workaround | 2.5 s | manifest `requirements-pymolt-eval.txt`, tool container, 3.6 -> 3.12 | partly (D6) |
| 2 | assess `--agent` | degraded | 39 s | bronze base, 2 upgrades (cffi 1.15.1>2.1.1, pycparser 2.21>3.12), 0 conflicts, `requirements-target.txt` written | partly (D4, D5) |
| 2b | assess `--risk` | ok | 4 s | brotlipy: no 3.12 wheel, abandoned (medium); h2 2.6.2: 2 open CVEs (high) | yes, best output of the run |
| 3 | capture baseline | degraded | 21 s vs 2.4 s uncaptured (~9x) | 1496 events, backend setprofile, 0 activation failures, 0 dropped | yes; refused until failures deselected (D3); relative-dir bug (D1) |
| 4 | build 3.12 target env | ok | 7 s | `uv pip install -r requirements-target.txt`; brotlipy 0.7.0 built from sdist fine | n/a |
| 5 | plan (offline and `--online`) | degraded | 4 s each | 0 files, 0 sites, 2 dependency moves, both INCONCLUSIVE | no (D2) |
| 6 | apply, then run subset on 3.12 | apply: ok (no-op); run: failed | 0.3 s | apply "Files changed: 0". Subset: 8 of 8 modules fail at collection, `from collections import Iterable, Mapping` | apply says "Migration applied" for a no-op (D7) |
| 7 | capture post-migration | failed (project breakage) | 1 s | exit code 2, capture not accepted | yes |
| 8 | verify | inconclusive | 4 s | "no baseline trace is available"; static 0, confirmed 0, trust 0% | no (D8) |

Phases 7 and 8 could not be completed: hyper does not import on 3.12, so there is no post-migration trace. Nothing was faked.

## 2. Defects

### pymolt defects

**D1 (major): a relative `--project-dir` stores a cwd-relative trace path; `plan` then fails with a doubled path.**
Repro: from the repo root, `pymolt capture baseline --mode tests --project-dir tests/artifacts/hyper -- python -m pytest ...`, then `pymolt plan tests/artifacts/hyper --target-manifest requirements-target.txt --agent`.
```
"error": "could not create migration plan: cannot bind the plan to baseline trace: /Users/.../tests/artifacts/hyper/tests/artifacts/hyper/.pymolt/contract_traces/baseline.jsonl"
.pymolt/contract_state.json: "trace_path": "tests/artifacts/hyper/.pymolt/contract_traces/baseline.jsonl"
```
`capture` succeeded, so the failure appears two commands later and the message does not name the cause. With an absolute dir (or when run from inside the project) the trace path is stored absolute and it works. Location: `pymolt/verify/service.py` ~1436 (`trace_path=result.out_path` stored as given), consumed in `pymolt/migration_plan.py` ~284. Diagnostic traces also store relative paths.

**D2 (major): plan is blind to the Python-version breakage, the real migration problem here.**
`plan` reports only the 2 transitive moves (cffi, pycparser) and "No call sites matched ... The plan changes no file." The code cannot even be imported on 3.12 (`collections.Iterable` removed in 3.10, `imp` removed in 3.12, `log.warn`). Neither `plan`, `--online`, `assess` nor `verify` mention this. `scan` does flag Python-2 imports (StringIO, httplib) but not the 3.x removals that actually break it. Quote: `Predicted risks: 0 need attention, 0 fixed by the plan (... evidence partial)`. The summary is truthful, but a user reading "0 need attention" is not told that the project fails on import.

**D3 (minor, design): capture refuses a baseline when the project's own tests fail.**
3 of 226 tests fail on the baseline (`test_useful_error_with_no_protocol` and two `TestHTTP20Adapter` certificate tests). `capture not accepted as baseline evidence: command exited with code 1`. This is honest and a diagnostic artifact is kept, but there is no flag for "accept a known-red baseline", so the user must edit the test command (`--deselect`). A legacy project with some red tests is the normal case.

**D4 (minor): `assess` contradicts itself on fixation.**
JSON says `"source_fixation": "pinned"` while the warning says `Source manifest requirements-pymolt-eval.txt is not fully pinned. The generated resolution map represents a reconstruction`. The manifest is all ranges, so "pinned" is wrong.

**D5 (minor): pycparser "3.12" in the upgrade row looks like a Python version.** `pycparser 2.21>3.12 upgrade` is a real release number, but the row sits next to "Python 3.12" and nothing says so. Cosmetic.

**D6 (minor): `setup` lists containers that do not mount the project.**
Header: "Discovered Running Docker Containers (bind-mounting project)", but all six containers on the host are listed, including `pymolt-eval-vibora`, `-lasagne`, `-zipline`, `-theano` and `-alphalens`, which mount other directories. (Four of those were the other runners' containers.) A user could pick one that has no `/work` project. Setup also edits the project's `.gitignore` (adds `.pymolt/env_config.json`) without saying so.

**D7 (minor): `apply` reports "Migration applied" for a plan with 0 files and 0 rewrites** and then points at `capture post-migration -- python -m pytest`, although the target env is `.venv-target` and the bare `python` is not it. The "next" hint does not carry the target interpreter.

**D8 (major): `verify` gives a misleading reason with a baseline present.**
`pymolt status` shows `baseline 1496 events`, yet verify says `contract: no baseline trace is available for a version diff` and reports `Static 0 ... Contract trust: 0%`. `verification.json` shows `impact_filter_active: true`: the static denominator was filtered to the plan's impact targets (cffi, pycparser), which the project never calls, so 238 contacts became 0. The hint ("Only one side is recorded") is correct, but the reason and the 0% trust read as "no data" rather than "filtered to symbols your code does not touch". Warnings also say "58 unresolvable" static paths because the capture interpreter (container) is not runnable on the host.

### Project's own breakage (not pymolt)
- `from collections import Iterable, Mapping` (hyper/http11/connection.py:13) fails on 3.10+.
- `import imp` in test_import.py; `log.warn` deprecations.
- 3 tests fail on 3.6 already (certificate and no-protocol tests).

## 3. What worked well
- `scan` numbers check out. Spot-checks: `brotli.Decompressor` (hyper/http11/response.py:93, true), `mock.patch` (test/test_http20.py:9, true), `imp.reload` (true), `pycohttpparser` appears only in setup.py extras and in a test docstring (reported as a dep, plausible), `setup.py install_requires` unparsed as stated. Contact kinds (call / attribute-read / protocol) look right.
- `assess --risk` produced exactly the right warnings: brotlipy has no cp312 wheel and is abandoned, h2 2.6.2 has 2 open CVEs with fixed versions.
- Baseline capture inside the container worked on the first accepted run: 0 activation failures, 0 dropped events; the diagnostic file for the rejected run is kept.
- Pinned `requirements-target.txt` with hashes installed on the host in 7 s; brotlipy built from source without trouble.
- `pymolt status` is accurate and the next-step hints are mostly usable.
- Rejected captures never overwrite state.

## 4. Workarounds applied
- `requirements-pymolt-eval.txt` written from `install_requires` (setup.py is not parsed into edges): 4 lines.
- Baseline env (container `python:3.6`): `pip install -r requirements-pymolt-eval.txt pytest requests<2.28 mock pyOpenSSL service_identity` took 54 s, one command, no problems.
- Test subset with 3 `--deselect`s (D3).
- Absolute `--project-dir` (D1); re-captured baseline once.
- Target env: `uv venv .venv-target --python 3.12`, `uv pip install -r requirements-target.txt`, plus pytest/mock/requests/pyOpenSSL/service_identity/setuptools. Under the repo, pytest on the host picks up the parent `conftest.py`; needed `--confcutdir=<artifact>`.
- No source edits to hyper. No codemod was possible, so no manual port was attempted.

## 5. Environment state left behind
- Container `pymolt-eval-hyper` running (python:3.6, amd64, `/work` = the artifact).
- Target env `tests/artifacts/hyper/.venv-target` (Python 3.12.11) in place.
- Artifact: no tracked source changed by the run except `.gitignore` (added by `setup`). It was restored with `git -C tests/artifacts/hyper checkout -- .gitignore`; `git status` of the artifact now shows only untracked files. Untracked leftovers kept as the evidence trail: `.pymolt/`, `requirements-pymolt-eval.txt`, `requirements-target.txt`, `.venv-target`. `apply` changed 0 source files, so `rollback` was not needed.
