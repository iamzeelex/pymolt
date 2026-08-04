# pymolt — Claude Code plugin

Bundles the pymolt migration funnel as agent tools + slash commands, and **auto-registers**
the pymolt stdio MCP server (no manual `claude mcp add`).

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

- **MCP tools**: `scan`, `assess`, `contract_map`, `contract_capture`, `contract_report`,
  `codemods_preview`, `env_hint`, `setup_options`, `setup_apply`.
- **Slash command**: `/pymolt:migrate [project-dir]` — walk the funnel honestly.
- **Subagent**: `pymolt-migrator` — drives a dead-project migration, reporting the medallion
  baseline tier and never overclaiming feasibility.

## Sanity check

```bash
# a human running the server directly gets guidance, not a hang:
pymolt mcp
# force-run for a manual JSON-RPC test:
pymolt mcp --serve
```
