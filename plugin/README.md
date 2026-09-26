# pymolt — Claude Code plugin

Gives your agent the one part of a Python migration it cannot do by reading files: proof
that behaviour didn't change. Bundles the contract tools and the funnel that feeds them,
and **auto-registers** the pymolt stdio MCP server (no manual `claude mcp add`).

Your agent can already resolve dependencies with `uv`. What it cannot do is record what
your code actually calls at runtime — before and after — and tell you what changed. Asked
to count its own dependency contact surface unaided, a coding agent undercounts it
severalfold; with these tools it gets the exact number. See `bench/RESULTS.md` in the repo.

## Prerequisite

Install the CLI with the MCP extra so the `pymolt` command exists on your PATH:

```bash
uv tool install 'pymolt[mcp]'   # or: pipx install 'pymolt[mcp]'
```

## Install the plugin

**Claude Code** (from this repo):

```
/plugin marketplace add zeelex/python_migrator
/plugin install pymolt
```

or point `/plugin` at a local checkout's `plugin/` directory. Installing wires the MCP server
declared in `.claude-plugin/plugin.json` — restart Claude Code and the `pymolt` tools appear.

**Codex / other MCP clients** — the plugin format is Claude-Code-specific; register the server
directly instead:

```toml
# ~/.codex/config.toml
[mcp_servers.pymolt]
command = "pymolt"
args = ["mcp"]
```

## What you get

- **Proof tools** — `contract_map` (every call site into a dependency, resolved through
  imports and scopes), `contract_capture` (runs *your* command with a tracer injected,
  zero edits), `contract_report` (confirmed / BLIND / trust %, and what changed).
- **Preparation tools** — `scan`, `setup_options`, `setup_apply`, `assess`,
  `codemods_preview`, `codemods`, `env_hint`, `status`, `migrate`. For approved source changes,
  use the CLI `pymolt plan` → `pymolt apply`; `pymolt rollback` retains the recovery path.
- **Skill**: `migration-contract` — loads automatically when you ask about a Python or
  dependency upgrade, so the agent captures a baseline *before* it starts editing.
- **Slash command**: `/pymolt:migrate [project-dir]` — walk the funnel honestly.
- **Subagent**: `pymolt-migrator` — drives a dead-project migration, reporting the medallion
  baseline tier and never overclaiming feasibility.

> The single most important thing this plugin changes: it makes the agent record a
> **baseline before the first edit**. That recording cannot be recreated afterwards.

## Sanity check

```bash
# a human running the server directly gets guidance, not a hang:
pymolt mcp
# force-run for a manual JSON-RPC test:
pymolt mcp --serve
```
