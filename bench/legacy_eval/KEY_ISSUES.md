# Key issues from the legacy evaluation

These are distilled from [SUMMARY.md](SUMMARY.md) and the six per-project reports. ✔ = confirmed in the code (`file:line`); "reported" = only seen in a runner's report.

They fall into three groups:
- **A:** the funnel cannot produce an answer (capability gaps);
- **B:** functional bugs;
- **C:** inaccuracies, where pymolt states something false or misleading.

Group C breaks the product's core promise ("honesty markers"), so it ranks high even where the code fix is small.

## A. The funnel cannot produce an answer

| id | Issue | Evidence |
|---|---|---|
| A1 | **API changes are always INCONCLUSIVE for container baselines.** The delta engine resolves the old side only on the host (host env, throwaway env with `base_python`); there is no 3.6 on the host and no container path. All packages on all four container projects came back INCONCLUSIVE, so alphalens's `pandas.util.testing` removal was missed. | ✔ `pymolt/delta/envs.py` has no container or docker code; `resolve_old` at `envs.py:265` |
| A2 | **Python-version breakage is invisible.** Stdlib removals at the project's own sites (`collections.*` ABCs, `imp`, `distutils`, `ssl.wrap_socket`) never appear in assess, plan or verify. All six projects break on 3.12 this way. | ✔ the contact map drops stdlib names by the *host* `sys.stdlib_module_names` (`verify/contact_map.py:400`); no stdlib facts exist anywhere |
| A3 | **Resolves ≠ installs ≠ imports.** assess stops at the resolve. Unbuildable pins (cython 0.28.3, empyrical, bcolz, Theano) and dependencies that fail to import on the target (hyperframe 3.2.0) pass as fine. | ✔ by absence: assess has no install or import step |
| A4 | **No evidence path when tests call no dependency.** vibora captured 0 events. `-t ujson,uvloop,pytest` was skipped as `no-module` although they were installed. | reported; the `-t` cause is unconfirmed |

## B. Functional bugs

| id | Issue | Evidence |
|---|---|---|
| B1 | **Paths anchored to the current directory instead of the project.** (a) With a relative `--project-dir`, capture stores a cwd-relative `trace_path`; readers join it onto the project root and get a doubled path, so `plan` fails later. (b) The post-migration capture opens `target_env_path` relative to the cwd, misses the venv, runs `python` from PATH and fails with "No module named pytest". | ✔ (a) `verify/service.py:1314,1436` vs `migration_plan.py:278`; (b) `verify/service.py:575-576` |
| B2 | **Container paths are not mapped.** Sites are recorded as `/work/<project>/...`, `_relativize` only handles paths under the host root, the static/dynamic join collapses, and trust is 0–7% on every container project. | ✔ `verify/facts.py:2512`; lasagne trace sites are `/work/lasagne/...` |
| B3 | **`plan --agent` crashes with a rich traceback instead of JSON** when uv fails on the target manifest. | ✔ `interfaces/cli/commands.py:570` (alphalens log) |
| B4 | **setup sees only root manifests plus `requirements/`.** zipline's real lock `etc/requirements_py36_locked.txt` (scan found the `etc` root) is refused as "unknown manifest". | ✔ `ingestion/detect.py:157,168`; `setup/service.py:372` |
| B5 | **Two `setup.py` parsers disagree.** scan's inventory skips `setup.py` (0 edges); assess extracts `install_requires` by regex. | ✔ `inventory/edges.py:307-316` vs `ingestion/detect.py:386` |
| B6 | **scan is slow and repeated:** 80 s on theano (serial LibCST metadata per file), rerun by plan and verify. | reported (cProfile `contact_map.py:383`) |
| B7 | **Container names are truncated** to 12 characters (`pymolt-eval-`). | ✔ `status.py:103`, `verify/service.py:560` |
| B8 | **A baseline with red tests is refused with no override,** although legacy suites are often partly red. | reported (a design decision, not a bug) |
| B9 | **The configured target env's interpreter was resolved through its symlink.** A venv's `bin/python` links to the base interpreter, so `resolve()` ran the base Python without the venv: "No module named pytest". Found by the scripted evaluation (WP4); fixed in Wave 0 by keeping the absolute link path. | ✔ `verify/service.py` `resolve_capture_env` |

## C. Inaccuracies (pymolt says something false or misleading)

| id | Statement | Reality | Evidence |
|---|---|---|---|
| C1 | plan: "0 need attention", "The plan changes no file" | 100% of API deltas were INCONCLUSIVE, so nothing was rated | ✔ views wording; every container project |
| C2 | assess: "GOLD — resolves on Python 3.12 … Pinned, production-exact base; resolves cleanly" | the only pin cannot be built on 3.12 (vibora) | reported, confirmed by re-running the install |
| C3 | assess: every package `conflict` (97/97 on zipline), hint "fix the conflict in target_error" | the resolver's stderr (a `SafeConfigParser` crash in versioneer, `git+git://` lines, an unbuildable sdist, an old cap) is dropped | ✔ `assess/service.py:198-199` keeps `str(e)` only; `:149-150` marks every row `conflict` |
| C4 | verify: "no baseline trace is available for a version diff" | the baseline exists; the post-migration side is missing | ✔ `verify/service.py:1970` names the `against` slot "baseline" regardless of which side is missing |
| C5 | verify: "Static 0 … trust 0%" with a 238-contact project | the impact filter narrowed the denominator to the plan's packages and does not say so | reported (hyper D8) |
| C6 | capture: `ok: true` + "Fix the command (see command_log)" | not accepted; `command_log` is null; for a 0-event run the command was fine | ✔ `interfaces/views.py:611` (a fixed hint for every rejection) |
| C7 | apply: "Migration applied" | a no-op (0 files) | reported |
| C8 | succession: "No known succession path" for Theano; "frameworks" = every manifest edge (scipy, six) | the knowledge simply lacks Theano, Aesara, PyTensor and nose | ✔ `pymolt/knowledge/succession.json` |
| C9 | scan: `imp`, `distutils` listed as dependencies; `.pyx` skipped silently; "0 edges" for setup.py-only projects | stdlib removed in 3.12 (host-dependent list); 11 Cython files unparsed; deps exist in `setup.py` | ✔ `contact_map.py:400`; B5 |
| C10 | setup: lists every running container as "bind-mounting project"; edits the project's `.gitignore` without saying so | only one container mounts it | reported |
| C11 | assess: `source_fixation: "pinned"` alongside "not fully pinned … a reconstruction" | the manifest holds ranges only | reported (hyper D4) |

## Reusable prior work

| Need | Verdict | Detail |
|---|---|---|
| A1: API changes without the old interpreter | **adapt the idea: a static old side only** | `analyzers/griffe_diff.py` + `public_surface.py` diff two releases from wheels/sdists via griffe, tested and wired. The whole-package diff is unsafe. Re-measured on flask 2.0.3→3.0.0: **56 of 111 "removed" are alive on 3.0**, because griffe 2.0.2 cannot load the namespace subpackage `flask/sansio`, so inherited members vanish. Re-exports (`flask.Markup`, `flask.abort`) and PEP 562 `__getattr__` names are invisible. All of these errors fall on the *new* side, which pymolt always probes live. Safe use: a static view of the **old** side for touched symbols only, where an unresolved base means unknown, aliases are followed, defaults are compared only as literals, and the tier is capped. |
| A2: Python stdlib changes | nothing | build new (design P1–P3) |
| A1 runner (container / legacy interpreter) | nothing | `ghost_stubs.py` runs the host interpreter only; pymolt's docker exec code is already richer |
| A3: installability | nothing | build new (design P6) |
| B5: setup.py parsing | nothing | build new |
| C8: succession | nothing new | pymolt's `succession.json` is already a 1:1 port (keras, tf1→tf2, keras→torch). Theano→PyTensor/Aesara and nose→pytest must be written |
| codemod kinds | nothing | pymolt's appliers are stronger (binding-aware). Neither repo has attr rewrite with an added import, `replace-attr-path` or unittest aliases |
| successor mining | idea only | add to `delta/fetch.py::successor_from`: the "replaced by / moved to / superseded by" regexes, stopwords, the dispatcher denylist and no-op gate, recurrence demotion, and `@deprecated("…")` messages. Skip CFG/SSA (about 1,200 lines for forwarding the probe already sees) |
| first release that works on 3.12 (hyperframe) | idea, plus a small port | nothing checks Python compatibility. Build it in pymolt's `Fetcher`: `requires_python`, wheel tags, range-read modules checked against the stdlib lifetimes. Port the sdist fallback and safe extraction (`acquisition/sdist_cache.py:39-151`) for sdist-only releases (Theano) |
| cheap ports | port | `acquisition/import_roots.py` (pure, 8 tests); the curated `DataFrame.lookup` rule (`core/rule_catalog.py:31`) as JSON |

## Proposed order

Principle: first make the instrument honest and the evaluation repeatable, so every later wave is measured by re-running the six-project scorecard.

| Wave | Content | Issues | Size |
|---|---|---|---|
| **0. Honest instrument** | Anchor every stored path to the project (trace path, target env). `plan --agent` never tracebacks. assess keeps the resolver's stderr and marks only the culprit, not every row "conflict". Correct wording: verify says which side is missing; "0 need attention" never shows over unrated deltas; capture's hint follows the real cause and `ok` means accepted; apply says "no change" for no-ops; succession says "not in the knowledge". setup accepts sub-root manifests that scan found; one setup.py parser. **Script the evaluation** (`bench/legacy_eval/run.py`: containers, era deps, workaround manifests, fixed test subsets) | B1, B3, B4, B5, B7, C1, C3, C4, C5, C6, C7, C8 (wording), C10, C11 | 3–4 d |
| **1a. Container runner** (track A) | `RunnerSpec`; delta old side, identity resolution and path mapping through `docker exec`; `/work` → project paths | A1, B2, B7 | 4–5 d |
| **1b. Stdlib detection** (track B, parallel to 1a) | design P1–P2: lifetimes data, stdlib sites with guards, per-file scan cache, python pseudo-migration (target probe plus data), evidence-capped tiers, plan without a baseline | A2, B6, C9 | 6–7 d |
| **2. Fixes and fallback** | design P3 (LibCST: attr rewrite plus import, `replace-attr-path`, curated stdlib rules, minimal-diff gate). Static old side from the griffe idea above, for when no runner is reachable | A2 fixes, A1 fallback | 5 d |
| **3. Imports and installs** | design P5 (`capture --mode imports`, verify import layer, calibration, `rejection`) and P6 (assess install/import ladder, culprit pin) | A3, A4 (partly) | 7 d |
| **4. Knowledge and search** | Theano→PyTensor and nose→pytest succession; the prose-miner ideas; the `DataFrame.lookup` rule; first release importable on the target; A4's `-t no-module` | A4, C8, hyperframe | 4–5 d |

Each wave ends with the scripted evaluation (Docker now has 16 GB, so the six projects run in parallel) and a scorecard diff.

**Owner decisions needed:**
- B8: should a red baseline be accepted with a stamp? Needed before wave 0 ends.
- Q1: may an imports-only baseline unlock `apply`? Q2: should assess check installs by default? Both needed before wave 3.
