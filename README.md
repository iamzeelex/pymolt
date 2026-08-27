# PyMolt

Modernizing legacy Python codebases without breaking things.

PyMolt is an open-source migration tool and MCP server designed to take the guesswork out of Python version bumps (3.6/3.8 → 3.12/3.13) and major dependency upgrades (Pandas 1.x → 2.x, Pydantic v1 → v2, SQLAlchemy 1.4 → 2.0).

It combines lock-first dependency resolution, LibCST AST codemods, and zero-instrumentation runtime behavioral contracts. You can run it manually via CLI or hand it over to AI coding agents (Claude, Cursor, Windsurf) through its built-in Model Context Protocol server.

---

## Quick Start

Install via `uv` or `pipx`:

```bash
uv tool install pymolt
# or with MCP server support:
uv tool install "pymolt[mcp]"
```

Run an end-to-end migration in one pass:

```bash
cd your-legacy-project
pymolt migrate
```

PyMolt automatically discovers your environments, resolves target dependency versions for Python 3.12+, fetches verified AST codemods, and shows you clean Git diffs before touching a single line of code.

---

## How It Works: The 5-Step Funnel

You don't have to guess what's broken or why a package won't install. PyMolt breaks migrations down into five distinct phases:

```
  ┌──────────┐     ┌───────────┐     ┌───────────┐     ┌───────────┐     ┌───────────┐
  │   Scan   │ ──► │   Setup   │ ──► │  Assess   │ ──► │ Codemods  │ ──► │ Verify /  │
  │ Surfaces │     │ Target Py │     │ Resolv &  │     │ LibCST    │     │ Contract  │
  │ & Edges  │     │  & Tool   │     │ Risk Plan │     │ Transforms│     │ Capture   │
  └──────────┘     └───────────┘     └───────────┘     └───────────┘     └───────────┘
```

```bash
# 1. SCAN: Discover monorepo surfaces, Dockerfiles, and Python version divergence offline
pymolt scan .

# 2. SETUP: Pick your target Python (3.12 / 3.13) and resolver toolset (uv, pip, conda)
pymolt setup .

# 3. ASSESS: Perform lock-first resolution, calculate CVE deltas and compilation risks
pymolt assess . --target-python 3.12 --risk

# 4. CODEMODS: Fetch AST transformation rules from Axiom Cloud Hub and preview or apply diffs
pymolt codemods .             # dry-run preview with unified diffs
pymolt codemods . --write     # apply verified AST transforms to disk

# 5. CONTRACT: Ensure runtime behavior hasn't shifted underneath you
pymolt contract capture --when baseline --mode tests -- pytest tests/
# ... apply code and dependency upgrades ...
pymolt contract capture --when post-migration --mode tests -- pytest tests/
pymolt contract report .      # diff runtime interaction contracts
```

---

## Built for AI Agents (MCP Server)

PyMolt isn't just a CLI — it's a first-class Model Context Protocol (MCP) server. If you use Cursor, Claude Desktop, Windsurf, or Google Antigravity, your agent can run migrations autonomously without hallucinatory guesswork.

### Configure in your MCP Client

Add PyMolt to your agent config (e.g. `~/.cursor/mcp.json` or `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "pymolt": {
      "command": "pymolt",
      "args": ["mcp"]
    }
  }
}
```

### Available MCP Tools

| Tool | What the agent gets |
|---|---|
| `status` | Instant funnel orientation (`scan` → `setup` → `assess` → `codemods` → `contract`). |
| `scan` | Project roots, dependency edges, and version divergence without parsing Dockerfiles manually. |
| `setup_options` / `setup_apply` | Valid target Python versions and package managers. |
| `assess` | Target dependency resolution matrix with risk scoring (CVEs, C-wheel compilation, package abandonment). |
| `codemods_preview` / `codemods` | Structured Unified Git Diffs of AST rewrites with optional `write=True`. |
| `contract_map` / `contract_capture` | Static dependency contact maps and dynamic test execution traces. |
| `contract_report` | Behavioral regression comparison between baseline and post-migration runs. |
| `migrate` | Complete end-to-end migration execution in a single RPC call. |
| `env_hint` | Ready-to-use Dockerfile recipes and `uv` virtualenv setup commands. |

---

## AST Codemods & Axiom Cloud Hub

Regex replacements break code; large language models frequently hallucinate non-existent API flags. PyMolt uses **LibCST (Concrete Syntax Trees)** paired with **Axiom Cloud Hub** recipes.

- **Non-destructive AST edits**: Preserves your formatting, indentation, and comments exactly as written.
- **Typed Metavariables**: Declarative match/rewrite templates (e.g., `Series_1.append(Series_2)` → `pd.concat([Series_1, Series_2])`).
- **Client Verification**: PyMolt locally re-verifies every rule before applying it to your repository.
- **Free Public Cloud Hub**: Recipes are fetched on demand from Axiom Cloud Hub. Custom/private on-prem instances are supported via `--endpoint <url>` or `PYMOLT_ENDPOINT`.

---

## Behavioral Contract Verification

Static type checkers catch signature changes, but they cannot tell you if a method started returning an empty generator instead of `None`.

PyMolt includes a zero-dependency runtime tracer that attaches via standard library hooks (`sys.setprofile` / import wrappers) — **no code changes or decorators required**.

1. Run your existing test suite under baseline Python to record runtime interaction shapes.
2. Upgrade dependencies and apply codemods.
3. Re-run tests under the target Python.
4. `pymolt contract report .` surfaces real behavioral drifts (type changes, unexpected exceptions, missing keys) while ignoring volatile values like timestamps and randomized IDs.

---

## Commands Summary

| Command | Description |
|---|---|
| `pymolt migrate [dir]` | Autonomous macro-command: detects, resolves dependencies, and applies codemods in one step. |
| `pymolt status [dir]` | Inspects funnel status and recommends the exact next command to run. |
| `pymolt scan [dir]` | Scans monorepos, Dockerfiles, and lockfiles for version divergence and edges. |
| `pymolt setup [dir]` | Interactive setup for target Python, package manager, and manifest. |
| `pymolt assess [dir]` | Resolves target dependencies, calculates CVE deltas, and emits pinned manifests. |
| `pymolt codemods [dir]` | Previews (`--dry-run`) or writes (`--write`) AST transformations. |
| `pymolt contract ...` | Captures, traces, and reports runtime behavior contracts. |
| `pymolt env hint [dir]` | Generates target `Dockerfile` and `uv` virtual environment commands. |
| `pymolt mcp` | Starts the stdio Model Context Protocol server for AI coding agents. |
| `pymolt auth login` | Authenticates your CLI for Axiom Cloud Hub recipe access. |

---

## License

PyMolt is open-source software licensed under the [Apache License, Version 2.0](LICENSE).
Axiom Cloud Hub recipes are provided under the Business Source License 1.1 with free public community access.


