# Named, versioned and cumulative plans; one human report; a README by scenario

Status: design, 2026-11-22. Base: `integration/release-0.2` (8d797c4). Target: 0.3, except the
three items in 8.1, which land in 0.2.

The owner's request (2026-11-21):
1. The README is a starting point. It must describe scenarios, from the most basic to advanced.
2. PLAN.md has an unacceptable name and is poorly versionable: no date, no time, no name of the
   capture it came from. A plan can be followed by a hand edit that calls for a new plan, based on
   the previous one or fresh. A plan can also be cumulative: over a month of CI runs, N needed
   changes accumulate, then the team does them.
3. PLAN.md is written in internal language. Align it with established standards and schemas.
4. A person who does not read code has no summary. This is critical: move from Markdown to HTML,
   as pytest-html and coverage.py do, with charts, so the report can be attached to a report or a
   ticket.

How this design was made: web research on standards and prior art; a fact report on how plans are
identified and replaced today; two independent designs (one Claude, one Gemini) on the same brief,
merged here. Where they differed, the choice and the reason are given.

Owner decisions (2026-11-22):

| Question | Decision |
|---|---|
| Command for the report | A new read-only command, `pymolt report` (the 13th). `plan` and `verify` also refresh the report, so the basic flow has no extra step |
| Charts | SVG that pymolt generates itself, through a small chart module inside pymolt. No interactive charts are needed now, but the design leaves room for them (5.4) |
| Cumulative plans | Built from CI artifacts by `pymolt report --combine`. No stored backlog |
| Before 0.2.0rc1 | The three small items of 8.1. Everything else is 0.3 |

## 0. Starting point

| Fact | Where |
|---|---|
| `plan_id` is a random uuid4, new on every `plan` run, even for identical content. It is also the run id of the plan's first apply run, and approvals bind to it | `migration_plan.py:213`, `workorder/apply.py:181-195`, `workorder/approval.py:33-36` |
| The plan records `created_at`, but no pymolt version and no git commit. The baseline is bound by trace path, `captured_at` and the trace's sha256. Captures have no id | `migration_plan.py:208-250`, `:385-399` |
| One fixed plan file, overwritten by every `plan` run, never archived. An applied plan survives only as `migrations/<run>/plan.json` | `migration_plan.py:43`, `:288-296`; `workorder/apply.py:610-612` |
| PLAN.md's header shows the id, the moves and the raw baseline timestamp; not `created_at` | `workorder/render.py:145-156` |
| Step ids are positional (`mech-1`, `beh-2`). Each step has a `key` (sha1 of type and ident) meant to survive a re-plan; it is written, never read | `workorder/models.py:322-330` |
| Risk ids are content-based, without line numbers | `verify/predict.py:644-649` |
| Planning reads the disk, the baseline slot and `env_config.json`, never the receipt or ledger. No plan knows its predecessor. The cross-run links of apply-work-order §1.6 were designed and never built | `migration_plan.py:385-510`; `apply-work-order.md:334-340` |
| One active baseline. Captures from several runs are never combined | `verify/models.py:246-257` |
| No HTML, no templates. Runtime deps: pydantic, typer, rich, griffe, libcst, packaging | `pyproject.toml` |
| `status` reports the plan phase DONE whenever a run is applied, before it looks at the saved plan (fixed in 8.1) | `status.py:367-375` |

## 1. Identity and naming

- **`plan_id` stays a uuid4.** It is the run id and the key of approvals. Replacing it with a
  structured id (the Gemini design) would break those bindings for no gain: the name below gives
  people a readable identity.
- **Schema 3 adds:** `name`, `label`, `pymolt_version`, `source {git_commit, git_dirty,
  git_prefix}`, `baseline` (a `CaptureRef`), `lineage` and `content_digest`.
  - git facts come from a new `adapters/git.py`, which reads the local repository only and never a
    remote URL.
  - `CaptureRef {capture_id, captured_at, trace_sha256, env_fingerprint, env_setup_fingerprint,
    command}` is copied from the baseline slot. The plan-time fingerprints stay beside it, so a
    mismatch shows.
- **Plan name:** `YYYYMMDDTHHMMZ-<id8>[-<label>]`, e.g. `20261120T0631Z-f74941c5-numpy2`. It sorts
  by time, is safe as a file name, and stays unique across a CI matrix (time alone would collide).
- **Capture id:** `<captured_at as YYYYMMDDTHHMMSSZ>-<trace sha256[:8]>`, e.g.
  `20261120T060620Z-b3e1a2c0`. It names content, not a path, so it survives archiving. It is stamped
  on the slot at capture (an additive field) and computed on read for older slots. An environment
  hash (the Gemini design) is rejected: a baseline and its replicate share it.
- **Layout:**
  - `.pymolt/plans/<name>/plan.json`: immutable, committable;
  - `.pymolt/plans/<name>/report.html` and `summary.md`: regenerable, gitignored;
  - `.pymolt/plans/current.json`: the pointer, written through `state_io` under `state_lock` by
    `plan` and by the first `apply` of another plan. No symlinks (Windows, git).
  - `plan_path(root)` (`migration_plan.py:120`) resolves the pointer, so the callers of
    `MigrationPlan.load(root)` do not change.
- **An unchanged re-plan saves nothing.** When `content_digest` (which excludes ids and times) and
  the inputs (capture id, setup fingerprint, target-manifest sha) equal the current plan's, `plan`
  says so and keeps the current plan and its approvals.
- **What a person sees:**
  - terminal: `plan f74941c5 · 2026-11-20 06:31 UTC · baseline 20261120T060620Z-b3e1a2c0 ·
    commit 1a2b3c4 (dirty) · derived from 3c9e0d12`;
  - ticket title: `PyMolt plan 2026-11-20 f74941c5: pandas 1.5.3→2.2.3, Python 3.11→3.12, 7 steps`.
  - Commands accept an id prefix, a name, a path or `current`.
- **Branch drift:** when `current.json` names a plan made on another commit than `HEAD`, `status`
  says so (an advisory, not a gate).
- **Retention:** the current plan and its ancestors, every plan a run references, and the 20 newest.
  `plan` prunes the rest after its blob GC.
- **Migration from schema 2:** with no pointer, the legacy `migration_plan.json` is read through an
  adapter (as `workorder/v1.py` reads schema 1), as a `fresh` plan of unknown version. Only `plan`
  moves the file. `plan_digest` keeps hashing the stored JSON, so upgrading voids no approval.
- 0.2 stays strict: it refuses a schema-3 plan as newer (`StateFileError`). Reading unknown fields
  silently (`extra="ignore"`, proposed by the Gemini design) is rejected: refusing a newer file is a
  project principle.

## 2. Lineage and re-planning

- **A plan is always recomputed** from the disk, the baseline and the environment; pymolt never
  patches a saved plan (`apply-work-order.md:126`, `:1194`). A reconciliation against the parent
  follows the computation.
- **Derived** is the default when a current plan exists: `lineage {root, parent, generation}`, the
  role Terraform gives a state's lineage and serial. **Fresh** (`--fresh`, or no plan on disk, as in
  CI) inherits nothing.
- **Matching:**
  - steps pair by `key`: type plus package, symbol, fact kind and parameter, or the recipe key. No
    anchors and no versions in it. Putting anchor keys into the step fingerprint (the Gemini design)
    is rejected: one added site would make the whole step "new", while a person tracks a kind of
    change;
  - inside a pair, sites compare by risk id for agent and person steps (built from the baseline's
    text, so hand edits keep it), by effect keys for pymolt steps, by pins for manifest steps;
  - a key is unique within a plan (a test, 8.1).
- **Diff vocabulary:** SARIF `baselineState`: `new`; `unchanged`; `updated`, naming what changed
  (sites, tier, default, claim, executor or recipe); `absent`, split into `fixed` (credited to an
  in-effect step by the statement test of apply-work-order 5.5, or verified) and `gone`.
- **Ids:** a matched step keeps its parent's id. A new step takes the next number unused in the
  lineage, so `beh-3` keeps one meaning across derived plans.
- **What carries over:**

  | Item | In a derived plan |
  |---|---|
  | In-effect steps of earlier runs, with records and attestations | stay in their run's ledger and are listed as done; their risks link to them (the §1.6 links) and get no new step |
  | Accepted findings | hold while their anchors, fingerprint and captures hold (`verify/accepts.py`) |
  | A person's skip, with its reason | inherited only by an `unchanged` step; an `updated` step asks again |
  | Approvals for agents | never |
  | Verdicts, judgements, tiers, effort | recomputed |

- **Hand edits:** a site edited since the baseline keeps its step. The report says "edited since
  the baseline: record it, or verify judges it as a hand edit".
- **Prerequisite, to confirm first:** risk-site anchors are built on the current disk text at the
  baseline's line numbers (`workorder/manual.py:81-110`), so lines shifted by a hand edit can
  anchor the wrong statement. Build them on the baseline's source snapshot, then resolve on disk.

## 3. Cumulative plans

- **No merged baseline.** Raw traces of different runs are never merged into one baseline: they come
  from different commits, and the noise model is built for one baseline. Same-commit shards of a CI
  matrix (the coverage-combine pattern) are a later, separate question.
- **The backlog is computed, never stored.** Items are step keys; their sites are risk ids. It is a
  fold over immutable plans, ordered by `created_at`. A stored `backlog.jsonl` restored from a CI
  cache or a metadata branch (the Gemini design) is rejected: a second source of truth, caches get
  evicted, and a branch needs push rights for a bot.
- **States:** `open`; `in progress` (applied, not verified); `fixed` (verified, or credited with no
  failing verify); `dismissed` (skipped by a person with a reason, GitHub's "dismissed"); `gone`
  (absent from the latest plan and not credited); and a `reopened` flag.
- **CI from fresh checkouts:** each job uploads `.pymolt/plans/<name>/` (and `verification.json`
  when verify ran) as an artifact and appends `summary.md` to the job summary. A monthly job runs
  `pymolt report --combine DIR...`: stateless, matched by fingerprint, identical consecutive digests
  collapsed into one point, plans of other projects refused.
- **Doing the work:** the team plans on today's code. The report shows which open items that plan
  covers and which only an earlier capture exercised. Later (M5), `plan --from DIR...` carries those
  observations in where their anchors still resolve, stamped with their source.
- **The verdict:** the backlog never enters it. Open items outside the plan are listed as an
  unknown. `verification.json` gains `plan_id`; a displaced one is archived.

## 4. Vocabulary and standards

- **The JSON and the CLI keep pymolt's terms.** The human report uses plain labels, with the CLI
  term in a tooltip and in a glossary:

  | Term | Report label |
  |---|---|
  | pymolt / agent / person | Automatic / Developer or agent / Person |
  | preserve / intended | Must behave the same / Changes on purpose |
  | BLIND | No test reaches it |
  | needs_review / needs_evidence | Needs a person / Needs more evidence |
  | certain / high / medium / low | Will break / Likely / Possible / Unlikely or untested |

  Ruff-style fix applicability (safe fix / suggestion / manual) is shown beside the executor.
- **Exports** (`report --format`):
  - **SARIF 2.1.0:** one result per open site and per finding; `partialFingerprints` from risk ids
    and step keys; the line resolved at render time, as a hint; `uriBaseId` plus the git prefix for
    monorepos; `fixes` from hunks; `suppressions` from skips; `baselineState` from `--diff` or
    `--combine`. `level` is always set (certain or FAIL → error, high or medium → warning, low →
    note), because whether GitHub code scanning honours `result.kind` is not confirmed. Gate: a real
    upload to a scratch repository.
  - **GitLab Code Quality:** fingerprint = risk id; severity from the tier (certain → critical …
    low → info; FAIL → blocker). Mapping effort to severity is rejected: severity is risk, not work.
- **Docs follow Diátaxis:** the README orients and lists scenarios; `docs/scenarios.md` holds the
  how-tos; `docs/commands.md` is the reference; `docs/verify.md` and `docs/plan.md` explain.

## 5. The human report

### 5.1 Producers

- `pymolt report [DIR]` (new, read-only): reads state and writes only its output.
  `--format html|md|sarif|codeclimate`, `-o PATH`, `--plan NAME`, `--diff [NAME]`,
  `--combine DIR...`. Markdown goes to stdout, for `$GITHUB_STEP_SUMMARY` (limit 1 MiB; ours stays
  under 50 KB).
- `plan` writes `plan.json`, `report.html` and `summary.md` into the plan's folder, replacing
  PLAN.md; `--plan-md` becomes a hidden alias. `verify` refreshes the current plan's report; it still
  writes no other state.
- No MCP tool (as for `forks`); the status agent view gains `report_path`.
- Rejected: `plan --html` / `verify --html` only (re-rendering would need a full re-plan, and
  `--combine` has no home there); `status --report` (status writes nothing a person keeps); an
  `htmlcov`-style directory (it does not attach to a ticket); separate plan and verify reports (their
  numbers would disagree).

### 5.2 One self-contained HTML file

- **First page, for a person who does not read code:**
  - provenance: name, date, baseline capture, commit, pymolt version, moves, lineage;
  - where the migration stands and the next command;
  - who does what;
  - the size of the work (section 6);
  - what is unknown: sites no test reaches; whether the target environment imports (target-env,
    the smoke import, or "not checked"); unmeasured noise; packages not probed; the manual zone;
    stale captures; backlog items outside the plan;
  - the top risks in plain words;
  - a definition of done, each item with its evidence: pymolt steps applied; the other steps
    recorded or skipped; the target imports; a fresh post-migration capture; PASS, or every
    INCONCLUSIVE cause settled;
  - history (for a lineage or `--combine`).
- **Developer section:** one card per step (where, why as "data from …", the diff in `<details>`,
  the judgement, the effort basis), the plan diff, the findings, the glossary. Long diffs are cut,
  with a pointer to `plan.json`, so a large project's report stays small.
- **Print:** a print stylesheet, so "Save as PDF" gives an attachment a ticket can preview.
- **Safety and privacy:** built with stdlib `html.escape` and a small `pymolt/report/` builder (no
  Jinja); untrusted text through `render.clean()`; a CSP meta (`default-src 'none'`, scripts only by
  hash); project-relative paths, `~` for `$HOME`, scrubbed commands, no hostnames, container ids or
  e-mails. A privacy test renders a fixture full of home paths.

### 5.3 The Markdown summary

The same model rendered as GitHub Markdown (≤ 50 KB): the first page without charts, for job
summaries, PR comments and ticket descriptions.

### 5.4 Charts (owner decision)

- pymolt generates the charts itself, as inline SVG, through a small chart module in
  `pymolt/report/` (stdlib only). Kinds needed: stacked bar (work by executor and type), donut
  (shares), progress bar (definition of done), small line or step chart (history).
- SVG reads without JavaScript in e-mail, ticket attachments and print, and adds no runtime
  dependency.
- **Room for interactive charts later:** every chart is built from a chart spec (kind, series,
  labels, units), and the page embeds the specs as JSON (`<script type="application/json">`). The
  SVG renderer is one backend. An interactive renderer can later read the same specs, behind an
  option, without changing the report's data or the first page's no-JS reading.
- Rejected: plotly (≥ 1 MB even in the basic bundle per report, or a CDN that fails offline and
  leaks IPs; blank without JS); canvas libraries such as uPlot or Chart.js (blank without JS); a
  Python chart dependency for four chart kinds.

## 6. Effort, honestly

- pymolt never states hours of its own. The type of work replaces today's size rule
  (`workorder/verify_effort.py:155-182`):
  - **automatic**: pymolt applies it;
  - **edit**: a known successor;
  - **decision**: a behaviour change rated certain or high at a recorded site;
  - **check**: rated medium or low, e.g. a default moved to a sentinel whose effective value may not
    change (today "large");
  - **investigate**: no successor is known;
  - **setup**: environment and manifest.
- Size comes from sites only: 1 trivial, 2–5 small, 6–20 medium, over 20 large; an investigation is
  `unknown` (a new size). Risk is shown separately from effort.
- The first page reads, e.g.: "4 edits at 9 sites in 3 files, 2 decisions, 1 check, 1 investigation
  (unsized)".
- **Team mapping (opt-in):** ranges in `[tool.pymolt.report.effort]`, shown as "your team's
  estimate (not measured)". No default.
- **Calibration:** with at least 5 recorded steps of one type and size, the report shows the median
  time from `--record` to `--done`, labelled "measured here, includes breaks".
- Rejected: OpenRewrite's "time saved", SonarQube's minutes, Code Climate's points: invented numbers.

## 7. The README scenario ladder

| # | Scenario (for whom) | Commands | The reader gets |
|---|---|---|---|
| 1 | Where does my code touch dependencies? (anyone) | `scan` | the contact map, offline |
| 2 | Will the target resolve? (tech lead) | `setup`, `assess` | a target manifest |
| 3 | Did my upgrade change behaviour? (developer; the tutorial, on the bench toy) | `setup`, `capture baseline`, upgrade, `capture post-migration`, `verify` | a verdict |
| 4 | Rewrite what pymolt can (developer) | `plan`, `apply`, `rollback --step` | applied steps |
| 5 | Share the plan (manager, ticket) | `report`, `report --format md` | HTML and a summary |
| 6 | Re-plan after hand edits (developer) | `apply --step --record`, `plan`, `report --diff` | what changed |
| 7 | Hand work to an agent (team) | `apply --approve-agent`, `--agent`, the plugin | a guarded apply |
| 8 | A service or a container (ops) | `--mode command` / `--mode attach`, `setup --container`, `--runs 2` | a live capture, measured noise |
| 9 | CI nightly, backlog monthly (lead) | a workflow, `report --combine`, SARIF | a cumulative report |

- Each scenario block sits between `<!-- scenario:N -->` markers, like today's README example.
- `bench/scenarios/run.py` runs each block verbatim under `bash -euo pipefail` on a fresh copy of the
  e2e_pandas toy and asserts its lines and exit codes; steps that need a person go through a stdlib
  `pty` driver; scenario 9 runs in two fresh clones with the artifacts copied between them and
  validates its SARIF. The docs test runs on every PR; the ladder runs nightly.
- Before the ladder is written, an agent that knows nothing of pymolt follows the README literally on
  a real project and logs where it gets stuck; the ladder answers those points.

## 8. Staging

### 8.1 In 0.2, before 0.2.0rc1 (owner decision)

1. `status`: the plan phase is DONE after a newer plan was saved (`status.py:367-375`). Reproduce
   with a test, then project the saved plan.
2. PLAN.md header: the plan's date, the pymolt version, and the baseline capture as
   `<captured_at>-<sha8>` (the future capture id), with no schema change.
3. A test that step keys are unique within a plan, so 0.2 plans stay matchable by 0.3.

### 8.2 In 0.3

- **M1:** schema 3, capture ids, `plans/` with its pointer, the schema-2 adapter, retention,
  unchanged re-plans, `--label` / `--fresh`. Scenarios 1–4, 7, 8.
- **M2:** the risk-anchor prerequisite (2), the matcher, stable ids, the plan diff, inherited skips,
  the §1.6 links. Scenario 6.
- **M3:** `pymolt report` in HTML and Markdown with SVG charts, effort by type of work, PLAN.md
  retired. Scenario 5.
- **M4:** `--combine`, backlog states, `plan_id` in the verdict, GitLab Code Quality, then SARIF
  behind a verified upload. Scenario 9.
- **M5 (optional):** `plan --from`, the team effort mapping, calibration, same-commit capture shards.

### 8.3 M1: owner decisions (2026-11-22)

Taken on the M1 implementation spec's open questions. They amend §1 where they differ.

| Question | Decision |
|---|---|
| Retention | Keep the current plan, its **parent only** (not every ancestor: a derived re-plan is the default, so a lineage grows without bound), every plan a run references, and the 20 newest |
| `--fresh` or a new `--label` on an unchanged plan | Saves a new plan; the old plan's approvals do not carry to it |
| `plan -o PATH` | Keeps 0.2's meaning: the plan is written only to `PATH`; nothing is stored under `plans/` and the pointer stays. A mutating `apply --plan PATH` imports it (its stored form, under its name) |
| `plans/current.json` | Local state, gitignored. With no pointer and no 0.2 file but saved plans (a fresh clone, a CI checkout), nothing is adopted silently: `status` names the newest and how to choose one, `plan` starts a fresh lineage, and commands that need the current plan say the same |
| Plan folders git tracks | Retention never removes them (one `git ls-files -z -- .pymolt/plans` through `adapters/git.py`). Outside a repository or without git nothing is tracked; when git fails or times out, nothing is pruned and `pruned` says so |

## 9. Sources

- SARIF 2.1.0 (OASIS): https://docs.oasis-open.org/sarif/sarif/v2.1.0/os/sarif-v2.1.0-os.html
- GitHub code scanning, SARIF support: https://docs.github.com/en/code-security/reference/code-scanning/sarif-files/sarif-support
- GitLab Code Quality: https://docs.gitlab.com/ee/ci/testing/code_quality.html
- Terraform plan and apply: https://developer.hashicorp.com/terraform/cli/commands/plan
- Renovate Dependency Dashboard: https://docs.renovatebot.com/key-concepts/dashboard/
- Ruff fix safety: https://docs.astral.sh/ruff/linter/#fix-safety
- pytest-html: https://pytest-html.readthedocs.io/en/latest/user_guide.html
- coverage.py HTML: https://coverage.readthedocs.io/en/latest/cmd.html#html-reporting
- Allure history: https://allurereport.org/docs/history-trend/
- GitHub job summaries: https://docs.github.com/en/actions/using-workflows/workflow-commands-for-github-actions#adding-a-job-summary
- Diátaxis: https://diataxis.fr/
