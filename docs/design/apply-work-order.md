# Apply as a work order: hardening, self-check, steps, every executor

Status: design, 2026-11-17. Revised the same day to close an adversarial review (B1–B3, M1–M9 and
the minors; section 10 maps each finding to where it is closed). Base:
`feat/verify-coverage-and-recall` (d35e759).

One scope, approved by the owner, to finish before Wave 3:
- **H**, a hardening pass, and **D**, a self-checking apply. Both are being implemented as stacked
  commits in `.claude/worktrees/apply-hardening`. This spec builds on them and does not redefine them
  (0.2).
- **B**, a stepwise apply: the plan becomes ordered steps, and apply, rollback and verify work per
  step.
- **C**, apply as the step ledger for every executor: whether pymolt, an agent or a person does a
  step, the ledger records it with the claim verify judges it by.

Evidence: the apply review synthesis of 2026-11-17, re-checked against d35e759 (corrections in 0.3).
Out of scope: environment builds and installs (Wave 3), and git patch series (9). Section 7.2 sets a
lean variant next to each part a scope cut could remove, with its size and what it gives up. The
full variant stays the plan.

## 0. Starting point

### 0.1 What the code does today

| Fact | Where | What B/C changes |
|---|---|---|
| `plan` freezes whole-file `old_source`/`new_source` per changed `.py` file | `migration_plan.py:165-177`, `:323-342` | Steps become the unit. `changes` stays as a view derived from the steps |
| Dependency rewrites and Python fixes merge into one `FilePreview` per file. The text between them is not kept | `codemods/service.py:663-673`, `pyversion/fixes.py:305-360` | The step builder replays the recipes and keeps each intermediate text (1.5) |
| `apply` writes the frozen batch all or nothing, under an OS lock | `migration_plan.py:47-95`, `:569-726`; `codemods/apply.py:909-975`, `:1084-1111` | Kept: one invocation is one transaction over the steps it selects |
| Ledger: one run; difflib ranges over the (old, written) text; recipe found by leaf substring; archived by the next apply | `apply_ledger.py:58-121` | Ledger v2: events and entries with epochs, accumulated (2) |
| Verify loads the ledger whenever the file exists | `verify/service.py:2843-2844` | H8 scopes it to the run in effect. v2 computes coverage from the current epochs through one alignment (2.3) |
| Strict provenance for 4 claims; hand-edit rules for the rest | `verify/facts.py:2710`, `:3062-3074`, `:6594-6640`, `:6737-6753` | A monotone ladder of claims. The 4 claims become `basis` (5.1) |
| Risk id = sha1(symbol, file, func, **line**, text) | `verify/predict.py:433-435` | No line (1.4) |
| `fixed_by_plan` = the call's line changed and the leaf left it | `verify/predict.py:224-237`, `:533-541` | A risk is credited only when an in-effect step's effect removed or replaced its statement in the post snapshot (5.5) |
| Receipt: one run. `in_effect` = applied, or rolled back with no file restored | `migration_state.py:25-61` | A set of receipts, `in_effect` per step (2.4) |
| Rollback: whole run; refuses unless every file is at `new_sha256` (or `--force`) | `migration_plan.py:756-868` | Per step, with a state table (4) |
| `status`: a post capture without a receipt is stale; the report is done only with an `applied` receipt | `status.py:473-523` | H7, then a projection across steps (6) |
| `apply`/`rollback` have no `--agent` and no MCP tool | `interfaces/cli/commands.py:798-956`, `interfaces/mcp_server.py:85-306` | Decision 4 (3.6) |
| A capture records, per fact: file, `co_name` func, line, line text, counts. Per process it records which of the project's lines or functions ran. **It records no test id** | `verify/facts.py:238-268`, `verify/_backend.py:1465`, `:927-960` | "How to verify" lists sites, not tests. Test ids are optional stage 7 |
| In a user project, setup adds only `.pymolt/env_config.json` to an existing `.gitignore`. The rest of `.pymolt/` is not ignored | `setup/service.py:450-460` | The new stores keep copies of project source, so they get their own `.pymolt/.gitignore` (2.1) |

### 0.2 H and D (in flight): where B and C extend them

| Item | As implemented in the H/D stack | Extended here by |
|---|---|---|
| H1: re-applying an applied plan is a no-op success | whole plan | the same no-op per step (4.1, rows 2 and 3) |
| H2: re-apply after a rollback, without re-planning, while files match `old_sha256` | whole plan, exact hashes | per step: a rolled-back step re-applies with a new epoch while its anchors and its recipe check pass (4.1, row 7) |
| H3: the precondition = setup fingerprint + target-manifest hash | `inspect_migration_plan` (`migration_plan.py:466-469`). The H agent also fixes the baseline check at `:616` under H3 | invocations 2..n of a run check the baseline against the setup fingerprint plus the target-manifest hash the plan is bound to, so a manifest pin written by invocation 1 never makes the baseline stale (3.2, step 2) |
| H4: calibration credits import moves | in `_fix_for` | the statement rule of 5.5. Replacing the binding import counts |
| H5: blocked or downgraded rules become manual items in the plan | a manual-items list | each item becomes a `manual-rewrite` step (1.1) |
| H6: apply shows advisories | apply summary | the summary lists the remaining steps, review steps included (3.5) |
| H7: status projects a migration with no receipt | status | the same rule when the steps were never recorded (6) |
| H8: the ledger carries `run_id`; verify reads only the run in effect | per run | epochs per step across runs, and coverage computed through one alignment (2.2, 2.3) |
| D: compile before writing (target, and base where claimed); an optional smoke import after writing, with auto-rollback; results on the receipt | per apply | run per invocation over its steps; results on each step's receipt (`checks`). Recorded agent and person edits get the compile check too (3.3) |

D chooses the flags, defaults and exit code of its checks. This spec needs only one thing per step:
`checks.{compile_target, compile_base, smoke_import}` = `ok | failed: <reason> | not checked: <reason>`.

### 0.3 Defects found while writing this spec (stage B0)

| # | Defect | Evidence | Fix |
|---|---|---|---|
| B0-1 | **A Python fix can land on the wrong statement.** `apply_python_fixes` finds its target lines on the original file (`live_lines(sites)`, contact map on disk) and applies them to the text the dependency rewrite produced (`start = prior.new_source`, `pyversion/fixes.py:301-324`). A dependency rewrite that adds a line above the fix shifts it | Reproduced on d35e759. A `rewrite-import` split `from pkg.old import a, b` (+1 line). A `collections.Iterable` fix restricted to the live site's original line 5 then rewrote the non-live use (line 4, shifted to 5) and left the live one (shifted to 6). The minimal-diff gate passes, because the changed line names the spelling | Resolve the fix's sites through anchors (1.4), or map the lines through `diff(T0, T_dep)`. Add a test that splits a multi-name import above a fix site |
| B0-2 | **`recipes` and `claims` name rules that wrote nothing.** In verified-only previews, a FilePreview's `rules` holds every rule that *touched* the file, blocked ones included (`codemods/apply.py:1150-1158`, `:1340`, `:1389`). The plan copies that list into `recipes`/`claims` (`migration_plan.py:335-340`). `claim_for` reads the evidence prefix before the confidence (`apply_ledger.py:42-48`), so an unverified LEGACY rule (`delta/provider.py:114-127`) carries `same-object` | Code reading. Not seen in a real plan | Take plan recipes and claims from adopted hits only. Blocked rules go to H5's manual items |

## 1. Plan schema v2: the work order

### 1.1 Phases and step types

Steps are ordered by phase, then by application order within the phase. That order reproduces v1's
composition exactly (1.5).

| Phase | `type` | Default executor | `claim` / `basis` | Comes from | Recipe re-runnable |
|---|---|---|---|---|---|
| `env` | `manifest-pin` | pymolt (decision 1), else person | `intended` / `target-manifest` | the source manifest's `==` pins, compared with the target manifest | yes (a line rewrite) |
| `env` | `target-env` | person | none (no code edit) | assess's build commands (`commands.py:2096-2122`) | no. Status projects it from the environment check (`status.py:85-131`) |
| `mechanical` | `codemod` (`recipe.kind` ∈ `rewrite-import`, `rename-call`, `rule`) | pymolt | `preserve` / `same-object`, `same-signature`, `verified-rule` | adopted dependency patterns and rules | yes |
| `mechanical` | `python-fix` (`recipe.kind` ∈ `rewrite-attr`, `replace-attr-path`, `rewrite-module`, `rewrite-import`, `rename-call`) | pymolt | `preserve` / `python-fixer` | adopted Python fixes | yes |
| `mechanical` | `manual-rewrite` | agent | `preserve` / `manual` | H5 items (blocked or downgraded rules); rule advisories `not-applied` | no. The template is shown as a suggestion |
| `signature` | `api-change` | agent | `preserve` / `manual` | risks needing attention: REMOVED/MOVED with no auto fix, SIGNATURE at a passed parameter, DEPRECATED with a successor | no |
| `signature` | `python-manual` | agent | `preserve` / `manual` | Python risks with `fix: advisory`; compile findings on the target | no |
| `behaviour` | `behaviour-change` | person | `preserve` (proposed) or `intended` | DEFAULT at a defaulted parameter; warning-only high risks; Python BEHAVIOR facts | no |
| `behaviour` | `review` | person | `preserve` | rule advisories `precondition` | no |

How sites group into steps:
- **Automated step:** one recipe, or one move group, across every file it changes.
  `MoveNamesCodemod` applies together the patterns that share an old and a new module
  (`codemods/apply.py:774-811`).
- **Manual step:** one (scope, package, symbol, delta fact kind and parameter), across its risk sites.

### 1.2 Step fields

| Field | Content |
|---|---|
| `id` | `<phase>-<n>`: `env-1`, `mech-3`, `sig-2`, `beh-1`. Stable within a plan; this is what the CLI takes |
| `key` | sha1(type, recipe key \| package+symbol+fact kind+param)[:10]. **No anchors**, so a key survives a re-plan. It names the kind of change, not the step instance |
| `phase`, `type`, `title` | as in 1.1. `title` is one line, e.g. "`Series.append` → `pd.concat`" |
| `executor` | `pymolt` \| `agent` \| `person`: who is expected to do the step. `--record` records the actual actor (3.3) |
| `claim` | `preserve` \| `intended` (5.1) |
| `basis` | why pymolt trusts the claim: `same-object`, `same-signature`, `verified-rule`, `python-fixer` (today's claims), `target-manifest`, `manual` |
| `runs_on_base` | `true` \| `false` \| `null`: the Python fixes' flag (`codemods/models.py:41`) at step level. D compiles on the base when `true` |
| `scope`, `package`, `from`, `to` | `dependency` \| `python` \| `env`, plus the migration the step belongs to |
| `symbol` | `{old, new}` dependency symbols. `new` is the pairing anchor (5.1) |
| `depends_on` | step ids, hard dependencies only (1.5.3) |
| `status` | always `planned` in the file. The live status is projected (1.3) |
| `where` | the risk sites, as anchors made on T0 (the text the baseline ran), for joining with baseline facts |
| `why` | `{evidence[], tier, risk_ids[], baseline: {calls, sites, warnings[]}, delta: {kind, param, old, new}}`. `risk_ids` are the plan-time ids, persisted (M4); `calls` is the baseline witness count summed over the sites |
| `do` | pymolt steps: `{recipe}`, the serialized `CodemodPattern`/`CodemodRule` with `sites: "anchors"`. Other steps: `{manual}` = `{summary, successor, signature: {old, new, diff[]}, before_after: {before, after}, pitfalls[], consumers[]}` |
| `edits` | pymolt steps only: per file `{file, before_sha256, after_sha256, hunks[], effect, anchors_old[], anchors_new[]}`. The anchors are made on the step's own before-text and after-text (1.5.2) |
| `verify` | `{judge, expect, sites[], tests, command, uncovered}` (1.5.5) |
| `effort` | `{size: none\|trivial\|small\|medium\|large, sites, files, why}` (1.5.5) |
| `checks_at_plan` | Python fixes only: the plan-time recompile results (`pyversion/fixes.py:363-389`) |

### 1.3 Lifecycle

The plan file is immutable once saved. One function, `pymolt/workorder/state.py::project_steps`,
projects each step's status, and `status`, the views, `apply` and `verify --step` all call it. It
reads the receipt set, the ledger, the saved verification and the current files. Anchor resolutions
are cached per (file sha256, anchor key) in `.pymolt/cache/anchors.json`; a file is re-hashed only
when its `(mtime_ns, size)` changes.

| Status | Meaning | Set by | Stored in |
|---|---|---|---|
| `planned` | in the plan; no epoch | `plan` | plan file |
| `blocked` | a `depends_on` step is not in effect | projected | none |
| `stale` | an anchor is lost or ambiguous (1.4), or the recipe no longer reproduces the reviewed effect | projected: an anchor check in `status`, the full check in `apply` | none |
| `present` | its effect is on disk, but no epoch of this step wrote it (4.1, row 3). Verify judges those lines as hand edits | projected | none |
| `in_progress` | `--record` took the snapshot. At most one per run (3.3) | `apply --record` | receipt step |
| `applied` | the current epoch is a pymolt `apply` | `apply` | receipt step + ledger |
| `recorded` | the current epoch is a `record` | `apply --record --done` | receipt step + ledger |
| `skipped` | a person declined it, with a reason | `apply --step ID --skip` | receipt step + ledger event |
| `rolled_back` | a rollback ended its epoch | `rollback` | receipt step + ledger event |
| `verified` | in effect and in the capture: every site was compared and judged by its claim; nothing failing or for review | `verify` | `verification.json` `steps` |
| `failed` | a site's judgement failed | `verify` | same |
| `needs_review` | a review item at its sites (decision 7) | `verify` | same |
| `needs_evidence` | a site not exercised (BLIND) or uncomparable (decision 7) | `verify` | same |
| `not_in_capture` | the step's new statements (`anchors_new`) do not resolve in the post snapshot P | projected | none |
| `accepted` | an `intended` step whose outcome a person attested (5.2) | `verify --accept` | ledger `accept` event + attestation log |

Precedence, first match:
1. run-level stale (the setup fingerprint or target manifest changed, or the baseline was replaced);
2. `in_progress`;
3. `rolled_back` / `skipped`;
4. `not_in_capture`;
5. a verify judgement newer than the step's current epoch;
6. `applied` / `recorded`;
7. `present`;
8. `stale` / `blocked`;
9. `planned`.

### 1.4 Anchors and risk ids

```json
{"id": "a1", "file": "toyapp/core.py", "scope": "combine", "func": "combine",
 "symbol": "pandas.core.series.Series.append",
 "stmt": "return s1.append(s2, ignore_index=True)", "key": "5d0c41e2a9f37b18",
 "prev": "9a1f0c33d2e4b7a5", "next": "$", "nth": 0, "count": 1,
 "site_text": "return s1.append(s2, ignore_index=True)", "line": 13}
```

| Field | Rule |
|---|---|
| `scope` | the dotted path of the enclosing `class`/`def` names; `<module>` at top level. It must resolve to exactly one scope in the text; two definitions with the same path (conditional defs) make it ambiguous |
| `func` | the innermost `def` name, or the class name in a class body, or `<module>`: what a capture records (`co_name`, `verify/_backend.py:1465`). It is used to join with facts. A call inside a comprehension is `<listcomp>`, `<genexpr>`, … under 3.6–3.11 and the enclosing def under 3.12 (PEP 709), so joins try both |
| `stmt` | display text: the smallest enclosing statement (a simple statement, or a compound statement's header) |
| `key` | **sha1 of the statement's AST with formatting stripped** (`ast.dump(node, include_attributes=False)`)[:16]. A compound statement keeps only its header: the body becomes `pass`, and `orelse`, `handlers` and `finalbody` are emptied. A docstring statement is keyed on `inspect.cleandoc` of its value. The AST normalises quote style, implicit string concatenation, redundant parentheses, magic trailing commas, whitespace, comments and line breaks (checked on d35e759: `f('a', (b), c,)` and `f("a", b, c)` dump equal). It is parsed with `feature_version` = the minor of that text's Python: the base for T0 and B, the target for after-texts and P. That keeps 3.6 code such as `async` used as a name parseable. **Fallback** when the text does not parse: a token key, with string tokens replaced by `repr(literal_eval(token))`, the trailing comma before a closing bracket dropped, and COMMENT/NL/INDENT/DEDENT dropped. The fallback does not normalise redundant parentheses; it says so in the anchor (`"key_kind": "tokens"`) |
| `prev`, `next` | the neighbour fingerprint: the `key` of the previous and next sibling statement in `scope`, `^` and `$` at the edges |
| `nth`, `count` | the ordinal among the statements in `scope` with the same `key`, and how many there were, both computed in the text the anchor was made on |
| `site_text` | the stripped source line holding the symbol: the same string as `Fact.text`, for joining with captures |
| `line` | where it was when made. A hint only, never an identity |

**Resolution** (`pymolt/anchors.py`, host side, LibCST positions plus `ast` keys) on a text X:
1. Resolve `scope` uniquely. If it does not resolve, the anchor is lost.
2. Collect the candidates: statements in that scope whose `key` equals the anchor's.
3. One candidate: match.
4. Several: keep those whose `prev` and `next` keys equal the anchor's. One left: match.
5. Still several: the `nth` one, but only when the number of candidates equals `count` (the tie is
   the same one the anchor was made in). Otherwise the anchor is **ambiguous**, which counts as lost.
6. None: lost. pymolt never guesses a nearest match.

**Which text each anchor is made on:**
- `where` (risk sites): T0;
- `edits[].anchors_old`: the step's before-text T(k-1), with `nth`, `count` and neighbours computed
  there;
- `edits[].anchors_new`: the step's after-text Tk;
- an inserted import: `scope: "<module>"`. Its position is free, and it compares as a member of a
  set.

**Risk ids** (`verify/predict.py:433-435`) become `sha1("v2|" + symbol | file | func | site_text | nth)[:12]`.
- A static site with no line uses `site_text: ""` and `nth: 0`.
- `RiskSite` gains `nth`.
- Calibration already matches risks to entries by file, func and text (`predict.py:1193-1203`); the
  id names the risk and links it to steps (5.5).
- The ids are persisted: in the plan, in `why.risk_ids`, and on each step receipt at apply time (2.4).
- A v1 plan keeps its ids.

### 1.5 Building the steps

#### 1.5.1 Replay

`build_migration_plan` (`codemods/service.py:613-685`) keeps its inputs. The preview walk gains an
observer, `on_step(recipe, file, before, after)`:
- it is called by `apply_rules` (`codemods/apply.py:1126`), `apply_patterns` (`:774`) and
  `apply_python_fixes` (`pyversion/fixes.py:286`), each time a recipe or a move group changes a
  file's text;
- it changes nothing those functions return, so one code path produces both views;
- per file it gives the chain T0 → T1 → … → Tn, in today's order: rules in bundle order, then
  patterns and move groups, then Python fixes;
- Python fixes run on the dependency result, with their sites resolved through anchors (B0-1);
- the minimal-diff and recompile gates stay per file, over all of that file's Python fixes. A file
  they drop contributes to no Python step.

Test oracle: Tn equals v1's `new_source` for every file. The only differences allowed are those B0-1
causes, and each one is listed.

#### 1.5.2 Edits, hunks and effects

For each step and file, `edits[]` holds:
- `before_sha256` = sha(T(k-1)), `after_sha256` = sha(Tk);
- `hunks`: the difflib opcodes between T(k-1) and Tk, as `{old_start, old: [lines], new_start,
  new: [lines], anchors: [ids]}` (1-based, like the ledger);
- `effect`: the same change without positions, over anchor keys:
  `{"replace": [[old_key, new_key], ...], "insert": [key, ...], "delete": [key, ...],
  "imports_inserted": [key, ...]}`;
- `anchors_old` and `anchors_new`: made on T(k-1) and Tk (1.4).

No intermediate full text is stored in the plan. Tk is rebuilt from T0 (`changes[].old_source`) by
applying the hunks in order. `changes[]` stays, with `new_source` = T after every pymolt step (the v1
view, for the readers in 1.8).

A step's **reviewed hunk** is its `effect`. `apply --step` writes only when a fresh run of the recipe
on the current file produces the same effect at the step's resolved anchors (3.2).

#### 1.5.3 `depends_on`, from effect overlap

For each file, consider each pair of steps i < j that both change it. Step j depends on step i when
any of these holds:
1. **Statement overlap:** j's old keys (`replace[*][0]`, `delete`) meet i's new keys (`replace[*][1]`,
   `insert`). Here j rewrites what i produced.
2. **A shared inserted import:** i inserted an import that j also needs. j's after-text binds a name
   that only i's inserted import provides, or j's own effect lists the same key in
   `imports_inserted`.
3. **Fallback:** when an effect cannot be keyed (the token fallback is ambiguous), or when more than
   8 steps change the same scope: "same file, same scope". Steps that change the same scope depend
   on the earlier ones there, not on every earlier step in the file.

`depends_on` is the union over files. Stage 2 keeps a test oracle on fixtures: j's recipe re-run with
i left out must reproduce `effect_j` whenever the rules above found no dependency. It is a test only,
never run at plan time.

Manual steps get no code dependencies: verify judges them on the final code. `env-2` (`target-env`)
is a dependency of nothing. It gates only D's smoke import, which reports "not checked: no target
environment" without it.

#### 1.5.4 Risks to steps

Every `PredictedRisk` gets a `step_id`:
- **Addressed by an automated step:** the step's `symbol.old` (or a recipe spelling) equals the
  risk's symbol, and its effect replaces or removes either the risk's anchor statement or the import
  statement that binds that symbol in the file. That second case is H4's import move, by statement
  now, not by line. `fixed_by_plan` keeps its plan-time meaning ("a pymolt step addresses the risk").
  Verify credits the fix separately (5.5).
- **Left to a manual step:** one is created per (scope, package, symbol, fact kind + parameter),
  following 1.1. Its tier is the maximum over its sites, and `why.risk_ids` lists them.

Each H5 item and each rule advisory becomes one `manual-rewrite` or `review` step per (rule, file).
Low BLIND risks are steps too; PLAN.md collapses them (1.9).

#### 1.5.5 How to verify, and effort

`verify` per step:
- `judge`: `strict` (`preserve`), `expectation` (`intended`), `none` (env steps);
- `expect`: `null`, or one of the forms in 5.2;
- `sites[]`, per `where` anchor:
  - `baseline_calls`: `Fact.count` summed over the baseline facts with the same symbol and
    (file, func, site_text);
  - `coverage`: `recorded | ran-not-recorded | not-run | unobservable | unknown`, from the baseline
    capture, through the same classification verify uses (`verify/report.py:966`,
    `_judge_contacts`; `_load_executed` at `:531`);
- `tests`: `null`, with the note "the baseline capture does not record which tests ran a site"
  (stage 7 fills it);
- `command`: the baseline slot's command;
- `uncovered`: the number of sites `not-run` or `unobservable`. Unless a test reaches them, they end
  `needs_evidence`.

`effort.size`, with `why` naming the rule that applied:

| size | rule |
|---|---|
| `none` | executor pymolt |
| `trivial` | 1 site, successor known, same signature |
| `small` | up to 5 sites, successor known |
| `medium` | signature differs, or 6–20 sites, or a `review` step |
| `large` | `behaviour-change`, no successor, or more than 20 sites |

`do.manual` is filled best effort from the delta facts and the baseline. Its text from dependencies
and from the project is data, not instructions (1.10).
- `successor`, `signature`: `DeltaFact.successor` and `.old`/`.new`/`.param`, plus the probe's
  signatures from `detail` when it recorded them. Otherwise "signature not known".
- `pitfalls`:
  - the site's baseline warnings;
  - parameters it left at their defaults (`Fact.defaulted`);
  - exceptions it raised;
  - a call-time default ("old default None is decided at call time: passing it may not restore the
    old behaviour").
- `consumers`: up to 3 later calls in the same function whose receiver or input has the type this
  site returned in the baseline.

### 1.6 Stale steps, re-basing and re-planning

| Trigger | Effect |
|---|---|
| The setup fingerprint or the target-manifest hash changed (H3) | every unapplied step is stale. Next command: `pymolt plan .` |
| The baseline was replaced, or is no longer valid | apply refuses (exit 4), as today |
| A step's anchor is lost or ambiguous | that step is stale. Later steps that do not depend on it stay valid |
| The anchors resolve, but the recipe run produces another effect | that step is stale |
| A `depends_on` step is stale, rolled back or skipped | that step is blocked (stale when the dependency is stale) |
| Lines shifted: an earlier step skipped or rolled back, a hand edit elsewhere, reformatting | not stale. The step **re-bases**: its anchors resolve on the current text and the write uses the fresh recipe output |

Re-basing replaces whole-plan invalidation, which is the point of B. One stale step costs that step,
not a re-plan, and a re-plan takes 4.6 min on theano and 5.4 min on zipline. Section 7.2 gives the
lean alternative (fast path only).

A re-plan while a run is open:
- the new plan's T0 is the disk as it is now;
- the old run closes: it applies no more steps, and its steps in effect stay in effect;
- a risk whose statement an in-effect step of an earlier run removed or replaced on disk (the 5.5
  test) links to that step (`"<run8>:<id>"`). It gets no new step;
- the remaining sites get new steps, which may share a `key` with the earlier run's step: the key
  names the kind of change, so the two are shown as a continuation.

### 1.7 JSON schema

The pydantic models in `pymolt/workorder/models.py` are the source.
`MigrationPlan.model_json_schema()` is committed as `pymolt/schemas/migration_plan-2.schema.json`,
and a test asserts that the committed file equals the generated one. The generated file must match
what v2 adds:

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
      "required": ["id", "file", "scope", "func", "stmt", "key", "prev", "next", "nth", "count"],
      "properties": {
        "id": {"type": "string"}, "file": {"type": "string"},
        "scope": {"type": "string"}, "func": {"type": "string"},
        "symbol": {"type": ["string", "null"]}, "stmt": {"type": "string"},
        "key": {"type": "string", "pattern": "^[0-9a-f]{16}$"},
        "key_kind": {"enum": ["ast", "tokens"], "default": "ast"},
        "prev": {"type": "string"}, "next": {"type": "string"},
        "nth": {"type": "integer", "minimum": 0}, "count": {"type": "integer", "minimum": 1},
        "site_text": {"type": ["string", "null"]},
        "line": {"type": ["integer", "null"], "description": "a hint, never an identity"}
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
        "delete": {"type": "array", "items": {"type": "string"}},
        "imports_inserted": {"type": "array", "items": {"type": "string"}}
      }
    },
    "edit": {
      "type": "object",
      "required": ["file", "before_sha256", "after_sha256", "hunks", "effect", "anchors_old",
                   "anchors_new"],
      "properties": {
        "file": {"type": "string"},
        "before_sha256": {"type": "string"}, "after_sha256": {"type": "string"},
        "hunks": {"type": "array", "items": {"$ref": "#/$defs/hunk"}},
        "effect": {"$ref": "#/$defs/effect"},
        "anchors_old": {"type": "array", "items": {"$ref": "#/$defs/anchor"}},
        "anchors_new": {"type": "array", "items": {"$ref": "#/$defs/anchor"}}
      }
    },
    "expect": {
      "description": "5.2. The plan holds only agent-settable forms; person-only forms exist in attested ledger events",
      "oneOf": [
        {"type": "null"},
        {"type": "object", "required": ["outcome"], "properties": {"outcome": {"const": "any"}}},
        {"type": "object", "required": ["representation"],
         "properties": {"representation": {"const": true}}}
      ]
    },
    "step": {
      "type": "object",
      "required": ["id", "key", "phase", "type", "title", "executor", "claim", "basis",
                   "runs_on_base", "depends_on", "status", "where", "why", "do", "verify",
                   "effort"],
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
        "runs_on_base": {"type": ["boolean", "null"]},
        "scope": {"enum": ["dependency", "python", "env"]},
        "package": {"type": ["string", "null"]},
        "from": {"type": ["string", "null"]}, "to": {"type": ["string", "null"]},
        "symbol": {"type": "object", "properties": {"old": {"type": ["string", "null"]},
                                                    "new": {"type": ["string", "null"]}}},
        "depends_on": {"type": "array", "items": {"type": "string"}},
        "status": {"const": "planned"},
        "where": {"type": "array", "items": {"$ref": "#/$defs/anchor"}},
        "why": {"type": "object", "properties": {
          "evidence": {"type": "array", "items": {"type": "string", "maxLength": 300}},
          "tier": {"enum": ["certain", "high", "medium", "low", null]},
          "risk_ids": {"type": "array", "items": {"type": "string"}},
          "baseline": {"type": "object", "properties": {
            "calls": {"type": "integer"}, "sites": {"type": "integer"},
            "warnings": {"type": "array", "items": {"type": "string", "maxLength": 300}}}},
          "delta": {"type": ["object", "null"]}}},
        "do": {"type": "object", "oneOf": [
          {"required": ["recipe"]},
          {"required": ["manual"], "properties": {"manual": {"type": "object", "properties": {
            "summary": {"type": "string"}, "successor": {"type": ["string", "null"]},
            "signature": {"type": ["object", "null"]},
            "before_after": {"type": ["object", "null"]},
            "pitfalls": {"type": "array", "maxItems": 10, "items": {"type": "string", "maxLength": 300}},
            "consumers": {"type": "array", "maxItems": 3, "items": {"type": "string"}}}}}}]},
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
| `views.plan_view` diffs (`interfaces/views.py:746-750`) | `changes` | unchanged (the full automated diff); the view adds the steps |
| `predict.planned_rewrites` at plan time (`verify/predict.py:197-214`) | `changes` | replaced by the step links (1.5.4) for v2 plans |
| `_predictions_to_calibrate` (`verify/service.py:2900-2935`) | `changes` (fixed by plan) | the statement credit (5.5) |
| `_edited_stdlib_modules` (`verify/service.py:414-440`) | `changes[].old_source` | unchanged |
| `bench/e2e_pandas/run.py:150-166`, `bench/generalization/run.py:351`, `:505-536` | `changes`, `risks` | unchanged |

`MigrationPlan.load` reads a schema-1 file into the v2 model with one synthetic step, `v1`:
- `type: codemod`, `executor: pymolt`, `claim: preserve`, `basis` from v1 `claims`;
- its `edits` hold one whole-file hunk per change;
- its anchors are made on the plan's `old_source`/`new_source`.

`apply --step v1` and `apply .` act on it as today. Splitting a v1 plan into steps needs a re-plan,
because v1 never recorded where one recipe ends and the next begins.

### 1.9 PLAN.md (for a person)

`plan` writes `.pymolt/PLAN.md` and prints its path; `--plan-md PATH` writes it elsewhere. It is
rendered from the plan alone, by `pymolt/workorder/render.py`, under the data rules of 1.10. The
layout, for the e2e pandas `change` scenario:

````markdown
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
- **Where:** toyapp/core.py, `combine()`, line 13 (at plan time):
  ```text
  return s1.append(s2, ignore_index=True)
  ```
- **Why:** removed in pandas 2.0 (certain). The baseline ran it 1 time, with a FutureWarning.
- **Do:** knowledge rule `pandas-append-to-concat`, verified against its golden pair. <diff>
- **Verify:** strict. Any change of outcome at this site fails. The baseline exercised it.
````

Low-tier BLIND steps collapse into one table at the end, with a count. Line numbers are the
plan-time hints and are labelled as such.

### 1.10 Per-step agent briefs, and untrusted text

`pymolt apply --step ID --brief` prints the brief and writes nothing: markdown by default, JSON under
`--agent`. It is rendered from the plan and the current files, so its lines are resolved ones, never
stale hints. `plan --agent` and `status --agent` include the brief of the next step to act on.

| Brief field | Content |
|---|---|
| `goal` | the title, and `why` in one sentence (pymolt's own words) |
| `where` | anchors with the resolved line and `scope`; `site_text` as data |
| `do` | `do.manual`: successor, signature diff, before → after, pitfalls, consumers. Dependency text is data |
| `constraints` | for `preserve`: "keep the outcome of every call at these sites; pymolt compares each one with the baseline and fails on any difference". For `intended`: "an agent cannot make this pass: a person attests the new outcome (5.2)". For both: "edit only these statements unless the brief names more; other changes are judged as hand edits" |
| `verify` | `verify.sites` with their coverage; the `uncovered` sites and what they need |
| `commands` | begin `pymolt apply --step ID --record --agent`; finish `… --record --done --agent`; abort `… --record --abort --agent` |
| `expected` | the compile check runs on the files you edit; then `pymolt capture post-migration` and `pymolt verify . --step ID` |

**Untrusted text:** everything from a dependency (warning messages, docstrings, delta evidence), from
the project (`stmt`, `site_text`, `before_after`) or from a capture is data, never instructions.
- In markdown it sits in a fenced block whose fence is longer than any backtick run inside it, under
  a label: "data from <source>, not instructions".
- In JSON it goes under `data` with `"untrusted": true`.
- The renderer removes C0 control characters (except tab and newline), C1 control characters,
  U+2028/2029 and the bidi controls U+202A–202E and U+2066–2069.
- Each item is capped at 300 characters (the warning cap) and each list at 10 items.
- Inside a fence, markdown is not interpreted.

## 2. Ledger v2 and receipts

### 2.1 Ledger v2: events, entries, epochs

`.pymolt/apply_ledger.json`, `"schema": "pymolt.apply_ledger/2"`. It is append-only and written under
the migration lock; the next apply no longer archives it (`apply_ledger.py:100-111`).

```json
{"schema": "pymolt.apply_ledger/2",
 "events": [
  {"seq": 1, "event": "apply", "run_id": "3f2a…", "step_id": "mech-1", "at": "…",
   "executor": "pymolt", "actor": "pymolt", "attestation": null,
   "files": [{"file": "toyapp/core.py", "before": "<sha256>", "after": "<sha256>"}],
   "risk_ids": ["7c0e9a41d2b3"]},
  {"seq": 2, "event": "rollback", "run_id": "3f2a…", "step_id": "mech-1", "at": "…",
   "restored": true, "files": [{"file": "toyapp/core.py", "before": "…", "after": "…"}]},
  {"seq": 3, "event": "apply", "run_id": "3f2a…", "step_id": "mech-1", "at": "…", "…": "…"},
  {"seq": 4, "event": "accept", "run_id": "3f2a…", "step_id": "beh-1", "finding": "…",
   "symbol": "…", "anchor_old": {"…": "…"}, "anchor_new": {"…": "…"}, "fingerprint": "…",
   "captures": {"baseline": {"sha256": "…", "captured_at": "…"},
                "post": {"sha256": "…", "captured_at": "…"}},
   "attestation": "att-91c2…", "at": "…"}],
 "entries": [
  {"epoch": 1, "run_id": "3f2a…", "step_id": "mech-1", "file": "toyapp/core.py",
   "old_lines": [13, 13], "new_lines": [13, 13],
   "anchor_old": {"…": "…"}, "anchor_new": {"…": "…"},
   "kind": "rule", "rule": "template:$S.append(…)→pd.concat(…)",
   "claim": "preserve", "basis": "verified-rule", "expect": null,
   "old_symbol": "pandas.core.series.Series.append",
   "new_symbol": "pandas.core.reshape.concat.concat"},
  {"epoch": 3, "…": "the re-apply's entries, re-based"}]}
```

| Field | Rule |
|---|---|
| `events[].event` | `apply` \| `record` \| `rollback` \| `skip` \| `abort` \| `accept`. An `apply` has `wrote: true\|false` (false: the file was already at the reviewed after-text, 3.2 rule 2). A `rollback` has `restored` |
| `entries[].epoch` | the `seq` of the `apply` or `record` event that wrote the entry |
| a step's **current epoch** | the `seq` of its latest `apply`/`record` event, unless a later `rollback` with `restored: true`, or a `skip`, ended it. A `rollback` with `restored: false` (it rewrote no file) keeps the epoch, as v1's `in_effect` does |
| ranges | 1-based and inclusive in the entry's own before/after text, as in v1. `null` for a pure insertion or deletion. Verify never uses them directly; it locates the entry by its anchors (2.3) |
| `claim`, `basis`, `expect` | from the step, or from `--record --done` (3.3) within the ladder of 5.1 |
| `attestation` | the id of an out-of-tree person attestation (5.6), or `null` |
| attribution | by step, not by leaf substring. `_recipe_for` (`apply_ledger.py:58-73`) is used only for v1 entries |

**Blob store.** Every `before`/`after` text and every `--record` snapshot file is stored once, at
`.pymolt/blobs/<sha256>.gz`.
- The store holds copies of project source, so stage 3 writes `.pymolt/.gitignore` with `blobs/`,
  `cache/` and `migrations/*/steps/`. It does not touch the project's own `.gitignore`.
- **Retention:** a blob stays while one of these references it: an event of a run in the receipt
  chain (2.4), a snapshot of a step `in_progress`, or the two most recently closed runs. `plan` and
  `apply` collect the rest under the lock.
- The lean variant keeps hashes and anchors only (7.2).

### 2.2 In effect

| Scope | `in_effect` when |
|---|---|
| step | it has a current epoch, and its run is in the receipt chain |
| entry | `entry.epoch` is its step's current epoch. Entries of a superseded epoch (a re-apply or re-record wrote a newer one) or of an ended epoch are audit data only |
| run | at least one of its steps is in effect. This keeps the v1 property `MigrationReceipt.in_effect` and its readers (`verify/service.py:320-321`, `:429-431`, `:2711`, `:2861`, `:2921`, `:3330`) |

### 2.3 Coverage (verify side): one alignment per file

`build_diff_context` (`verify/service.py:2826-2884`) computes `ctx.edits` once, in
`pymolt/workorder/coverage.py::ledger_coverage(B, P, entries)`. B is the baseline snapshot
(`.sources.json.gz`), P the post-migration snapshot, and `entries` the in-effect entries only. For
each file in both B and P:
1. **One alignment.** `A = _line_map(B, P)` (`verify/facts.py:3165-3184`): the alignment provenance
   already uses to decide which lines are edited. `E_B` and `E_P` are the lines A does not mark
   `same`.
2. **Locate each entry.**
   - New side: resolve `anchor_new` on P (1.4). The statement's lines ∩ `E_P` is the entry's P range.
   - Old side: resolve `anchor_old` on B. The statement's lines ∩ `E_B` is its B range.
   - An anchor that is lost or ambiguous gives that side no range: a later edit took it over, or an
     earlier step's entry covers it.
   - The ranges the entry was written with are not used.
3. **Conflicts.** A line covered by more than one entry takes the strictest claim among them, on
   the ladder of 5.1: `preserve` > `intended` unattested > `intended` attested. The pairing anchors
   of every covering entry are offered for pairing. An edited line no entry covers is a hand edit.
4. **Pairing only.** An entry that ends up with no range on either side is kept as a pairing anchor
   (`old_symbol → new_symbol`) only.

Consequences:
- A ledger entry covers only lines that the single alignment calls edited, so coverage and provenance
  cannot disagree.
- Coverage depends only on B, P and the statements of the current epochs. Not on how many invocations
  produced them, in which order, or how often a step was re-based. That is what the metamorphic guard
  (8) checks: `apply .`, `--step` one at a time and `--through` give the same coverage and verdict.
- `_Provenance.ledger_covers` (`verify/facts.py:3062-3074`) keeps its 1-based contract and returns
  the covering entry instead of `True` (5.1).
- This replaces H8's per-run scoping and generalises it to several runs, several steps and hand
  edits after apply.
- A step applied after the post capture has `anchors_new` that do not resolve in P: no P range, and
  the step is `not_in_capture`.

### 2.4 Receipt set

`.pymolt/migration_state.json`, `schema_version: 2`. The v1 fields stay as a summary of the latest
run, with **their v1 values**, so every v1 reader keeps working.

```json
{"schema_version": 2, "run_id": "3f2a…", "status": "applied", "progress": "partial",
 "applied_at": "…", "updated_at": "…", "target_python": "3.12", "source_manifest": "requirements.txt",
 "baseline_trace_path": "…", "baseline_captured_at": "…", "environment_fingerprint": "…",
 "allow_no_baseline": false, "files_changed": ["…"], "patterns_applied": 1, "plan_path": "…",
 "runs": [{"run_id": "3f2a…", "plan_path": "…", "opened_at": "…", "closed": false,
           "starting_fingerprint": "…", "setup_fingerprint": "…",
           "target_manifest_sha256": "…", "baseline_kind": "full"}],
 "steps": {"3f2a…:mech-1": {"status": "applied", "epoch": 3, "executor": "pymolt",
            "actor": "pymolt", "claim": "preserve", "applied_at": "…", "rolled_back_at": null,
            "files": ["toyapp/core.py"], "files_restored": null,
            "risk_ids": ["7c0e9a41d2b3"],
            "checks": {"compile_target": "ok", "compile_base": "not checked: no base claim",
                       "smoke_import": "not checked: no target environment"}}}}
```

| Field | Rule |
|---|---|
| `status` | the v1 values only. `applied`: at least one step of the latest run is in effect. `rolled_back`: none is |
| `progress` | `complete` (every pymolt step in effect or skipped), `partial`, or `none` |
| `files_changed`, `patterns_applied` | the union and the sum over the steps in effect |
| `runs` | the chain of runs whose code is on disk. A re-plan closes the open run and appends the new one |
| `starting_fingerprint` | stamped once, by the run's first invocation, before any write. When `baseline_setup_unchanged` holds (`verify/service.py:3316`), the baseline's own fingerprint |
| `steps[*].risk_ids` | the plan-time risk ids the step addresses, persisted when it is applied or recorded (M4). Calibration reads these links; it never recomputes them from a later plan |

### 2.5 From v1 state

| v1 file | Read as | Rewritten as v2 |
|---|---|---|
| plan (`schema_version: 1`) | one synthetic step `v1` (1.8) | never; a re-plan writes v2 |
| `migrations/<run>/plan.json`, `state.json` | the run, and step `v1` with `state.status` | `state.json` gains `steps` on the run's next mutation |
| `migration_state.json` (`schema_version: 1`) | `runs: [that run]`; `steps: {"<run>:v1": …}`; `status` unchanged | on the next apply, rollback or record, after a copy to `.pymolt/archive/` |
| `apply_ledger.json` (`pymolt.apply_ledger/1`) | events `seq 1: apply` (and `seq 2: rollback, restored: true` when the v1 receipt shows files restored); entries `epoch 1`, `step_id: v1`, `run_id` from the top level (v1 already writes it: `apply_ledger.py:115`). The claim is `preserve` with `basis` set to the v1 claim, or `null` (a hand edit) where v1 had none. Anchors are made on the frozen plan's `old_source`/`new_source` | the same, on the first v2 write |
| archived v1 ledgers (`.pymolt/archive/apply_ledger-*.json`) | ignored, as today | never |

## 3. The apply CLI

### 3.1 Options

| Option | Meaning | `--agent` (decision 4, variant A) | MCP `apply` |
|---|---|---|---|
| (none) | every pymolt step whose dependencies are in effect, in order, in one transaction. On a v1 plan: today's apply | with an approval | with an approval and `PYMOLT_MCP_ALLOW_WRITE=1` |
| `--step ID` | that step only. Refuses when a dependency is not in effect | same | same |
| `--through ID` | every pymolt step up to ID that is not in effect, plus their dependencies, in one transaction. Refuses when a dependency on the way is an agent or person step that is not in effect | same | same |
| `--dry-run` | resolve, re-base and check, then print the diff that would be written. Writes nothing | yes | yes |
| `--record` / `--record --done` / `--record --abort` | record an agent's or a person's edit (3.3) | yes | yes |
| `--claim preserve\|intended`, `--expect any\|representation`, `--pair OLD=NEW` | with `--done`: within the ladder (5.1), these can only keep or tighten the judgement | yes (lean: person only, 7.2) | **no** |
| `--expect raise:<T>\|return:<T>` | with `--done`, on a TTY: a person's attested expectation (5.6) | **no** | **no** |
| `--by agent\|person`, `--actor NAME` | who did it. `person` is recorded as attested only through 5.6 | `--by agent` only | `agent` fixed |
| `--skip --reason TEXT` | mark a step `skipped`; its dependents stay blocked | no | no |
| `--brief` | print the step's brief (1.10) | yes | yes |
| `--approve-agent` | a person approves the plan for agents (3.6) | **no** | **no** |
| `--plan`, `--allow-no-baseline`, `--json` | as today | `--allow-no-baseline` no | no |

`rollback`'s `--force` and `--cascade` are refused under `--agent` and do not exist over MCP (4.2).

### 3.2 `apply`, `--step` and `--through`

Under `_migration_lock`:
1. **Load.** If the run's frozen copy exists (`migrations/<run>/plan.json`), it must be the same plan
   (`plan_id` and content). A new `plan_id` opens a new run and closes the old one (4.1, row 9).
2. **Run preconditions:**
   - recover an interrupted invocation. `state.json` holds the invocation's steps and each file's
     before/after hashes; this extends `migration_plan.py:540-566`;
   - H3: the setup fingerprint plus the target-manifest hash;
   - the baseline binding (`migration_plan.py:606-642`, as fixed under H3 at `:616`). From the run's
     second invocation on, the baseline is checked against the setup fingerprint plus the plan's
     bound target-manifest hash, never against the full fingerprint, so an `env-1` pin written by
     invocation 1 does not make it stale;
   - stamp the starting fingerprint (2.4), on the first invocation only.
3. **Selection:** the steps asked for, with their dependency closure for `--through`. A step already
   in effect is a no-op (4.1). An agent or person step in the closure that is not in effect: refuse.
4. **Per selected step, in order, on the text produced so far.** For each of the step's files, with
   current text C, the first rule that matches decides:
   1. `sha(C) == before_sha256`: write the frozen after-text (fast path).
   2. `sha(C) == after_sha256`: the text is byte for byte what pymolt reviewed. Write nothing. If the
      step has no current epoch, append an `apply` event with `wrote: false`. Its files carry the
      plan's `before_sha256`/`after_sha256`, the before-text is rebuilt from the plan into the blob
      store, and the entries come from the plan's hunks. That gives the step an epoch: the text is
      exactly the recipe's reviewed output, so the claim holds.
   3. The `anchors_old` resolve on C: run the recipe with its sites restricted to them, giving C′. If
      `effect(C, C′)` equals the step's effect, write C′ (re-based). Otherwise refuse: the step is
      stale.
   4. Every `anchors_old` is lost, but the effect sits in its slots:
      - the anchor's `scope` resolves uniquely;
      - for each `replace`/`delete` pair, the statements whose keys are the anchor's `prev` and
        `next` resolve in that scope, adjacent around exactly one statement, and that statement's
        key is the new key (for `delete`: they are adjacent);
      - every inserted import is present at module scope.

      Then write nothing and append no epoch. The step is projected `present` (1.3), and verify
      judges those lines as hand edits.
   5. Anything else: refuse, step stale, naming each lost or ambiguous anchor.
5. **D before writing.** Compile every changed file on the target, and on the base where
   `runs_on_base` is true. On a failure, nothing is written (D's exit code).
6. **Write** every file of every selected step in one transaction (`write_previews`,
   `codemods/apply.py:1084`).
7. **Ledger and receipts:** one `apply` event per step (its `seq` is the new epoch), its entries, the
   blobs, and a step receipt with `checks` and `risk_ids`. A failure here reverses the writes
   (`migration_plan.py:693-725`).
8. **D after writing: the smoke import.** On a failure, the whole invocation is rolled back,
   `rollback` events (`restored: true`) are appended, and the step receipts record the failure. Over
   MCP without `PYMOLT_MCP_ALLOW_EXEC`, the check is skipped, and the receipt says
   `smoke_import: not checked: MCP without PYMOLT_MCP_ALLOW_EXEC`.

### 3.3 `--record` for agent and person steps

| Phase | What happens |
|---|---|
| `--record` | Refuses unless the step is `planned`, `rolled_back`, `stale` or `recorded` (a re-record) and its dependencies are in effect. Refuses when **another step of the run is `in_progress`**: at most one per run, checked under the migration lock, because two open records would attribute each other's edits. Snapshot: every project `.py` file (the `_iter_py_files` skip rules, `codemods/apply.py:835-851`) goes into the blob store, which de-duplicates them. A per-step manifest, `migrations/<run>/steps/<id>/snapshot-<seq>.json` = `{file: sha256}`, lists them. Above 50 MB of new blob bytes, the remaining files are listed by hash only (`"hash_only": true`). Appends no event; the step receipt becomes `in_progress` |
| `--record --done` | Diffs the snapshot against now. For a re-record, a file the step's previous epoch touched is diffed from that epoch's before-text instead, so the new epoch describes the whole step. Appends a `record` event (the new epoch) and its entries: anchors made on both texts, the claim and `expect` within the ladder (5.1), `basis: manual`, the actor, and `symbol` plus `--pair` as pairing anchors. The previous epoch is superseded. A file listed by hash only is recorded with "before text unknown", as a hand edit. The compile check runs on the changed files: target, plus base when the step says so. A failure goes into `checks`, the edit stays (pymolt did not write it), and the command exits with D's failure code. **No change: refuse**, except for `review` and `env` steps, which record a completion with no entries. A behaviour change with no code edit is accepted only through `verify --accept` (5.2) |
| `--done` with no `--record` first | the before-text of each file is its last known state: the after-text of the latest ledger event on that file, whatever the event (an `apply`, a `record`, a restoring `rollback`, a run of another plan), recovered from the blob store. The plan's T0 only when the ledger has no event on the file. A text missing from the store is refused (exit 2), never guessed. Only hunks inside the step's anchor scopes are attributed to it. The others are listed, left unrecorded, and judged as hand edits. (Revised 2026-11-18: the first version took the in-effect after-text, else T0. A step rolled back after a re-plan left the file at a text newer than the new plan's T0, and its undo was then credited to the step being recorded) |
| `--record --abort` | appends an `abort` event, drops the snapshot manifest (the blobs follow the 2.1 retention rules) and restores the previous status. Files are untouched |

A `manifest-pin` step recorded by a person (decision 1 = no) records the manifest's lines with
`claim: intended`. `target-env` needs no record: status projects it from the environment check.

### 3.4 Refusals and exit codes

| Code | When |
|---|---|
| 0 | applied, recorded, skipped, aborted, already in effect, or `present` |
| 1 | D: a compile check failed (nothing written), or the smoke import failed (rolled back). D's choice of code applies if it differs |
| 2 | refused: an unknown step; a dependency not in effect; a stale step (each lost or ambiguous anchor, or differing effect, named); `--record` in a wrong state, or another step `in_progress`; a run conflict or a closed run (4.1); a plan bound to another run; another migration operation active; an option refused under `--agent` or MCP; an invalid combination of options |
| 3 | an environment or IO failure; nothing written |
| 4 | evidence: no valid baseline bound to the plan (unless `--allow-no-baseline`) |

### 3.5 Summary

The summary lists the steps applied (re-based ones marked), each step's checks, and the steps that
remain, by status, review steps and advisories included (H6). It ends with the next command (6).

### 3.6 Agent view and MCP (decision 4)

| | Variant A (default: agents run pymolt steps with one approval per plan) | Variant B (agents only record their own edits) |
|---|---|---|
| Approval | `pymolt apply . --approve-agent` needs a person on a TTY (5.6): it is refused under `--agent`, `--json` and MCP. It writes `migrations/<run>/approval.json` = `{plan_id, plan_sha256, scope: ["pymolt-steps", "rollback-step"], approved_at, attestation}`. A re-plan, or any change to the plan file, voids it | none |
| `apply --agent` | pymolt steps only with a matching approval (otherwise exit 2, naming the command). Always allowed: `--record`, `--done`, `--abort`, `--brief`, `--dry-run` | only `--record`, `--done`, `--abort`, `--brief`, `--dry-run`. A pymolt step under `--agent`: exit 2, "a person runs `pymolt apply .`" |
| `rollback --agent` | `--step` on an approved plan; never `--force` or `--cascade` | not available |
| `verify --accept` | person only (5.6) | same |
| View | `views.apply_view` = `{summary, data: {applied[], rebased[], noop[], present[], refused: {step, reasons}, checks, remaining: {status: [ids]}, next: {command, step, brief}}}`, under the view layer's caps | the same view for record operations |
| MCP | `apply(step, through, dry_run, record, done, abort, brief)` and `rollback(step)`, gated by `PYMOLT_MCP_ALLOW_WRITE=1` in the server's environment plus the approval. They never take `force`, `cascade`, `approve_agent`, `claim`, `expect`, `pair` or `allow_no_baseline`. The CLI↔MCP parity test covers them | no new tool. `plan` and `status` return the steps and the next brief |
| Docs | the sentence in `docs/agents.md` "`apply` and `rollback` have no agent view" (`:29`) is rewritten | that sentence gains "except `--record`" |

The approval is a gate against accidents, not against an adversarial agent (5.6).

## 4. Rollback per step and the state table

### 4.1 State table

R1 and R2 are two runs of the same project; R2 came from a re-plan while R1 was open. B is the step's
before-text, A its after-text. "Capture" is the post-migration capture.

| # | Before | Command | Disk | Ledger | Epoch | After | Capture |
|---|---|---|---|---|---|---|---|
| 1 | `planned` | `apply` (rule 1 or 3 of 3.2) | B → A (or re-based) | `apply` event + entries | new = event `seq` | `applied` | kept. Stale by the sources rule (6) when it recorded the file |
| 2 | `planned`, file already at `after_sha256` | `apply` | none | `apply` event `wrote: false`, with the plan's hashes and hunks (rule 2) | new | `applied`; a later rollback restores the plan's before-text, as row 5 | kept |
| 3 | `planned`, effect in its slots, other text (rule 4) | `apply` | none | none | none | `present` | kept |
| 4 | `applied` or `recorded` | `apply` (or `--done` with no change) | none | none | unchanged | unchanged | kept |
| 5 | `applied` | `rollback --step`, file-level (4.2) | A → B | `rollback`, `restored: true` | ended | `rolled_back` | set aside only when the step's `anchors_new` resolve in the capture's snapshot **and** the capture postdates the step's epoch event. Otherwise kept |
| 6 | an epoch that changed no file (a `review` or `env` completion with no entries; a v1 run that changed no file) | `rollback --step` | none | `rollback`, `restored: false` | kept (in effect, as v1) | `rolled_back` (in effect) | kept |
| 7 | `rolled_back` | `apply` | re-based onto the current text | `apply` event + entries | new; the old one stays ended | `applied` | as row 1 |
| 8 | `recorded` | `--record`, edit, `--done` | the person's edit | `record` event + entries for the whole step | new; the previous one superseded | `recorded` | stale by the sources rule |
| 9 | R1 open, some steps in effect | `plan` → R2, then `apply` | R2's steps | R2's events | R1's epochs unchanged | R1 closed; its in-effect steps stay in effect | kept |
| 10 | R1 closed | `apply --step <R1 step>` | none | none | none | refused (exit 2: "run closed; re-plan") | kept |
| 11 | R1 and R2 in effect on the same file | `rollback --step <R1 step>` | as rows 5 and 7 when allowed | | | refused unless the R1 step's rollback condition holds and no in-effect R2 step's old keys meet the R1 step's new keys (R2 built on it) | as row 5 |
| 12 | R1 and R2 in effect | `rollback .` | every in-effect step of the **open** run (R2), in reverse order, in one transaction | one `rollback` per step | ended | R2's steps `rolled_back`; R1 untouched | as row 5, per step |
| 13 | R1 and R2 in effect | `rollback . --run-id R1` | none | none | none | refused while an R2 step is in effect on a file R1 touched | kept |
| 14 | any run | `rollback . --force [--run-id R]` | each file an in-effect step of R touched is restored to its text before R's first event on it (from the blob store), discarding later edits, hand edits included. Files R did not touch are never written | one `rollback` per step, `restored: true` | ended | R's steps `rolled_back` | set aside when any restored file is in its snapshot |
| 15 | `in_progress` | `--record --abort` | none | `abort` | unchanged | the previous status | kept |
| 16 | `planned` / `rolled_back` / `stale` | `--skip` | none | `skip` | none | `skipped` | kept |

`--force` with several runs (row 14) restores one run at a time, the open one by default. It does not
override row 13: rolling back R1 with `--force` while R2 is in effect is refused, and R2 is rolled
back first. Agents never pass `--force` or `--cascade`: both are refused under `--agent` and absent
from MCP.

### 4.2 Rollback rules

| Phase | Rule |
|---|---|
| 1 (stage 4): file-level | `rollback --step ID` is allowed when each of the step's files is at the step's current-epoch after-text (`sha == after`). The step must be the last change to each of those files. It restores the epoch's before-text from the blob store. Otherwise it refuses (exit 2), naming the later steps or edits on those files |
| 2 (stage 6): inverse effect | for a file that has moved on: resolve the epoch's `anchors_new` on the current file. Each must resolve, with the anchor's key, in its slot (the 3.2 rule 4 conditions). Apply the inverse effect: new → old statements; an inserted import is removed only if nothing uses it any more. Then parse with LibCST and run D's compile on the target. Any mismatch → refuse. The file-level refusal stays the safe fallback |
| Dependents | an in-effect step that `depends_on` ID → refuse, naming it. `--cascade` (person only) rolls back the dependents first, in reverse order |
| Recorded steps | the same two phases, using the record's before-text and after-text blobs |
| Ledger | a `rollback` event per step. `restored` says whether it rewrote a file. Entries are never deleted; an ended epoch's entries are out of effect (2.2) |
| Receipt | the step's `rolled_back_at` and `files_restored` |
| Later steps | not invalidated. They re-base on their next apply (1.6). A later step left in effect keeps its entries, located again by their anchors (2.3) |

## 5. Verify

### 5.1 Judging by claim: a monotone ladder (decision 3)

`facts._Provenance` (`verify/facts.py:2732-2760`, `:3062-3074`, `:2874-2880`) receives the covering
entry from `ledger_coverage` (2.3). `_judge_pairs` (`:6594-6640`) and `_single_entry` (`:6737-6753`)
then judge by its claim:

| Outcome on an edited line | hand (no entry) | `preserve` (any executor) | `intended`, unattested (agent, or no TTY) | `intended` with a person-attested expectation |
|---|---|---|---|---|
| equal | REWRITTEN equal | REWRITTEN equal | REWRITTEN equal | REWRITTEN equal |
| representation only | DRIFT | **FAIL** | DRIFT | ACCEPTED if the expectation is `representation`; FAIL if it states another outcome |
| different value | review | **FAIL** | review; **FAIL** if it contradicts a stated `representation` expectation | ACCEPTED if the expectation is met (`return:<T>`, or the fingerprint of a `verify --accept`); otherwise FAIL |
| new raise | **FAIL** | **FAIL** | **FAIL** | ACCEPTED only if `raise:<T>` or an accept fingerprint covers it; otherwise FAIL |
| unpaired | unverified | unverified | unverified | unverified |

The ladder is monotone. For every outcome, `preserve` ≥ `intended` unattested ≥ hand, in strictness.
Only a person-attested expectation (5.6) can judge more leniently than a hand edit, so nothing an
agent records can produce a better judgement than leaving the edit unrecorded. A new raise stays FAIL
unless a person attested it.

- `_STRICT_CLAIMS` (`:2710`) is removed. The v1 claims normalise to `preserve` + `basis` when loaded
  (2.5).
- `FactEntry` gains `step_id` and `accepted`. `fails` and `inconclusive` (`:491-507`) return False
  when `accepted` is set.
- `edited_sites()` (`:534-552`) gains `accepted`, and the summary reads "N edited sites: a equal,
  b representation-only, c need review, d unverified, e accepted".
- `DiffContext.successors` takes the `old_symbol → new_symbol` of every in-effect entry, recorded
  steps included. That pairing is what lowers the unverified share on agent edits (Wave G: 120 of 191
  edited sites). Section 8 pairs that measurement with a must-FAIL set, so a drop cannot come from
  weaker judging.

### 5.2 `intended`: the reviewed-change record (ACCEPTED)

Wave G left open a "reviewed-change record, keyed by symbol, site and an outcome fingerprint, that
turns a reviewed FAIL into ACCEPTED, printed, and expiring when the fingerprint changes". Here,
ACCEPTED comes only from a person-attested event (5.6).

| Expectation | Who can set it | Met when |
|---|---|---|
| `any` (the default for `intended`) | anyone | never on its own: hand rules apply until a person accepts |
| `representation` | anyone | `_representation_only(old, new)` (`verify/facts.py`, used at `:6624`) |
| `raise:<T>`, `return:<T>` | a person on a TTY only (`--record --done`, attested) | every new outcome is of that kind and type |
| fingerprint | nobody types it. `verify --accept` computes it from the observed capture | sha256 of the canonical JSON of the new outcomes (masked values), in the same captures |

- `verify --accept FINDING_ID` (a person, attested) appends an `accept` event. It carries the finding,
  symbol, both anchors, the fingerprint and **both captures' identities** (trace sha256 and
  `captured_at`).
- A later verify honours the event only if all of these hold:
  - the attestation verifies (5.6);
  - the symbol and anchors resolve to the same entry;
  - the fingerprint is equal;
  - neither capture has been replaced.

  Otherwise it has **expired**: the item is a review again, and the report says why.
- Findings get ids, `sha1(class | mechanism | symbol | file | func | site_text)[:10]`, added to
  `fact_report._finding` (`verify/fact_report.py:268-282`).
- On an edited site, the accept attaches to the covering `intended` step.
- On an unedited site (a library behaviour change the person accepts, such as django `escape`'s
  `&#39;` → `&#x27;`), it creates an ad-hoc step `x-<finding id>` (`type: review`,
  `executor: person`, `claim: intended`) that exists in the ledger only. Every ACCEPTED is therefore
  an attested intended step, never a silent pass. Section 7.2 gives the lean variant without ad-hoc
  steps.
- The verdict counts an ACCEPTED entry as compared equal. The report always prints "N accepted
  changes", each with who accepted it and when. `verify --strict` treats ACCEPTED as review.

### 5.3 `verify --step ID`

A filter over the one post-migration capture, never a new capture per step. Verify builds the whole
report as today, and the verdict still covers the whole surface. Then:

| Part | Selected by |
|---|---|
| entries | lines `ledger_coverage` assigns to the step (2.3), plus facts at the step's `where` anchors resolved on B, and their pairs |
| risks and calibration | the step receipt's persisted `risk_ids` (2.4), or `PredictedRisk.step_id == ID` before it is applied |
| coverage | the step's anchor contacts, judged in the post capture (`verify/report.py:966`) |
| step judgement | `failed` > `needs_review` > `needs_evidence` > `verified`; or `not_in_capture` |

Exit codes: 0 `verified`, 1 `failed`, 4 `needs_review`, `needs_evidence` or `not_in_capture`, 2
unknown step. Every `verify` run, with or without `--step`, saves every step's judgement in
`verification.json` under `steps: {id: {status, epoch, entries, failing, review, unexercised, risks}}`.
Status projects from it (1.3); `--step` only prints one entry.

### 5.4 Step status from verify

A saved judgement applies to a step only when all of these hold:
- it was made for the step's current epoch;
- it compared the post-migration capture (`compared.after_capture == "post-migration"`, the existing
  rule at `status.py:507-516`);
- the step's `anchors_new` resolve in P.

Otherwise the step keeps its apply-side status, or is `not_in_capture`. Steps with no sites (env)
take no verify status: `env-1` stays `applied`, and `env-2` is projected from the environment check.

### 5.5 Calibration by step

- `CalibratedRisk` and `Calibration` (`verify/predict.py:1140-1170`) gain `step_id`, `fix` and
  `by_step: {id: {hit, prevented, miss, not_exercised, accepted}}`.
- **A fix is credited at verify time, by statement.** Three conditions:
  1. the risk is linked to a step (persisted `risk_ids`);
  2. the step is in effect;
  3. in P, the step's effect **removed or replaced the risk's statement**: the risk's `where`
     anchor (or, for an import-bound symbol, the binding import) does not resolve in P at its slot,
     and the step's matching `anchors_new` statement does (or the statement is gone, for a
     deletion).

  `fix` is then `fixed`.
- A linked step in effect whose effect did not touch the risk's statement in P is
  `fix: recorded-not-fixing`. It is scored as unfixed (hit or miss), never prevented.
- `fixed_by_plan` from plan time no longer decides `prevented` (`calibrate`, `:1239-1244`). A risk
  whose step was skipped or rolled back is unfixed. A manual step a person recorded can score
  `prevented`.
- A risk at an ACCEPTED entry scores `accepted`, a fifth result.
- H4's import-move credit is the binding-import case of rule 3: a zipline-style import move fixes its
  risk when the move is in P.

### 5.6 Trust and threat model

**What the gates are for.** The gates in this spec prevent accidents: an agent following briefs
through `--agent` or MCP, a person running the wrong command, a stale plan. They do not stop an
adversarial agent. An agent with a shell in the project can:
- edit any file under `.pymolt/` (ledger, receipts, approvals);
- run any command without `--agent`.

**One real mechanism, for the one event that can judge an edit more leniently than a hand edit:** a
person attestation.

| | Rule |
|---|---|
| Attested commands | `verify --accept`, `apply --approve-agent`, and `apply --step X --record --done` with `--by person`, `--claim intended` or a person-only `--expect` |
| Conditions | stdin and stdout are TTYs; not `--agent`, not `--json`, not MCP. pymolt prints the item and a random nonce, and the person types the nonce back |
| Record | an attestation `{id, project: sha256(realpath), plan_id, step, finding, fingerprint, captures, at, user}` appended to `$XDG_STATE_HOME/pymolt/attestations.jsonl` (default `~/.local/state/pymolt/`), outside the project tree. It carries `hmac = HMAC-SHA256(key, canonical record)`, with the key at `$XDG_CONFIG_HOME/pymolt/attest.key` (32 random bytes, created `0600` on first use). The ledger event stores only the attestation id |
| Verify | honours `attested`/ACCEPTED only when the out-of-tree record exists, its HMAC verifies, and its fields match the ledger event and the current captures. Anything else counts as unattested (at most the hand judgement), and the report says why |
| Scope | attestations are per machine and per user. On another machine (CI) an accepted item is a review again. `.pymolt/` is local state anyway: CI runs phases in fresh checkouts |

| It defeats | It does not defeat |
|---|---|
| a non-interactive agent (subprocess, MCP) producing ACCEPTED; forged in-tree JSON (`attested: true`, a copied accept event); a fingerprint copied from another finding or capture (bound to finding and captures); an approval written by hand | an agent with a pseudo-terminal that reads and types the nonce; an agent with read access to the key in the user's home; a person accepting carelessly |

**What an adversarial agent can still do, and why it never yields PASS:**
- **Delete or rewrite ledger entries.** Without an attestation the ladder never goes below the
  hand judgement. Deleting a `preserve` entry turns a FAIL on a different value into a review
  (INCONCLUSIVE), never PASS. A new raise and every change on unedited code still FAIL.
- **Restructure code so calls stop pairing.** The result is "unverified edited sites", which do not
  block PASS (`docs/verify.md`, edited lines). This is the existing hand-edit limit, unchanged here.
  Section 9 lists it.

Stage 5 adds adversarial tests (8) for each row of the "defeats" column and for the three cases that
must never PASS:
1. a new raise recorded as `intended`;
2. a copied fingerprint;
3. an unrecorded edit beside a recorded one.

## 6. Status and the next command

`build_status` (`status.py:390-549`) adds a `steps` phase between `plan` and `migration`:
- `detail` holds counts by status, e.g. "6 steps: 3 applied, 1 recorded, 1 planned (person),
  1 stale";
- `FunnelStatus.steps` lists `{id, status, executor, title}`;
- `status --agent` includes them, capped, with the next step's brief.

**When the post-migration capture is stale** (this replaces the `receipt.applied_at` test at
`:478-486`): when `sources_changed(project, post.sources_path)` (`verify/service.py:1951`) is not
empty, that is, when a file the capture recorded no longer has the recorded text. It does not matter
whether a later step, a rollback, a re-record or a hand edit changed it. Hashes are cached by
`(mtime_ns, size)`, so status stays cheap.

After a rollback, that rule gives:
- the capture was set aside (row 5 of 4.1): no post capture;
- a step applied after the capture is rolled back: the file is back at the text the capture recorded,
  so the capture is not stale;
- a rollback with `restored: false`: not stale.

With no receipt and no recorded step, a valid post capture is not stale, and the report can be done
(H7).

`_decide_next` (`status.py:552-653`) keeps its existing rules up to the baseline. After them, the
first match wins:

| # | When | Next command |
|---|---|---|
| 1 | no v2 plan, no receipt, and no post capture newer than the baseline | `pymolt plan .` (unchanged) |
| 2 | run-level stale | `pymolt plan .` |
| 3 | a step `in_progress` | `pymolt apply --step X --record --done` |
| 4 | pymolt steps `planned` whose dependencies are in effect | `pymolt apply .` (the reason lists them) |
| 5 | a pymolt step `stale` | `pymolt apply --step X --dry-run` (shows the lost anchor), then `pymolt plan .` |
| 6 | the first agent or person step `planned` with its dependencies in effect, in plan order | `pymolt apply --step X --record`, and its `--brief` |
| 7 | the post capture is missing or stale | `pymolt capture post-migration …` |
| 8 | no verdict on that capture | `pymolt verify .` |
| 9 | a step `failed` | for a pymolt step, `pymolt rollback --step X`; for an agent or person step, fix it, then `pymolt apply --step X --record` again |
| 10 | a step `needs_review` | `pymolt verify . --step X`, then `pymolt verify . --accept ID` (a person, 5.6) |
| 11 | a step `needs_evidence` | the existing replicate rule when the reason is noise; otherwise "add a test that reaches X's sites, then capture post-migration again" |
| 12 | anything else | done (wording unchanged) |

Rules 4–6 apply only while a receipt exists or no post capture newer than the plan exists. A
migration done entirely by hand (H7) goes straight to rules 7 and 8, and its edits are judged as
hand edits.

## 7. Owner decisions and lean variants

### 7.1 Decisions

**Decided by the owner on 2026-11-17: every default below, as written. The full variants were chosen over the lean ones.**
- `manifest-pin` is a pymolt step (decision 1).
- Agents run pymolt-checked steps after one person-attested approval per plan (variant A, decision 4).
- Re-basing, stage 6, ad-hoc accepts and the text store stay in scope.
- Decision 5 (an imports-only baseline) stays the optional stage 8.

The "if the owner decides otherwise" column is kept as a record of the alternatives.

Each row names the default this spec is written for, and exactly what changes otherwise.

| # | Decision | Default | If the owner decides otherwise |
|---|---|---|---|
| 1 | apply writes the manifest bump: pinned requirements-style files only, reversible, in the ledger | yes. `manifest-pin` is a pymolt step. It rewrites lines `name[extras]==X [; marker]` with no `--hash` to the target pin, for packages on both sides. Other lines, other manifest kinds (pyproject, poetry, conda) and hashed files get a person step with the exact lines. Installs and environment builds stay in assess | `manifest-pin` is always a person step (recordable, 3.3), and apply never writes a file that is not `.py`. Lean variant "manifest pin" (7.2). `docs/commands.md` documents the trap: bump after apply and before the post capture. `bench/e2e_pandas` keeps its manual bump (`run.py:183-184`), wrapped in `--record` in stage 5 |
| 2 | the unit of apply is a step anchored on symbol + file + function + statement | yes (1.4) | Steps anchor on lines. Stage 1's anchors module is dropped, and B0-1 is fixed by mapping lines through the diff. Risk ids keep the line. The lean "re-basing" variant becomes forced, and stage 6 is dropped. `ledger_coverage` locates entries by their written ranges mapped through the one alignment (2.3), not by anchors |
| 3 | verify judges an edit by its claim, not its author | yes: the ladder of 5.1 | Recorded agent and person edits are judged as hand edits, whatever their claim. Their pairing anchors still count. Strict judging stays only for the four pymolt bases. Person-attested ACCEPTED (5.2) remains. Stage 5's `feat/verify-judge-by-claim` shrinks (M → S), and the lean "agent-settable claims" variant is selected |
| 4 | agents may run pymolt-checked steps with one approval per plan (`--agent`; MCP optional and gated) | yes: variant A (3.6) | Variant B (3.6), lean variant "agents" (7.2) |
| 5 | Q1: an imports-only baseline for plans of Python fixes only, verdict capped below PASS | yes, not urgent | Under yes, optional stage 8: apply accepts a baseline captured with `--mode imports` when every selected step has `scope: python` and `basis: python-fixer`; `runs[].baseline_kind: imports`; verify caps the verdict at INCONCLUSIVE ("imports-only baseline"). Under no: stage 8 is dropped and nothing else changes |
| 6 | Q2: install and import check on by default | yes in the CLI with an opt-out; off over MCP unless `PYMOLT_MCP_ALLOW_EXEC`; always "not checked" when skipped | The same choice sets D's smoke-import default after a write (3.2, step 8). The opposite makes it opt-in, with receipts reading "smoke_import: not checked: off by default". This spec's code does not change; D's flag does |
| 7 | INCONCLUSIVE split into "needs a person" (review) and "needs evidence" (BLIND, uncomparable) | proposed: split | With the split: step statuses `needs_review` and `needs_evidence`, next-command rules 10 and 11, and `verification.json` gains `inconclusive_kind`. Exit code 4 stays for both. Without it: one `inconclusive` step status, and rules 10 and 11 pick the command from the reason text |

### 7.2 Lean variants (presented, not adopted)

The owner has put B and C in scope. Each row sets the full design, which stays the plan, next to a
lean variant a scope cut could select. Sizes include tests.

| Item | Full (the plan) | Lean variant | Lean saves | What the lean variant gives up | Selected by |
|---|---|---|---|---|---|
| Re-basing apply | anchors + a fresh recipe run + effect equality (`feat/apply-step-rebase`, ~400) | fast path only: write when `sha == before_sha256`; anything else is stale → re-plan | stage 4, −400 | Any line shift costs a re-plan (4.6 min on theano, 5.4 on zipline): an earlier step skipped or rolled back out of order, a hand edit elsewhere, reformatting. H2's re-apply after a rollback works only on exact hashes | decision 2 = yes: either; **full is the default**. Decision 2 = no: lean, forced |
| Stage 6, inverse-effect rollback | stage 6 (~500) | file-level rollback only (4.2, phase 1) | −500 | A step whose file moved on cannot be rolled back alone: roll back the later steps first (`--cascade`), or the whole run with `--force` | no decision; **full stays the plan** |
| Manifest pin | a pymolt step (`feat/apply-env-manifest-pin`, ~300) | a person step, recordable (~100) | −200 | The bump-after-apply trap stays a documented manual step | decision 1 = yes: **full**; = no: lean |
| Agents | variant A: approval, `apply`/`rollback --agent`, MCP write tools, gated (~600) | variant B: record, brief and dry-run only (~200) | −400 | Agents cannot run pymolt steps; a person runs `apply .` | decision 4 = yes: **A**; = no: B |
| Agent-settable `--claim`, `--expect`, `--pair` | the CLI under `--agent` may set them, within the ladder (5.1): they can only keep or tighten a judgement. MCP never takes them | the plan's claim and pairing anchors only; the flags are person-only (attested) | −150, one test class | An agent cannot add a pairing anchor or tighten a claim, so more of its edits stay unpaired (unverified) | decision 3 = yes: **full**; = no: lean (claims no longer change judging) |
| Ad-hoc `x-<id>` accept steps | an accept on an unedited site creates a ledger-only intended step (5.2) | accepts only on sites a plan step covers | −150 | A deliberate library behaviour change on unedited code (django `escape`) cannot be accepted, so the verdict stays FAIL | no decision; **full stays the plan** (this spec's choice) |
| Ledger text store | content-addressed blobs of every before/after text and snapshot, retained as in 2.1 (~250) | hashes plus each entry's anchors and statement keys. Blobs only for an `in_progress` snapshot, deleted at `--done` | −250, and no source copies under `.pymolt/` | File-level rollback of a re-based or recorded step (its before-text is gone). Only stage 6's inverse effect can undo it, or it is refused. `--force` can restore only text the plan holds (T0). No exact-text audit. With stage 6 also cut, pymolt cannot roll back a recorded step at all | no decision; **full stays the plan** |

The person attestation (5.6) has a lean form too: a TTY check alone, with the record kept in the
project tree. It defends against accidents only; this spec selects the full form.

| Stage | Full | Lean (every cut above) |
|---|---|---|
| B0 | S ~200 | S ~200 |
| 1 anchors | M ~800 | M ~800 (anchors still serve coverage, credit and `--step`) |
| 2 work order | L ~2,000 | L ~2,000 |
| 3 ledger and receipts | L ~1,400 | M ~1,150 |
| 4 stepwise apply and rollback | L ~1,900 | L ~1,300 |
| 5 every executor | L ~2,300 | L ~1,600 |
| 6 inverse-effect rollback | M ~500 | dropped |
| **Total (B0–6)** | **~9,100** | **~7,050** |
| 7 test ids (optional) | M ~400 | M ~400 |
| 8 imports-only baseline (optional, decision 5) | S–M ~350 | S–M ~350 |

### 7.3 Choices this spec makes on its own (the owner may veto each)

| Choice | Alternative |
|---|---|
| one invocation = one transaction over its steps (keeps "all or nothing", `docs/plan.md`) | commit each step on its own, keeping the ones before a refusal |
| PLAN.md at `.pymolt/PLAN.md` | the project root (it would show in `git status`) |
| person attestation with an out-of-tree HMAC log (5.6) | a TTY check only (accidents only) |
| an accept expires when either capture is replaced | an accept that survives a re-capture with an equal fingerprint |
| test ids not recorded (stage 7 optional) | stage 7 inside this scope |

## 8. Staged implementation plan

Each stage is a stack of one-commit branches named `<type>/<scope>-<slug>`. The stages octopus-merge
into `integration/apply-work-order`, which is stacked on the H/D tip. Sizes include tests: S < 300
lines, M 300–1,000, L > 1,000.

**Guards.** Each one, once a stage introduces it, must stay green in every later stage.

| Guard | What | Requirement | From stage |
|---|---|---|---|
| suite | `uv run pytest -q` | all pass; ruff on touched files no worse than at HEAD | all |
| e2e | `python bench/e2e_pandas/run.py run` | 4/4; `append` prevented, `mean` hit | all |
| Wave G | `python bench/generalization/run.py run --out …`, then `diff` against `results/waveG` | no equiv FAIL; every change scenario FAILs on its expected symbol; recall unchanged | all |
| legacy | `bench/legacy_eval/run.py` against `results/after-waveG` | the outcome cells are unchanged, unless the stage says otherwise | 2 |
| perturbation | `tests/workorder/test_anchor_perturbation.py`: each corpus file is formatted with black and with `ruff format`, quotes swapped, trailing commas toggled, redundant parentheses added, re-indented, comments and blank lines inserted, long calls wrapped | every anchor resolves to the statement with the same key and neighbours, or is reported ambiguous; never to another statement | 1 |
| metamorphic | `tests/workorder/test_metamorphic.py`, on the e2e and Wave G toys and on fixtures with mixed files | `apply .`, `--step` one at a time in plan order, `--through <last>`, and an order permuted within `depends_on` give identical files, identical `ledger_coverage` and identical verdicts | 4 (coverage already in 3) |
| state machine | `tests/workorder/test_state_machine.py`: 500 seeded random sequences (stdlib `random`, no new dependency) of apply, rollback, record, abort, skip, re-plan and hand edits over two runs | invariants: at most one `in_progress` per run; in-effect entries are exactly the current epochs; a file-level rollback restores the epoch's before sha; `project_steps` is total and deterministic; every transition is a row of 4.1 | 4 |
| concurrency | `tests/workorder/test_concurrency.py`, two processes | apply versus record, two records, rollback versus apply: the second is refused by the lock. An interrupted invocation (killed between write and ledger) is recovered. `status` during an apply reads a consistent state | 4 |
| adversarial claims | `tests/workorder/test_adversarial_claims.py` | never PASS: a new raise recorded as `intended` (agent, non-TTY, any `--expect`); a copied fingerprint (another finding, another capture, or written into the ledger); an unrecorded edit beside a recorded one; `attested: true` forged in the ledger; a deleted `preserve` entry (must be review, not PASS). MCP `apply` with `claim`/`expect`/`force` is rejected. `--expect raise:` under `--agent` is refused | 5 |
| Wave G record mode | `bench/generalization/run.py --record`: `manual_migration` edits wrapped in `apply --step … --record` / `--done` | **paired with a must-FAIL set**: every change scenario still FAILs on its expected symbol in record mode, and a mutation per toy with manual edits (a changed return value or a new raise, recorded as `preserve`, as agent `intended`, and left unrecorded beside a recorded edit) FAILs or stays INCONCLUSIVE, never PASS. Only then does a drop in unverified edited sites below 120 of 191 count | 5 |
| runtime | 3.6 parse of the injected runtime; `bench/load` | stage 7 only | 7 |

| Stage | Branches (one commit each) | Tests | Size (full / lean) | Depends on; what runs in parallel |
|---|---|---|---|---|
| **B0** defects | `fix/pyversion-fix-sites-after-dependency-rewrite` (B0-1); `fix/plan-recipes-adopted-only` (B0-2) | the shift reproduction (0.3); a blocked rule absent from `recipes`/`claims`; Wave G plan `changes` identical on all 10 toys; legacy plans differ only by the listed B0-1 diffs | S ~200 / same | the H tip. The two in parallel; either could also join the H stack |
| **1** anchors | `feat/plan-statement-anchors` (`pymolt/anchors.py`: AST keys with `feature_version`, the token fallback, docstring `cleandoc`, neighbours, `nth`/`count`, ambiguity; `black` added to the dev group); `refactor/predict-risk-id-from-anchor` (`predict.py:433-435`, `RiskSite.nth`) | the perturbation guard, with the black and `ruff format` round trip; duplicate statements told apart by neighbours; an ambiguous tie refused; decorators, multi-line statements, class bodies; comprehension `func` joins on 3.11 and 3.12; 3.6-only syntax (`async` used as a name); risk ids stable under shifts | M ~800 / same | B0-1 may use it. Can start alongside B0 |
| **2** work order | `feat/plan-schema-v2-steps` (models, schema file with its equality test, v1 adapter); `feat/plan-step-builder-automated` (`on_step` observer, chains, grouping, order, anchors per before-text, `runs_on_base`, the `changes` oracle); `feat/plan-step-depends-on` (effect overlap, shared inserted imports, the same-scope fallback, the re-run oracle as a test); `feat/plan-step-builder-manual` (risks to steps, H5 items, advisories, `step_id`, persisted risk ids); `feat/plan-step-verify-effort` (1.5.5); `feat/plan-env-steps` (`manifest-pin`, `target-env`); `feat/plan-workorder-render` (PLAN.md, briefs with the untrusted-text rules, the plan summary by step, `plan --agent` steps, MCP parity) | Tn == v1 `new_source` on fixtures and on the e2e and Wave G toys; dependencies on crafted overlaps and shared imports; the fallback; a schema round trip; a v1 plan → step `v1`; a PLAN.md golden file; briefs with control characters, bidi characters, backtick runs and over-long warnings | L ~2,000 / same | stage 1. Models first. Then the automated builder, verify/effort, env steps and render (against model fixtures) in parallel; depends_on and the manual builder after the automated builder. Guards: e2e and Wave G `changes` byte-identical; legacy plan time no worse than +10% (theano 4.6 min, zipline 5.4 min), step counts in the scorecard |
| **3** ledger and receipts | `feat/ledger-v2-epochs` (events and entries, epochs, the v1 reader); `feat/pymolt-blob-store` (blobs, `.pymolt/.gitignore`, retention and collection); `feat/migration-receipt-step-set` (v1 status values kept plus `progress`, runs, steps, risk links, readers switched to `in_effect`); `feat/verify-ledger-coverage` (2.3: one alignment, anchor location, strictest-claim conflicts; replaces H8's scoping) | **apply → rollback → re-apply with a different re-base (a hand edit above in between): coverage sees exactly one generation**; re-record supersedes; the v1 → v2 reading of all three files; two runs plus a hand edit after apply; strictest-claim conflicts; a step after the capture → `not_in_capture`; blob retention | L ~1,400 / M ~1,150 (no text store) | the stage 2 models, not the builder, so it runs alongside the rest of stage 2. Ledger, blobs and receipt in parallel; coverage after the ledger and H8. Guards: Wave G verdicts and edited-site counts unchanged |
| **4** stepwise apply and rollback | `feat/apply-step-select` (3.1–3.4: selection, transaction, fast path, rules 1, 2, 4 and 5, idempotency, refusals, exit codes, D per step, the baseline check for invocations 2..n); `feat/apply-step-rebase` (rule 3); `feat/apply-env-manifest-pin` (decision 1); `feat/rollback-step-file-level` (4.1 rows 5–16, 4.2 phase 1, `--force` across runs, cross-run refusals, the capture rule, refusals under `--agent`); `feat/status-step-projection` (`project_steps`, the anchor cache, sources-based post staleness, section 6) | every row of 4.1; rule 4 false positives (the scope does not resolve; the new statement elsewhere in the scope); a stale anchor; a re-base after a skipped earlier step; the manifest pin and its rollback; the next command for each rule of 6; the metamorphic, state-machine and concurrency guards | L ~1,900 / L ~1,300 (no rebase, person pin) | stages 2 and 3. Step selection first; then rebase, the pin, rollback and status in parallel. Guards: e2e through `apply .` and through `--through mech-1`, files byte-identical to a v1 apply; legacy apply 6/6 byte-identical, and apply → rollback → apply on zipline with no re-plan |
| **5** every executor | `feat/apply-record-step` (3.3: one `in_progress` per run, blob snapshots with a manifest, epochs, re-record, `--skip`, `--abort`); `feat/verify-judge-by-claim` (the 5.1 ladder); `feat/verify-person-attestation` (5.6: TTY nonce, out-of-tree HMAC log); `feat/verify-accept-review` (5.2: finding ids, accepts bound to both captures, ad-hoc steps, expiry); `feat/verify-step-filter` (5.3–5.5: the statement credit, `recorded-not-fixing`); `feat/apply-agent-view` (3.6 in the chosen variant, the MCP tools without forbidden parameters, the smoke-import receipt note) | the adversarial-claims guard; a record with and without `--record` first; out-of-scope hunks left unrecorded; a second `--record` refused; a `preserve` FAIL on an agent edit; each expectation kind, attested and not; accept → expiry when the fingerprint or a capture changes; `--step` selection and exit codes; calibration by step (skipped → unfixed, an unrelated record → `recorded-not-fixing`, a recorded fix → prevented); an approval voided by a re-plan | L ~2,300 / L ~1,600 (variant B, no agent claims, no ad-hoc) | stage 4 (record, agent view) and stage 3 (step filter). Record and the ladder in parallel; attestation in parallel with both; accept after the ladder and attestation; agent view after record. Guards: e2e plus a variant where `beh-1` is recorded (`mean` prevented in `change`); Wave G record mode with its must-FAIL set; default mode unchanged |
| **6** inverse-effect rollback | `feat/rollback-step-inverse-effect` (4.2, phase 2) | rollback with later steps and hand edits elsewhere in the file; a slot mismatch → refusal; an import removed only when unused; state-machine sequences extended to it | M ~500 / dropped | stage 4. Alongside stage 5 |
| **7** test ids (optional) | `feat/capture-site-test-ids`: in tests mode the runtime keeps up to 3 `PYTEST_CURRENT_TEST` values per site, stdlib only and 3.6-safe; `verify.tests` gets filled | a 3.6 parse; the tox matrix; ids on the e2e toy | M ~400 / same | independent; after stage 2's models. Runtime guards |
| **8** imports-only baseline (optional, decision 5) | `feat/apply-imports-only-baseline` | 7.1 row 5 | S–M ~350 / same | stage 4. Legacy: vibora's apply, refused today for lack of a baseline, now applies with a capped verdict |

Each stage carries its own documentation:
- `docs/plan.md` (steps, PLAN.md);
- `docs/commands.md` (options);
- `docs/agents.md` (decision 4, the threat model);
- `docs/verify.md` (the ladder, ACCEPTED, `--step`);
- `docs/limits.md`;
- `docs/glossary.md` (step, claim, executor, anchor, epoch, accepted);
- the README usage block (`apply .` stays the one-line path).

Critical path: H/D → B0 → 1 → 2 (models) → 3 → 4 → 5. Parallel lanes:
- stage 3 alongside the rest of stage 2;
- stage 6 alongside stage 5;
- stage 7 from stage 2 on.

## 9. Risks, and what is out of scope

| Risk | Mitigation |
|---|---|
| Anchor ambiguity: identical statements, generated code, comprehension `func` names that differ by Python version | neighbour fingerprints, then `nth` only when `count` matches, else a refusal; never a guess. Joins try both `func` forms. An unresolved anchor is stale, never re-based by line |
| A source the host cannot parse (old syntax) | `feature_version` per side; the token fallback, which says it is weaker (`key_kind: tokens`) |
| A re-base accepts a statement whose context changed its meaning (another binding) | the recipe is re-run on the current file, binding-aware (ScopeProvider), so equality covers the new context; D compiles |
| difflib misaligns repeated lines | one alignment per file, the one provenance uses, so coverage and provenance cannot disagree. Entries are located by anchors, not by difflib. The limit is documented, not hidden |
| Plan time: replay per recipe | the observer adds no parse beyond today's chained runs; `depends_on` is set intersections; legacy guard +10% |
| Plan size: hundreds of manual steps (zipline: 515 risks need attention) | grouping per (scope, package, symbol, fact); PLAN.md collapses low and BLIND steps; view lists stay capped |
| Strict judging of agents' `preserve` claims raises the FAIL count on agent work | as intended: a preserve claim that does not hold is a finding. The guard is no equiv FAIL in Wave G record mode |
| An adversarial agent (5.6) | the ladder keeps every unattested record at or above the hand judgement; attestation is out of tree with an HMAC. Not defeated: a pty agent typing the nonce, or one reading the key |
| Unpaired edited sites do not block PASS (existing hand-edit rule) | unchanged here; counted and printed; decision 7's `needs_evidence` makes them visible per step. A rule that unpaired sites block PASS is a separate decision |
| ACCEPTED hides a regression | person-attested only; bound to the finding, fingerprint and both captures; expires; always printed; `--strict` |
| Snapshot size on large repos | blobs de-duplicate; past 50 MB of new bytes, files are listed by hash and their edits judged as hand edits, with a note |
| Source copies under `.pymolt/` | `.pymolt/.gitignore` covers `blobs/`, `cache/` and `migrations/*/steps/`; retention as in 2.1 |
| v1/v2 skew between benches and tools | v1 fields kept, with their v1 values; v1 files read through adapters; bench readers unchanged |
| Two stacks in flight (H/D and B) | B builds on H's interfaces as listed in 0.2. H3's fix at `migration_plan.py:616`, the receipt changes and the ledger changes land before stage 3 |

Deliberately out of scope:
- environment builds and installs (Wave 3). `target-env` is a person step; apply runs D's smoke import
  only when an environment exists;
- git commits per step, or a patch series. A later export (`apply --export-patches`) is possible,
  because the ledger keeps every epoch's hunks in order;
- incremental re-planning, which would recompute only the stale steps;
- applying part of a step (a subset of its files);
- pymolt writing agent or person steps: it suggests, and never writes them;
- bumping pyproject, poetry or conda manifests: a person step;
- defending against an agent with the user's full privileges (5.6);
- test-id attribution, unless stage 7 is approved.

## 10. Review findings closed (2026-11-17)

| Finding | Closed in |
|---|---|
| B1 epochs | 2.1 (`epoch`, current epoch), 2.2, 3.3 (re-record supersedes), 4.1 rows 5–8; test in stage 3 (apply → rollback → re-apply, one generation) |
| B2 monotone ladder, threat model | 5.1 (ladder), 5.2 (who can set which expectation; fingerprints only from `verify --accept`), 5.6 (threat model, TTY nonce, out-of-tree HMAC), 3.1 (agent and MCP flag limits), 3.3 (no-edit `--expect` escape removed); adversarial-claims guard (8) |
| B3 anchors | 1.4 (AST keys, token fallback, neighbours, `count`, ambiguity, anchors per before-text), 1.5.2, 3.2 rule 4 (scope and slot required); perturbation guard and black / `ruff format` round trip (stage 1) |
| M1 one alignment | 2.3 (`_line_map(B, P)` once per file, anchor location, strictest-claim conflicts); metamorphic guard (8) |
| M2 one record at a time, blob snapshots | 3.3, 2.1 (blob store, per-step manifest) |
| M3 capture after rollback | 4.1 row 5 (set aside only when the step is in the snapshot and the capture postdates it), 6 (the sources rule) |
| M4 step key, persisted links | 1.2 (`key` without anchors), 1.6 (re-plan), 2.4 (`risk_ids` per step), 5.3 |
| M5 credit by statement | 5.5 (`fixed` / `recorded-not-fixing`) |
| M6 state table, `--force`, agents | 4.1 (16 rows, two runs), 4.2, 3.6; state-machine guard (8) |
| M7 untrusted text, MCP limits | 1.10, 3.1, 3.6, 3.2 step 8 (smoke-import note under MCP) |
| M8 `depends_on`, `runs_on_base` | 1.5.3, 1.2, 1.7 |
| M9 baseline check for invocations 2..n | 0.2 (H3 row), 3.2 step 2 |
| Minors | v1 receipt `status` kept plus `progress` (2.4); stale check cached by file sha (1.3); `.pymolt/` and the text store (0.1, 2.1); plan-time risk ids persisted (1.2, 2.4); `not_in_capture` by anchor (1.3, 2.3, 5.4); accepts bound to both captures (5.2); no-edit `--expect` escape removed (3.3) |
| Guards | the perturbation, metamorphic, state-machine, concurrency and adversarial-claims guards, and Wave G record mode with its must-FAIL set (8) |
| Scope cuts | presented as lean variants with sizes and selectors (7.2), not adopted |
