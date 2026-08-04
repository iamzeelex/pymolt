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

Every tool takes `dir: str = "."` (the project directory).

| Tool | Mirrors | What it does |
|------|---------|--------------|
| `scan` | `pymolt scan` | Project roots, Python-version evidence + divergence, dependency edges (offline, no resolution). Use instead of reading Dockerfiles/lockfiles/tox by hand. |
| `setup_options` | `pymolt setup` (detection) | Lists the exact manifest / tool / interpreter / target-Python strings `setup_apply` accepts. |
| `setup_apply` | `pymolt setup` (apply) | Writes `.pymolt/env_config.json`. Deterministic: an invalid choice returns `ok:false` with the valid values. |
| `assess` | `pymolt assess` | Resolves baseline vs target graphs, compares per package (blockers/upgrades first), writes the pinned target manifest. `risk=true` does cached network calls. |
| `env_create` | `pymolt env create` | Provisions the target migration environment (uv venv or derived Dockerfile) from the assess target manifest. Run assess first. |
| `contract_map` | `pymolt contract map` | Static contact map: where your code calls into third-party deps. |
| `contract_capture` | `pymolt contract capture` | Runs YOUR command with the boundary tracer injected (zero edits to the project); records a `baseline` / `post-migration` slot. |
| `contract_report` | `pymolt contract report` | The verification oracle: static × dynamic → confirmed / BLIND / trust %, plus the behavioral verdict (result_changed / raise_changed / disappeared) across the migration. |
| `codemods_preview` | `pymolt codemods` (dry-run) | Dry-run preview of the codemods that would rewrite call sites. Never writes files, never spends money. |

### Workflow

```
scan  ->  (setup_options -> setup_apply)  ->  assess  ->  env_create
->  [human/agent edits the project's code]
->  contract_capture(when='baseline'  BEFORE editing)
->  contract_capture(when='post-migration'  AFTER editing)
->  contract_report   (repeat edits + post-migration capture until no result_changed)
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

## The money rule

`codemods_preview` is a **dry run**: it never writes files and never spends
slots or money. Applying codemods (and any purchase) always requires explicit
human approval through the CLI (`pymolt codemods ... --write`), outside this
server. Agents preview and recommend; humans apply.
