# Wave 0: an honest instrument

Status: in progress, 2026-11-15. Base: `rework/cli-first-local-delta`. Issues come from `bench/legacy_eval/KEY_ISSUES.md`.

**Goal:** every statement pymolt makes is true, every stored path is anchored to the project, and the six-project evaluation is a script, not a manual run.

**No new capability here:** container probing, stdlib facts and install checks are waves 1–3.

## Owner decision (B8)
A baseline whose tests fail is **still refused by default**. The new `capture --accept-failing` flag (and MCP `capture(accept_failing=True)`) accepts it, provided the run recorded events. The capture is then stamped **red**. A run with 0 events, or one that could not start, is refused even with the flag. The red stamp shows in `capture`, `status` and `verify`, and the calls from failing tests are compared as usual.

## Shared contract

All work packages code against these names and wording.

**Capture slot** (`verify/models.py` `ContractSlot`):
- gains `red: {"returncode": int, "failed": int | None, "summary": str} | None`;
- `failed` is parsed from a pytest-style summary in the command log (`N failed`), and is None if absent;
- a red slot has `validity: "valid"`.

**Capture view** (`views.capture_view`):
- `ok` is **false** when the capture was not accepted, so an agent never reads "ok" as "evidence recorded";
- `data.accepted` stays;
- the hint follows the cause, and mentions `command_log` only when one exists:

| cause | hint |
|---|---|
| zero events | "The command ran (exit {rc}) but called no recorded dependency ({n} static contacts). Run a command that exercises them, or pass --target." |
| tests failed (rc != 0, events > 0) | "{failed or 'Some'} test(s) failed (exit {rc}). Fix or deselect them, or keep a red baseline with --accept-failing." |
| could not start / timeout | the existing reason, plus the command log path if present |

**verify's missing-side reasons** (`verify/service.py` around `:1960-1975`):

| situation | reason |
|---|---|
| only the baseline is recorded | "no post-migration capture yet (only the baseline is recorded)" |
| only the post-migration capture | "no baseline capture" |
| neither | "no captures" |

Never "no baseline trace" while a baseline exists.

**verify, impact filter:** when the static denominator is narrowed to the plan's packages, a note says "static contacts narrowed to the plan's packages ({pkgs}): {kept} of {total} kept". Trust is labelled as computed over the narrowed set.

**Plan wording** (`views.plan_view` and the CLI's plan render). Let K = API deltas that are INCONCLUSIVE and T = their total.
- If K > 0, the risk line is "Predicted risks: {a} need attention, {f} fixed by the plan; API changes unknown for {K} of {T} package(s) (inconclusive)".
- If K == T, it is "Predicted risks: none rated — API changes unknown for {K} of {T} package(s) (inconclusive)".
- If no call site matched and K > 0, the no-change line becomes "The plan changes no file, but API changes are unknown for {K} of {T} package(s)".
- "0 need attention" is never shown alone while K > 0.

**Assess on a failed resolve** (`assess/service.py`):
- `AssessResult` gains `target_error_detail: str | None` (the resolver's stderr tail, at most 40 lines, ANSI stripped) and `target_culprits: list[str]` (the package names on uv's failure lines: "Failed to build `x==1`", "Because x … depends on …", "No solution …");
- rows get `"conflict"` only for culprits and `"unresolved"` for the rest;
- `target_error` stays a one-line summary (uv's first `error:`/`×` line);
- the view counts `unresolved` separately, and its hint becomes "The target does not resolve: {summary}. Culprit(s): {…}. Fix or loosen them, then assess again."

**Plan on a resolver failure:**
- it raises `PlanInputError(message, hint)` with the resolver's summary, never a raw `CalledProcessError`;
- the CLI exits 2 with the message;
- `--agent` prints the JSON failure envelope.

**Paths** (B1):
- slot paths (`trace_path`, `metadata_path`, `command_log`, diagnostics) are stored **relative to the project root**;
- readers join relative paths onto the root, as today;
- a legacy path that only resolves relative to the cwd is accepted on read, with a warning;
- `target_env_path` is resolved against the project dir.

**Container names** (B7): print the name in full. Shorten to 12 characters only when the value is a 64-character hex id.

## Work packages and file ownership

Edit only the files you own. `views.py` is split by function.

| WP | Owns | Issues |
|---|---|---|
| WP1, paths/capture/verify | `verify/service.py`, `verify/models.py`, `verify/report.py`, `status.py`, `interfaces/cli/verify_cmd.py`, `interfaces/mcp_server.py`, and in `views.py` only `capture_view`, `capture_attach_view`, `verify_view` and `status_view` | B1, B7, B8, C4, C5, C6 |
| WP2, assess/plan/apply/succession | `assess/service.py`, `codemods/service.py`, `migration_plan.py`, `interfaces/cli/commands.py` (except the `setup` command), `strategic/succession/*`, and in `views.py` only `plan_view` and `assess_view` | B3, C1, C3, C7, C8, C11 |
| WP3, manifests/parsers/setup | `ingestion/detect.py`, `inventory/edges.py`, `setup/service.py`, `scan/*`, `ingestion/fallback_compiler.py` (container listing), and the `setup` command in `commands.py` | B4, B5, C10 |
| WP4, scripted evaluation | new files under `bench/legacy_eval/` | the `run.py` harness and the "before" scorecard |

Tests go next to the code each package owns. Each package runs its own tests. The orchestrator runs the full suite after all packages land.
