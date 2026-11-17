---
name: migration-contract
description: Use when migrating a Python project to a new Python version or a major dependency version (3.6/3.8 → 3.12/3.13, Pandas 1→2, Pydantic v1→v2, SQLAlchemy 1.4→2.0, Flask/Django upgrades), reviving an abandoned or dead-framework project, or when asked whether an upgrade is safe, what it would break, or which dependency calls a project makes. Drives the `pymolt` CLI, which records what the code actually calls before and after the change and reports what changed.
---

# Migrating a Python project, provably

The hard part of a migration is not resolving dependencies: you can do that
yourself with `uv pip compile` in under a minute. The hard part is knowing
whether the program still behaves the same afterwards. That is what pymolt is
for, and it is the part you cannot do by reading files.

You drive pymolt through the shell. Run every command with `--agent`:

```bash
pymolt <command> <project-dir> --agent
```

It prints one JSON object on stdout (diagnostics go to stderr):

```json
{"ok": true, "summary": "...", "data": {...}, "full_report_path": "...", "hint": "..."}
```

- `summary` is the answer in a few lines, `hint` the next step.
- `data.warnings` lists what pymolt flagged. Read it and repeat what matters to the user.
- `data.complete` is false when a list was cut; `data.truncated` says which and
  how many items were omitted. Then read `full_report_path` before you answer.
  Answering from a partial list is the usual way to get this wrong.
- On failure you get `{"ok": false, "error": "...", "hint": "..."}`. Do what the hint says.
- `--json` instead of `--agent` prints the full payload. Use it only when you need
  detail the view leaves out; `full_report_path` usually holds the same thing.
- Exit codes: 0 ok, 1 negative answer (verify FAIL, capture command failed),
  2 bad input, 3 environment problem, 4 inconclusive. The JSON is on stdout in every case.

## Two rules that decide whether the migration is verifiable at all

**1. Capture the baseline BEFORE touching any code.** A baseline recorded after
the first edit is not a baseline. It needs `setup` first: a capture records the
environment it ran in. If the user has already started editing, say so plainly
and offer the fallback: capture from a clean checkout of the pre-migration
commit, or proceed with static-only evidence and label it as such.

**2. A successful resolve is not proof.** Never tell the user a migration is
"feasible" or "done" because `assess` resolved or the tests passed. Quote the
assess verdict (`data.verdict.title`) and the base tier (`data.baseline_tier`):
gold (pinned, production-exact), silver (unpinned, but its tests pass), bronze
(unpinned, unvalidated) or none (no base resolved). Then point at the
behavioural evidence.

## The sequence

```bash
pymolt status . --agent                     # where things stand and the next command
pymolt scan . --agent                       # roots, dependencies, call sites (not imports)
pymolt setup . --agent                      # lists the valid choices, saves nothing
pymolt setup . --manifest requirements.txt --tool uv \
  --base-python 3.8 --target-python 3.12 --agent      # saves them
pymolt assess . --agent                     # resolve the target, pin requirements-target.txt
pymolt capture baseline --mode tests --project-dir . --agent -- pytest -q   # BEFORE any edit
pymolt plan . --agent                       # codemods as a plan + diffs; writes no source
# ... the user approves and applies the plan, or you edit the code;
#     build the target environment (assess data.target_env says how) ...
pymolt capture post-migration --mode tests --project-dir . --agent -- pytest -q
pymolt verify . --agent                     # the verdict
```

Take the setup values from the listing; do not guess them. Repeat edit →
`capture post-migration --force` → `verify` until verify says PASS. A capture
that already exists needs `--force` (the old one is archived, not deleted).

Check `data.accepted` on every capture. `false` means the run cannot stand as
evidence (the command failed, the trace is empty, a process ran untraced); the
summary says why. Fix that before going on.

## Reading verify

Read the coverage numbers to the user:

- **confirmed**: calls the run exercised on both sides.
- **BLIND**: calls in the static map the run never touched. High BLIND means the
  test suite proves little, however green it is. Say this out loud. A PASS covers
  only what was exercised. `data.blind_split` says why: not run (a test gap), ran but
  not recorded (a tracer gap), unobservable (no capture can record it).
- **coverage %** (`data.coverage_pct`): the share of the static contacts the run reached.
- **recall %** (`data.recall_pct`): the share of the contacts that ran which the capture
  recorded. Below 100% is a tracer limit: say so.
- `data.granularity` says how "ran" was known: `line` (3.12+), `function` (3.6–3.11,
  so recall is a lower bound), `file` (older captures), `symbol`.

`data.changes` lists the dependency calls that now return or raise something
else, or stopped happening. A green test suite does not tell you that.
`baseline_stale` true means a capture describes another environment: capture again.

## Rules

- Count call sites with `scan`, never by grepping imports: an import names a
  module, not the symbols called, and undercounts the contact surface severalfold.
- `assess` output is an input, not a conclusion. Report which versions move and
  what enters the manual zone; leave the verdict to `verify`.
- `plan` writes no source. Never run `pymolt apply` without the user's explicit
  go-ahead in this conversation, after they have seen the diffs,
  `data.advisories` (sites that need a person) and `data.manual` (rule matches
  that are not applied). `pymolt rollback .` undoes an apply.
- Use `pymolt --help` and `pymolt <command> --help` for options; do not invent flags.

## Dead frameworks

For a project built on a dead framework (TF1 + standalone Keras, say), present
both strategies and let the user choose: an in-place shim (living corpse: cheap,
low ceiling, e.g. `compat.v1`) or a transplant (durable, effortful, e.g. Keras →
torchvision with an `.h5` → `.pth` weight port). If the base tier is `none`,
relay `data.baseline_env` from assess and stop: either build that legacy
environment in a container, or hand it back. Never fabricate a base environment.
