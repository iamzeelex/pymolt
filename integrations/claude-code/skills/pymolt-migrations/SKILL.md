---
name: pymolt-migrations
description: Drive Python dependency and version migrations with PyMolt — scan repo facts, assess feasibility and risk, verify runtime behavior with the contract loop. Use when upgrading Python or dependencies (pandas, django, numpy…), or when asked what a version bump will break.
---

# PyMolt migrations

PyMolt is the ground-truth sensor for Python migrations: deterministic facts
with provenance instead of token-hungry repo spelunking and API guesswork.

## Prefer the MCP tools

If the `pymolt` MCP server is connected, use its tools (`scan`,
`setup_options`/`setup_apply`, `assess`, `contract_map`, `contract_capture`,
`contract_report`, `codemods_preview`). Otherwise every command below works in
the shell with `--json`.

## Workflow

1. **Facts first — never spelunk.** `scan` gives every surface, Python-version
   divergence and dependency edge with provenance (~2k tokens). Do not read
   Dockerfiles/lockfiles/tox configs manually for this.
2. **Pin the migration.** `setup_options` → pick manifest/tool/base+target →
   `setup_apply`.
3. **Feasibility + risk.** `assess` (with risk) — real lock-first resolution
   and live CVE / wheel / abandonment data. Never guess package versions from
   memory.
4. **Baseline before touching code.**
   `contract_capture when=baseline mode=tests command=<test cmd>`.
5. **Edit code** (yours or the human's changes).
6. **Close the loop.** `contract_capture when=post-migration …` then
   `contract_report`. Iterate until no `result_changed` / `raise_changed`
   remains. Green tests are not the oracle — the contract report is.

## Hard rules

- A fact from PyMolt carries provenance + confidence — cite it, don't re-derive it.
- BLIND zones in the report are honest gaps: exercise them with tests or flag
  them to the human, never paper over them.
- **Money:** codemod recipes cost real money ($5/$10/$25 by delivered weight;
  the first one is free). `codemods_preview` is always safe (dry-run, no
  spend). Anything that consumes a slot requires the human's explicit approval
  — show the quote, wait for a yes.
