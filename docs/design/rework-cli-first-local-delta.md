# Rework: CLI-first agent surface, local delta engine, predicted risks

Status: in progress (2026-11-15). Base: `integration/cli-command-set` (the approved 17-command CLI:
status → scan → setup → assess → capture baseline → plan → apply → capture post-migration → verify).

## Decisions and evidence

| decision | evidence |
|---|---|
| Agents use the CLI (`--json`) plus one Agent Skill. MCP becomes an optional thin adapter. | The agent bench never had a CLI arm, and its wins came from capability plus compact answers. The attached server costs ~500–850 tokens per turn. Rounds 1–2 were not isolated (bench/RESULTS.md, round 3 notes). The MCP-only honesty bugs lived in duplicated logic. |
| API-change facts are computed locally, scoped to the symbols the project touches; no remote service is involved. | bench/delta_accuracy/RESULTS.md: over 22 changes, the local engine reaches recall 0.95 and precision 1.00, with successor hints for 3/3. It takes < 2 s per library. |
| Predicted risks attach to `plan`. `verify` reports predicted vs observed. | `verify` stays the only verdict (cli-command-set decision). The pandas 1.5 FutureWarning about `numeric_only` predicted the break the fact diff later caught. |

## 1. Agent surface

- **One view layer** in `pymolt/interfaces/views.py` builds compact, honest payloads for every command. It covers the summary line, counts, capped lists with true totals, a `full_report_path` for the rest, and completeness flags. It holds what `mcp_server.py` does today (`_fit_answer`, truncation notes, honest summaries), so CLI and MCP cannot drift apart.
- **CLI:** `--json` on every command prints the full payload. `--agent` prints the compact view, which is the same object MCP returns.
- **MCP (optional, `pymolt[mcp]`, `mcp>=1.2,<2`):** the approved 7 tools (status, scan, setup, assess, capture, plan, verify), each a few lines over the view layer, with no logic of its own. A parity test asserts that every tool's payload equals the CLI `--agent` payload for the same project. `capture` keeps the `PYMOLT_MCP_ALLOW_EXEC=1` gate. No tool writes source.
- **Plugin:** remove `mcpServers` from `plugin.json`. The skill and the agent instruct `pymolt <cmd> --agent` and `--json`. A separate doc section explains how to attach the MCP server for shell-less clients.
- **Fresh-install smoke test:** build a wheel, install it into a temp tool env with `[mcp]`, and check that `pymolt mcp` answers `initialize`.

## 2. Local delta engine (`pymolt/delta/`)

**Inputs:**
- the project's touched symbols: the contact map plus the identity resolver's canonical symbols, plus symbols seen in the baseline trace (dynamic-only);
- the dependency migrations: (name, from, to) from assess/plan.

**Interpreters:**
- **old:** the baseline capture's interpreter (trace metadata `executable`), else setup's base Python environment;
- **new:** a target environment if one exists. Otherwise build a cached throwaway environment under `.pymolt/cache/envs/<hash>/` from the target manifest with uv (`uv venv` + `uv pip install`, versions only). If that cannot be built, the delta for that package is INCONCLUSIVE with the reason, never "no changes".

**Probe** (`probe.py`):
- pure stdlib, 3.6 syntax, run once per interpreter as a subprocess with the project as cwd;
- per symbol it records existence, canonical identity, signature (parameters, defaults, kinds), its deprecation docstring lines and the PEP 702 `__deprecated__` attribute, its forwarding target (`__wrapped__`, a one-line delegate), and the defining module/class (base-class moves are not removals).

**Diff** (`engine.py`): classify each symbol and parameter as
`REMOVED | MOVED (same object reachable elsewhere) | SIGNATURE (param added/removed/renamed/kind) | DEFAULT (default value changed) | DEPRECATED (in old) | UNCHANGED`. Each fact carries evidence (which probe field) and a `successor` hint when known.

**Successor hints**, in order:
1. the old version's runtime warning text recorded during the baseline capture (section 3);
2. the old version's deprecation docstring;
3. curated knowledge (`pymolt/knowledge/`);
4. opt-in `--online` only: fetch intermediate releases from PyPI (versions only), newest first, reading single module files by HTTP range request. This recovers "deprecated after old, removed by new" hints such as `flask.Markup → markupsafe.Markup`.
Hints are cached under `.pymolt/cache/delta/`.

**Knowledge** (`pymolt/knowledge/*.json`): versioned data shipped with pymolt. It holds curated codemod rules (e.g. pandas `Series.append`/`DataFrame.append` → `pd.concat`), framework-succession edges (moved from the succession client) and warning switches (e.g. `SQLALCHEMY_WARN_20`). Each entry has a source and a date.

**Provider** (`provider.py`): `LocalDeltaProvider.fetch_bundle(migrations, progress=None, use_cache=None) -> dict[str, CodemodBundle]`.
- `patterns`/`rules` come from facts with a concrete successor: rename-call / rewrite-import when the successor is a same-signature move or rename, and knowledge rules for structural rewrites.
- `impacts` hold every non-UNCHANGED fact, so plan shows them even when nothing is auto-fixable.
- It is the only provider for `plan`.

**Delta fact schema** (consumed by section 3):
```json
{"schema": "pymolt.delta/1", "package": "pandas", "from": "1.5.3", "to": "2.2.3",
 "old_python": "3.11.13", "new_python": "3.12.11", "quality": "complete|partial|inconclusive",
 "facts": [{"symbol": "pandas.core.frame.DataFrame.mean", "aliases": ["pandas.DataFrame.mean"],
            "kind": "DEFAULT", "param": "numeric_only", "old": "None", "new": "False",
            "deprecated_in_old": true, "successor": null,
            "evidence": ["signature", "warning:FutureWarning: The default value of numeric_only ..."]}]}
```

**Engine API** (shared by the provider and predict):
`pymolt.delta.compute_deltas(project_dir, migrations, *, symbols=None, online=False, use_cache=True) -> list[DeltaReport]`.
`DeltaReport` is a pydantic model with `.model_dump()` equal to the schema above. When `symbols` is
None, the engine derives the touched symbols itself (contact map + identity resolver + baseline trace).

## 3. Runtime warnings and predicted risks

**Warning capture** (injected runtime, stdlib, 3.6):
- While a boundary call is being recorded, `DeprecationWarning`/`FutureWarning`/`PendingDeprecationWarning` raised through `warnings.warn` are attributed to that call's (sym, site) and stored on the fact as `warnings: [{category, message, count}]`. The message is capped at 300 chars.
- The original `warnings.warn` still runs, so the program's visible behaviour is unchanged. The filters the app configured still decide what is shown, and capture happens before them.

**Predict** (`pymolt/verify/predict.py`): join the baseline facts (symbol, site, input with `defaulted`, outcomes, warnings) with the delta facts and produce risks per call site:

| tier | rule |
|---|---|
| certain | the call site's symbol is REMOVED (and not MOVED) |
| high | a parameter the site left at its default has a DEFAULT change between concrete values, or one to/from `None` or a sentinel that the site's own baseline warning names; or the site emitted a deprecation warning in the baseline |
| medium | SIGNATURE change touching a parameter the site passes; a DEFAULT change to/from `None` or a sentinel that no warning at the site names (decided at call time: often no behaviour change) |
| low | DEPRECATED in old without a recorded warning at the site, or a change on a symbol reached only statically (BLIND) |

Each risk carries the site, the symbol, the fact, the evidence and whether a planned codemod fixes it.

**Where it shows:**
- `plan` prints and stores the risks in the plan file, split into "fixed by the plan" and "needs attention".
- `verify` adds a calibration section: for each predicted risk, did the post-migration fact diff show a change for that call (hit / prevented by the plan / miss / not exercised)? Only entries for the risk's own symbol score it (a line holds several calls); on a hit, the line's other entries count as its consequences. A risk the plan fixed whose rewrite kept the behaviour is **prevented**, never a miss. Plus observed changes no prediction covered.
- MCP `plan` and `verify` return the same through the view layer.

## Acceptance

1. **Pandas toy**, `bench/e2e_pandas`, with the baseline captured:
   - `pymolt plan` works with no network and no hub;
   - it proposes the `Series.append → pd.concat` rewrite;
   - it predicts `DataFrame.mean` at `core.py:24` as **high**, from the default change plus the warning;
   - after apply and the post-migration capture, `verify` reports that prediction as a **hit**;
   - the existing PASS/FAIL scenarios are unchanged.
2. **Delta accuracy:** `bench/delta_accuracy` runs through `pymolt.delta` and keeps recall ≥ 0.95 and precision 1.00 on its 22 changes, < 2 s per library, excluding environment builds.
3. **MCP:**
   - 7 tools, the server under ~300 lines;
   - the parity test passes;
   - the plugin has no `mcpServers`;
   - the skill uses the CLI;
   - `mcp<2` is pinned and the fresh-install smoke test passes.
4. **No network by default.** Only `--online` fetches intermediate releases, and only package names and versions leave the machine.
5. **Full suite passes;** the injected runtime parses at `feature_version=(3, 6)`.

## Acceptance results (2026-11-15)

All five criteria pass:
- `bench/e2e_pandas` now runs the plan → apply pipeline for every scenario. In each one the offline plan:
  - takes pandas 1.5.3 from the baseline capture;
  - proposes the one `toyapp/core.py` rewrite;
  - rates `Series.append` certain and fixed by the plan, and `DataFrame.mean`@24 high.
- The verdicts are PASS, FAIL, PASS, FAIL as before. Calibration has `mean` as a hit, `append` as prevented, and the four call-time-default risks as misses.
- Delta accuracy: P 1.00, R 0.95, the same as before.
- Full suite: 1149 passed.

The first run through plan → apply surfaced four defects. The previous bench edited the code by hand, so it never ran this path. All four are fixed:
1. **From-versions were reconstructed.** An unpinned manifest re-resolved to the newest release: the plan diffed pandas 3.1.1 → 2.2.3, found no rewrite and mis-rated every risk.
   - The runtime now records `distributions` (dist → version of every loaded installed distribution, read from `*.dist-info`/`*.egg-info` metadata, ~4 ms) in each process's capture metadata.
   - `plan` takes the from-side from the baseline's record (`witness_rows`) and states each correction.
2. **The post-migration capture after `apply` was narrowed to the plan's symbols.** v0.1.0 auto-filled impact targets, which with no explicit mode meant watch-only, so every other baseline call showed as DISAPPEARED. Plan-derived targets now widen the default mode (`widen_targets`); an explicit `--impact-targets` still means a targeted capture.
3. **`__init__`'s receiver was rendered per backend:** opaque before the body runs, the finished object after it. The same constructor call keyed differently, so it came out as DISAPPEARED + APPEARED. The receiver is now canonicalized to `{"__opaque__": type}` when facts load.
4. **Calibration was line-wide and penalized fixes:**
   - a drift of `groupby` scored the `GroupBy.sum` risk on the same line;
   - a removed call the plan rewrote, with its behaviour kept, counted as a miss.
   
   Both are fixed: scoring is per symbol, and the second case now counts as **prevented**.
