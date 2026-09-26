---
name: migration-contract
description: Use when migrating a Python project to a new Python version or a major dependency version (3.6/3.8 → 3.12/3.13, Pandas 1→2, Pydantic v1→v2, SQLAlchemy 1.4→2.0, Flask/Django upgrades), reviving an abandoned or dead-framework project, or when asked whether an upgrade is safe, what it would break, or which dependency calls a project makes. Provides PyMolt's behavioural contract — recording what the code actually calls before and after the change and reporting what changed.
---

# Migrating a Python project, provably

The hard part of a migration is not resolving dependencies — you can do that
yourself with `uv pip compile` in under a minute. The hard part is knowing
whether the program still behaves the same afterwards. That is what PyMolt's
contract tools exist for, and it is the part you cannot do by reading files.

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

```
contract_map            # see the surface first: call sites, not imports
contract_capture(when='baseline')      # BEFORE any edit
scan → setup_options/setup_apply → assess    # prepare the migration
… edit code, build the target environment, apply codemods …
contract_capture(when='post-migration')
contract_report         # the verdict
```

Repeat the edit → re-capture → report loop until `contract_report` comes back
clean. Each report gives three numbers; read all three to the user:

- **confirmed** — calls the run actually exercised on both sides.
- **BLIND** — calls in the static map the run never touched. High BLIND means the
  test suite proves little, however green it is. Say this out loud.
- **trust %** — confirmed against the static denominator.

## Reading the tools honestly

- `contract_map` counts **call sites**, not imports. If you are tempted to grep
  imports instead, don't — measured against this tool, import-grepping undercounts
  the real contact surface severalfold.
- `assess` output is an input, not a conclusion. Report which versions move and
  what enters the manual zone; leave the verdict to `contract_report`.
- Every tool returns a `summary` that states its own completeness ("All 31
  listed" / "Showing 20 of 31 … full list: <path>"). If it says a list was
  truncated, **read the `full_report_path` file** before answering — answering
  from a partial list is the classic way to get this wrong.
- `codemods_preview` is a dry run. Applying codemods needs explicit human
  approval, in the CLI, outside the agent loop.

## Dead frameworks

For a project built on a dead framework (TF1 + standalone Keras, say), present
both strategies and let the user choose: an in-place shim (living corpse — cheap,
low ceiling, e.g. `compat.v1`) or a transplant (durable, effortful, e.g. Keras →
torchvision with an `.h5` → `.pth` weight port). If the baseline tier is `none`,
surface the baseline-env hint from `env_hint` and stop — either build that legacy
environment in a container, or hand it back. Never fabricate a baseline.
