# pymolt legacy evaluation: lasagne

Project: `tests/artifacts/lasagne` (library on top of Theano). Branch `rework/cli-first-local-delta`.
Logs: `/private/tmp/claude-501/-Users-zeelex-Documents-projects-python-migrator/b5b734e8-1757-4fe9-b0ed-03fd9bc6c401/scratchpad/eval/lasagne/`.
Test subset: `lasagne/tests/test_init.py test_utils.py test_nonlinearities.py layers/test_dense.py` (110 passed, 8 skipped). Base python:3.6 (linux/amd64), target host Python 3.12.

## 1. Scorecard

| # | Phase | Outcome | Wall | Key numbers | Honest message |
|---|---|---|---|---|---|
| 0 | scan | ok | n/a (brief) | 10 edges (9 dev, 1 main), 1703 contacts, 58 files, 8 deps; theano 148 symbols, numpy 132 | yes |
| 1 | setup (requirements.txt, container) | ok | ~2 s | saved | yes |
| 2 | assess (`requirements.txt`) | failed, with the right target | 55 s | "Target dependencies do not resolve"; 4 conflicts (numpy, scipy, six, theano) | partly (D1) |
| 2b | assess (workaround: `requirements-pymolt-eval.txt` = numpy, scipy, six, no Theano) | ok | 18 s | SILVER; numpy 1.19.5 to 2.5.4, scipy 1.5.4 to 1.18.1; `requirements-target.txt` written | yes, but the manifest is not the project |
| 3 | capture baseline | ok | 74 s captured vs 24 s uncaptured warm (3.1x; first uncaptured run 148 s) | 1961 events, setprofile, 0 dropped, no activation failures, coverage_pct null | yes |
| 4 | build 3.12 env | degraded | seconds | numpy, scipy, six, pytest, mock install; Theano fails to build; `import lasagne` raises "Could not import Theano" | n/a |
| 5 | plan (offline and `--online`) | degraded | 10 s each | 0 files, 2 moves, 2 of 2 INCONCLUSIVE, 1 risk "need attention" | partly (D2) |
| 6 | apply | ok (empty) | <1 s | "Files changed: 0"; rolled back | misleading |
| 7 | capture post-migration | failed (correctly refused) | 2 s | pytest exit 4: ImportError Theano | yes |
| 8 | verify | INCONCLUSIVE (exit 4) | ~15 s | static 132, confirmed 0, BLIND 132, dynamic-only 9, trust 0% | no (D3) |
| + | succession | ok, empty | 1 s | "Frameworks detected: ... theano"; "No known succession path" | honest but useless (D4) |

## 2. Does pymolt see Theano as the real problem?
Partly, by accident of the resolver, not by design:
- **assess** (original manifest): the table shows exactly one direct row, `theano  >=0.8.2  1.0.5 -> conflict`, which does point at Theano. The verdict text, though, is `Command '['uv','pip','compile',...]' returned non-zero exit status 1` with no cause. uv's stderr says why (Theano 1.0.5's sdist cannot be built on 3.12: `configparser.SafeConfigParser` removed). The agent view lists numpy, scipy and six as "conflict" too (D1).
- **plan**: with a resolvable target (Theano removed by my workaround) there is no mention of Theano in the plan at all, and no "dependency removed/replaced" move. The only Theano item is one trace-based risk (`theano.tensor.basic.stack`, DeprecationWarning in baseline).
- **succession**: lists `theano` among 11 "frameworks" (with jinja2, mock, pytest-pep8, sphinx-rtd-theme) and answers "No known succession path". It does not say Theano is dead, nor name Aesara/PyTensor.
- **scan** does expose it: Theano is the top dependency (148 of 293 distinct symbols).
So pymolt reports the symptom (does not resolve; no succession knowledge) but never states "this project depends on a discontinued framework that has no 3.12 build".

## 3. Defects

### Project's own breakage (not pymolt)
- Theano 1.0.5 sdist does not build on 3.12 (versioneer `SafeConfigParser`), so Lasagne cannot be installed on 3.12 at all.
- The project's `setup.cfg` `addopts` (`--pep8`, `--cov`, `--doctest-modules lasagne/`) breaks collection on modern pytest ("Direct construction of pytest_pep8.Pep8Item has been deprecated"). Workaround: `-o addopts= -p no:pep8`.
- Host-side pytest in the artifact picks up the parent repo `conftest.py`: use `--confcutdir=.`.

### pymolt defects

**D1 (major): assess hides the cause of a failed resolve and mislabels packages.**
Repro: `pymolt assess tests/artifacts/lasagne --agent`
```
target_error: "Command '['uv', 'pip', 'compile', 'requirements.txt', '--python-version', '3.12']' returned non-zero exit status 1."
changes: ["numpy 1.19.5>- conflict", "scipy 1.5.4>- conflict", "six 1.17.0>- conflict", "theano 1.0.5>- conflict"]
```
Running the same command by hand shows the real error (build failure of the Theano sdist). pymolt drops uv's stderr, calls an sdist build failure a "conflict", and marks the innocent transitive deps as conflicts in the agent view (the human table shows only theano). An agent reading `data` is told to "Fix the conflict in data.target_error (or loosen constraints)", which is wrong advice. Location: `pymolt/assess/service.py` / the uv resolve adapter (CalledProcessError is formatted without `.stderr`).

**D2 (major): plan is blind for container baselines** (same as theano D1): numpy and scipy deltas are INCONCLUSIVE because no Python 3.6 exists on the host and the live container is not used. `--online` makes no difference. The "1 need attention" risk is real, but its site is a container path (`/work/lasagne/utils.py`), not project-relative.

**D3 (major): verify trust 0% for a container baseline.** `Static 132, confirmed 0, BLIND 132`, after a baseline with 1961 recorded events. The reason text is also wrong: "contract: no baseline trace is available for a version diff" while the baseline exists and post-migration is the missing side. Notes: "static identity: 2/293 resolved ... 288 unresolvable (imp.reload, mock.Mock, numpy.abs, ...)", "trace's call sites lie outside the project root". Same cause as theano D3.

**D4 (major, product gap): succession lists any manifest entry as a "framework" and knows nothing about Theano**, so the "library on a dead framework" case yields no guidance (no Aesara/PyTensor, no statement that Theano is unmaintained). It is also unaware that the `theano.config.floatX`, `theano.tensor.*` surface (148 symbols) is what must be ported.

**D5 (minor): scan synthetic chains.** Of 132 distinct numpy contacts, 76 have 3+ dotted parts, e.g. `numpy.arange.reshape.__mul__`, `numpy.exp.T.__truediv__`, `numpy.dot.__add__`, `numpy.ceil.__int__`. They are derived from `np.arange(...).reshape(...) * 3` (`lasagne/tests/layers/test_dense.py:28`): real call chains, but not importable symbols. They inflate the count (293 distinct symbols), they never resolve to an identity (static identity 2/293), and `numpy.arange.reshape` reads like an attribute of the function. They help to show how results are used, but they mislead anything that matches contacts against API deltas. A separate "derived" kind, or dropping chains past the first hop, would be better. Also `theano.config.floatX` is listed dozens of times (one per caller).

**D6 (minor): setup/scan inconsistency.** scan's agent view says `chosen_manifest: requirements-dev.txt` (9 dev edges), while setup's default and saved choice is `requirements.txt` (1 edge: `Theano>=0.8.2`, which has no numpy). So the numpy/scipy/six versions pymolt assesses come only from Theano's transitive requirements. Nothing says so.

**D7 (minor): same cosmetics as theano.** Cwd-relative `trace_path` in `contract_state.json` made `plan` fail until rewritten to absolute (reproduced; see theano D10); container name truncated to `pymolt-eval-` in status; `setup` lists unrelated containers as "bind-mounting project"; setup edits the tracked `.gitignore`; "Migration applied" for a 0-file plan.

**D8 (minor): SILVER on a manifest that omits the dead dependency.** After my workaround the verdict is "SILVER base - resolves on Python 3.12" with no sign that the project cannot import on 3.12. pymolt cannot know; the next phases correctly refuse (capture rejected, verify INCONCLUSIVE).

## 4. What worked well
- assess on the real manifest correctly refused (blocked, no target manifest written) and pointed (in the table) at `theano` as the failing direct dependency.
- Lasagne baseline resolved in the container (Theano 1.0.5, numpy 1.19.5, scipy 1.5.4, six 1.17.0); capture ran inside the container first time and tracked 1961 events with no dropped events.
- The post-migration capture was refused instead of being recorded as evidence; verify returned INCONCLUSIVE (not PASS/FAIL).
- scan makes the Theano dependency visible (148 symbols) and found `pylearn2` (optional GPU code in `lasagne/layers/cuda_convnet.py`), `mock`, and `imp.reload` in `docs/conf.py` (all true).
- Plan of a small project is fast (10 s).

## 5. Workarounds and effort
- Baseline env (not pymolt's job): `pip install numpy scipy mock pytest pytest-cov Theano pep8 pytest-pep8` in python:3.6. It resolved to Theano 1.0.5, numpy 1.19.5, scipy 1.5.4; about 2 min. Test run needs `-o addopts= -p no:pep8` (project config incompatible with pytest 7).
- Target env: `.venv-target` (3.12) with numpy/scipy/six/pytest/mock from `requirements-target.txt`; Theano could not be installed, so the project cannot run there. No Aesara port attempted.
- Manifest workaround: `requirements-pymolt-eval.txt` (numpy, scipy, six; no Theano) so assess could produce a target manifest and the downstream phases could be exercised. It changes what is being assessed, and the report flags it as such.
- `contract_state.json` paths made absolute; `GIT_DIR=/nonexistent` and `--confcutdir=.` for target runs.

## 6. Final state
- Container left running: `pymolt-eval-lasagne` (python:3.6, project at `/work`, Theano 1.0.5 installed).
- Target env: `tests/artifacts/lasagne/.venv-target` (3.12) in place.
- Artifact: `pymolt rollback` run (0 files); `.gitignore` restored with `git checkout`. Remaining untracked: `.pymolt/`, `requirements-target.txt`, `requirements-pymolt-eval.txt` (my workaround file). No source file differs from HEAD.
