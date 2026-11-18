# Legacy-project evaluation protocol

The goal is to measure how well pymolt handles real legacy projects, phase by phase. **Evaluate; do not fix pymolt.** Record what happened, whether pymolt's messages were honest and actionable, and every defect with evidence.

## Ground rules
- Use pymolt from `/Users/zeelex/Documents/projects/python_migrator/.venv/bin/pymolt`, branch `rework/cli-first-local-delta`. Do not switch branches.
- Do not edit `pymolt/`, `tests/` (outside your artifact), git config, or anything in the parent repo except:
  - your report `bench/legacy_eval/<project>.md`;
  - files pymolt itself writes into the artifact.
  - Never run `git add`, `commit`, `stash` or `checkout` in the parent repo.
- Artifacts are `tests/artifacts/<project>`: shallow git submodules, `ignore = dirty`. pymolt writes `.pymolt/` and target manifests there; that is expected.
  - A workaround file you add there must be named `*-pymolt-eval*`.
  - At the end, run `pymolt rollback <dir>` if you applied a plan. Restore any other source edits with `git -C tests/artifacts/<project> checkout -- <paths>`.
- Logs and scratch go under `<local scratch>/eval/<project>/`. Keep every command's full output there; quote only excerpts in the report.
- Time:
  - about 90 min wall time per project;
  - abort a single step after 20 min and record it as a timeout;
  - run long commands in the background and wait for them; don't poll in a tight loop.

## Environments
- **Host:** macOS arm64, Python 3.8–3.14 via uv. There is no 3.6/3.7 on the host.
- **Baseline (the project's era):** a running container, so that pymolt's container path is exercised:
  ```bash
  docker run -d --name pymolt-eval-<project> --platform linux/amd64 \
    -v <abs path to tests/artifacts/<project>>:/work -w /work python:3.6 sleep infinity
  ```
  - `python:3.6` is Debian with gcc. linux/amd64 is used because the old wheels only exist for x86_64; it runs emulated, so it is slow.
  - Install the project's era dependencies inside it (its own pins or locks), plus pytest.
  - Record how hard that was. That is the user's real starting point, not pymolt's job.
- **Target:** host Python 3.12, built from what `assess` produces (it prints how). Install the project into it, and build any Cython or C extensions if needed.
- Docker has 10 CPUs and 8 GB, shared by three evaluation runs in parallel. Keep containers lean.

## Phases (record each)
| # | Command (adapt paths) | Record |
|---|---|---|
| 0 | `pymolt scan <dir>` (already run; numbers given in your brief) | spot-check 3–5 reported contacts or edges against the source: true or false? |
| 1 | `pymolt setup <dir> --manifest <m> --tool <container\|uv> --container pymolt-eval-<p> --base-python 3.6 --target-python 3.12` | what it chose and refused; the messages |
| 2 | `pymolt assess <dir> --agent` (and `--json` to a log) | baseline tier, upgrade/conflict/manual rows, `requirements-target.txt` written?, time, honesty of the verdict |
| 3 | `pymolt capture baseline --mode tests --project-dir <dir> -- python -m pytest -q <subset>` (it runs in setup's container) | events, backend, activation failures, trust/coverage; wall time captured vs the same command uncaptured (overhead) |
| 4 | build the 3.12 target env | success, or what blocked it |
| 5 | `pymolt plan <dir> --target-manifest requirements-target.txt --agent` (offline; then `--online` if the API changes come back inconclusive) | where the from-versions came from, delta quality per package, rewrites proposed (correct?), risks per tier, time |
| 6 | `pymolt apply <dir>` (if the plan changes files), then run the subset in the target env | applied cleanly? did the rewritten code run? |
| 7 | `pymolt capture post-migration --mode tests --project-dir <dir> -- <target python> -m pytest -q <subset>` | as in 3 |
| 8 | `pymolt verify <dir> --agent` (and `--json` to a log) | verdict and reasons, fact-diff counts, calibration (hit / prevented / miss / not exercised / unpredicted) |

Pick a test subset that runs in under about 5 min uncaptured inside the emulated container. Use the same subset on both sides.

When a phase cannot proceed:
1. Record why.
2. Judge whether pymolt said so honestly and usefully.
3. Apply the smallest documented workaround if one exists, and continue. For example, if pymolt finds no dependencies because they live only in `setup.py`, write `requirements-pymolt-eval.txt` from `install_requires` and pass it as the manifest.
4. Never fake a later phase.

## Report: `bench/legacy_eval/<project>.md`
1. **Scorecard:** one row per phase. Columns: outcome (ok / degraded / failed / not reached), wall time, key numbers, honest message (yes / no / n/a).
2. **Defects:** each with severity (blocker / major / minor), a repro command, an evidence excerpt (≤ 10 lines), and the suspected code location if a quick grep finds it. Separate pymolt defects from the project's own breakage.
3. **What worked well.**
4. **Workarounds applied,** and how much manual work the baseline and target envs took.
5. **Environment state left behind:** the container name, env paths, and whether the artifact was restored.
