---
description: Walk a Python project through the pymolt migration funnel (scan → assess → contract → codemods), honestly.
argument-hint: "[project-dir]"
---

Drive the migration of the project at **$ARGUMENTS** (default: current directory) using the
pymolt MCP tools. Do each phase, report its `summary` and next-step `hint`, and stop for the
user where a human decision or a build step is required.

1. **scan** — surface map + dependency edges. Note the detected frameworks (e.g. `tensorflow`,
   `keras`) and any Python-version divergence.
2. **assess** with a target Python — read the **medallion baseline tier** and the honest verdict:
   - `none` (no legacy interpreter): surface the baseline-env hint and STOP — the user (or an
     agent) must stand up the legacy env, then re-run with `--container`.
   - `bronze`: unpinned/unvalidated — say so; do not call the migration "feasible".
   - `silver`/`gold`: proceed.
3. **contract_map** + **contract_capture** (baseline slot) — establish the behavioural contract.
   A passing test run promotes a bronze baseline to **silver**.
4. **codemods_preview** — fetch + preview codemods (dry-run). Show the diff cards; apply only on
   explicit user confirmation.

Rules: never claim "upgrade feasible" from a successful resolve alone — always cite the baseline
tier. For a dead-framework project (TF1 + standalone Keras), note that in-place shims are the
cheap path and a transplant (torchvision) is the durable one.
