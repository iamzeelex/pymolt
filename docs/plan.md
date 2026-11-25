# API changes, Python breakage and codemods

`pymolt plan` works out what the migration breaks in *your* code and rewrites what it can. It saves
the result as a plan with a unified diff and writes no source; `pymolt apply` writes exactly that
plan.

## Dependency API changes

For each dependency that moves between the base and the target, `plan` computes the changes to the
symbols your project touches:
- a symbol removed or moved;
- a changed signature or default;
- a new deprecation.

Only the touched symbols are compared. A whole-package diff of pandas produces thousands of
irrelevant changes.

- **Old side.** The interpreter that ran the baseline capture: locally, or `docker exec` in the
  configured container. When no interpreter is reachable, pymolt reads the old release's source
  statically for the touched symbols only, and caps how certain the result can be.
- **New side.** Your target environment, or a throwaway one built from uv's cache under
  `.pymolt/cache/envs/`.
- **From-versions.** These are the versions the baseline capture actually ran, which it records,
  not what an unpinned manifest resolves to today. The plan states every correction. With no
  capture, lock, base interpreter or exact pin, a base version is `unknown` (a floor such as
  `flask>=0.10` is not what is installed): the move is listed from `unknown` and its API changes
  are inconclusive, never computed from the floor.
- **Successors.** Where a removed, deprecated or renamed name (or parameter) went is *named* from
  the library's own text and data:
  - the warnings the baseline run recorded at its sites, when they are about it (below);
  - the old and new versions' deprecation prose: docstrings, `warn` and `raise` texts with a
    message variable followed one step, and the warning helpers a body calls one level down
    (flask's `_prepare_send_file_kwargs` says `attachment_filename` became `download_name`);
  - the new version's error for a removed name (numpy 2's module `__getattr__`: "Use `np.nan`
    instead"), and the body of a callable that warns on some calls only (polars `count()`);
  - the data of known deprecation helpers, read from their decorator arguments or their
    wrappers' closure cells (`deprecate_renamed_parameter("periods", "n")`,
    `_api.rename_parameter("3.9", "labels", "tick_labels")`, `_api.deprecated(alternative=...)`;
    the list is `pymolt/knowledge/deprecation_helpers.json`);
  - the knowledge shipped with pymolt;
  - with `--online`, intermediate releases on PyPI. Only package names and versions leave the
    machine.

  A named successor must exist and be public in the target, and must not be the old symbol. A
  private name (`numpy._core`) or a compatibility namespace (`pydantic.v1`) is never chosen while
  another candidate is, and a bare generic word ("use `fit`") counts only when quoted, qualified,
  or a member of the same class. A recorded warning is evidence about a symbol only when it names
  the symbol or one of its parameters, or matches a warning its own code emits; any other one is
  a pitfall of the site (seaborn's `factorplot` call warning about numpy's `result_type`). When
  the evidence names several objects, or offers a choice ("`st.cache_data` or
  `st.cache_resource`"), no successor is picked: the fact lists them as `succession.candidates`,
  ordered by evidence, and the agent or person chooses. Every fact that names a successor says
  how (`succession`: its `trust`, the `sources`, `caveats` such as "the default has changed"
  or "is not equivalent to", and the names it `rejected` and why). A parameter paired with another only by its position is a
  candidate, never a successor. Prose and text only name a successor; they never rewrite code.
- **Proven successors.** A successor is *proven* only by the library's own code, and only a
  proven one is rewritten:
  - **identity**: the same object (a move, or a removed name that was its named successor in
    the old version: `np.NaN is np.nan`);
  - **forward**: after its docstring and warnings, the old callable is one `return f(...)`
    (polars 0.19's `groupby` is `return self.group_by(by, *more_by, ...)`), or one guarded by a
    condition the call's own arguments decide (polars 1.x `count()` without columns is
    `F.len().alias("count")`);
  - **alias**: the body makes one parameter the other (`height = size`,
    `kwargs["height"] = kwargs.pop("size")`, a helper handing `interpolation` back as `method`),
    or a registered rename helper says so;
  - **twin**: its body, apart from the warning, is the successor's, in the same module or class
    (aiohttp's `has_body`).

  A proof rewrites code only when the probe's summary of the callable is **complete**. What it
  may contain is an allow-list:
  - besides the forward (or the alias, or the twin's body): a docstring, warnings and
    deprecation logs that are handed none of the parameters, the message they print, and a
    conflict check that only raises;
  - wrappers and decorators: builtin descriptors (`property`, `classmethod`, `staticmethod`)
    and the registered helpers whose wrapper only warns or dispatches (for a keyword rename
    also those that pass keywords through or rename another keyword), each layer on the
    `__wrapped__` chain checked;
  - a forward that is a rename hands every parameter on unchanged, with no call chained on its
    result (`return isin(a, b).ravel()` is no rename of `isin`) and with the successor's
    defaults equal to the ones it hands on;
  - an alias is the old parameter's only use, under no condition, or under one any value a
    caller passes meets (`if size is not None:` holds only for a value other than None, so only
    such a literal is renamed), and a forward with an alias beside it is no forward;
  - a rename helper that also maps the values (pandas `deprecate_kwarg(..., mapping=...)`)
    proves no plain rename.

  Anything else (an unknown decorator, a helper that is handed `kwargs`, a twin in another
  module) leaves the successor named, with the reason in `succession.rejected`.

  A proof made in the old version holds when the successor is not deprecated in the target and
  every call binds alike against the successor's old and new signatures; one made in the target
  holds only when the old version's own implementation is the same (pydantic 2's `.json()`
  forwards to `model_dump_json`, but pydantic 1's wrote other bytes). A forward to a private
  callee proves nothing (numpy's `in1d` forwards to `_in1d`, which flattens, while `isin` keeps
  the shape). When the successor deprecates a parameter the forward passes, its own text names
  one more hop (polars `map_dict` -> `replace` -> `replace_strict`): named, never rewritten.
  `succession.trust` is `identity` or `proof` for these, `transfer` says whether and how they
  hold in the target, and `proof` names the code that proves them.
- **Same object, new name.** A name that now points to the same object under another spelling is
  followed, not reported as removed.
- **Receivers.** A method call is also compared under its receiver's class, and under that class's
  public spellings, not only under the class that defined the method in the old version:
  `df.groupby(...)`, recorded as `NDFrame.groupby`, is probed as `DataFrame.groupby` too.
- **Inconclusive, never "no change".** A package pymolt cannot build or probe offline is reported as
  inconclusive.

Measured on 22 known API changes in flask, werkzeug and pandas: recall 0.95, precision 1.00
([bench/delta_accuracy](../bench/delta_accuracy/RESULTS.md)).

## Python-version breakage

Moving Python is treated as one more migration with its own facts:
- **Stdlib removals.** The plan finds the call sites that use stdlib names removed or moved between
  the base and target Python: the `collections` ABCs, `imp`, `distutils`, `inspect.getargspec`,
  `ssl.wrap_socket`, and so on. The data comes from shipped stdlib lifetime data and a probe of the
  target interpreter.
- **Guards.** A use inside a compatibility fallback (`try: ... except ImportError:`) is listed as
  guarded and is not rated.
- **Compile check.** Every `.py` file is compiled on the target Python to catch syntax errors and
  invalid escapes.
- **Fixes.** The fixable cases are rewritten:
  - attribute paths, adding the import they need (`collections.Iterable` → `collections.abc.Iterable`
    plus `import collections.abc`);
  - moved names in imports.

  Each fix is kept only if the file still compiles and the diff stays minimal. The rest stays manual,
  with a hint.

Ruff and pyupgrade are not used as fixers. They rewrite the `except ImportError:` fallbacks too,
and churn dozens of files on a real project ([details](design/python-version-breakage.md)).

## Codemods

A codemod is a match/rewrite template applied with LibCST, for example
`Series_1.append(Series_2)` → `pd.concat([Series_1, Series_2])`.
- **Binding-aware.** It rewrites only true bindings of the old name, not textual matches.
- **Formatting kept.** Indentation and comments stay as written.
- **Checked first.** Every rule is re-checked before it enters a plan. A rule that is not verified,
  or that the check downgraded, is never applied: the plan lists it as manual work (`manual` in
  `--json`), with each file and line it matched and the reason.
- **All or nothing.** `apply` prepares and parses every changed file before the first write, and a
  failed apply restores them all. `pymolt rollback` undoes a successful one.
- **Self-checked.** `apply` compiles every changed file on the target Python before writing, the
  dependency rewrites included, and then imports the changed modules in the target environment
  unless `--no-smoke` is given ([commands.md](commands.md#pymolt-apply-dir)).

A proven successor becomes one of these codemods (basis `library-proof`, claim `preserve`):
- **`rename-keyword`**: `old=` becomes `new=` in calls of the callable, in a `**{"old": ...}`
  literal passed to it and in `functools.partial(callable, old=...)`. A call that passes `new`
  already, or a `**mapping` that is no literal, is left alone, and so is one that passes, or
  leaves at a changed default, another parameter whose signature changes;
- **`rename-member`**: `.old` becomes `.new`, a method call or an attribute read;
- **`forward-template`**: the call is rebuilt from the forward, the old defaults the successor
  changed written out (`df.with_row_count()` -> `df.with_row_index("row_nr")`).

A function is reached through its import binding only, the same binding on every path (not an
`if`/`else` that imports another module under the same name): never a shadowed name, a binding
made in an `except ImportError` fallback, a string or a `getattr` name. A method is rewritten
only at the statements (anchors) where the baseline recorded receivers of the class the proof
is about, and no project class in the receiver's hierarchy defines either name; the statement's
own lines must hold the method once (a compound statement's header, never its body), and a
call the baseline recorded under a deprecation wrapper counts only under the method's own
wrapper. Every rewritten call is checked against the old and new signatures: an argument that
would bind to another parameter, a new keyword already passed by position, or a value an alias
guarded by `is not None` could not tell (anything but a literal) leaves the site alone. A
`forward-template` keeps the call's evaluation: it refuses to drop, repeat or reorder an
argument that is not a name or a literal. A successor that is named but not proven, or proven
but not holding in the target, is an agent step with the successor, the forward rendered as
`do.manual.suggestion`, and its provenance in `why.successor`; one the evidence names several of
lists them as `do.manual.candidates`. A keyword rename's step names its parameters in
`symbol.params`.

## The plan as steps

The saved plan (`"schema_version": 2`) lists the migration as ordered steps, `steps` in `--json`.
A step pymolt applies is one recipe across every file it changes: a rule, a pattern, or a group
of import moves applied together. Its `edits` hold, per file, the hunks between the text before
and after that recipe, the statements it replaces, inserts or deletes, and anchors that name
those statements by content rather than by line. Steps run in the order the plan built them:
rules, then patterns, then Python fixes. Applied in that order they give exactly the file
changes of `changes`, which `apply` writes.

A step's `depends_on` lists the earlier steps it cannot do without, in the same file:
- one whose output it rewrites;
- one that inserted an import its new code needs, or the same import it inserts;
- when a statement cannot be keyed (a file no Python grammar parses), or more than 8 steps change
  one function, the earlier steps that change the same function or class.

Work pymolt does not do becomes steps too, with who is expected to do it (`executor`):

| Step | Who | From |
|---|---|---|
| `manual-rewrite` | agent | a rule that matched but is not applied (`manual`), or a rule advisory that it could not rewrite a site |
| `api-change` | agent | a removed, moved, deprecated symbol or a changed signature the plan does not rewrite |
| `python-manual` | agent | a Python-version risk the plan does not fix, or the target's compile findings |
| `behaviour-change` | person | a changed default the site relies on, a warning the baseline recorded, a Python behaviour change |
| `review` | person | a rewrite applied with a caveat to check |

One step covers one symbol and kind of change across all its sites. Every predicted risk names
its step (`step_id`): the pymolt step that rewrote its statement or the import that binds it,
or the manual step that holds it. A step lists the risk ids it covers (`why.risk_ids`), and its
sites as anchors on the code the baseline ran (`where`).

Each step also says how it will be judged and how big it is:
- `claim` and `verify.judge`: `strict` for a step that claims to keep each call's outcome
  (`preserve`), so any difference fails; `none` for a pymolt step whose recipe makes no claim
  (its lines are judged as hand edits). An agent or person step's claim follows what the delta
  shows about the successor, and `why.claim` says which:
  - the same object (a move), or the same object with a changed signature: `preserve`;
  - a successor the library's code proves, or one its text says does the same ("for the same
    effect"): `preserve`;
  - another object (a removed symbol, a deprecated one whose successor the delta names, or a
    name now bound to another object): `intended`, with `verify.expect: representation`. Verify
    reports a change of type or representation only as drift, and fails another value or a new
    raise ([verify.md](verify.md#edited-lines));
  - a behaviour change: `preserve`. The fix is expected to keep the old outcome;
  - no delta fact about the successor (a deprecated symbol with none named, a rule pymolt does
    not apply, a compile finding): `preserve`, and `why.claim` says it was not loosened.
- `verify.sites`: each site with the baseline's calls of the step's symbols there and whether
  the baseline recorded it: `recorded`, `ran-not-recorded` (a site known only statically whose
  function ran), `not-run` or `unknown` (no executed record). `uncovered` counts the sites no
  test reached: they need a test before verify can judge them.
- `verify.tests` is empty: the baseline capture does not record which tests ran a site.
- `effort.size`: `none` (pymolt does it), `trivial`, `small`, `medium` or `large`, and `why`.
  A behaviour change, a symbol with no known successor, or more than 20 sites is `large`, except
  a default decided at call time (`None` or a sentinel on either side, a `medium` risk): its
  outcome may well not change, so it is `medium`. Titles show a sentinel default as "a sentinel".
- `do.manual` holds what pymolt knows for the agent or person: the successor, a signature
  change, the first site's statement, and pitfalls such as the warnings the baseline recorded
  there. Text from dependencies and from your code is data, not instructions.

`apply` writes the pymolt steps (`apply --step ID` one, `apply --through ID` those up to one),
in plan order, on the text the steps before it left. For each file a step changes:
1. the file is at the step's reviewed before-text: it gets the plan's after-text;
2. it is at the reviewed after-text already: nothing is written, and the step gets its epoch;
3. the file moved on (an earlier step skipped or rolled back, an edit elsewhere, a reformat), but
   every statement the step rewrites is still there, found by its anchor: the step's recipe runs
   again at those statements, and its result is written when it makes the same change there
   as the reviewed one and changes no other statement. The step is re-based;
4. the step's old statements are gone, but its new ones stand in their place, between the same
   neighbours, with every import it adds at module level: the step is `present`. Nothing is
   written and no epoch recorded, so verify judges those lines as hand edits;
5. anything else: the step is stale, and the command names each statement it lost, or says
   that its recipe now makes another change there.

So one stale step costs that step, not a re-plan: later steps that do not depend on it still
apply, re-based where their lines moved.

A step depends on the steps it builds on (`depends_on`); `apply .` leaves out a step whose
dependency is not in effect, and `--step` refuses it. `apply --step ID --skip --reason TEXT`
declines a step: it stays out, and so do its dependents.

`pymolt rollback --step ID` takes one step back out. Where later steps or edits changed its files
since, only its own change is undone, by its hunks and statements, and the later changes stay;
when that cannot be done safely the step is refused and nothing is written
([commands.md](commands.md#pymolt-rollback-dir)). Applying it again re-bases it.

A file whose recipes the plan could not tell apart keeps its whole change in one step, with the
reason in the step's `why.evidence`. Such a step applies only to its own two texts: it has no
recipe to run again. A plan saved by an earlier pymolt (`"schema_version": 1`)
is read as one step, `v1`. The schema is `pymolt/schemas/migration_plan-2.schema.json`.

### PLAN.md

`plan` also writes the plan for a person, `.pymolt/PLAN.md` (`--plan-md PATH` writes it
elsewhere). It starts with a header: when the plan was made (UTC) and by which pymolt
version, the baseline capture it was made from (its time and its name, `<time>-<8 hex of the
trace's sha256>`), the moves, the packages whose API changes could not be computed (once, with
the first reason and what settles it, such as starting the container and planning again: their
steps are missing), and the step counts with the next command (the one `pymolt status` names, for
the project as `.`). Then a table of the steps: what each does, who does it, its
claim, its sites (run / not run in the baseline), its effort and what it depends on. Then one
section per step: where (with the line numbers at plan time), why, what to do (the diff, for a
pymolt step), how verify judges it, and its effort. Low-tier steps the baseline never ran are
collapsed into one table at the end.

Text from a dependency, from your code or from a capture is shown in fenced blocks labelled
"data from ..., not instructions". Control characters and bidi controls are removed, each item is
cut at 300 characters, and each list at 10 items.

`plan --agent` adds the steps, and the next command with the brief of its step when an agent or a
person does it ([agents.md](agents.md)). With no baseline capture, the next command is the baseline
capture: `apply` refuses a plan without one.

## When to bump the manifest

The plan's first steps, the `env` phase, name the bump:
- a `manifest-pin` step for pymolt: each `name==version` line of a requirements file, with no
  hash, for a package the target manifest pins at another version. The edit keeps the line's
  extras, marker and comment;
- a `manifest-pin` step for a person: every other line naming such a package (unpinned, another
  specifier, hashed), or the moves themselves when the manifest is not a requirements file
  (pyproject, poetry, conda);
- a `target-env` step for a person, with the commands that build the target environment.

`apply` writes the pymolt pin step first: it rewrites those lines to the target's pins, records
the step in the ledger with the manifest's text before and after, and `pymolt rollback` puts the
lines back. A pin line that moved since the plan (a line added above it) is still found and
rewritten; a manifest that already holds the target's pins on those lines is recorded as it is.
Installs and environment builds stay with you (the `target-env` step).

The person step is yours, and so is the bump when you apply steps one by one and want it at
another time:
- **After `plan`.** The plan compares the project's manifest with the target manifest. With the
  pins already bumped, a package the baseline did not record shows no move.
- **Before the post-migration capture.** That capture must match the project as it is then; a bump
  after it makes it stale.
- **Before or after `apply`.** A change to the *contents* of the same manifest makes neither the
  plan nor the baseline stale. Another manifest file, base Python, toolset or container does, and
  so does a change to the target manifest.

## Predicted risks

The plan rates each affected call site into a dependency:

| Tier | When |
|---|---|
| certain | a name the site writes is gone (removed, or moved so that the name no longer exists): a name on its statement or its function's, or the attribute it calls on a receiver whose class is still there |
| high | a parameter the site leaves at its default changes its default between two concrete values, or to or from `None` or a sentinel when the site's own baseline warning names it; or the site emitted a deprecation-family warning in the baseline run |
| medium | the signature changes for a parameter the site passes, or gains a required one; a default moves to or from `None` or a sentinel and no warning at the site names it (decided at call time, so the behaviour may not change) |
| low | a deprecation with no warning at the site; a moved symbol the site does not write; a class-level signature change; any change on a symbol the baseline never ran (BLIND), even a removal; a removal known only under the name the old version defined the call under (`unverified`, below) |

A site is rated by what it writes, not by where the old version defined the callee. The baseline
records `df.groupby(...)` on pandas 0.25 as `NDFrame.groupby`; pandas 3 no longer defines `groupby`
on `NDFrame`, but `DataFrame.groupby` still resolves, so the call is no risk. When the plan knows a
removal only under that internal name and could not check the name the site uses, the risk is
`low` and `unverified`: its step asks to check that name in the target environment, gives the
one-line command, names no successor and does not loosen what `verify` accepts.

Python-version risks (stdlib removals, syntax) have their own tiers, which depend on the evidence.
For example, `certain` needs the symbol to be absent on a probed target interpreter, while an
absence known only from data is at most `high`.

A default the call passes explicitly never applies, so it is not a risk.

Each risk is marked "fixed by the plan" or "needs attention". A site is fixed when the plan rewrites
its line, or rewrites the import that binds the name the site reads (an import move). A site that
reads a name of the same spelling bound otherwise, such as a parameter, is not. After the post-migration capture,
`verify` scores every prediction as hit, prevented by the plan, miss or not exercised (see
[verify.md](verify.md#predicted-risks)).

Each risk has an `id` built from its symbol, file, function, the text of the call's line and
its rank among equal lines there (`site.nth`), not from the line number, so an edit that moves
lines keeps it. The plan saves the ids it predicted, and `verify` scores those; a plan saved by an
earlier pymolt (`"schema": "pymolt.risks/1"`) keeps its line-based ids.

