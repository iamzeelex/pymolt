# pymolt legacy evaluation: zipline

Project `tests/artifacts/zipline` (Quantopian backtester, Python 2.7/3.5/3.6, 17 Cython `.pyx` files, bcolz, pandas 0.22 lock).
Branch `rework/cli-first-local-delta`. Logs: `/private/tmp/claude-501/-Users-zeelex-Documents-projects-python-migrator/b5b734e8-1757-4fe9-b0ed-03fd9bc6c401/scratchpad/eval/zipline/`.
Test subset (baseline only): `tests/test_memoize.py tests/test_execution_styles.py tests/test_restrictions.py tests/test_ordering.py` (732 passed, 21 s uncaptured in the container).

Bottom line: zipline reached **capture baseline** (50 244 events), plan and apply (one rule-based rewrite), and an INCONCLUSIVE verify. It did not reach a working 3.12 target: the Cython extensions cannot be built against numpy 2, a project-level blocker that no pymolt phase could fix. The most useful findings are about assess (opaque "conflict" verdict, locks not selectable) and the plan summary.

## 1. Scorecard

| # | Phase | Outcome | Wall | Key numbers | Honest message |
|---|---|---|---|---|---|
| 0 | scan | ok | 33 s | roots `.` (setup.py, 34 edges once my eval file existed) and `etc` (235 edges); 3917 contacts; Dockerfile `FROM python:3.5` found | mostly (D5) |
| 1 | setup | degraded | ~1 s | `--manifest etc/requirements_py36_locked.txt` and `etc/requirements_locked.txt` both refused: `unknown manifest`; only `setup.py` and `setup.cfg` were offered. Default picked `setup.py` (0 edges). | no (D1) |
| 2a | assess on the py36 lock with 3 `git+git://` editable lines | failed (blocked) | 2:37 | base tier GOLD (97 pinned), 97 rows all `conflict` | **no** (D2) |
| 2b | same lock without the git lines | failed (blocked) | ~2:30 | GOLD, 97 `conflict`, uv exit 1 | **no** (D2) |
| 2c | `etc/requirements.in` + `requirements_build.in` as written (caps `pandas<=0.22`, `networkx<2`) | failed (blocked) | ~2:30 | BRONZE, 50 `conflict` | no |
| 2d | caps removed, bcolz removed, empyrical wheel supplied (`UV_FIND_LINKS`) | ok (BRONZE) | 1:48 | upgrades 29, added 27, removed 8, conflicts 0, manual zone 0; pandas 1.1.5 -> 3.1.1, numpy 1.19.5 -> 2.5.4, cython 3.0.12 -> 3.3.1 | yes (BRONZE, "unproven") |
| 3 | capture baseline (container) | ok | 150 s captured vs 21 s uncaptured (**7x**) | 50 244 events, backend `setprofile`, dropped 0; contract trust 3% | yes |
| 4 | 3.12 target env | failed | n/a | deps installed (sans bcolz) but zipline's own Cython extensions do not compile: `zipline/assets/continuous_futures.pyx:35:0: 'numpy/long_t.pxd' not found` (14 Cython errors) | n/a (project) |
| 5 | plan (`--online`) | degraded | 61 s | 41 dependency moves; 41/41 API impacts INCONCLUSIVE; 1 rewrite from a built-in rule | partly (D3) |
| 6 | apply (then rollback) | ok | 2 s | 1 file, 1 site: `tests/pipeline/test_blaze.py` `df.append(x, ignore_index=True)` -> `pd.concat([df, x], ignore_index=True)` (correct, `pd` is imported there); `pymolt rollback` restored it | yes |
| 7 | capture post-migration | not reached | n/a | no runnable target (phase 4) | n/a |
| 8 | verify | ok (INCONCLUSIVE) | 31 s | static 654, confirmed 9, BLIND 645, dynamic-only 185, trust 3%; only 21/654 static paths resolvable under pymolt's interpreter | yes, but D6 |

Phase 0 spot checks:
- Roots, edge counts (`.` setup.py; `etc` 235) and the 3917 contacts reproduce. Top deps by contacts: numpy 1444, pandas 1359, six 311, toolz 146, nose_parameterized 81.
- True: `zipline/testing/core.py` `patch_read_csv` touches `pandas`; `tests/pipeline/test_blaze.py` calls `pandas.Categorical`.
- True: Dockerfile evidence `FROM python:3.5`, pinned.
- Observation (D5): both lock files are merged into one edge list (`numpy==1.11.3` and `numpy==1.19.1`, `pandas==0.18.1` and `==0.22.0` sit side by side). Nothing says they are alternatives per Python version.

Does pymolt pick the py36 lock, and are locks treated as fixed (lock-first)? Neither is available for this layout.
- Setup cannot select anything under `etc/` (D1), so there is no choice to make between the py35 and py36 locks.
- With the lock copied to the root (`requirements-pymolt-eval.txt`), assess did treat it as pinned and lock-first for the **base**: tier GOLD, 97 packages exactly as locked. For the **target** it then tried to resolve the exact old pins on 3.12 and could only fail (D2).

## 2. Defects

**D1 (major): `setup` cannot select a manifest outside the project root, so lock files in a sub-root are unusable.**
- Repro: `pymolt setup tests/artifacts/zipline --manifest etc/requirements_py36_locked.txt --tool container --container pymolt-eval-zipline --base-python 3.6 --target-python 3.12`.
- Evidence: `Error: unknown manifest 'etc/requirements_py36_locked.txt'` / `Detected: setup.py, setup.cfg.` Scan, in contrast, had found the `etc` root with 8 manifests including both locks (235 edges).
- Consequence: the user's only offered manifest is `setup.py` (0 edges, computed `install_requires`); the project's real pins are invisible to setup/assess. Workaround: copy the lock to the root as `requirements-pymolt-eval.txt`.
- Suspected location: manifest detection in `pymolt/setup/service.py` uses root-level `detect_sources` only; scan uses `discovery.build_surface_map` (multiple roots).

**D2 (major): assess reports every package as `conflict`, hides the real error, and gives a hint that points at nothing.**
- Repro: `pymolt assess tests/artifacts/zipline --agent` with the py36 lock as manifest (run 2b).
- Evidence:
  ```
  "summary": "Target dependencies do not resolve (Python 3.12)\n... conflicts: 97, manual-zone: 0."
  "target_error": "Command '['uv', 'pip', 'compile', 'requirements-pymolt-eval.txt', '--python-version', '3.12']' returned non-zero exit status 1."
  "hint": "Fix the conflict in data.target_error (or loosen constraints), then assess again."
  ```
  Real causes (reproduced by hand, in the order they appear):
  1. `Unsupported Git URL scheme 'git:' in git://github.com/quantopian/blaze.git` (the lock's `-e git+git://` lines; exit 2).
  2. `Metadata for pandas (v0.22.0) could not be parsed: ... pytz (>=2011k)`, and `bcolz@1.2.1 depends on pkg_resources`, a failed sdist build.
  3. `empyrical 0.5.5` and `trading-calendars 2.1.1` sdists: `AttributeError: module 'configparser' has no attribute 'SafeConfigParser'` (versioneer).
  None of these is a version conflict. The 97 `conflict` rows are every lock entry, not a conflicting subset. A user cannot tell which of the four different problems to fix.
- Also: for a lock, pymolt tries the exact pins on the target and has no relaxation step; the project's own intent files (`etc/requirements.in`: `pandas>=0.18.1,<=0.22`) cap pandas at a version that cannot exist on 3.12. Assess does not say "a hard upper bound in your intent file forbids 3.12". It would be the single most actionable sentence for this project. The only override mechanism (`target_overrides`) is interactive-only (`pymolt/interfaces/cli/commands.py:1523-1555`), not reachable from `--agent`/MCP.
- Suspected location: uv runner discards stderr; `pymolt/assess/service.py` marks all rows `conflict` when `target_resolved` is false.

**D3 (major): plan cannot analyse any API change for a container baseline (same defect as alphalens D1), and the summary reads like a clean plan.**
- Repro: `pymolt plan tests/artifacts/zipline --target-manifest requirements-target.txt --online --agent` after the container baseline capture.
- Evidence: `Counter({('INCONCLUSIVE', 'unknown'): 41})`; pandas row: `API changes for pandas 0.22.0 -> 3.1.1 could not be computed: no old interpreter: ... no Python 3.6 interpreter found for the base environment`. Meanwhile the numbers are large: pandas 0.22 -> 3.1, numpy 1.19 -> 2.5, sqlalchemy 1.4 -> 2.1, networkx 2.5 -> 3.7, 3917 static contacts. The summary says "1 file(s) to change, 1 site(s)" and no warning that 41/41 packages were not analysed.
- What did work: the one rewrite came from a built-in rule (`pandas rules=1`), not from the delta engine, and it was correct. How many `DataFrame.append` sites exist in the project is unknown; only 1 was matched, so recall is unproven.
- Suspected location: `pymolt/delta/envs.py` `resolve_old` (needs a host interpreter for the base Python).

**D4 (major, shared with alphalens D3): the manifest assess writes or implies is not installable/re-resolvable without hand-built wheels.** Build failures for old sdists with versioneer (empyrical 0.5.5, trading-calendars 2.1.1), and `bcolz` has no 3.12 wheel (sdist needs `pkg_resources`, fails). Assess in run 2d still said "resolves" only after I removed bcolz and supplied wheels. Assess/plan need an "unbuildable on target" status separate from "conflict".

**D5 (minor): scan merges alternate lock files into one edge list** (see phase 0). Also the Dockerfile's `FROM python:3.5` is reported as the pinned version evidence, while the lock for 3.6 exists; nothing reconciles them.

**D6 (minor): verify's reason is wrong when a baseline exists.** Reason text: `contract: no baseline trace is available for a version diff` while the baseline was captured; the actual state (post-migration missing) is only in the hint `Only one side is recorded`. Also a warning contains an example from another domain (`typer.prompt().strip`). Same output seen for alphalens.

**D7 (minor): `.gitignore` of the project is modified by `setup`, and `setup` lists other projects' containers** (see alphalens D8, D10). During this run the shared Docker daemon died twice (exit 137 for every container); not attributable to pymolt, but a baseline capture/uv run and three parallel emulated builds were running.

**D8 (minor, evidence only): capture overhead** 150 s vs 21 s (7x) on a small subset; fine for this size, but a full suite would not finish in an emulated container.

### Project's own breakage (not pymolt)
- Dockerfile/py35 locks and `etc/requirements_py36_locked.txt` contain `-e git+git://...` (blaze, datashape, odo) which uv rejects and GitHub no longer serves.
- `python-interface==1.6.0` in the lock cannot be installed on Python 3.6 (1.5.3 used).
- `bcolz 1.2.1` fails to compile on the python:3.6 image (`c-blosc/blosc/shuffle.c: conflicting types for '_xgetbv'`); `DISABLE_BCOLZ_AVX2=1` fixes it.
- `pandas<=0.22`, `networkx<2.0`, `pandas-datareader<0.9` caps in `etc/requirements.in` make 3.12 impossible as written.
- `versioneer.py` (project, empyrical, trading-calendars) uses `configparser.SafeConfigParser` and `readfp` (removed in 3.12).
- The 17 Cython extensions import `numpy/long_t.pxd`, removed in numpy 2: `zipline/assets/continuous_futures.pyx:35:0: 'numpy/long_t.pxd' not found`. No target env for this project can exist without editing `.pyx` files or pinning numpy<2 (not what assess produced).
- Parent repo `tests/conftest.py` shadows artifact tests on the host (not hit here, since tests only ran in the container).

## 3. What worked well
- Lock-first for the base: with the lock at the root, assess reported 97 packages exactly as pinned and tier GOLD.
- The capture worked in the container for a Cython-heavy project: 50 244 events, 0 dropped, traced code compiled with Cython unaffected (setprofile backend sees Python frames).
- `plan` corrected its from-versions with the witnessed ones (alembic 1.4.2, h5py 2.10.0, networkx 1.11, ...) instead of the stale re-resolved base (1.7.7, 3.1.0, 2.5.1), and says so per package.
- apply/rollback: exact, reversible, one file; the rewrite was semantically right.
- Honest top-level states: assess BLOCKED, BRONZE "unproven", verify INCONCLUSIVE.
- `suggested_dockerfile` for the target was generated from the project's Dockerfile with `python:3.12`, labelled "Review before use".

## 4. Workarounds applied and manual effort
- Copied/derived manifests (all named `*-pymolt-eval*`, left in the artifact): lock without git lines, then `requirements.in` + `requirements_build.in` with the caps removed and `bcolz` dropped (D1/D2).
- Built empyrical and trading-calendars wheels under Python 3.11 and passed them with `UV_FIND_LINKS` to assess/plan and the target install; hashes in `requirements-target.txt` do not match them, so a hash-stripped copy was installed (scratch dir).
- Baseline env: `lock-slim-pymolt-eval.txt` (66 pins; docs/lint/nose-timer excluded as noise except the nose packages) installed in the container: first attempt failed on bcolz; a second took 7.5 min and was killed by the Docker daemon crash; the third (binary wheels first, then sdists one by one, `inst-pymolt-eval.sh`) took 7.4 min. Then `python setup.py build_ext --inplace -j2 && pip install -e .` took 4.4 min in the container. A host-side Cython 3 build attempt had left generated `.c` files that broke the first container build; they were removed with `git clean -fX zipline`. About 50 min of manual work in total, mostly waiting.
- The 3.12 target env (`.venv-target`) contains the dependencies from `requirements-target.txt` minus bcolz; zipline itself is not built (see phase 4).
- `pymolt forks` not run (`GITHUB_TOKEN`/`GH_TOKEN` not set).

## 5. Environment state left behind
- Container `pymolt-eval-zipline` running (python:3.6 amd64; baseline pins incl. pandas 0.22.0, numpy 1.19.1, bcolz 1.2.1; zipline extensions built in the mounted tree and installed editable). It was restarted once with `docker start` after the Docker daemon died.
- Host env `tests/artifacts/zipline/.venv-target` (Python 3.12, target deps without bcolz; no zipline).
- Artifact source restored: the applied plan was rolled back (`pymolt rollback`; `git diff` is empty), `.gitignore` restored with `git checkout -- .gitignore`. Untracked/ignored leftovers: `.pymolt/`, `requirements-pymolt-eval.txt`, `lock-slim-pymolt-eval.txt`, `inst-pymolt-eval.sh`, `requirements-target.txt`, container-built `*.so`/`*.c` under `zipline/` (git-ignored).
