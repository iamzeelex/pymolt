---
name: pymolt-migrations
description: Drive Python dependency and version migrations with PyMolt — scan repo facts, assess feasibility and risk, verify runtime behavior with the contract loop. Use when upgrading Python or dependencies (pandas, django, numpy…), or when asked what a version bump will break.
---

# PyMolt migrations

PyMolt is the ground-truth sensor for Python migrations: deterministic facts
with provenance instead of token-hungry repo spelunking and API guesswork.

## Prefer the MCP tools

If the `pymolt` MCP server is connected, use its tools (`status`, `scan`,
`setup`, `assess`, `capture`, `plan`, `verify`): the same names as the CLI
commands. Otherwise every command below works in the shell with `--json`.

## Workflow

1. **Facts first — never spelunk.** `scan` gives every surface, Python-version
   divergence, dependency edge and call site into a dependency, with provenance.
   Do not read Dockerfiles/lockfiles/tox configs manually for this.
2. **Pin the migration.** `setup` with no choices lists them; `setup` with
   manifest/tool/base+target saves them.
3. **Feasibility + risk.** `assess` (with risk) — real lock-first resolution
   and live CVE / wheel / abandonment data. Never guess package versions from
   memory.
4. **Baseline before touching code.**
   `capture when=baseline mode=tests command=<test cmd>`.
5. **Change the code.** `plan` returns the codemods as diffs and saves them as
   the plan; the human applies it with `pymolt apply`. Or edit the code yourself.
6. **Close the loop.** `capture when=post-migration …` then `verify`. Iterate
   until verify says PASS. Green tests are not the oracle — verify is.

## Hard rules

- A fact from PyMolt carries provenance + confidence — cite it, don't re-derive it.
- BLIND zones in the report are honest gaps: exercise them with tests or flag
  them to the human, never paper over them.
- **No source writes from the agent loop:** `plan` writes no source. Applying it
  requires the human's explicit approval via the CLI (`pymolt apply`).
