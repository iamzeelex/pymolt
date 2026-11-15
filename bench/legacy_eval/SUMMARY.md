# Legacy evaluation: summary (2026-11-15)

Six dead or legacy projects were run through the full pymolt funnel, following [PROTOCOL.md](PROTOCOL.md):
- **Base:** the project's era Python, 3.6, in a running `python:3.6` container (amd64, emulated on arm64).
- **Target:** host Python 3.12.
- **pymolt:** branch `rework/cli-first-local-delta` (27e0790).
- **Per-project reports:** [hyper](hyper.md), [vibora](vibora.md), [theano](theano.md), [lasagne](lasagne.md), [alphalens](alphalens.md), [zipline](zipline.md).

The design that answers the biggest gaps is [docs/design/python-version-breakage.md](../../docs/design/python-version-breakage.md).

## Scorecard

✓ ok · ~ degraded or ok only after a workaround · ✗ failed · — not reached. The "project" label marks the project's own breakage on 3.12, not a pymolt defect.

| | scan | setup | assess | capture baseline | target env 3.12 | plan | apply | capture post | verify |
|---|---|---|---|---|---|---|---|---|---|
| hyper | ✓ | ~ manifest written by hand (deps only in setup.py) | ~ bronze; `--risk` ✓ (CVEs, no wheel) | ~ 3 red tests deselected; 1496 events, 9× | ✓ | ~ 0 rewrites, 2 inconclusive | no-op | ✗ project (`collections.Iterable`, then hyperframe 3.2.0) | INCONCLUSIVE, misleading reason |
| vibora | ✓ (`.pyx` skipped silently) | ✓ | ✗ "GOLD, resolves", but `cython==0.28.3` can't build on 3.12 | ✗ 0 events (tests call no dependency) | ~ after a Cython bump | ~ no baseline | refused (no baseline) | ✗ project (`collections.Callable`) | INCONCLUSIVE |
| theano | ~ 80 s | ✓ | ~ bronze, 11 moves | ✓ 830 events, 3.7× | ~ deps ok; `import theano` ✗ (`numpy.distutils`) | ~ 11/11 inconclusive | no-op | ✗ project | INCONCLUSIVE, trust 3% |
| lasagne | ✓ | ✓ | ✗ real manifest (Theano sdist; all "conflict"); ~ workaround silver | ✓ 1961 events, 3× | ~ Theano won't install | ~ 2/2 inconclusive | no-op | ✗ project | INCONCLUSIVE, trust 0% |
| alphalens | ✓ | ✓ | ✗ setup.py (versioneer `SafeConfigParser` → 8 "conflict"); ~ workaround bronze | ✓ 8× | ~ empyrical sdist | ~ 31/31 inconclusive; `--agent` crashed with a traceback first | no-op | ✗ project (`pandas.util.testing`, `Int64Index`, `scipy.stats.mode`) | INCONCLUSIVE, trust 7% |
| zipline | ~ 30 s | ~ `etc/` locks not selectable | ✗ lock: 97/97 "conflict" (old pandas cap); ~ after relaxing | ✓ 50,244 events, 732 tests, 7× | ✗ project (Cython vs numpy 2) | ~ 41/41 inconclusive, 1 rewrite | ✓ `df.append` → `pd.concat`, correct, rolled back | — | INCONCLUSIVE |

**Bottom line:**
- Behavioural capture works on legacy code inside a container: 5 of 6 projects, 0 activation failures, 0 lost events.
- Everything that should turn that evidence into a migration answer comes back empty or wrong:
  - **dependency API changes:** 100% inconclusive on container baselines;
  - **Python-version breakage:** invisible;
  - **target installability:** unchecked.
- No project reached a compared post-migration capture. Every one is broken on 3.12 in ways pymolt neither predicted nor named.

The capture overhead (3–9×) was measured under amd64 emulation on the 3.6 setprofile backend. It is not comparable with the 3.12 numbers in bench/load.

## Defects, deduplicated and ranked

✔ = re-checked in the code or by re-running it (not only reported by the runner).

| # | Severity | Defect | Seen in | Fix lands in |
|---|---|---|---|---|
| 1 | blocker | The delta engine has no container support. With a 3.6 container baseline the old side is missing, every package comes back INCONCLUSIVE, and the headline still says "0 need attention" ✔ | theano, lasagne, alphalens, zipline, hyper | design P4 (plus the wording in P2) |
| 2 | blocker | Python-version breakage is invisible: stdlib removals at the project's sites (`collections.*`, `imp`, `distutils`, `ssl.wrap_socket`) ✔ | all six | design P1–P3 |
| 3 | major | "Resolves" is reported as success. Unbuildable pins (cython 0.28.3, empyrical, bcolz, Theano) and dependencies that don't import (hyperframe 3.2.0) go unchecked | vibora, hyper, lasagne, alphalens, zipline | design P6 |
| 4 | major | assess drops the resolver's stderr, labels every package "conflict" (97/97 on zipline) and hints "fix the conflict" | lasagne, alphalens, zipline | P0 |
| 5 | major | `plan --agent` crashes with a raw rich traceback (`commands.py:570`, uv `CalledProcessError`) instead of JSON ✔ | alphalens | P0 |
| 6 | major | A relative `--project-dir` stores a cwd-relative trace path (`verify/service.py:1314,1436`), and readers join it onto the project root, so `plan` fails later with a doubled path ✔ | hyper, theano, alphalens | P0 |
| 7 | major | `capture post-migration` does not run in the configured target env, and the post-apply hint says bare `python`. The run uses PATH python and fails with "No module named pytest" ✔ | alphalens, hyper | P0 |
| 8 | major | verify says "no baseline trace is available" when the post-migration side is missing. The impact filter zeroes the static denominator without saying so | hyper, theano, lasagne, alphalens | P0 |
| 9 | major | verify trust is 0–7% on container captures: `/work/...` paths and identity resolution against the host interpreter collapse the static/dynamic join | theano, lasagne, alphalens, hyper | design P4 (path mapping, runner identity) |
| 10 | major | setup offers only root manifests and refuses `etc/requirements_py36_locked.txt`, which scan found | zipline | P0 |
| 11 | major | `setup.py` is parsed by assess (regex) but not by scan (0 edges); two parsers disagree ✔ | hyper, alphalens, zipline | P0 |
| 12 | major | succession knows nothing about Theano, Aesara, PyTensor or nose, and its "frameworks" are every manifest edge ✔ | theano, lasagne | knowledge (separate) |
| 13 | major | A 0-event capture returns `ok: true, accepted: false` with a hint to "see command_log" while `command_log` is null. `-t ujson,uvloop,pytest` is skipped as `no-module` although they are installed (cause unconfirmed) | vibora | P0 (hint); investigate `-t` |
| 14 | minor | A baseline with red tests is refused with no override, though legacy suites are often red | hyper | P0 decision: accept as red-baseline with a stamp? |
| 15 | minor | scan takes 80 s on theano (serial LibCST metadata, `contact_map.py:383`) and is rerun by plan and verify | theano, zipline | design P1 (per-file cache) |
| 16 | minor | `status` cuts container names to 12 characters (`status.py:103`) ✔; setup lists every container as "bind-mounting project"; setup edits `.gitignore` silently; apply says "Migration applied" for a no-op; assess calls a ranges-only manifest "pinned"; `.pyx` is skipped silently; `imp`/`distutils` show as dependencies (host stdlib list) | several | P0 / design P1 |
| 17 | minor | The base is reconstructed at the latest 3.6-compatible versions, not the project's era | zipline | later |

## What worked
- **Container capture on 3.6:** 5 of 6 projects, 0 activation failures, 0 lost events. On zipline it recorded 50,244 events from 732 tests.
- **From-versions come from what the baseline capture ran,** through the container. On alphalens that was pandas 0.25.3, not 1.1.5 from today's resolve.
- **The fixes that exist are correct:** zipline's `df.append` → `pd.concat` rewrite was right and rolled back cleanly.
- **Honest refusals:** failed and empty captures never displace evidence; apply refuses without a baseline.
- **`assess --risk`:** h2 2.6.2 CVEs, brotlipy with no cp312 wheel and abandoned.
- **scan:** every spot-checked contact was true (25+ checks). The contact map already held the call sites that later broke (`pandas.util.testing`, `pandas.Int64Index`, `scipy.stats.mode`).
- **`status`** was accurate throughout.

## Environment notes
- The Docker daemon (8 GB) died twice during the parallel emulated runs, with every container exiting 137.
- `pymolt forks` was not run: there was no `GITHUB_TOKEN`.
- **Artifacts:** source restored. Untracked pymolt output remains (`.pymolt/`, `requirements-target.txt`, `*-pymolt-eval*`), which `ignore = dirty` keeps out of the parent's status.
- **Containers:** `pymolt-eval-<project>`. Target envs: `tests/artifacts/<project>/.venv-target`.
