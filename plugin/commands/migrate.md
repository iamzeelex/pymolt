---
description: Walk a Python project through the pymolt migration funnel (scan → assess → contract → codemods), honestly.
argument-hint: "[project-dir]"
---

Drive the migration of the project at **$ARGUMENTS** (default: current directory) using the
pymolt MCP tools. Do each phase, report its `summary` and next-step `hint`, and stop for the
user where a human decision or a build step is required.

0. **contract_map**, then **contract_capture(when='baseline')** — FIRST, before any edit and
   before assess. The contact map shows the surface a change could break; the baseline
   recording is the only artifact here that cannot be reconstructed later. If the project has
   already been edited, say so and offer to capture from a clean pre-migration checkout.

1. **scan** — surface map + dependency edges. Note the detected frameworks (e.g. `tensorflow`,
   `keras`) and any Python-version divergence.
2. **assess** with a target Python — read the **medallion baseline tier** and the honest verdict:
   - `none` (no legacy interpreter): surface the baseline-env hint and STOP — the user (or an
     agent) must stand up the legacy env, then re-run with `--container`.
   - `bronze`: unpinned/unvalidated — say so; do not call the migration "feasible".
   - `silver`/`gold`: proceed.
3. **contract_capture(when='post-migration')** then CLI **`pymolt verify .`** — the verdict. Report
   all three numbers: confirmed, **BLIND** (surface the run never touched — a green suite with
   high BLIND proves little, say so), and trust %. Loop edit → re-capture → report until clean.
   A passing test run promotes a bronze baseline to **silver**.
4. **codemods_preview** — fetch + preview codemods (dry-run). Show the diff cards. After explicit
   user confirmation, use the CLI `pymolt plan .` then `pymolt apply .`; rollback is available via
   `pymolt rollback .`.

Rules: never claim "upgrade feasible" or "migration done" from a successful resolve or a green
test run alone — cite the baseline tier and the contract report. If a tool's `summary` says its
list was truncated, read the `full_report_path` file before answering. For a dead-framework project (TF1 + standalone Keras), note that in-place shims are the
cheap path and a transplant (torchvision) is the durable one.
