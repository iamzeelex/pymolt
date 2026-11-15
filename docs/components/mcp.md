# MCP server (`pymolt mcp`)

A stdio [Model Context Protocol](https://modelcontextprotocol.io) server that
exposes PyMolt's migration funnel as native agent tools. It is the same
scan → setup → assess → contract → codemods logic the CLI runs, reshaped so an
agent gets compact, verified, token-disciplined facts instead of re-discovering
the repo and trusting its memory of APIs that moved since training.

The server is a thin interface: every tool calls the same service function the
matching CLI command calls and never re-implements analysis.

## Install

The MCP SDK is an optional extra (it pulls in starlette/uvicorn, too heavy for
the offline-first core), so install PyMolt with the `mcp` extra:

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

| Tool | Mirrors | What it does |
|------|---------|--------------|
| `status` | `pymolt status` | Where the project stands, whether the configured environments exist, and the one tool to run next. |
| `scan` | `pymolt scan` | Project roots, Python-version evidence + divergence, dependency edges, and every call site into a dependency (offline, no resolution). Use instead of reading Dockerfiles/lockfiles or grepping imports. |
| `setup` | `pymolt setup` | With no choices: the exact manifest / tool / interpreter / target-Python strings it accepts. With them: writes `.pymolt/env_config.json`; an invalid choice returns `ok:false` with the valid values. |
| `assess` | `pymolt assess` | Resolves base vs target graphs, compares per package (blockers/upgrades first), writes the pinned target manifest, and returns how to build the target environment. `risk=true` does cached network calls. |
| `capture` | `pymolt capture` | Runs YOUR command with the boundary tracer injected and keeps it as the baseline or post-migration capture. Needs setup. Opt-in: `PYMOLT_MCP_ALLOW_EXEC=1`. |
| `plan` | `pymolt plan` | Saves the codemods for the target manifest as the migration plan and returns the diffs. Writes no source. |
| `verify` | `pymolt verify` | The verdict: PASS / FAIL / INCONCLUSIVE with reasons, confirmed / BLIND / trust %, and the calls that changed. Saved like the CLI's. |

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

Exceptions never cross the MCP boundary. Lists in `data` are capped at 20 items,
with a sibling `"truncated": <N>` when cut; the full payload is written under
`.pymolt/mcp/<tool>.json` and its path returned in `full_report_path`. Output is
plain text — no rich/ANSI.

## No source writes

`plan` saves the plan and returns the diffs; no tool writes a source file.
Applying a plan always requires explicit human approval through the CLI
(`pymolt apply .`), outside this server. Agents plan and recommend; humans
apply. The applied plan retains rollback data (`pymolt rollback .`).
