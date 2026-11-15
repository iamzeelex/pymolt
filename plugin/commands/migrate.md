---
description: Walk a Python project through the pymolt migration funnel (scan → setup → assess → capture → plan → verify), honestly.
argument-hint: "[project-dir]"
---

Drive the migration of the project at **$ARGUMENTS** (default: current directory) with the
`pymolt` CLI. Run every command with `--agent`; each prints one JSON object with `summary`,
`data`, `full_report_path` and `hint`. Do each phase, report its `summary` and `hint`, and
stop for the user where a human decision or a build step is required.

`pymolt status <dir> --agent` says where the project is and which command is next; use it
whenever unsure.

1. **scan** (`pymolt scan <dir> --agent`): project roots, dependency edges, and every call
   site into a dependency (the surface a change could break). Note the detected frameworks
   (e.g. `tensorflow`, `keras`) and any Python-version divergence.
2. **setup**: `pymolt setup <dir> --agent` lists the valid choices and saves nothing; then
   `pymolt setup <dir> --manifest … --tool … --base-python … --target-python … --agent`
   saves them. Take the values from the listing.
3. **assess** (`pymolt assess <dir> --agent`): quote `data.verdict.title` and the
   **medallion base tier** (`data.baseline_tier`):
   - `none` (no base resolved): relay `data.baseline_env` and STOP; the user (or you, in a
     container) must stand up the legacy environment first.
   - `bronze`: unpinned and unvalidated; say so, and do not call the migration "feasible".
   - `silver`/`gold`: proceed.
4. **capture baseline**, before any edit:
   `pymolt capture baseline --mode tests --project-dir <dir> --agent -- <test command>`.
   It is the only artifact here that cannot be reconstructed later. If the project has
   already been edited, say so and offer to capture from a clean pre-migration checkout.
   `data.accepted` must be true.
5. **plan** (`pymolt plan <dir> --agent`): saves the codemods as the migration plan and
   returns the diffs; writes no source. Show the diffs and `data.advisories`. Only after the
   user explicitly approves, run `pymolt apply <dir>`; `pymolt rollback <dir>` undoes it.
   (You may also edit the code yourself.)
6. **capture post-migration** (same command with `post-migration`, plus `--force` on a
   re-run), then **verify** (`pymolt verify <dir> --agent`). Report all three numbers:
   confirmed, **BLIND** (surface the run never touched; a green suite with high BLIND
   proves little, say so), and trust %, plus `data.changes`. Loop edit → re-capture →
   verify until PASS. A passing baseline test run promotes a bronze base to **silver**.

Rules: never claim "upgrade feasible" or "migration done" from a successful resolve or a green
test run alone; cite the base tier and the verify verdict. When `data.complete` is false, read
the `full_report_path` file before answering, and relay `data.warnings`. For a dead-framework
project (TF1 + standalone Keras), note that in-place shims are the cheap path and a transplant
(torchvision) is the durable one.
