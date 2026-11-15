# Legacy evaluation: vibora

Project: `tests/artifacts/vibora` (asyncio web framework, Python 3.6 only, 11 Cython `.pyx` + 14 extensions, archived). Branch `rework/cli-first-local-delta`. Logs: `/private/tmp/claude-501/-Users-zeelex-Documents-projects-python-migrator/b5b734e8-1757-4fe9-b0ed-03fd9bc6c401/scratchpad/eval/vibora/`.

Test subset: 17 files under `tests/` (`headers forms cache hooks limits responses blueprints components exceptions streaming subdomains timeouts router schemas templates client/interface client/streaming`). The test files are not named `test_*.py`, so they must be passed explicitly. Baseline uncaptured in the container: 74 passed in 28.6 s.

## 1. Scorecard

| # | Phase | Outcome | Wall | Key numbers | Honest message |
|---|-------|---------|------|-------------|----------------|
| 0 | scan | ok, 16 contacts confirmed plausible (see below) | 2 s | 1 edge (cython==0.28.3), 16 contacts, 139 `.py` files | partly: `.pyx` not scanned and not said |
| 1 | setup | ok | 2 s | requirements.txt, container, 3.6 -> 3.12 | yes |
| 2 | assess `--agent` / `--risk` | degraded | 43 s / 4 s | "GOLD base, resolves on Python 3.12"; 0 upgrades; risk: cython medium ("no binary wheel ... builds from source") | no (D1) |
| 3 | capture baseline | failed | 46 s (also with `--backend wrap` and `hybrid`) | 0 events each time, nothing accepted | partly (D2, D3) |
| 4 | build 3.12 target env | failed as pinned; ok with workaround | 1 s fail / 11 s ok | `cython==0.28.3` sdist: `AttributeError: module 'collections' has no attribute 'Iterable'`. With `cython==0.29.37` all 14 extensions build on 3.12 | pymolt said nothing wrong about the pin (D1) |
| 5 | plan | degraded | 0.5 s | 0 files, 0 moves, `baseline_bound: false` | yes (warns "No baseline capture is bound") |
| 6 | apply, run subset on 3.12 | apply: refused; run: failed | - | `Error: migration plan needs evidence: a valid baseline capture is required before apply`. Subset: 13 of 17 files fail at collection, `cannot import name 'Callable' from 'collections'` (vibora/templates/ast.py:1) | yes |
| 7 | capture post-migration | failed | 1 s | exit code 2 (project breakage), not accepted | yes |
| 8 | verify | inconclusive | 3.5 s | static 13, confirmed 0, BLIND 13, trust 0% | yes |

The funnel stops at phase 3. Phases 6 to 8 were run only to record how pymolt reports a missing baseline; nothing was faked.

### Phase 0 spot-checks
- 16 contacts: all are `import` sites in `samples/` and `vibora/` (`ujson`, `uvloop`, `cryptography.fernet`, `aiohttp`, `asyncio_redis`, `jinja2`, `requests`, `setuptools`). I grepped every `import`/`from` of those names in `.py` and `.pyx`: no third-party import is missed. Cython sources import only vibora, stdlib and `cimport` modules. `vendor/http-parser-2.8.1` is C, not Python.
- So 16 in 139 files is plausible: vibora is nearly self-contained (own HTTP parser, router, client, template engine). The brief said 6 deps; the report actually has 8 (also `ujson`, `uvloop`).
- Gap: `files_scanned` = 139 is the count of `.py` files only; the 11 `.pyx` files (and `.pxd`) are never parsed. It happens that they hold no third-party imports, but the scan does not say it skipped them. Also `ujson`/`uvloop` are only in `extras_require['fast']` in setup.py, which scan does not edge-classify (it says so: "not yet edge-classified").

## 2. Defects

### pymolt defects

**D1 (major): `assess` reports "GOLD base, resolves on Python 3.12" for a project whose only pinned dependency cannot be installed on 3.12.**
Repro: `pymolt setup <dir> --manifest requirements.txt --tool container --container pymolt-eval-vibora --base-python 3.6 --target-python 3.12; pymolt assess <dir> --agent`, then follow the printed `uv pip install -r requirements-target.txt`.
```
"summary": "GOLD base — resolves on Python 3.12 ... 0 package(s) differ, 1 unchanged."
uv pip install -r requirements-target.txt  ->  Building cython==0.28.3 ... collections.Iterable
AttributeError: module 'collections' has no attribute 'Iterable'
```
`assess` resolves metadata but never checks that the pinned sdist builds. The `--risk` output does flag `cython: no binary wheel for target Python, builds from source (C toolchain)` (medium), but it does not say that Cython 0.28.3 is incompatible with 3.12, and the default (non-`--risk`) run says GOLD with no warning. "Resolves" and "installs" are different; the verdict detail should not say "Pinned, production-exact base; resolves cleanly" when the printed install command fails. Also, requirements.txt holds only the build tool; runtime deps (ujson, uvloop in setup.py extras) are not part of the resolve, so GOLD covers 1 package. Location: `pymolt/assess/service.py` verdict assembly; risk logic under `pymolt/assess/risk/`.

**D2 (major): a test suite that never calls a tracked Python dependency gives "capture produced zero events" and a misleading hint.**
Repro: `pymolt capture baseline --mode tests --agent --project-dir <abs> -- python -m pytest -q tests/responses.py` in the vibora container.
```
"ok": true, "accepted": false, "validity": "empty", "validity_reason": "capture produced zero events"
"returncode": 0, "command_log": null
"hint": "Fix the command (see command_log) and capture again; the slot is unchanged."
```
The command is fine (rc 0, 74 tests pass) and `command_log` is null, so the hint sends the user to a log that does not exist. The real cause is that vibora's tests use only `unittest` and vibora itself; the only third-party contacts are `ujson` (C extension) and `uvloop`, and pymolt records nothing for them. Meta shows `discovered: 0`. Also `"ok": true` with `accepted: false` is ambiguous for agents. The user gets no actionable path (for example, "no dependency symbol was exercised; static contacts: 16; try a command that runs the app").

**D3 (major, suspected): explicit `--target` of installed packages is skipped as `no-module`.**
Repro: `pymolt capture baseline --mode tests --backend hybrid -t ujson,uvloop,pytest --project-dir <abs> -- python -m pytest -q tests/responses.py tests/streaming.py`.
```
"instrumentation_skipped": [{"reason":"no-module","target":"ujson"},{"reason":"no-module","target":"uvloop"},{"reason":"no-module","target":"pytest"}]
```
All three are installed in the container (`distributions` lists `ujson 1.35`, `uvloop 0.10.2`, `pytest 7.0.1`) and `import vibora.utils` resolves `ujson`. I could not confirm whether the target check runs before the module is imported, or whether single-file C extensions (ujson) are unsupported, but pytest is a normal package and is skipped too. This blocked the only route to getting any events from this project. Not traced to a line.

**D4 (minor): container name truncated to 12 characters in `status` and capture notes.**
```
✓ base environment container pymolt-eval- is running
"env_note": "baseline → container pymolt-eval-"
```
`pymolt/status.py:103` uses `config.container_id[:12]`, meant for a hex id; with names `pymolt-eval-hyper`/`-vibora` both read as `pymolt-eval-`. Same code path was seen on hyper. Affects anyone with named containers.

**D5 (minor): `setup` lists every running container as "bind-mounting project"** (here: hyper, lasagne, zipline, alphalens, theano) although only one mounts this directory. `setup` also adds `.pymolt/env_config.json` to the project's `.gitignore` without saying so (restored at the end).

**D6 (minor): `scan` skips `.pyx`/`.pxd` silently** (see phase 0). A Cython-heavy project's `files_scanned` should state what extensions were not parsed.

**D7 (minor): `plan` with no baseline says `ok: true` and "0 dependency moves"** while `apply` is refused. It does carry the warning "No baseline capture is bound to this plan: apply refuses it without --allow-no-baseline", which is good, but the Python-language breakage (D8) is again not mentioned.

**D8 (major, shared with hyper): nothing in `assess`/`plan`/`verify` reports the stdlib removals that break the code on 3.12.** `from collections import Callable` in vibora/templates/ast.py:1 breaks 13 of 17 test files; the funnel is silent about it. `scan` warns only about Python-2 imports.

### Project's own breakage (not pymolt)
- `cython==0.28.3` cannot build on 3.12; Cython 0.29.37 can build the 14 extensions (stale `-I/git/vibora/vibora` include in setup.py is harmless).
- `from collections import Callable` (removed in 3.10) and invalid escape sequences (`SyntaxWarning`) in vibora/templates/nodes.py.
- `build.py` deletes `.c`/`.so`/`.html` under `vibora/vibora/` before building. All build products are gitignored.
- Declared deps are incomplete: aiohttp, jinja2, requests, cryptography, asyncio_redis are imported by samples but declared nowhere; `ujson`/`uvloop` only as the `fast` extra.

## 3. What worked well
- `scan` is accurate on what it parses; its edge note about setup.py is honest.
- `setup`/`assess` handled the legacy 3.6 container path in 2 s and 43 s without errors; `--risk` correctly flagged the source build.
- `plan` refuses to pretend: `baseline_bound: false` plus a clear warning, and `apply` refuses with a precise message and a fix ("Capture a valid baseline and regenerate the plan").
- `verify` is honest with no data: INCONCLUSIVE, 13 static, 13 BLIND, trust 0%, "no trace given - every static contact is BLIND".
- Rejected captures keep a diagnostic artifact and leave the slot unchanged.
- `status` prints a clear next command at each point.

## 4. Workarounds applied
- Baseline env (container `python:3.6`): `pip install -r requirements.txt pytest ujson==1.35 uvloop==0.10.2 aiohttp requests jinja2 cryptography` plus `python build.py` (Cython -> C -> 14 `.so`). The install was quick; the build took 6 min 23 s under amd64 emulation (installs and build in one 6:23 command). Easy but slow; pymolt was not involved.
- Target env: `uv venv .venv-target --python 3.12`; `requirements-target.txt` fails; then `uv pip install cython==0.29.37 setuptools wheel pytest` and `.venv-target/bin/python build.py` (11 s). Note this host build overwrote the linux `.so` files in the shared mount with darwin ones, so the container's baseline env no longer imports vibora until rebuilt (`docker exec pymolt-eval-vibora python build.py`). I did not rebuild, since no baseline could be captured anyway.
- Tried `--backend wrap` and `hybrid`, and `-t ujson,uvloop,pytest`: all zero events (D2, D3). No other workaround exists, so phases 3, 6, 7, 8 stop here.
- Host pytest needs `--confcutdir=<artifact>` or the parent repo's `conftest.py` is picked up.

## 5. Environment state left behind
- Container `pymolt-eval-vibora` running (python:3.6, amd64, `/work` = the artifact; vibora is currently built for darwin/3.12, see above).
- Target env `tests/artifacts/vibora/.venv-target` (Python 3.12.11, Cython 0.29.37, pytest) in place, with host-built `.so` files in the source tree (gitignored).
- Artifact restored: no tracked source file changed; the `.gitignore` edit made by `setup` was reverted with `git checkout -- .gitignore`. `pymolt apply` was refused, so `rollback` was not needed. Untracked pymolt output remains: `.pymolt/`, `requirements-target.txt`.
