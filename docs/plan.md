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
  not what an unpinned manifest resolves to today. The plan states every correction.
- **Successors.** Where a removed name went is taken from:
  - the warnings the baseline run recorded;
  - the old version's docstrings;
  - the knowledge shipped with pymolt;
  - with `--online`, intermediate releases on PyPI. Only package names and versions leave the
    machine.
- **Same object, new name.** A name that now points to the same object under another spelling is
  followed, not reported as removed.
- **Inconclusive, never "no change".** A package pymolt cannot build or probe offline is reported as
  inconclusive.

Measured on 22 known API changes in flask, werkzeug and pandas: recall 0.95, precision 1.00
([bench/delta_parity](../bench/delta_parity/RESULTS.md)).

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
- **Checked first.** Every rule is re-checked before it enters a plan. Rules that cannot be checked
  are listed for manual review.
- **All or nothing.** `apply` prepares and parses every changed file before the first write, and a
  failed apply restores them all. `pymolt rollback` undoes a successful one.

## Predicted risks

The plan rates each affected call site into a dependency:

| Tier | When |
|---|---|
| certain | the symbol the site calls is removed |
| high | a parameter the site leaves at its default changes its default between two concrete values, or to or from `None` or a sentinel when the site's own baseline warning names it; or the site emitted a deprecation-family warning in the baseline run |
| medium | the signature changes for a parameter the site passes, or gains a required one; a default moves to or from `None` or a sentinel and no warning at the site names it (decided at call time, so the behaviour may not change) |
| low | a deprecation with no warning at the site; a moved symbol; a class-level signature change; any change on a symbol the baseline never ran (BLIND), even a removal |

Python-version risks (stdlib removals, syntax) have their own tiers, which depend on the evidence.
For example, `certain` needs the symbol to be absent on a probed target interpreter, while an
absence known only from data is at most `high`.

A default the call passes explicitly never applies, so it is not a risk.

Each risk is marked "fixed by the plan" or "needs attention". After the post-migration capture,
`verify` scores every prediction as hit, prevented by the plan, miss or not exercised (see
[verify.md](verify.md#predicted-risks)).

## A remote Axiom (optional)

A remote Axiom service is used only when you configure one with `plan --endpoint URL` or
`PYMOLT_AXIOM_ENDPOINT`, with the token in `PYMOLT_API_TOKEN`. Only package names and versions are
sent, never code. On the same 22 changes, Axiom's cached data scored recall 0.55 and precision
0.63.
