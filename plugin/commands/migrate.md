---
description: Walk a Python project through the pymolt migration funnel (scan → setup → assess → capture → plan → verify), honestly.
argument-hint: "[project-dir]"
---

Drive the migration of the project at **$ARGUMENTS** (default: current directory) using the
pymolt MCP tools. Do each phase, report its `summary` and next-step `hint`, and stop for the
user where a human decision or a build step is required.

`status` says where the project is and which tool is next; use it whenever unsure.

1. **scan** — project roots, dependency edges, and every call site into a dependency (the
   surface a change could break). Note the detected frameworks (e.g. `tensorflow`, `keras`)
   and any Python-version divergence.
2. **setup** — call it with no choices to list them, then with manifest, tool, base_python and
   target_python to save them.
3. **capture(when='baseline')** — before any edit. It is the only artifact here that cannot be
   reconstructed later. If the project has already been edited, say so and offer to capture
   from a clean pre-migration checkout.
4. **assess** with a target Python — read the **medallion base tier** (`baseline_tier`) and the honest verdict:
   - `none` (no legacy interpreter): surface the base-environment hint and STOP — the user (or an
     agent) must stand up the legacy env, then re-run with `--container`.
   - `bronze`: unpinned/unvalidated — say so; do not call the migration "feasible".
   - `silver`/`gold`: proceed.
5. **plan** — saves the codemods as the migration plan and returns the diffs; writes no
   source. Show the diffs. After explicit user confirmation, the user runs `pymolt apply .`;
   `pymolt rollback .` undoes it. (You may also edit the code yourself.)
6. **capture(when='post-migration')** then **verify** — the verdict. Report all three numbers:
   confirmed, **BLIND** (surface the run never touched — a green suite with high BLIND proves
   little, say so), and trust %. Loop edit → re-capture → verify until PASS.
   A passing test run promotes a bronze base to **silver**.

Rules: never claim "upgrade feasible" or "migration done" from a successful resolve or a green
test run alone — cite the base tier and the verify verdict. If a tool's `summary` says its
list was truncated, read the `full_report_path` file before answering. For a dead-framework project (TF1 + standalone Keras), note that in-place shims are the
cheap path and a transplant (torchvision) is the durable one.
