# pymolt legacy evaluation: alphalens

Project `tests/artifacts/alphalens` (Quantopian factor analysis, Python 2.7-3.5, pandas/scipy/matplotlib/empyrical).
Branch `rework/cli-first-local-delta`. Logs: `<local scratch>/eval/alphalens/`.
Test subset (both sides): `alphalens/tests/test_performance.py` + `test_utils.py`, with 6 tests deselected (they fail on the era baseline itself, see "Environments").

Bottom line: the funnel reached **verify**, but only as INCONCLUSIVE, because the migration to pandas 3 / scipy 1.18 breaks the project and pymolt neither predicted nor rewrote anything. The reason is a pymolt gap (the delta engine cannot build the "old" environment from a container baseline), not a missing API fact.

## 1. Scorecard

| # | Phase | Outcome | Wall | Key numbers | Honest message |
|---|---|---|---|---|---|
| 0 | scan | ok | 3 s (rerun) | 13 files, 319 contacts, 13 deps; spot checks below all true | yes |
| 1 | setup (container 3.6 -> 3.12) | ok / degraded | ~1 s | no `--manifest` -> picked `setup.py`; with the workaround file picked it. It never mentions that `setup.py` cannot be evaluated. | partly |
| 2a | assess on `setup.py` | failed (blocked) | 36 s | 8 declared deps found; verdict "do not resolve"; 8 rows all labelled `conflict` | **no** (real cause hidden) |
| 2b | assess on `requirements-pymolt-eval.txt` (workaround) | ok (BRONZE) | 1:51 | upgrades 31, added 14, removed 11, conflicts 0; baseline tier bronze; `requirements-target.txt` written | mostly (see D3) |
| 3 | capture baseline (container) | ok after deselecting 6 failing tests | 192 s captured vs 24 s uncaptured (**8x**) | 11 697 events, backend `setprofile`, dropped 0 | yes (first attempt refused: exit code 1) |
| 4 | build 3.12 target env | degraded | ~10 min manual | `requirements-target.txt` does **not** install as written (empyrical sdist); needed prebuilt wheel + `six` | no (D3) |
| 5 | plan (offline, then `--online`) | degraded | 10 s / 5 s | 31 dependency moves, **0 files, 0 rewrite sites**; all 31 API impacts INCONCLUSIVE; from-versions taken from the baseline capture (good) | partly (D1) |
| 6 | apply | ok (no-op) | 1 s | 0 files changed. The subset cannot be collected on the target. | yes |
| 7 | capture post-migration | failed (refused) | 0.5 s then 5 s | exit 1 (wrong interpreter), then exit 2 (collection errors) | partly (D5) |
| 8 | verify | ok (INCONCLUSIVE) | 3 s | "Only one side is recorded, so nothing was compared"; static 88, confirmed 4, BLIND 84, dynamic-only 102, trust 7%; calibration null | yes |

Phase 0 spot checks (all true):
- `scipy.stats.mode` called from `alphalens.utils.compute_forward_returns`: `utils.py:319` has `mode(days_diffs).mode[0]`.
- `IPython.display.display` from `utils.print_table`: present.
- `cx_Freeze.dist.build_exe` from vendored `versioneer.py:1563`: present.
- `ConfigParser` Python-2 note: `versioneer.py:283`.
- The contact map also lists `pandas.Int64Index` and `pandas.util.testing.assert_frame_equal` (test code), exactly the APIs that later broke.

Note on "assess has nothing to resolve": scan found 0 edges, but assess on `setup.py` does extract the 8 `install_requires` entries (they are literal). Assess does not run the computed list; the failure is in `uv pip compile setup.py` executing `setup.py` (D2).

## 2. Defects

### pymolt defects

**D1 (major): the delta engine cannot build an "old" environment for a container baseline, so every API impact is INCONCLUSIVE and the plan looks clean.**
- Repro: `pymolt plan <alphalens> --target-manifest requirements-target.txt --agent` after a container baseline capture (base 3.6). Same with `--online`.
- Evidence (`impacts[].explanation` in `.pymolt/migration_plan.json`):
  ```
  no old interpreter: the capture's interpreter (/usr/local/bin/python) is not runnable here (container capture?);
  ... no Python 3.6 interpreter found for the base environment; `uv venv -p` failed:
  error: Invalid version request: Python <3.7 is not supported but 3.6 was requested.
  ```
  Summary line: "Predicted risks: 0 need attention ... No call sites matched: none of the returned recipes apply in the 13 file(s) scanned. The plan changes no file."
- Impact: this is the central scenario (legacy base Python the host cannot run). The from-versions are known (pandas 0.25.3, scipy 1.5.2) and the static contact map already holds the call sites `scipy.stats.mode(...).mode[0]` and `pandas.Int64Index`, yet nothing is flagged. The real result on the target: `IndexError: invalid index to scalar variable` at `alphalens/utils.py:319` (scipy 1.11+ `mode` keepdims change), 24/24 `test_tears.py` tests fail, and both test modules fail to import (`Int64Index`, `pandas.util.testing`).
- Honesty: per-package rows say INCONCLUSIVE, but the headline ("0 need attention", "No call sites matched", `files_to_change: 0`) reads as an all-clear. The headline should say "0 of 31 packages could be analysed".
- Suspected location: `pymolt/delta/envs.py` (`resolve_old`, around line 265-303) and the plan summary text in `pymolt/codemods/service.py`.

**D2 (major): assess hides the real resolve error and labels every package `conflict`.**
- Repro: `pymolt assess tests/artifacts/alphalens --agent` with manifest `setup.py`.
- Evidence:
  ```
  "target_error": "Command '['uv', 'pip', 'compile', 'setup.py', '--python-version', '3.12']' returned non-zero exit status 2."
  "changes": ["empyrical 0.5.0>- conflict", "ipython 3.2.3>- conflict", ... 8 rows]
  "hint": "Fix the conflict in data.target_error (or loosen constraints), then assess again."
  ```
  Real cause (from running uv by hand): executing `setup.py` fails in `versioneer.py:342`, `AttributeError: module 'configparser' has no attribute 'SafeConfigParser'`. There is no version conflict at all. Scan had even printed the ConfigParser note.
- The same pattern repeats for zipline (see zipline report): uv's stderr is dropped, and the hint points at `target_error`, which contains only an exit status.
- Suspected location: the `uv pip compile` runner (`pymolt/adapters/` uv runner) discarding `stderr`; the `conflict` status assignment in `pymolt/assess/service.py` when `target_resolved` is false.

**D3 (major): the `requirements-target.txt` that assess writes is not installable or re-resolvable on 3.12, and `plan` then crashes with a raw traceback.**
- Repro: `uv pip install -r requirements-target.txt --python .venv-target/bin/python`.
- Evidence: `x Failed to build empyrical==0.5.5 ... AttributeError: module 'configparser' has no attribute 'SafeConfigParser'` (empyrical's sdist ships old versioneer). Assess reported "resolves" and wrote the file; `pymolt plan ... --target-manifest requirements-target.txt --agent` then died with a Rich traceback ending
  ```
  CalledProcessError: Command '['uv', 'pip', 'compile', 'requirements-target.txt', '--python-version', '3.12']' returned non-zero exit status 1.
  ```
  at `pymolt/codemods/service.py:283` -> `ingestion/orchestrator.py:105`. With `--agent` it should emit `{"ok": false, ...}` JSON, not a traceback.
- Also `six` is dropped from the target set ("six 1.17.0>- removed") although empyrical 0.5.5 imports it, so `import empyrical` fails (`No module named 'six'`). That is an empyrical packaging bug, but assess presents "removed" as neutral.
- Also: `plan` re-resolves the target manifest with a fresh `uv pip compile` instead of reading the assess result, so it needs build isolation to work a second time. Suspected location: `resolve_codemod_migrations` (`codemods/service.py:310-340`) only uses the cached assess rows when an `assess_result` object is passed in, which the CLI does not do.
- Workaround used: build the empyrical wheel under Python 3.11, put it in a find-links dir (`UV_FIND_LINKS`), `uv pip install --no-deps`, then `uv pip install six`.

**D4 (major): `capture` stores a trace path relative to the shell's cwd; `plan` then cannot find it.**
- Repro (from the repo root): `pymolt capture baseline --mode tests --project-dir tests/artifacts/alphalens -- ...` then `pymolt plan tests/artifacts/alphalens --target-manifest requirements-target.txt --agent`.
- Evidence:
  ```
  "error": "could not create migration plan: cannot bind the plan to baseline trace: /Users/.../tests/artifacts/alphalens/tests/artifacts/alphalens/.pymolt/contract_traces/baseline.jsonl"
  ```
  `contract_state.json` has `"trace_path": "tests/artifacts/alphalens/.pymolt/contract_traces/baseline.jsonl"` (cwd-relative) while the post-migration diagnostic entry is absolute. The error cost a 3-minute capture re-run with an absolute path (`--force`).
- Suspected location: `pymolt/verify/service.py` capture state writer (`ContractSlot.trace_path`); plan joins it onto the project dir.

**D5 (major): `capture post-migration` ignores the configured target env and runs whatever `python` is on PATH; its own next-step hint tells the user to type `python -m pytest`.**
- Repro: `pymolt setup ... --target-env .venv-target` then `pymolt capture post-migration --mode tests -- python -m pytest -q ...`.
- Evidence (`diagnostics/post_migration-*.command.log`): `~/miniconda3/bin/python: No module named pytest`. Pymolt reports "command exited with code 1", which looks like a test failure. Status shows "target environment .venv-target configured", so the user believes it is used. Workaround: pass the absolute `.venv-target/bin/python`.
- Suspected location: capture runner command resolution in `pymolt/verify/service.py` / `pymolt/interfaces/cli/verify_cmd.py` (target env only used for static identity resolution).

**D6 (minor): a capture is refused when any test fails, with no way to accept a known-red baseline.** First baseline attempt (35 failures on the latest-3.6 reconstruction, later 6 on era pins) was rejected: `capture not accepted as baseline evidence: command exited with code 1`. This is a defensible honesty choice, but legacy suites are rarely green, and the only escape is hand-written `--deselect` lists. The refusal does point at a diagnostic artifact. The message does not say that deselecting failing tests is the expected route.

**D7 (minor): `.pymolt` env note truncates the container name.** `contract_state.json`: `"env_note": "baseline -> container pymolt-eval-"` and `pymolt status`: "base environment container pymolt-eval- is running" (real name `pymolt-eval-alphalens`). Looks like a split on the last hyphen.

**D8 (minor): `setup` edits the project's tracked `.gitignore`** (adds `.pymolt/env_config.json`) without saying so, and ignores only that one file, leaving `.pymolt/` (caches, traces up to 13 MB) untracked-but-visible. Restored with `git checkout -- .gitignore`.

**D9 (minor): `source_fixation: "pinned"` for a manifest made of `>=` ranges.** assess on `requirements-pymolt-eval.txt` (`pandas>=0.18.0` ...) reports `"source_fixation": "pinned"` and `resolution_quality: "resolved"`, while the same output's verdict says "latest releases matching your UNPINNED bounds" and tier bronze. The marker seems to be derived from the filename (`requirements*.txt`).

**D10 (minor): setup lists unrelated running containers under "Discovered Running Docker Containers (bind-mounting project)".** vibora/lasagne/theano/hyper/zipline containers were listed for alphalens; `docker inspect` shows they mount other directories.

**D11 (minor): the base reconstruction is not the project's era.** Base resolved in the 3.6 container gives `pandas 1.1.5 / numpy 1.19.5` (latest for 3.6), against tox's `pandas 0.16-0.18`. On that base the subset had 35 failures; on era-like pins (`pandas 0.25.3, numpy 1.16.6, scipy<1.6`) 6. Pymolt flags this correctly as BRONZE ("unvalidated reconstruction"), and `plan` even notes "the baseline capture ran 0.25.3 (the baseline manifest resolves to 1.1.5 today)". That witnessed-version correction is good; the assess table still shows the unwitnessed 1.1.5.

### Project's own breakage (not pymolt)
- `setup.py` / `versioneer.py` / empyrical's versioneer use `configparser.SafeConfigParser` (removed in 3.12): `pip install -e .` and any `setup.py` evaluation fail on 3.12.
- Test modules use `pandas.Int64Index` and `pandas.util.testing` (gone in pandas 2/3).
- `alphalens/utils.py:319`: `scipy.stats.mode(x).mode[0]` raises `IndexError` on scipy >= 1.11.
- Baseline (pandas 0.25.3) has 6 failing tests in the era env: `test_factor_returns_{1,4}`, `test_information_coefficient_{2,3}`, `test_mean_information_coefficient_{2,3}`.
- Host-side: the artifact lives under the parent repo's `tests/`, so pytest on the host picked up the parent `tests/conftest.py` (`No module named 'pymolt'`); `--noconftest` was needed.

## 3. What worked well
- Scan numbers were accurate; the contact map contained the exact call sites that later broke (`scipy.stats.mode`, `pandas.Int64Index`, `pandas.util.testing`).
- Honest refusals: capture does not accept a red run as evidence; verify returns INCONCLUSIVE with "Only one side is recorded, so nothing was compared"; assess says BRONZE and "a resolve is not evidence the code still works".
- `plan` binds to the baseline capture and corrects from-versions with what the capture witnessed (pandas 0.25.3, numpy 1.16.6, matplotlib 3.2.2, statsmodels 0.11.1) rather than trusting the re-resolved base.
- Container path works: capture ran in `pymolt-eval-alphalens` with no changes needed (11 697 events, 0 dropped).
- `apply` and `rollback` on an empty plan are clean.

## 4. Workarounds applied and manual effort
- `requirements-pymolt-eval.txt` written from `install_requires` plus `nose`, `parameterized`, `pytest` (setup.py cannot be evaluated, D2).
- Baseline env (container): `pip install -r requirements-pymolt-eval.txt && pip install -e .` took 2 min 21 s (all wheels, no compile). That gave pandas 1.1.5 with 35 failures; then `pip install pandas==0.25.3 numpy==1.16.6 "statsmodels<0.12" "scipy<1.6" "matplotlib<3.3"` (59 s) gave 6 failures; those 6 were `--deselect`ed on both sides. About 15 min of work in total.
- Target env: `uv venv .venv-target --python 3.12`; the install of `requirements-target.txt` failed on empyrical; built the wheel with Python 3.11 (`pip wheel`), `UV_FIND_LINKS=<wheels>`, `uv pip install --no-deps`, plus `six`, `pytest`, `parameterized`. `pip install -e .` is impossible (SafeConfigParser), so the project is imported from the working directory. About 20 min.
- `plan` needed `UV_FIND_LINKS` (D3), an absolute project path (D4), and a re-capture of the baseline.
- Host pytest: `--noconftest` (parent conftest).
- Post capture: absolute interpreter path (D5).
- No `--online` benefit and no `pymolt forks` run (needs `GITHUB_TOKEN`/`GH_TOKEN`, neither was set; a maintained `alphalens-reloaded` fork exists).

## 5. Environment state left behind
- Container `pymolt-eval-alphalens` running (python:3.6, amd64, baseline deps at pandas 0.25.3, project installed editable).
- Target env: `tests/artifacts/alphalens/.venv-target` (Python 3.12.11; pandas 3.1.1, numpy 2.5.4, empyrical 0.5.5 from a 3.11-built wheel). `.venv-target` and the wheel dir under scratch are kept.
- `.pymolt/` kept in the artifact (baseline capture, diagnostics, `verification.json`, saved plan); untracked files `requirements-pymolt-eval.txt`, `requirements-target.txt`.
- Source restored: `pymolt rollback` ran (0 files); `.gitignore` restored with `git checkout -- .gitignore`. `git -C tests/artifacts/alphalens status --short` shows only the three untracked paths above.
