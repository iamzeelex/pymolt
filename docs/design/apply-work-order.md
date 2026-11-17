# Apply as a work order: hardening, self-check, steps, every executor

Status: design, 2026-11-17. Base: `feat/verify-coverage-and-recall` (d35e759). One scope, approved
by the owner, finished before Wave 3:
- **H**, a hardening pass, and **D**, a self-checking apply. Both are being implemented as stacked
  commits in `.claude/worktrees/apply-hardening`. This spec builds on them and does not redefine
  them (section 0.2).
- **B**, a stepwise apply: the plan becomes ordered steps, and apply, rollback and verify work per
  step.
- **C**, apply as the step ledger for every executor: pymolt, an agent or a person does a step, and
  the ledger records it with the claim verify judges it by.

Evidence: the apply review synthesis of 2026-11-17, re-checked against d35e759. The corrections are
in section 0.3. Out of scope: environment builds and installs (Wave 3), git patch series (section 9).

## 0. Starting point

### 0.1 What the code does today

| Fact | Where | What B/C changes |
|---|---|---|
| `plan` freezes whole-file `old_source`/`new_source` per changed `.py` file | `migration_plan.py:165-177`, `:323-342` | Steps become the unit. `changes` stays as a view derived from the steps |
| Dependency rewrites and Python fixes merge into one `FilePreview` per file; the text between them is not kept | `codemods/service.py:663-673`, `pyversion/fixes.py:305-360` | The step builder replays the recipes and keeps each intermediate text (1.5) |
| `apply` writes the frozen batch all or nothing, under an OS lock | `migration_plan.py:47-95`, `:569-726`; `codemods/apply.py:909-975`, `:1084-1111` | Kept. One invocation is one transaction over the steps it selects |
| Ledger: one run, difflib ranges on (old, written) text, recipe found by leaf substring, archived by the next apply | `apply_ledger.py:58-121` | Ledger v2: entries per step, accumulated, remapped (2) |
| Verify loads the ledger whenever the file exists | `verify/service.py:2843-2844` | H8 scopes it to the run in effect; v2 composes every step in effect (2.3) |
| Strict provenance for 4 claims, hand-edit rules for the rest | `verify/facts.py:2710`, `:3062-3074`, `:6594-6640`, `:6737-6753` | Judged by `claim` (`preserve`/`intended`); the 4 claims become `basis` (5.1) |
| Risk id = sha1(symbol, file, func, **line**, text) | `verify/predict.py:433-435` | The line drops out (1.4) |
| `fixed_by_plan` = the call's line changed and the leaf left it | `verify/predict.py:224-237`, `:533-541` | Replaced by risk→step links, so a fix counts only when its step is in effect (5.5) |
| Receipt: one run, `in_effect` = applied, or rolled back with no file restored | `migration_state.py:25-61` | A set of receipts, with `in_effect` per step (2.4) |
| Rollback: whole run; refuses unless every file is at `new_sha256` (or `--force`) | `migration_plan.py:756-868` | Per step, file-level first, inverse hunk later (4) |
| `status`: a post capture without a receipt is stale; the report is done only with an `applied` receipt | `status.py:473-523` | H7, then a projection across steps (6) |
| `apply`/`rollback` have no `--agent` and no MCP tool | `interfaces/cli/commands.py:798-956`, `interfaces/mcp_server.py:85-306` | Decision 4 (3.6) |
| A capture records, per fact, file, `co_name` func, line, line text, counts, and per process the lines or functions of the project that ran. **It records no test id.** | `verify/facts.py:238-268`, `verify/_backend.py:1465`, `:927-960` | "How to verify" lists sites, not tests. Test ids are optional stage 7 |

### 0.2 H and D (in flight): where B and C extend them

| Item | As implemented in the H/D stack | Extended here by |
|---|---|---|
| H1 re-apply of an applied plan is a no-op success | whole plan | the same no-op per step: a step whose effect is on disk (3.4) |
| H2 re-apply after rollback, no re-plan, while files match `old_sha256` | whole plan, exact hashes | per step through anchors: a rolled-back step re-applies while its anchors and recipe check pass (1.6) |
| H3 precondition = setup fingerprint + target-manifest hash | `inspect_migration_plan` (`migration_plan.py:466-469`) | Stage 4 also applies it to the baseline check (`:616`, today the full fingerprint), unless H3 already does. The run's starting fingerprint becomes the baseline's own when only the manifest's contents moved (3.2 step 2) |
| H4 calibration credits import moves | in `_fix_for` | step links replace `_fix_for` for v2 plans, and H4's rule becomes one case of the link (1.5.4) |
| H5 blocked or downgraded rules become manual items in the plan | the plan's manual-items list | each item becomes a `manual-rewrite` step (1.1) |
| H6 apply shows advisories | apply summary | the summary lists the remaining steps, review steps included (3.5) |
| H7 status projects a migration with no receipt | status | the same rule with steps that were never recorded (6) |
| H8 the ledger carries `run_id`; verify reads only the run in effect | per run | entries carry `run_id` and `step_id`, accumulate across runs, and are remapped to the snapshots (2.3) |
| D compile before writing (target, and base where claimed); optional smoke import after writing, auto-rollback; results on the receipt | per apply | run per invocation over its steps. The results go on each step's receipt (`checks`), and recorded agent and person edits get the compile check too (3.3) |

The flags, defaults and exit code of D's checks are D's to choose. This spec only needs, per step,
`checks.{compile_target, compile_base, smoke_import}` = `ok | failed: <reason> | not checked: <reason>`.

### 0.3 Defects found while writing this spec (stage B0)

| # | Defect | Evidence | Fix |
|---|---|---|---|
| B0-1 | **A Python fix can land on the wrong statement.** `apply_python_fixes` finds its target lines on the original file (`live_lines(sites)`, contact map on disk) and applies them to the dependency-rewritten text (`start = prior.new_source`, `pyversion/fixes.py:301-324`). A dependency rewrite that adds a line above the fix shifts it. | Reproduced on d35e759: `from pkg.old import a, b` split by a `rewrite-import` (+1 line), then a `collections.Iterable` fix restricted to the live site's original line 5. It rewrote the non-live use (line 4, shifted to 5) and left the live one (shifted to 6). The minimal-diff gate passes, because the changed line names the spelling. | Resolve the fix's sites through anchors (1.4), or map the lines through `diff(T0, T_dep)`. A test with a multi-name import split above a fix site |
| B0-2 | **`recipes` and `claims` name rules that wrote nothing.** In verified-only previews a FilePreview's `rules` is every rule that *touched* the file, blocked ones included (`codemods/apply.py:1150-1158`, `:1340`, `:1389`); the plan copies it into `recipes`/`claims` (`migration_plan.py:335-340`). `claim_for` reads the evidence prefix before the confidence (`apply_ledger.py:42-48`), so an unverified LEGACY rule (`delta/provider.py:114-127`) carries `same-object`. `_recipe_for` can then attribute a hunk to it. | Code reading. Not seen in a real plan. | Plan recipes and claims come from adopted hits only. Blocked rules go to H5's manual items |

B0-1 changes plan output in files that have both kinds of rewrite. B0-2 changes only metadata. Both
are one-commit fixes. They can go on top of the H stack or open stage 1.

## 1. Plan schema v2: the work order

### 1.1 Phases and step types

Steps are ordered by phase, then by application order within the phase. That order reproduces v1's
composition exactly (1.5).

| Phase | `type` | Default executor | `claim` / `basis` | Comes from | Recipe re-runnable |
|---|---|---|---|---|---|
| `env` | `manifest-pin` | pymolt (decision 1), else person | `intended` / `target-manifest` | the source manifest's `==` pins against the target manifest | yes (line rewrite) |
| `env` | `target-env` | person | none (no code edit) | assess's build commands (`commands.py:2096-2122`) | no; status projects it from the environment check (`status.py:85-131`) |
| `mechanical` | `codemod` (`recipe.kind` ∈ `rewrite-import`, `rename-call`, `rule`) | pymolt | `preserve` / `same-object`, `same-signature`, `verified-rule` | adopted dependency patterns and rules | yes |
| `mechanical` | `python-fix` (`recipe.kind` ∈ `rewrite-attr`, `replace-attr-path`, `rewrite-module`, `rewrite-import`, `rename-call`) | pymolt | `preserve` / `python-fixer` | adopted Python fixes | yes |
| `mechanical` | `manual-rewrite` | agent | `preserve` / `manual` | H5 items (blocked or downgraded rules), rule advisories `not-applied` | no (the template is shown as a suggestion) |
| `signature` | `api-change` | agent | `preserve` / `manual` | risks needing attention: REMOVED/MOVED with no auto fix, SIGNATURE at a passed parameter, DEPRECATED with a successor | no |
| `signature` | `python-manual` | agent | `preserve` / `manual` | Python risks with `fix: advisory`; target compile findings | no |
| `behaviour` | `behaviour-change` | person | `preserve` (proposed) or `intended` | DEFAULT at a defaulted parameter, warning-only high risks, Python BEHAVIOR facts | no |
| `behaviour` | `review` | person | `preserve` | rule advisories `precondition` | no |

One step groups the sites of one change:
- an automated step is one recipe, or one move group (`MoveNamesCodemod` applies the patterns that
  share an old and a new module together: `codemods/apply.py:774-811`), across every file it
  changes;
- a manual step is one (package, symbol, delta fact) across its risk sites.

### 1.2 Step fields

| Field | Content |
|---|---|
| `id` | `<phase>-<n>`: `env-1`, `mech-3`, `sig-2`, `beh-1`. Stable within a plan; this is what the CLI takes |
| `key` | sha1(type, recipe key or package+symbol+fact, sorted anchor keys)[:10]. Identity across plans: a re-plan drops a step whose key is in effect from an earlier run (1.6) |
| `phase`, `type`, `title` | 1.1. `title` is one line, e.g. "`Series.append` → `pd.concat`" |
| `executor` | `pymolt` \| `agent` \| `person`: who is expected to do it. `--record` accepts a different actor and records it (3.3) |
| `claim` | `preserve` (the outcome at every site must stay the same) \| `intended` (the outcome is meant to change; it is compared with `verify.expect`) |
| `basis` | why pymolt trusts the claim: `same-object`, `same-signature`, `verified-rule`, `python-fixer` (today's claims), `target-manifest`, `manual` |
| `scope`, `package`, `from`, `to` | `dependency` \| `python` \| `env`, with the migration it belongs to |
| `symbol` | `{old, new}` dependency symbols. `new` is the pairing anchor verify uses (5.1) |
| `depends_on` | step ids, hard dependencies only (1.5.3) |
| `status` | always `planned` in the file. The live status is projected (1.3) |
| `where` | anchors (1.4), one per site |
| `why` | `{evidence[], tier, risk_ids[], baseline: {calls, sites, warnings[]}, delta: {kind, param, old, new}}`. `calls` is the baseline witness count summed over the step's sites |
| `do` | `{recipe}` for pymolt steps: the serialized `CodemodPattern`/`CodemodRule` with `sites: "anchors"`. `{manual}` otherwise: `{summary, successor, signature: {old, new, diff[]}, before_after: {before, after}, pitfalls[], consumers[]}` |
| `edits` | pymolt steps only: per file `{file, before_sha256, after_sha256, hunks[], effect}` (1.5.2) |
| `verify` | `{judge, expect, sites[], tests, command, uncovered}` (1.5.5) |
| `effort` | `{size: none\|trivial\|small\|medium\|large, sites, files, why}` (1.5.5) |
| `checks_at_plan` | Python fixes only: the plan-time recompile results (`pyversion/fixes.py:363-389`) |

### 1.3 Lifecycle

The plan file is immutable once saved. A step's status is projected by one function,
`pymolt/workorder/state.py::project_steps(project_dir, plan)`. `status`, the views, `apply` and
`verify --step` all call it. It reads the receipt set, the ledger, the saved verification and the
current files.

| Status | Meaning | Set by | Stored in |
|---|---|---|---|
| `planned` | in the plan; nothing applied | `plan` | plan file |
| `blocked` | a `depends_on` step is not in effect | projected | none |
| `stale` | an anchor no longer resolves, the recipe no longer reproduces the reviewed effect, or a dependency became stale | projected (an anchor-text check in `status`, the full check in `apply`) | none |
| `in_progress` | `--record` started: the snapshot was taken | `apply --record` | receipt step |
| `applied` | pymolt wrote it | `apply` | receipt step + ledger |
| `recorded` | an agent's or person's edit was recorded | `apply --record --done` | receipt step + ledger |
| `skipped` | a person declined it, with a reason | `apply --step ID --skip` | receipt step + ledger event |
| `rolled_back` | undone | `rollback` | receipt step + ledger event |
| `verified` | in effect, in the post capture, every site compared and judged per claim, nothing failing or for review | `verify` | `verification.json` `steps` |
| `failed` | a site's judgement failed | `verify` | same |
| `needs_review` | a review item at its sites (decision 7) | `verify` | same |
| `needs_evidence` | a site not exercised (BLIND) or uncomparable (decision 7) | `verify` | same |
| `not_in_capture` | applied after the post-migration capture, or the post snapshot lacks its effect | projected | none |
| `accepted` | an `intended` step whose observed outcome a person accepted | `verify --accept` | ledger `accept` event |

Precedence: run-level stale (setup fingerprint or target manifest changed, baseline replaced) >
`in_progress` > `rolled_back`/`skipped` > a verify judgement newer than the step's last event and
covering it > `applied`/`recorded` > `stale`/`blocked` > `planned`.

### 1.4 Anchors and risk ids

```json
{"id": "a1", "file": "toyapp/core.py", "scope": "combine", "func": "combine",
 "symbol": "pandas.core.series.Series.append",
 "stmt": "return s1 . append ( s2 , ignore_index = True )",
 "site_text": "return s1.append(s2, ignore_index=True)", "nth": 0, "line": 13}
```

| Field | Rule |
|---|---|
| `scope` | dotted path of the enclosing `class`/`def` names, `<module>` at top level. Used to resolve the anchor |
| `func` | innermost `def` name, the class name in a class body, `<module>`: what a capture records (`co_name`, `verify/_backend.py:1465`). Used to join with facts. A call inside a comprehension is `<listcomp>`, `<genexpr>`, ... under 3.6–3.11 and the enclosing def under 3.12 (PEP 709); joins try both |
| `stmt` | the smallest enclosing statement (a simple statement line, or a compound statement's header up to its `:`), as its tokens joined by one space, with comments, NL and INDENT/DEDENT dropped (`tokenize`). Reformatting (black, line breaks) leaves it unchanged |
| `site_text` | the stripped source line holding the symbol: the same string as `Fact.text`, for joining with captures |
| `nth` | ordinal among the statements in `scope` with the same `stmt`, in source order |
| `line` | where it was at plan time. A hint only, never an identity |

Resolution, in `pymolt/anchors.py` (host-side, LibCST positions plus `tokenize`): parse the current
file and list the statements of `scope` whose `stmt` is equal. The `nth` one is the match, and its
line is the resolved line. No match, or fewer than `nth + 1`, means the anchor is **lost**. pymolt
never guesses a nearest match.

An inserted import has an anchor with `scope: "<module>"` and `stmt` set to the import. Its
position is free: equality is by content (1.5.2).

**Risk ids** (`verify/predict.py:433-435`) become
`sha1("v2|" + symbol | file | func | site_text | nth)[:12]`. A static site with no line uses
`site_text: ""` and `nth: 0`. `RiskSite` gains `nth`. Calibration already matches risks to
entries by file, func and text (`predict.py:1193-1203`); the id only names the risk and links it
to its step. A v1 plan keeps its ids.

### 1.5 Building the steps

#### 1.5.1 Replay

`build_migration_plan` (`codemods/service.py:613-685`) keeps its inputs. The preview walk gains an
observer, `on_step(recipe, file, before, after)`, called by `apply_rules` (`codemods/apply.py:1126`),
`apply_patterns` (`:774`) and `apply_python_fixes` (`pyversion/fixes.py:286`) each time a recipe or
move group changes a file's text. It changes nothing those functions return, so one code path
produces both views. Per file this gives the chain T0 → T1 → … → Tn, in today's order: rules in
bundle order, then patterns and move groups, then Python fixes. Python fixes run on the dependency
result as today, with their sites resolved through anchors (B0-1). The minimal-diff and recompile
gates stay per file over all of that file's Python fixes: a file they drop contributes to no Python
step.

Test oracle: for every file, Tn equals v1's `new_source`. The only differences allowed are those B0-1
causes, and each is listed.

#### 1.5.2 Edits, hunks and effects

For each step and file, `edits[]` holds:
- `before_sha256` = sha(T(k-1)), `after_sha256` = sha(Tk);
- `hunks`: difflib opcodes between T(k-1) and Tk, as `{old_start, old: [lines], new_start,
  new: [lines], anchors: [ids]}` (1-based, as the ledger);
- `effect`: the same change without positions: `{"replace": [[old_stmt, new_stmt], ...],
  "insert": [stmt, ...], "delete": [stmt, ...]}`, over normalized `stmt`s. Imports compare as a
  set.

No intermediate full text is stored. Tk is rebuilt from T0 (`changes[].old_source`) by applying the
hunks in order. `changes[]` is kept, with `new_source` = T after every pymolt step (the v1 view, for
the readers in 1.8).

The **reviewed hunk** of a step is its `effect`. `apply --step` writes when a fresh run of the
recipe on the current file produces the same effect at the step's anchors (3.2).

#### 1.5.3 `depends_on`

For each file, consider each pair of steps i < j that change it:
1. **Overlap.** Step j rewrites a statement that step i produced: `effect_i.replace[*][1]` or
   `effect_i.insert` meets `effect_j.replace[*][0]`. That makes a hard dependency.
2. **Re-run test.** Otherwise, j's recipe is run on T0 with every step before j except i applied. If
   the result's effect differs from `effect_j`, that is a hard dependency (j needs a binding or an
   import that i adds).
3. **Cap.** A file with more than 8 steps gets sequential dependencies (each step depends on every
   earlier step in that file) instead of the O(k²) test.

`depends_on` is the union over files. Manual steps get no code dependencies: verify judges them on
the final code. `env-2` (`target-env`) is a dependency of nothing. It gates only D's smoke import,
which reports "not checked: no target environment" without it.

#### 1.5.4 Risks to steps

Every `PredictedRisk` gets a `step_id`:
- **fixed by an automated step.** The step's `symbol.old` (or a recipe spelling) equals the risk's
  symbol, and the step's effect changes the risk's anchor statement or the import statement that
  binds that symbol in the file. That second case is H4's import move, now stated by anchor, not by
  line. `fixed_by_plan` keeps its plan-time meaning: a pymolt step addresses the risk;
- **left to a manual step.** One is created per (scope, package, symbol, fact kind + param), using
  the table in 1.1. Its tier is the maximum over its sites, and `why.risk_ids` lists them.

An H5 item and each rule advisory become one `manual-rewrite` or `review` step per (rule, file). Low
BLIND risks are steps too; PLAN.md collapses them (1.8).

#### 1.5.5 How to verify, and effort

`verify` per step:
- `judge`: `strict` (`preserve`), `expectation` (`intended`), `none` (env steps);
- `expect`: null, or 5.2's form;
- `sites[]`: per anchor:
  - `baseline_calls`: the sum of `Fact.count` over baseline facts with the same symbol and
    (file, func, site_text);
  - `coverage`: `recorded | ran-not-recorded | not-run | unobservable | unknown`, from the
    baseline capture through the same classification verify uses (`verify/report.py:966`,
    `_judge_contacts`, with `_load_executed` at `:531`);
- `tests`: `null`, with the note "the baseline capture does not record which tests ran a site" (stage
  7 fills it in);
- `command`: the baseline slot's command;
- `uncovered`: the count of sites that are `not-run` or `unobservable`. These sites end
  `needs_evidence` unless a test reaches them.

`effort.size`:

| size | rule |
|---|---|
| `none` | executor pymolt |
| `trivial` | 1 site, successor known, same signature |
| `small` | up to 5 sites, successor known |
| `medium` | signature differs, or 6–20 sites, or a `review` step |
| `large` | `behaviour-change`, no successor, or more than 20 sites |

`why` says which rule applied.

`do.manual`, filled best effort from the delta facts and the baseline:
- `successor` and `signature`: `DeltaFact.successor`, `.old`/`.new`/`.param`, and the probe's
  signatures in `detail` when it recorded them; otherwise "signature not known";
- `pitfalls`: the site's baseline warnings, parameters it left at their defaults (`Fact.defaulted`),
  exceptions it raised, and a call-time default ("old default None is decided at call time:
  passing it may not restore the old behaviour");
- `consumers`: up to 3 later calls in the same function whose receiver or input has the type this
  site returned in the baseline.

### 1.6 Stale steps and re-basing

| Trigger | Effect |
|---|---|
| Setup fingerprint or target-manifest hash changed (H3) | every unapplied step is stale; the next command is `pymolt plan .` |
| Baseline replaced, or no longer valid | apply refuses (exit 4), as today |
| A step's anchor is lost | that step is stale; later steps that do not depend on it stay valid |
| The anchor resolves but the recipe run produces another effect | that step is stale |
| A `depends_on` step is stale, rolled back or skipped | that step is blocked (stale if the dependency is stale) |
| Lines shifted (an earlier step skipped or rolled back, a hand edit elsewhere, reformatting) | not stale: the step **re-bases**. Its anchors resolve to new lines and the write uses the fresh recipe output |

Re-basing replaces whole-plan invalidation, the point of B: one stale step costs one step, not a
5-minute re-plan (theano 4.6 min, zipline 5.4 min). After a re-plan:
- the new plan's T0 is the disk as it is;
- a step whose `key` is in effect from an earlier run is left out, and its risks link to that run's
  step (`step_id: "<run8>:<id>"`);
- the earlier run is closed: it applies no more steps, and its steps in effect stay in effect.

### 1.7 JSON schema

The pydantic models in `pymolt/workorder/models.py` are the source. `MigrationPlan.model_json_schema()`
is committed as `pymolt/schemas/migration_plan-2.schema.json`, and a test asserts that the committed
file equals the generated one. The part v2 adds, which the generated file must match:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "pymolt.plan/2",
  "type": "object",
  "required": ["schema_version", "plan_id", "steps"],
  "properties": {
    "schema_version": {"const": 2},
    "plan_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
    "setup_fingerprint": {"type": ["string", "null"]},
    "steps": {"type": "array", "items": {"$ref": "#/$defs/step"}},
    "changes": {"description": "derived: every pymolt step applied in order (the v1 view)"}
  },
  "$defs": {
    "anchor": {
      "type": "object",
      "required": ["id", "file", "scope", "func", "stmt", "nth"],
      "properties": {
        "id": {"type": "string"}, "file": {"type": "string"},
        "scope": {"type": "string"}, "func": {"type": "string"},
        "symbol": {"type": ["string", "null"]}, "stmt": {"type": "string"},
        "site_text": {"type": ["string", "null"]},
        "nth": {"type": "integer", "minimum": 0},
        "line": {"type": ["integer", "null"], "description": "plan-time hint"}
      }
    },
    "hunk": {
      "type": "object", "required": ["old_start", "old", "new_start", "new"],
      "properties": {
        "old_start": {"type": "integer"}, "old": {"type": "array", "items": {"type": "string"}},
        "new_start": {"type": "integer"}, "new": {"type": "array", "items": {"type": "string"}},
        "anchors": {"type": "array", "items": {"type": "string"}}
      }
    },
    "effect": {
      "type": "object",
      "properties": {
        "replace": {"type": "array", "items": {"type": "array", "minItems": 2, "maxItems": 2,
                                              "items": {"type": "string"}}},
        "insert": {"type": "array", "items": {"type": "string"}},
        "delete": {"type": "array", "items": {"type": "string"}}
      }
    },
    "edit": {
      "type": "object", "required": ["file", "before_sha256", "after_sha256", "hunks", "effect"],
      "properties": {
        "file": {"type": "string"},
        "before_sha256": {"type": "string"}, "after_sha256": {"type": "string"},
        "hunks": {"type": "array", "items": {"$ref": "#/$defs/hunk"}},
        "effect": {"$ref": "#/$defs/effect"}
      }
    },
    "expect": {
      "oneOf": [
        {"type": "null"},
        {"type": "object", "required": ["outcome"], "properties": {"outcome": {"const": "any"}}},
        {"type": "object", "required": ["kind"], "properties": {
          "kind": {"enum": ["return", "raise"]}, "type": {"type": "string"},
          "fingerprint": {"type": "string"}}},
        {"type": "object", "required": ["representation"],
         "properties": {"representation": {"const": true}}}
      ]
    },
    "step": {
      "type": "object",
      "required": ["id", "key", "phase", "type", "title", "executor", "claim", "basis",
                   "depends_on", "status", "where", "why", "do", "verify", "effort"],
      "properties": {
        "id": {"type": "string", "pattern": "^(env|mech|sig|beh)-[0-9]+$"},
        "key": {"type": "string", "pattern": "^[0-9a-f]{10}$"},
        "phase": {"enum": ["env", "mechanical", "signature", "behaviour"]},
        "type": {"enum": ["manifest-pin", "target-env", "codemod", "python-fix", "manual-rewrite",
                          "api-change", "python-manual", "behaviour-change", "review"]},
        "title": {"type": "string"},
        "executor": {"enum": ["pymolt", "agent", "person"]},
        "claim": {"enum": ["preserve", "intended", null]},
        "basis": {"enum": ["same-object", "same-signature", "verified-rule", "python-fixer",
                           "target-manifest", "manual", null]},
        "scope": {"enum": ["dependency", "python", "env"]},
        "package": {"type": ["string", "null"]},
        "from": {"type": ["string", "null"]}, "to": {"type": ["string", "null"]},
        "symbol": {"type": "object", "properties": {"old": {"type": ["string", "null"]},
                                                    "new": {"type": ["string", "null"]}}},
        "depends_on": {"type": "array", "items": {"type": "string"}},
        "status": {"const": "planned"},
        "where": {"type": "array", "items": {"$ref": "#/$defs/anchor"}},
        "why": {"type": "object", "properties": {
          "evidence": {"type": "array", "items": {"type": "string"}},
          "tier": {"enum": ["certain", "high", "medium", "low", null]},
          "risk_ids": {"type": "array", "items": {"type": "string"}},
          "baseline": {"type": "object", "properties": {
            "calls": {"type": "integer"}, "sites": {"type": "integer"},
            "warnings": {"type": "array", "items": {"type": "string"}}}},
          "delta": {"type": ["object", "null"]}}},
        "do": {"type": "object", "oneOf": [
          {"required": ["recipe"]},
          {"required": ["manual"], "properties": {"manual": {"type": "object", "properties": {
            "summary": {"type": "string"}, "successor": {"type": ["string", "null"]},
            "signature": {"type": ["object", "null"]},
            "before_after": {"type": ["object", "null"]},
            "pitfalls": {"type": "array", "items": {"type": "string"}},
            "consumers": {"type": "array", "items": {"type": "string"}}}}}}]},
        "edits": {"type": "array", "items": {"$ref": "#/$defs/edit"}},
        "verify": {"type": "object", "properties": {
          "judge": {"enum": ["strict", "expectation", "none"]},
          "expect": {"$ref": "#/$defs/expect"},
          "sites": {"type": "array", "items": {"type": "object", "properties": {
            "anchor": {"type": "string"}, "baseline_calls": {"type": "integer"},
            "coverage": {"enum": ["recorded", "ran-not-recorded", "not-run", "unobservable",
                                  "unknown"]}}}},
          "tests": {"type": ["array", "null"], "items": {"type": "string"}},
          "command": {"type": ["array", "null"], "items": {"type": "string"}},
          "uncovered": {"type": "integer"}}},
        "effort": {"type": "object", "properties": {
          "size": {"enum": ["none", "trivial", "small", "medium", "large"]},
          "sites": {"type": "integer"}, "files": {"type": "integer"}, "why": {"type": "string"}}},
        "checks_at_plan": {"type": ["object", "null"]}
      }
    }
  }
}
```

Every v1 field stays, with its meaning. The new `setup_fingerprint` is
`environment_setup_fingerprint` at plan time (`verify/service.py:2205`), H3's input.

### 1.8 Readers of v1 fields, and the v1 adapter

| Reader | Reads | Under v2 |
|---|---|---|
| `views.plan_view` diffs (`interfaces/views.py:746-750`) | `changes` | unchanged (the full automated diff); adds the steps |
| `predict.planned_rewrites` at plan time (`verify/predict.py:197-214`) | `changes` | replaced by step links for v2 (1.5.4) |
| `_predictions_to_calibrate` (`verify/service.py:2900-2935`) | `changes` (fixed by plan) | step state (5.5) |
| `_edited_stdlib_modules` (`verify/service.py:414-440`) | `changes[].old_source` | unchanged |
| `bench/e2e_pandas/run.py:150-166`, `bench/generalization/run.py:351`, `:505-536` | `changes`, `risks` | unchanged |

`MigrationPlan.load` reads a schema-1 file into the v2 model with one synthetic step:
- `v1`: `type: codemod`, `executor: pymolt`, `claim: preserve`, basis taken from v1 `claims`;
- its `edits` are one whole-file hunk per change.

`apply --step v1` and `apply .` act on it as today. Splitting a v1 plan needs a re-plan, because its
recipe boundaries were never recorded.

### 1.9 PLAN.md (for a person)

`plan` writes `.pymolt/PLAN.md` (`--plan-md PATH` to write it elsewhere) and prints its path. It is
rendered from the plan alone by `pymolt/workorder/render.py`. Layout, shown for the e2e pandas
`change` scenario:

```markdown
# Migration plan 3f2a9c1e
pandas 1.5.3 → 2.2.3 · Python 3.11 → 3.12 · baseline 2026-11-17 09:12, 412 calls
4 steps: 1 by pymolt, 3 for a person. Next: `pymolt apply .`

| Step | What | Who | Claim | Sites (run / not run) | Effort | Depends on |
|---|---|---|---|---|---|---|
| env-1 | Pin the target versions in requirements.txt | pymolt | intended | none | none | none |
| env-2 | Build the target environment `.venv-312` | person | none | none | small | none |
| mech-1 | `Series.append` → `pd.concat` | pymolt | preserve | 1 (1 / 0) | none | none |
| beh-1 | `DataFrame.mean`: default `numeric_only` None → False | person | preserve | 1 (1 / 0) | large | none |

## mech-1 · `Series.append` → `pd.concat` · pymolt · preserve
- **Where:** toyapp/core.py, `combine()`: `return s1.append(s2, ignore_index=True)` (line 13)
- **Why:** removed in pandas 2.0 (certain). The baseline ran it 1 time, with a FutureWarning.
- **Do:** knowledge rule `pandas-append-to-concat`, verified against its golden pair. <diff>
- **Verify:** strict. Any change of outcome at this site fails. The baseline exercised it.

## beh-1 · `DataFrame.mean` default change · person · preserve
- **Where:** toyapp/core.py, `average()`: `return df.mean().to_dict()` (line 24)
- **Why:** the default of `numeric_only` changes None → False (high: the site warned in the baseline).
- **Do:** pass `numeric_only` explicitly. Pitfall: old default None is decided at call time...
- **Verify:** strict. The baseline ran this site 1 time; no test asserts its value.
- **Record:** `pymolt apply --step beh-1 --record`, edit, then `pymolt apply --step beh-1 --record --done`
```

Low-tier BLIND steps collapse into one table at the end, with a count. The rendered line numbers are
the plan-time hints, labelled as such.

### 1.10 Per-step agent briefs

`pymolt apply --step ID --brief` prints the brief (markdown; `--agent` prints JSON) and writes
nothing. It is rendered from the plan and the current files, so its lines are the resolved ones,
never stale hints. `plan --agent` and `status --agent` include the brief of the next actionable step.

| Brief field | Content |
|---|---|
| `goal` | title + `why` in one sentence |
| `where` | anchors with the resolved line, `scope`, `site_text` |
| `do` | `do.manual` (successor, signature diff, before → after, pitfalls, consumers) |
| `constraints` | `preserve`: "keep the outcome of every call at these sites; pymolt compares each one with the baseline and fails on any difference". `intended`: "state the expected new outcome with `--expect`, or a person accepts it after verify". In both: edit only these statements unless the brief names more; any other change is recorded and named |
| `verify` | `verify.sites` with coverage; `uncovered` sites and what they need |
| `commands` | begin: `pymolt apply --step ID --record --agent`; finish: `... --record --done --agent`; abort: `... --record --abort --agent` |
| `expected` | the compile check runs on the files you edit; then `pymolt capture post-migration` and `pymolt verify . --step ID` |

## 2. Ledger v2 and receipts

### 2.1 Ledger v2

`.pymolt/apply_ledger.json`, `"schema": "pymolt.apply_ledger/2"`. It is append-only: the next apply
no longer archives it (`apply_ledger.py:100-111`). It is written under the migration lock.

```json
{"schema": "pymolt.apply_ledger/2", "entries": [
  {"seq": 1, "event": "apply", "run_id": "3f2a…", "step_id": "mech-1", "at": "…",
   "executor": "pymolt", "actor": "pymolt",
   "file": "toyapp/core.py", "before_sha256": "…", "after_sha256": "…",
   "old_lines": [13, 13], "new_lines": [13, 13],
   "old_text": ["    return s1.append(s2, ignore_index=True)"],
   "new_text": ["    return pd.concat([s1, s2], ignore_index=True)"],
   "anchor": "a1", "kind": "rule", "rule": "template:$S.append(…)→pd.concat(…)",
   "claim": "preserve", "basis": "verified-rule",
   "old_symbol": "pandas.core.series.Series.append", "new_symbol": "pandas.core.reshape.concat.concat",
   "expect": null},
  {"seq": 2, "event": "record", "run_id": "3f2a…", "step_id": "beh-1", "executor": "person",
   "actor": "person", "file": "toyapp/core.py", "claim": "preserve", "basis": "manual", "…": "…"},
  {"seq": 3, "event": "rollback", "run_id": "3f2a…", "step_id": "mech-1", "at": "…",
   "files": ["toyapp/core.py"]},
  {"seq": 4, "event": "accept", "run_id": "3f2a…", "step_id": "x-9c1e44a0b2", "finding": "…",
   "symbol": "…", "anchor": {"…": "…"}, "fingerprint": "…", "by": "person", "at": "…"}
]}
```

| Field | Rule |
|---|---|
| `event` | `apply` \| `record` \| `rollback` \| `skip` \| `accept` |
| ranges | 1-based and inclusive in the entry's before/after text, as v1. `null` for a pure insertion or deletion |
| `claim`, `basis` | from the step. `--record --claim` can override `claim` (3.3) |
| texts | each `before_sha256`/`after_sha256` text is stored once, content-addressed, at `.pymolt/texts/<sha256>.gz`. The remap reads them from there |
| attribution | by step, not by leaf substring. `_recipe_for` (`apply_ledger.py:58-73`) is used only for v1 entries |

### 2.2 In effect

| Scope | `in_effect` when |
|---|---|
| step | its last event is `apply` or `record`; or it is `rollback` with `files: []`; and its run is in the receipt chain |
| entry | its step is in effect |
| run | at least one of its steps is in effect. This keeps the v1 property `MigrationReceipt.in_effect` and its readers (`verify/service.py:320-321`, `:429-431`, `:2711`, `:2861`, `:2921`, `:3330`) |

### 2.3 Remap (verify side)

`build_diff_context` (`verify/service.py:2826-2884`) builds `ctx.edits` from the entries in effect,
remapped to the two snapshots that verify compares: B (baseline `.sources.json.gz`) and P
(post-migration). For each entry:
1. **New side.** `_line_map(after_text, P)` (`verify/facts.py:3165-3184`, the alignment that already
   decides provenance) maps `new_lines`. A line that is not `same` drops out of the range: a later
   step, or a later hand edit, owns it.
2. **Old side.** `_line_map(before_text, B)` maps `old_lines`. A line an earlier step produced does
   not exist in B and drops out; the earlier step's entry covers B there.
3. An entry left with no line on either side is kept, without ranges, as a pairing anchor
   (`old_symbol → new_symbol`) only.

`_Provenance.ledger_covers` (`verify/facts.py:3062-3074`) keeps its 1-based contract and returns the
covering entry instead of `True` (5.1). Verify never reads a ledger entry against the line numbers it
had when written. This replaces H8's per-run scoping and generalises it: several runs, several steps,
and hand edits after apply. A step applied after the post-migration capture finds its after-text
missing from P. Its ranges drop out, its sites read as unedited between B and P, and the step is
`not_in_capture` (1.3).

### 2.4 Receipt set

`.pymolt/migration_state.json`, `schema_version: 2`. The v1 fields stay, as a summary of the latest
run, so every v1 reader keeps working.

```json
{"schema_version": 2, "run_id": "3f2a…", "status": "partial",
 "applied_at": "…", "updated_at": "…", "target_python": "3.12", "source_manifest": "requirements.txt",
 "baseline_trace_path": "…", "baseline_captured_at": "…", "environment_fingerprint": "…",
 "allow_no_baseline": false, "files_changed": ["…"], "patterns_applied": 1, "plan_path": "…",
 "runs": [{"run_id": "3f2a…", "plan_path": "…", "opened_at": "…", "closed": false,
           "starting_fingerprint": "…", "setup_fingerprint": "…", "baseline_kind": "full"}],
 "steps": {"3f2a…:mech-1": {"status": "applied", "executor": "pymolt", "actor": "pymolt",
            "claim": "preserve", "applied_at": "…", "rolled_back_at": null,
            "files": ["toyapp/core.py"], "files_restored": null,
            "checks": {"compile_target": "ok", "compile_base": "not checked: no base claim",
                       "smoke_import": "not checked: no target environment"},
            "ledger_seq": [1]}}}
```

| Field | Rule |
|---|---|
| `status` (run summary) | `applied` (at least one step in effect, and every pymolt step in effect or skipped), `partial` (a step in effect, a pymolt step not), or `rolled_back` (none in effect). v1 readers that test `== "applied"` (`status.py:227`, `:337`, `:515`) switch to `in_effect` in stage 3 |
| `files_changed`, `patterns_applied` | the union and the sum over the steps in effect |
| `runs` | the chain of runs whose code is on disk. A re-plan closes the open run and appends the new one |
| `starting_fingerprint` | stamped once, at the run's first invocation, before any write. When `baseline_setup_unchanged` holds (`verify/service.py:3316`) it is the baseline's own fingerprint, so a manifest bumped by hand before the first apply no longer makes the baseline stale |

### 2.5 From v1 state

| v1 file | Read as | Rewritten as v2 |
|---|---|---|
| plan (`schema_version: 1`) | one synthetic step `v1` (1.8) | never; a re-plan writes v2 |
| `migrations/<run>/plan.json`, `state.json` | the run, and the `v1` step with `state.status` | `state.json` gains `steps` on the next mutation of that run |
| `migration_state.json` (`schema_version: 1`) | `runs: [that run]`, `steps: {"<run>:v1": {status: receipt.status, files_restored}}` | on the next apply, rollback or record, after a copy to `.pymolt/archive/` |
| `apply_ledger.json` (`pymolt.apply_ledger/1`) | `event: apply`, `step_id: v1`, `run_id` from the top level (v1 already writes it: `apply_ledger.py:115`); `claim: preserve` with `basis` = the v1 claim, or `claim: null` (a hand edit) when it was null; texts = the frozen plan's `old_source`/`new_source` | the same, on the first v2 write |
| archived v1 ledgers (`.pymolt/archive/apply_ledger-*.json`) | ignored, as today | never |

## 3. The apply CLI

### 3.1 Options

| Option | Meaning |
|---|---|
| (none) | every pymolt step whose dependencies are in effect, in order, as one transaction. On a v1 plan: today's apply |
| `--step ID` | that step only. It refuses when a dependency is not in effect |
| `--through ID` | every pymolt step up to and including ID that is not in effect, with their dependencies, in one transaction. It refuses when an agent or person step on the way is a dependency and is not in effect |
| `--dry-run` | resolve, re-base and check, then print the diff that would be written. Writes nothing |
| `--record` | agent and person steps (and `manifest-pin` under decision 1 = no): start recording, take the snapshot |
| `--record --done` | finish: diff against the snapshot and record the edit |
| `--record --abort` | drop the snapshot; the step goes back to `planned`. Files are untouched |
| `--claim preserve\|intended`, `--expect SPEC`, `--pair OLD=NEW`, `--by agent\|person`, `--actor NAME` | with `--record --done`: override the claim, state the expectation (5.2), add a pairing anchor, say who did it |
| `--skip --reason TEXT` | mark a step `skipped`. A dependent step then stays blocked |
| `--brief` | print the step's brief (1.10) |
| `--plan`, `--allow-no-baseline`, `--json` | as today |
| `--agent`, `--approve-agent` | decision 4 (3.6) |

### 3.2 `apply`, `--step` and `--through`

Under `_migration_lock`:
1. Load the plan. If the run's frozen copy exists (`migrations/<run>/plan.json`), it must be the same
   plan (`plan_id` and content); a different `plan_id` opens a new run and closes the old one.
2. **Run preconditions:**
   - recover an interrupted invocation (2.4 `state.json` holds the invocation's steps and per-file
     before/after hashes; extends `migration_plan.py:540-566`);
   - H3: setup fingerprint + target-manifest hash;
   - the baseline binding (`:606-642`), with the setup-fingerprint rule;
   - starting fingerprint (2.4).
3. **Selection:** the selected steps, plus their dependency closure (`--through`). Steps already in
   effect are no-ops (3.4). An agent or person step in the closure that is not in effect → refuse.
4. **Per selected step, in order, on the text produced so far:** for each of the step's files, take
   the current text C, and stop at the first rule that matches:
   1. `sha(C) == before_sha256` → the frozen after-text (fast path);
   2. `sha(C) == after_sha256` → no-op for that file;
   3. the anchors resolve on C → run the recipe with its sites restricted to them → C′. If
      `effect(C, C′)` equals the step's effect, write C′ (re-based); otherwise refuse, step stale;
   4. the anchors are lost, but every new statement of the effect is present in the anchors'
      scopes (the change is already there, made by hand or by an earlier run) → no-op for that file;
   5. otherwise → refuse, step stale (the lost anchors are named).
5. **D before writing:** compile every changed file on the target, and on the base where the step
   claims `runs_on_base`. On a failure, nothing is written (D's exit code).
6. **Write** every file of every selected step in one transaction (`write_previews`,
   `codemods/apply.py:1084`).
7. **Ledger and receipts:** one `apply` entry per hunk per step, the texts in the store, and a
   receipt per step with `checks`. A failure here reverses the writes (`migration_plan.py:693-725`).
8. **D after writing:** the smoke import. On a failure, the whole invocation is rolled back,
   `rollback` events are appended, and the step receipts record the failure.

### 3.3 `--record` for agent and person steps

| Phase | What happens |
|---|---|
| `--record` | Refuses unless the step is `planned`/`rolled_back`/`stale` (a re-record) and its dependencies are in effect. Snapshot: the full text of the step's files and of every project `.py` file (the `_iter_py_files` skip rules, `codemods/apply.py:835-851`), gzipped, to `migrations/<run>/steps/<id>/before.json.gz`. Above 50 MB, the other files keep hashes only. The receipt step becomes `in_progress` |
| `--record --done` | Diff the snapshot against now. Each changed file gets entries with `event: record`, the step's anchors (or anchors made on the new text for hunks outside them), the claim, `basis: manual`, the actor and `symbol` as the pairing anchor (`--pair` adds more). A file with only a hash snapshot is recorded with "before text unknown", as a hand edit. The compile check runs on the changed files: target, and base when the step says so. A failure is recorded in `checks`, the edit stays (pymolt did not write it), and the command exits with D's failure code. No change → refuse, unless the step is `review` or `env`, or `--claim intended --expect …` accepts behaviour with no code change |
| without `--record` first | `--record --done` with no snapshot: the before text is each file's last known state (its latest in-effect after-text, else the plan's T0). Only hunks that overlap the step's anchor scopes are attributed to it; the others are listed, left unrecorded, and judged as hand edits |
| `--record --abort` | drop the snapshot; back to the previous state |

A `manifest-pin` step recorded by a person (decision 1 = no) records the manifest's lines with
`claim: intended`. `target-env` needs no record: status projects it from the environment check.

### 3.4 Idempotent re-runs

| Invocation | State | Result |
|---|---|---|
| `apply .` | every pymolt step in effect | exit 0, "nothing to apply" (H1) |
| `apply --step ID` | ID in effect, its effect on disk | exit 0, no write, no ledger entry |
| `apply --step ID` | ID `rolled_back`, anchors and recipe check pass | re-applies, with no re-plan (H2, per step) |
| `--record --done` | already `recorded`, no change since | exit 0, no entry |
| `--record --done` | already `recorded`, changes since | new entries for the same step, `seq` order (a re-record) |

### 3.5 Refusals and exit codes

| Code | When |
|---|---|
| 0 | applied, recorded, skipped, or already in effect |
| 1 | D: a compile check failed (nothing written), or the smoke import failed (rolled back). D's choice of code applies if it differs |
| 2 | refused: unknown step; a dependency not in effect; a stale step (each lost anchor or differing effect named); `--record` in a wrong state; a run conflict; a plan bound to another run; another migration operation active; an invalid combination of options |
| 3 | an environment or IO failure; nothing written |
| 4 | evidence: no valid baseline bound to the plan (unless `--allow-no-baseline`) |

The summary lists the steps applied (with re-based ones marked), each step's checks, and the steps
that remain, by status, review steps and advisories included (H6), with the next command (6).

### 3.6 Agent view and MCP (decision 4)

| | Variant A (default: agents run pymolt steps with one approval per plan) | Variant B (agents only record their own edits) |
|---|---|---|
| Approval | `pymolt apply . --approve-agent`, run by a person (refused under `--agent`/`--json`), writes `migrations/<run>/approval.json` = `{plan_id, plan_sha256, scope: ["pymolt-steps", "rollback"], approved_at}`. A re-plan or any change to the plan file voids it | none |
| `apply --agent` | pymolt steps only with a matching approval (otherwise exit 2, naming the command). `--record`, `--done`, `--abort`, `--brief`, `--dry-run` always | `--record`, `--done`, `--abort`, `--brief`, `--dry-run` only. A pymolt step under `--agent` → exit 2: "a person runs `pymolt apply .`" |
| `rollback --agent` | steps of an approved plan | not available |
| `verify --accept` | person only (refused under `--agent`) | same |
| View | `views.apply_view`: `{summary, data: {applied[], rebased[], noop[], refused: {step, reasons}, checks, remaining: {status: [ids]}, next: {command, step, brief}}}`, under the view layer's caps | the same view for record operations |
| MCP | `apply` and `rollback` tools, gated by `PYMOLT_MCP_ALLOW_WRITE=1` in the server environment and the approval. `apply(step, through, record, done, abort, claim, expect)` mirrors the CLI. The CLI↔MCP parity test covers them | no new tool. `plan` and `status` return the steps and the next brief |
| Docs | `docs/agents.md` "`apply` and `rollback` have no agent view" (`:29`) is rewritten | that sentence gains "except `--record`" |

## 4. Rollback per step

| Phase | Rule |
|---|---|
| 1 (stage 4): file-level | `rollback --step ID`: allowed when each of the step's files is at that step's after-text (`sha == after_sha256`, or the recorded after-text). The step must be the last change to each of them. Restores the before-text. Otherwise refuses (exit 2), naming the later steps or edits on those files |
| 2 (stage 6): inverse hunk | when a file has moved on: resolve the step's anchors on the current file; at each anchor the current statement must equal the step's new statement (the anchor check). Apply the inverse effect (new → old statements; remove an inserted import only if it is no longer used). Then LibCST parse plus D's target compile. Any mismatch → refuse; file-level refusal stays the safe fallback |
| Dependents | a step in effect that `depends_on` ID → refuse, naming it; `--cascade` rolls back the dependents first, in reverse order |
| Recorded steps | the same two phases, using the record's before/after texts |
| Whole run | `rollback .` with no `--step`: every step in effect in reverse order, as one transaction. `--force` keeps today's meaning: restore each file's run-start text and discard later edits |
| Ledger | a `rollback` event per step, with `files` = the files it rewrote. Entries are never deleted. An entry whose step is rolled back is out of effect (2.2) |
| Receipt | the step's `rolled_back_at` and `files_restored` |
| Later steps | not invalidated. They re-base on their next apply (1.6). A later step left in effect keeps its entries, remapped |
| Post-migration capture | a rollback that rewrote a file sets the capture aside (`set_aside_post_migration`, `verify/service.py:2145`; `migration_plan.py:836-840`). One that rewrote nothing keeps it. A capture taken with a step that is no longer on disk describes no code on disk |

## 5. Verify

### 5.1 Judging by claim (decision 3)

`facts._Provenance` (`verify/facts.py:2732-2760`, `:3062-3074`, `:2874-2880`) gets the covering
entry; `_judge_pairs` (`:6594-6640`) and `_single_entry` (`:6737-6753`) judge by its claim:

| Covering entry | Tier (`FactEntry.provenance`) | Equal outcome | Different value | New raise | Unpaired |
|---|---|---|---|---|---|
| none (edited line) | `hand` | REWRITTEN equal | review | FAIL | unverified |
| `claim: preserve`, any executor | `preserve` (today's `ledger`) | REWRITTEN equal | FAIL "a preserve claim did not hold" | FAIL | unverified |
| `claim: intended` + expectation met | `intended` | ACCEPTED | ACCEPTED | ACCEPTED if expected | unverified |
| `claim: intended` + expectation not met | `intended` | FAIL "the intended outcome did not occur" | FAIL | FAIL | unverified |
| `claim: intended`, `expect: any` or none, no accept | `intended` | REWRITTEN equal | review "intended change: accept or fix" | review | unverified |
| `claim: null` (v1 heuristic) | `hand` | as hand | | | |

- `_STRICT_CLAIMS` (`:2710`) is removed. The v1 claims normalize to `preserve` + `basis` at load
  time (2.5).
- `FactEntry` gains `step_id` and `accepted`. `fails` and `inconclusive` (`:491-507`) return False
  when `accepted` is set.
- `edited_sites()` (`:534-552`) gains `accepted`. The summary line is "N edited sites: a equal,
  b representation-only, c need review, d unverified, e accepted".
- `DiffContext.successors` takes every in-effect entry's `old_symbol → new_symbol`, recorded steps
  included. That anchor pairing is what lowers the unverified share on agent edits (Wave G: 120 of
  191 edited sites).

### 5.2 `intended`: the reviewed-change record (ACCEPTED)

Wave G left open "a reviewed-change record, keyed by symbol, site and an outcome fingerprint, that
turns a reviewed FAIL into ACCEPTED, printed, and expiring when the fingerprint changes". Here it is
always an `intended` step:

| Expectation (`--expect`) | Met when |
|---|---|
| `any` (default for `intended`) | never by itself: a review item until a person accepts it |
| `return:<type>` / `raise:<type>` | the new outcomes are all of that kind (and type) |
| `representation` | `_representation_only(old, new)` (`verify/facts.py`, used at `:6624`) |
| `fingerprint:<sha>` | sha256 of the canonical JSON of the new outcomes (masked values) equals it |

- `verify --accept FINDING_ID` (person only) appends an `accept` event: symbol, anchor, the new
  outcomes' fingerprint, `by`, `at`. A later verify matches on (symbol, anchor, fingerprint). A
  different fingerprint is no match, so the item returns as a review (expiry).
- Findings get ids: `sha1(class | mechanism | symbol | file | func | site_text)[:10]`, added to
  `fact_report._finding` (`verify/fact_report.py:268-282`).
- On an edited site the accept attaches to the covering `intended` step. On an unedited site
  (a library's behaviour change the person accepts, e.g. django `escape`'s `&#39;` → `&#x27;`) it
  creates an ad-hoc step `x-<finding id>` that exists in the ledger only, with `type: review`,
  `executor: person`, `claim: intended`. Every ACCEPTED is therefore an intended step in the ledger,
  never a silent pass.
- The verdict counts an ACCEPTED entry as compared equal. The report always prints "N accepted
  changes", each with who accepted it and when. `verify --strict` treats ACCEPTED as review.

### 5.3 `verify --step ID`

A filter over the one post-migration capture, never a new capture per step. Verify builds the whole
report as today (the verdict still covers the whole surface), then:

| Part | Selected by |
|---|---|
| entries | old or new site = (file, func, site_text) of a step anchor, resolved on B or P; or covered by one of the step's remapped ledger ranges |
| risks and calibration | `PredictedRisk.step_id == ID` |
| coverage | the step's anchor contacts, judged in the post capture (`verify/report.py:966`) |
| step judgement | `failed` > `needs_review` > `needs_evidence` > `verified`; or `not_in_capture` |

Exit codes: 0 `verified`, 1 `failed`, 4 `needs_review`/`needs_evidence`/`not_in_capture`, 2 unknown
step. Every `verify` run, with or without `--step`, saves every step's judgement in
`verification.json` under `steps: {id: {status, entries, failing, review, unexercised, risks}}`.
Status projects from that (1.3), so `--step` only prints one of them.

### 5.4 Step status from verify

The judgement applies only when all of these hold:
- the verification is newer than the step's last event;
- it compared the post-migration capture (`compared.after_capture == "post-migration"`, the
  existing rule at `status.py:507-516`);
- the post snapshot contains the step's effect.

Otherwise the step keeps its apply-side status, or is `not_in_capture`. Steps with no sites (env)
take no verify status: `env-1` stays `applied` and `env-2` is projected from the environment check.

### 5.5 Calibration by step

- `CalibratedRisk` and `Calibration` (`verify/predict.py:1140-1170`) gain `step_id` and
  `by_step: {id: {hit, prevented, miss, not_exercised}}`.
- **Fixed is decided at verify time.** A risk counts as fixed when its step is in effect: an applied
  pymolt step, or a recorded agent or person step. `fixed_by_plan` from plan time no longer decides
  `prevented` (`calibrate`, `:1239-1244`).
- So a risk whose step was skipped or rolled back is scored as unfixed (hit or miss). A manual step
  the person recorded can score `prevented`.
- An `intended` step's risk that matches its expectation is `accepted`, a fifth result.
- H4's import-move credit holds through the step link (1.5.4): a zipline-style import move fixes its
  certain risk when the step is in effect.

## 6. Status and the next command

`build_status` (`status.py:390-549`) adds a `steps` phase between `plan` and `migration`:
- `detail` holds counts by status, e.g. "6 steps: 3 applied, 1 recorded, 1 planned (person), 1
  stale";
- `FunnelStatus.steps` lists `{id, status, executor, title}`;
- `status --agent` includes them, capped, with the next step's brief.

`post-migration` is stale when it predates the latest in-effect step event, not just
`receipt.applied_at` (`:478-486`). With no receipt and no recorded step, a valid post capture is not
stale and the report can be done (H7).

`_decide_next` (`status.py:552-653`): the existing rules up to the baseline stay. After them, the
first match wins:

| # | When | Next command |
|---|---|---|
| 1 | no v2 plan and no receipt, and no post capture newer than the baseline | `pymolt plan .` (unchanged) |
| 2 | run-level stale | `pymolt plan .` |
| 3 | a step `in_progress` | `pymolt apply --step X --record --done` |
| 4 | pymolt steps `planned` with dependencies in effect | `pymolt apply .` (the reason lists them) |
| 5 | a pymolt step `stale` | `pymolt apply --step X --dry-run` (shows the lost anchor), then `pymolt plan .` |
| 6 | the first agent/person step `planned` with dependencies in effect, in plan order | `pymolt apply --step X --record`, plus its `--brief` |
| 7 | the post capture is missing or predates the latest step event | `pymolt capture post-migration …` |
| 8 | no verdict on that capture | `pymolt verify .` |
| 9 | a step `failed` | pymolt step: `pymolt rollback --step X`; agent/person step: fix it, then `pymolt apply --step X --record` again |
| 10 | a step `needs_review` | `pymolt verify . --step X`, then `pymolt verify . --accept ID` (a person) |
| 11 | a step `needs_evidence` | the existing replicate rule when the reason is noise; otherwise "add a test that reaches X's sites, then capture post-migration again" |
| 12 | anything else | done (unchanged wording) |

Rules 4–6 apply only while a receipt exists or no post capture newer than the plan exists. A
migration done entirely by hand (H7) goes straight to 7 and 8, and its edits are judged as hand
edits.

## 7. Open owner decisions

Each row names the default this spec is written for, and exactly what changes otherwise.

| # | Decision | Default | If the owner decides otherwise |
|---|---|---|---|
| 1 | apply writes the manifest bump: pinned requirements-style files only, reversible, in the ledger | yes. `manifest-pin` is a pymolt step: lines `name[extras]==X [; marker]` with no `--hash`, rewritten to the target pin for packages on both sides. Other lines, other manifest kinds (pyproject, poetry, conda) and hashed files: a person step with the exact lines. Installs and environment builds stay in assess | `manifest-pin` is always a person step (recordable, 3.3), and apply never writes a non-`.py` file. Stage 4's `feat/apply-env-manifest-pin` becomes `feat/plan-env-step-person` (S). `docs/commands.md` documents the trap: bump after apply and before the post capture. `bench/e2e_pandas` keeps its manual bump (`run.py:183-184`), wrapped in `--record` in stage 5 |
| 2 | the unit of apply is a step anchored on symbol + file + function + statement text | yes (1.4) | Steps anchor on lines: stage 1's anchors module is dropped (B0-1 is fixed by mapping lines through the diff). Risk ids keep the line. `apply --step` writes only on the fast path (`sha == before_sha256`), so any shift makes the step stale. Stage 4's `feat/apply-step-rebase` and stage 6 are dropped. `verify --step` selects by remapped ledger ranges only |
| 3 | verify judges an edit by its claim, not by its author | yes (5.1) | Recorded agent and person edits are judged as hand edits whatever their claim. Their pairing anchors still count, so the unverified share still drops. Strict judging stays only for the four pymolt bases. `intended` and ACCEPTED (5.2) remain, as review records. Stage 5's `feat/verify-judge-by-claim` shrinks to the pairing and ACCEPTED (M → S) |
| 4 | agents may run pymolt-checked steps with one approval per plan (`--agent`; MCP optional and gated) | yes: variant A (3.6) | Variant B (3.6): `--agent` only for record, brief and dry-run. No MCP write tool. Stage 5's `feat/apply-agent-view` shrinks (M → S) |
| 5 | Q1: an imports-only baseline for plans of Python fixes only, verdict capped below PASS | yes, not urgent | Under yes, stage 8 (optional): apply accepts a baseline captured with `--mode imports` when every selected step has `scope: python` and `basis: python-fixer`; `runs[].baseline_kind: imports`; verify caps the verdict at INCONCLUSIVE ("imports-only baseline"). Under no: stage 8 is dropped and nothing else changes |
| 6 | Q2: install and import check on by default | yes in the CLI with an opt-out; off over MCP unless `PYMOLT_MCP_ALLOW_EXEC`; always "not checked" when skipped | It also sets D's smoke import default after a write. The opposite choice makes the smoke import opt-in, with receipts reading "smoke_import: not checked: off by default". This spec's code does not change; D's flag does |
| 7 | INCONCLUSIVE split into "needs a person" (review) and "needs evidence" (BLIND, uncomparable) | proposed: split | Under the split: step statuses `needs_review`/`needs_evidence`, next-command rules 10/11, and `verification.json` gains `inconclusive_kind`. Exit code 4 stays for both. Without it: one `inconclusive` step status, and rules 10/11 pick the command from the reason text |

Choices this spec makes on its own (the owner may veto each):

| Choice | Alternative |
|---|---|
| one invocation = one transaction over its steps (keeps "all or nothing", `docs/plan.md`) | commit each step separately and keep the ones before a refusal |
| PLAN.md at `.pymolt/PLAN.md` | the project root (it would appear in `git status`) |
| an ACCEPTED on an unedited site creates an ad-hoc intended step in the ledger | accepting only on edited sites |
| test ids are not recorded (stage 7 is optional) | make stage 7 part of this scope |

## 8. Staged implementation plan

Each stage is a stack of one-commit branches `<type>/<scope>-<slug>`. The stages octopus-merge into
`integration/apply-work-order`, stacked on the H/D stack's tip. Sizes, tests included: S < 300 lines,
M 300–1,000, L > 1,000.

**Guards** (what each stage must keep green):

| Guard | Command | Requirement |
|---|---|---|
| suite | `uv run pytest -q` | all pass; ruff on touched files no worse than at HEAD |
| e2e | `python bench/e2e_pandas/run.py run` | 4/4, `append` prevented, `mean` hit |
| Wave G | `python bench/generalization/run.py run --out …` then `diff` against `results/waveG` | no equiv FAIL; every change scenario FAILs on its expected symbol; recall unchanged |
| legacy | `bench/legacy_eval/run.py` against `results/after-waveG` | the scorecard's outcome cells unchanged, unless the stage says otherwise |
| runtime | 3.6 parse of the injected runtime, `bench/load` | stage 7 only |

| Stage | Branches (one commit each) | Tests | Guards | Size | Depends on, and what runs in parallel |
|---|---|---|---|---|---|
| **B0** defects | `fix/pyversion-fix-sites-after-dependency-rewrite` (B0-1); `fix/plan-recipes-adopted-only` (B0-2) | the shift reproduction (0.3); a blocked rule is absent from `recipes`/`claims` | suite, e2e, Wave G (plan `changes` identical on all 10 toys), legacy (plans: only the listed B0-1 diffs) | S | H tip. The two run in parallel; they could also join the H stack |
| **1** anchors | `feat/plan-statement-anchors` (`pymolt/anchors.py`: make, resolve, normalize, `nth`); `refactor/predict-risk-id-from-anchor` (`predict.py:433-435`, `RiskSite.nth`) | resolution under black reformat, line shifts, duplicate statements (`nth`), decorators, multi-line statements, class bodies, comprehensions on 3.11 and 3.12 joins; risk ids stable under shifts | suite, e2e | M (~600) | B0-1 may use the anchors (otherwise the line-map fix). The anchors module can start in parallel with B0 |
| **2** work order | `feat/plan-schema-v2-steps` (models, schema file + equality test, v1 adapter); `feat/plan-step-builder-automated` (`on_step` observer, chains, grouping, order, `depends_on`, the `changes` oracle); `feat/plan-step-builder-manual` (risks to steps, H5 items, advisories, `step_id`); `feat/plan-step-verify-effort` (1.5.5); `feat/plan-env-steps` (`manifest-pin`, `target-env`); `feat/plan-workorder-render` (PLAN.md, briefs, `plan` summary by step, `plan --agent` steps, MCP parity) | a Tn == v1 `new_source` oracle on fixtures and on the e2e and Wave G toys; `depends_on` on crafted overlaps and binding needs; cap behaviour; schema round trip; v1 plan → step `v1`; PLAN.md golden file | suite; e2e (`changes` byte-identical); Wave G (`changes` identical); legacy: plan time no worse than +10% (theano 4.6 min, zipline 5.4 min), step counts added to the scorecard | L (~2,000) | stage 1. Models first; then the automated builder, verify/effort, env steps and render (against model fixtures) in parallel; the manual builder after the automated one |
| **3** ledger and receipts | `feat/ledger-v2-step-entries` (events, text store, v1 reader); `feat/migration-receipt-step-set` (runs, steps, `in_effect`, v1 adapter, readers switched to `in_effect`); `feat/verify-ledger-remap` (2.3, replaces H8's scoping) | the v1 → v2 reading of all three files; two runs plus a hand edit after apply → correct ranges; a step after the post capture → `not_in_capture`; v1 receipts and `in_effect` behaviour | suite; e2e; Wave G (verdicts and edited-site counts unchanged: one run with no later edits remaps to the identity) | M–L (~1,100) | the stage 2 models (not the builder): it runs in parallel with the rest of stage 2. Ledger and receipt in parallel; remap after the ledger and H8 |
| **4** stepwise apply and rollback | `feat/apply-step-select` (3.1–3.5: selection, transaction, fast path, idempotency, refusals, exit codes, D per step, the baseline check under H3); `feat/apply-step-rebase` (anchors + recipe re-run + effect equality); `feat/apply-env-manifest-pin` (decision 1); `feat/rollback-step-file-level` (4, phase 1, `--cascade`, capture set aside); `feat/status-step-projection` (`project_steps`, section 6) | every row of 3.4 and 3.5; re-base after a skipped earlier step; stale on a lost anchor; manifest pin and its rollback; rollback refusals; the next command for every rule in 6 | suite; e2e through `apply .` and through `--through mech-1`, files byte-identical to v1 apply; legacy: apply 6/6 with files byte-identical, and apply → rollback → apply on zipline without re-planning; Wave G | L (~1,600) | stages 2 and 3. Step selection first; then rebase, env pin, rollback and status in parallel |
| **5** every executor | `feat/apply-record-step` (3.3, `--skip`); `feat/verify-judge-by-claim` (5.1); `feat/verify-accept-review` (5.2, finding ids); `feat/verify-step-filter` (5.3–5.5); `feat/apply-agent-view` (3.6, the chosen variant, MCP) | a record with and without the start snapshot; out-of-scope hunks left unrecorded; preserve-strict FAIL on an agent edit; intended against each expectation kind; accept, then expiry on a new fingerprint; `--step` selection and exit codes; calibration by step (skipped → unfixed, recorded → prevented); approval voided by a re-plan | suite; e2e (plus a variant where `beh-1` is recorded: `mean` prevented in change); Wave G with a new `--record` mode in `bench/generalization/run.py` that wraps `manual_migration` in `apply --step … --record/--done`: no equiv FAIL, unverified edited sites below 120 of 191, default mode unchanged | L (~1,800) | stage 4 (record, agent view) and stage 3 (step filter). Record and judge-by-claim in parallel; accept after judge-by-claim; agent view after record |
| **6** hunk-level rollback | `feat/rollback-step-inverse-hunk` (4, phase 2) | rollback with later steps and hand edits elsewhere in the file; anchor mismatch → refusal; unused-import removal only | suite; legacy zipline cycle | M (~500) | stage 4. In parallel with stage 5 |
| **7** test ids (optional) | `feat/capture-site-test-ids`: in tests mode the runtime keeps up to 3 `PYTEST_CURRENT_TEST` values per site, stdlib only and 3.6-safe; `verify.tests` gets filled | a 3.6 parse; the tox matrix; ids on the e2e toy | runtime guards (3.6 parse, `bench/load` within 20%); Wave G | M (~400) | independent; can start after stage 2's models |
| **8** imports-only baseline (decision 5, optional) | `feat/apply-imports-only-baseline` | 7 row 5 | suite; legacy (vibora's apply was refused for lack of a baseline: now it applies, capped) | S–M (~350) | stage 4 |

Each stage carries its own documentation: `docs/plan.md` (steps, PLAN.md), `docs/commands.md`
(options), `docs/agents.md` (decision 4), `docs/verify.md` (claims, ACCEPTED, `--step`),
`docs/limits.md`, `docs/glossary.md` (step, claim, executor, anchor, accepted), and the README usage
block (`apply .` stays the one-line path).

Critical path: H/D → B0 → 1 → 2 (models) → 3 → 4 → 5. Parallel lanes: stage 3 alongside the rest of
stage 2; stage 6 alongside stage 5; stage 7 from stage 2 on.

## 9. Risks, and what is out of scope

| Risk | Mitigation |
|---|---|
| Anchor ambiguity: identical statements, generated code, comprehension `func` names differing by version | `nth` plus a refusal, never a guess; joins try both `func` forms; an unresolved anchor is stale, never re-based by line |
| Re-basing accepts a statement whose context changed its meaning (another binding) | the recipe is re-run, binding-aware (ScopeProvider), on the current file, so equality covers the new context; D compiles |
| difflib misaligns repeated lines during the remap | the same alignment as provenance (`_line_map`), so the two cannot disagree. The limit is documented, not hidden |
| Plan time: replay per recipe plus the dependency re-run test | the observer adds no parses beyond today's chained runs; the re-run test is capped at 8 steps per file; legacy guard +10% |
| Plan size: hundreds of manual steps (zipline: 515 risks need attention) | grouping per (package, symbol, fact); PLAN.md collapses low/BLIND; JSON lists stay capped in views |
| Strict judging of agents' `preserve` raises the FAIL count on agent work | intended: a preserve claim that does not hold is a finding. Wave G's guard is no equiv FAIL in `--record` mode |
| ACCEPTED hides a regression | person only, fingerprint-bound, expires, always printed; `--strict` |
| `--record` snapshot size on large repos | full text above 50 MB is replaced by hashes; edits there are judged as hand edits, with a note |
| v1/v2 skew between benches and tools | v1 fields kept; v1 files read through adapters; bench readers unchanged |
| Two stacks in flight (H/D and B) | B builds on H's interfaces as listed in 0.2; any change H makes to `inspect_migration_plan`, the receipt or the ledger lands before stage 3 |

Deliberately out of scope:
- environment builds and installs (Wave 3). `target-env` is a person step; apply only runs D's smoke
  import when an environment exists;
- git commits per step, or a patch series. A later export, `apply --export-patches`, is possible:
  the ledger has every hunk in order;
- incremental re-planning (recomputing only the stale steps);
- applying part of a step (a subset of its files);
- pymolt writing agent or person steps: it suggests, it never writes them;
- bumping pyproject, poetry or conda manifests: a person step;
- test-id attribution, unless stage 7 is approved.
