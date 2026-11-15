---
name: pymolt
description: Use when migrating a Python project to a new Python version or a major dependency version (3.6/3.8 → 3.12/3.13, Pandas 1→2, Pydantic v1→v2, SQLAlchemy 1.4→2.0, Flask/Django upgrades), reviving an abandoned or dead-framework project, or when asked whether an upgrade is safe, what it would break, or which dependency calls a project makes. Provides PyMolt's behavioural contract through the `pymolt` CLI — recording what the code actually calls before and after the change and reporting what changed.
---

# Migrating a Python project, provably (pymolt CLI)

The `pymolt` CLI is on your PATH. Prefer it over reading Dockerfiles, lockfiles,
manifests or running analysis by hand: `pymolt scan` -> `pymolt setup` ->
`pymolt assess` -> `pymolt contract map` / `pymolt contract capture` /
`pymolt contract report`. They are the pymolt funnel and return authoritative
results. Pass `--json` to every report command; diagnostics go to stderr.

The hard part of a migration is not resolving dependencies — you can do that
yourself with `uv pip compile` in under a minute. The hard part is knowing
whether the program still behaves the same afterwards. That is what PyMolt's
contract commands exist for, and it is the part you cannot do by reading files.

## Two rules that decide whether the migration is verifiable at all

**1. Capture the baseline BEFORE touching anything.** A baseline recorded after
the first edit is not a baseline. If the user has already started editing, say so
plainly and offer the fallback: capture from a clean checkout of the pre-migration
commit, or proceed with static-only evidence and label it as such.

**2. A successful resolve is not proof.** Never tell the user a migration is
"feasible" or "done" because `assess` resolved or the tests passed. Cite the
**medallion baseline tier** — gold (pinned, production-exact) · silver (unpinned
but tests pass) · bronze (unpinned, unvalidated) · none (no legacy interpreter) —
and point at the behavioural evidence.

## The sequence

```bash
pymolt contract map . --json        # see the surface first: call sites, not imports
pymolt contract capture --when baseline --mode tests -- <python> -m pytest -q   # BEFORE any edit
pymolt scan . --json ; pymolt setup . ; pymolt assess . --target-python 3.12 --json   # prepare the migration
# … edit code, build the target environment, apply codemods …
pymolt contract capture --when post-migration --mode tests -- <target python> -m pytest -q
pymolt contract report . --json     # the verdict
```

`pymolt status . --json` tells you where the migration stands and the one
command to run next.

Repeat the edit → re-capture → report loop until `pymolt contract report` comes
back clean. Each report gives three numbers; read all three to the user:

- **confirmed** — calls the run actually exercised on both sides.
- **BLIND** — calls in the static map the run never touched. High BLIND means the
  test suite proves little, however green it is. Say this out loud.
- **trust %** — confirmed against the static denominator.

## Reading the output honestly

- `pymolt contract map` counts **call sites**, not imports. If you are tempted to
  grep imports instead, don't — measured against this command, import-grepping
  undercounts the real contact surface severalfold.
- `pymolt assess` output is an input, not a conclusion. Report which versions move
  and what enters the manual zone; leave the verdict to `pymolt contract report`.
- Every `--json` payload is the complete report — nothing is clipped. If it is
  large, save it to a file and query the fields you need rather than answering
  from a partial read; answering from a partial list is the classic way to get
  this wrong.
- `pymolt codemods .` is a dry run by default. Applying codemods needs explicit
  human approval, outside the agent loop (`pymolt plan .` → `pymolt apply .`).

## Dead frameworks

For a project built on a dead framework (TF1 + standalone Keras, say), present
both strategies and let the user choose: an in-place shim (living corpse — cheap,
low ceiling, e.g. `compat.v1`) or a transplant (durable, effortful, e.g. Keras →
torchvision with an `.h5` → `.pth` weight port). If the baseline tier is `none`,
surface the baseline-env hint from `pymolt env hint` and stop — either build that
legacy environment in a container, or hand it back. Never fabricate a baseline.
