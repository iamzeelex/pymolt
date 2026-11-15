# MCP server (`pymolt mcp`)

A stdio [Model Context Protocol](https://modelcontextprotocol.io) server for
clients that have no shell. It is optional: agents with a shell use the CLI
(`pymolt <command> --agent` for the compact view, `--json` for the full payload),
and the Claude Code plugin does so without any MCP server.

The server is a thin adapter over the CLI view layer
(`pymolt/interfaces/views.py`). Each tool calls the same service function as the
matching CLI command and returns exactly what `pymolt <command> --agent` prints
for the same project; a parity test asserts this. It has no analysis logic of its
own.

## Install

The MCP SDK is an optional extra (it pulls in starlette/uvicorn, too heavy for
the offline-first core), so install PyMolt with the `mcp` extra. It pins `mcp>=1.2,<2`, because the server
uses the 1.x FastMCP API:

```bash
uv tool install 'pymolt[mcp]'          # or: pipx install 'pymolt[mcp]'
```

If you run `pymolt mcp` without the extra, it exits with a clear message telling
you to reinstall with `pymolt[mcp]` — no traceback.

## Client wiring

```bash
claude mcp add pymolt -- pymolt mcp    # Claude Code
```

For any other MCP client (ChatGPT, Cursor, a local model), configure a **stdio**
server whose command is `pymolt mcp`. The server name is `pymolt`.

## Tools

The tools have the names of the CLI commands. Every tool takes
`project_dir: str = "."` (the project directory).

| Tool | Same as | What it does |
|------|---------|--------------|
| `status` | `pymolt status` | Where the project stands, whether the configured environments exist, and the one tool to run next. |
| `scan` | `pymolt scan` | Project roots, Python-version evidence + divergence, dependency edges, and every call site into a dependency (offline, no resolution). Use instead of reading Dockerfiles/lockfiles or grepping imports. |
| `setup` | `pymolt setup` | With no choices: the exact manifest / tool / interpreter / target-Python strings it accepts. With them: writes `.pymolt/env_config.json`; an invalid choice returns `ok:false` with the valid values. |
| `assess` | `pymolt assess` | Resolves base vs target graphs, compares per package (blockers/upgrades first), writes the pinned target manifest, and returns how to build the target environment. `risk=true` does cached network calls. |
| `capture` | `pymolt capture` | Runs YOUR command with the boundary tracer injected and keeps it as the baseline or post-migration capture. Needs setup. Runs a host process, so it is opt-in: set `PYMOLT_MCP_ALLOW_EXEC=1` in the server's environment. |
| `plan` | `pymolt plan` | Saves the API changes, codemods and predicted risks for the target manifest as the migration plan and returns the diffs. Writes no source. |
| `verify` | `pymolt verify` | The verdict: PASS / FAIL / INCONCLUSIVE with reasons, confirmed / BLIND / trust %, and the calls that changed, plus how the plan's predicted risks fared. Saved like the CLI's. |

### Workflow

```
status (any time: which tool is next)
scan  ->  setup  ->  assess
->  capture(when='baseline'  BEFORE editing)
->  plan  ->  [a person runs `pymolt apply .`]   (or the agent edits the code)
->  capture(when='post-migration'  AFTER editing)
->  verify
```

This is also encoded in the server's `instructions`, which the client surfaces
to the model.

## Output contract

Every tool returns a JSON-serializable dict:

```json
{
  "ok": true,
  "summary": "<=6 short lines, plain-text digest",
  "data": { "...tool-specific..." },
  "full_report_path": "<path or null>",
  "hint": "<next-step suggestion or null>"
}
```

On any failure the tool catches the exception and returns:

```json
{ "ok": false, "error": "<one-line actionable message>", "hint": "<how to fix>" }
```

Exceptions never cross the MCP boundary. Lists that are context around an answer
are capped at 20 items; lists the agent answers from (which packages change, which
calls changed) are cut by size, not by count. Counts are always the true totals,
`complete` says whether every list is whole, and `truncated` gives the number
omitted per list. The full report is written to `.pymolt/agent/<command>.json`
(the same file `--agent` writes) and its path returned in `full_report_path`.
Output is plain text, with no rich/ANSI.

## No source writes

`plan` saves the plan and returns the diffs; no tool writes a source file.
Applying a plan always requires explicit human approval through the CLI
(`pymolt apply .`), outside this server. Agents plan and recommend; humans
apply. The applied plan retains rollback data (`pymolt rollback .`).
