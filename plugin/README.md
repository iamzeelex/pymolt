# pymolt — Claude Code plugin

Gives your agent the one part of a Python migration it cannot do by reading files: proof
that behaviour didn't change. The plugin teaches the agent to drive the `pymolt` CLI from
the shell; it registers no server and costs no tokens until a migration comes up.

Your agent can already resolve dependencies with `uv`. What it cannot do is record what
your code actually calls at runtime, before and after, and tell you what changed. Asked
to count its own dependency contact surface unaided, a coding agent undercounts it
severalfold; with pymolt it gets the exact number. See `bench/RESULTS.md` in the repo.

## Prerequisite

Install the CLI so the `pymolt` command is on your PATH:

```bash
uv tool install pymolt   # or: pipx install pymolt
```

## Install the plugin

**Claude Code** (from this repo):

```
/plugin marketplace add zeelex/python_migrator
/plugin install pymolt
```

or point `/plugin` at a local checkout's `plugin/` directory.

## What you get

- **Skill**: `migration-contract` loads when you ask about a Python or dependency upgrade.
  It runs `pymolt <command> --agent` for each step (`status`, `scan`, `setup`, `assess`,
  `capture`, `plan`, `verify`) and makes the agent capture a baseline *before* it starts
  editing.
- **Slash command**: `/pymolt:migrate [project-dir]` walks the funnel honestly.
- **Subagent**: `pymolt-migrator` drives a dead-project migration, reporting the medallion
  base tier and never overclaiming feasibility.

> The single most important thing this plugin changes: it makes the agent record a
> **baseline before the first edit**. That recording cannot be recreated afterwards.

### What the agent reads

`--agent` prints one compact JSON object per command:

```json
{"ok": true, "summary": "...", "data": {...}, "full_report_path": "...", "hint": "..."}
```

`data.warnings` carries everything pymolt flagged, and `data.complete` / `data.truncated` say
whether every list is whole; the rest is in `full_report_path`. `--json` prints the full
payload instead. Source changes need the user's go-ahead: `plan` writes no source,
`pymolt apply` applies the plan, `pymolt rollback` undoes it.

## Clients without a shell: attach the MCP server

Agents that cannot run commands (chat clients, some IDE assistants) can use the same
seven commands as MCP tools. Each tool returns exactly what `pymolt <command> --agent`
prints. The server is an optional extra:

```bash
uv tool install 'pymolt[mcp]'   # or: pipx install 'pymolt[mcp]'
```

Register it as a stdio server whose command is `pymolt mcp`:

```bash
claude mcp add pymolt -- pymolt mcp          # Claude Code, if you want tools instead of the CLI
```

```toml
# ~/.codex/config.toml
[mcp_servers.pymolt]
command = "pymolt"
args = ["mcp"]
```

The server only reads and writes inside its working directory (set `PYMOLT_MCP_ROOT` to
change that root). The `capture` tool runs a command on your machine, so it is off until
you set `PYMOLT_MCP_ALLOW_EXEC=1` in the server's environment. No tool writes source.

Sanity check:

```bash
pymolt mcp           # in a terminal: prints how to register it instead of hanging
pymolt mcp --serve   # force-run for a manual JSON-RPC test
```
