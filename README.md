# PyMolt (`pymolt`)

🐍 **PyMolt** is an open-source CLI migration tool (and MCP server) that brings all codebase migration facts into one place — making Python version upgrades and dependency changes visible, predictable, and behaviorally verifiable.

If you have ever had to upgrade a codebase from Python 3.7 to 3.12 or transition between major dependency versions (such as Pandas 1.x to 2.x or Pydantic v1 to v2), you know how easily subtle runtime breaking changes can slip through. PyMolt helps you navigate these migrations systematically across 5 core steps: **Scan → Setup → Assess → Codemods → Contract**, providing clear, verifiable evidence at every step for both you and your AI tools.

---

## Key Features

- 🛡️ **Behavioral Contract Locking (`contract`)**: PyMolt's primary focus — help you understand, record, and lock down the runtime interaction contract (`your_code → dependency`) before migrating, so you can diff execution behavior after the upgrade and catch silent breaks before production.
- ⚡ **Automated Code Rewrites (`codemods`) [Alpha]**: Automatically rewrite breaking API changes, renamed imports, and deprecated methods using recipes fetched from Axiom Graph. *(Currently in active alpha development & testing — details and status available at [pymolt.zeelex.me](https://pymolt.zeelex.me)).*
- 🎯 **Lock-First Feasibility & Risk Scoring (`assess`)**: Perform lock-first dependency graph resolution against target Python versions, compare baseline vs. target, rank risk tiers (OSV CVE deltas, C-compilation wheel requirements, package abandonment), and emit pinned target manifests.
- 🔍 **Zero-Context Surface Scanning (`scan`)**: Map all Python surfaces across monorepos, multi-stage Dockerfiles, tox/nox configs, and lockfiles in a single pass to discover version divergence across environments.
- 🤖 **AI Assistant Support via MCP (`pymolt mcp`)**: Serve these verified migration facts and contract evidence directly to AI editors (Claude Code, Cursor, ChatGPT) via a built-in stdio MCP server with deterministic `--json` outputs.

---

## Migration Workflow

```bash
# 1. SCAN: Discover all surfaces, version divergence, and dependency edges in one pass
pymolt scan .

# 2. SETUP: Configure target Python version, toolset (uv/pip), and target manifest
pymolt setup .

# 3. ASSESS: Perform lock-first resolution, compare versions, and score migration risks
pymolt assess . --target-python 3.12 --risk      # add --json for CI / AI Agents

# 4. CODEMODS: Fetch AST transformation recipes and rewrite code for target dependencies
pymolt codemods .

# 5. CONTRACT: Verify behavioral stability across dependency bumps (zero code edits required)
pymolt contract capture --when baseline --mode tests -- pytest tests/
# ... apply migration updates ...
pymolt contract capture --when post-migration --mode tests -- pytest tests/
pymolt contract report .                          # auto-diffs baseline vs post-migration
```

---

## Installation

PyMolt requires Python 3.12+ (or will automatically provision one via `uv`). The one-line installer provisions PyMolt as an isolated global tool without touching your system Python:

```bash
curl -fsSL https://pymolt.zeelex.me/install.sh | sh
```

Or install via standard Python tool installers:

```bash
uv tool install pymolt      # recommended
pipx install pymolt
pip install --user pymolt
```

Verify installation:

```bash
pymolt --version
pymolt status .
```

---

## Project Architecture

PyMolt uses a structured, modular design targeting a single navigable dependency graph substrate layered with static analysis, dependency resolution, migration-risk scoring, codemods, and behavioral verification.

```text
pymolt/
├── core/            # The Substrate (Graph, Node, Edge, Serialization)
├── discovery/       # Surface map: monorepo roots, Dockerfile/tox/nox, version evidence
├── ingestion/       # World-as-found (uv/conda/pip inputs, lock-first, name maps, config)
├── inventory/       # Dependency edge classification + code-usage intersection
├── setup/           # Manifest / toolset / base+target Python / container selection
├── assess/          # Lock-first resolve, baseline vs target compare, feasibility
├── risk/            # Migration risk: CVEs (OSV), wheels/compilation, abandonment
├── codemods/        # Fetch patterns/rules from Axiom Graph, apply with LibCST
├── verify/          # Behavioral contract: static contact map × dynamic trace, diff
└── interfaces/      # CLI (Typer) and MCP server entry points
```

---

## Commands Overview

| Command | Category | What it does |
|---|---|---|
| `pymolt status [dir]` | Status | **Migration State**: Reads current state on disk, flags stale or missing evidence, and suggests the exact next step to run. |
| `pymolt scan [dir]` | Phase 1 | **As-Is Discovery**: Gathers monorepo surfaces, Dockerfile multi-stage builds, tox/nox configs, Python version divergence, and dependency edges offline. |
| `pymolt setup [dir]` | Phase 2 | **Configuration**: Configures project migration settings, manifest type, target Python version, and toolset (`.pymolt/env_config.json`). |
| `pymolt assess [dir]` | Phase 3 | **Feasibility & Risk**: Lock-first dependency graph resolution against target Python, baseline vs target comparison, risk scoring (CVEs/compilation/abandonment), and pinned manifest generation. |
| `pymolt codemods [dir]` | Phase 4 | **Automated Rewrites [Alpha]**: Fetches syntax and API migration recipes from Axiom Graph and applies code rewrites locally. *(Active alpha testing — details at [pymolt.zeelex.me](https://pymolt.zeelex.me))*. |
| `pymolt contract ...` | Behavioral | **Runtime Verification**: Static contact map × dynamic boundary tracing, guided baseline → post-migration behavior diffing. |
| `pymolt succession [dir]` | Experimental | **Framework Succession [Experimental]**: Identifies migration pathways for deprecated frameworks (e.g. `Keras → tensorflow.keras` / `PyTorch`) and applies shim AST transforms. |
| `pymolt forks OWNER/REPO` | Experimental | **Fork Network Triage [Experimental]**: Ranks live community successor forks for unmaintained GitHub repositories based on recency, stars, and Python 3.12+ porting signals. |
| `pymolt env hint [dir]` | Utility | **Environment Guide**: Generates target `Dockerfile.pymolt-target` recipes and `uv` setup commands. |
| `pymolt login` / `logout` | Auth | **Account Auth**: Manage API authentication tokens for Axiom Graph codemods ([pymolt.zeelex.me](https://pymolt.zeelex.me)). |
| `pymolt mcp` | AI Integration | **MCP Server**: Stdio Model Context Protocol server exposing PyMolt tools to AI agents. |

---

## Detailed Features

### Risk Assessment (`pymolt assess --risk`)

`assess --risk` performs network-backed (cached under `.pymolt/cache/`) vulnerability and health lookups:
- **CVE Delta (OSV.dev)**: Evaluates security vulnerability deltas before vs. after migration.
- **Wheel / Compilation Status (PyPI)**: Flags `sdist-only` packages requiring C compilation environments on target platforms.
- **Package Abandonment**: Measures release recency and flags unmaintained dependencies.
- Assigns a structured `HIGH` / `MEDIUM` / `LOW` `RiskTier` for every dependency.

### Experimental Features (`succession` & `forks`)

> ⚠️ **Experimental & Active Evaluation**: The commands below cover non-standard migration edge cases. They are strictly experimental, in active testing, and we are currently evaluating how effectively they perform across real-world codebases.

1. **Framework Succession (`pymolt succession [dir]`)**
   - **Target Use-Case**: Transitioning away from a deprecated or unmaintained framework (e.g. legacy `Keras 2.x` → `tensorflow.keras` or `PyTorch`, or `Flask` → `FastAPI`).
   - **How it works**: Analyzes symbol import usage, queries migration path mappings from Axiom Graph, and applies compatibility shims to bridge API changes.

2. **Fork Network Triage (`pymolt forks OWNER/REPO`)**
   - **Target Use-Case**: Continuing migration when an upstream dependency is completely abandoned by its maintainer (no Python 3.12+ releases or wheel builds published to PyPI).
   - **How it works**: Analyzes the GitHub fork network for the target repository, scoring community forks by commit recency, star count, code divergence, and Python 3.12+ porting status to find active successor forks.

---

## For AI Agents & Claude Code Plugin (`plugin/`)

PyMolt is engineered to be driven autonomously by AI coding agents (Claude Code, Cursor, ChatGPT):
- **Claude Code Plugin (`plugin/`)**: Auto-registers the `pymolt` MCP server, adds the `/pymolt:migrate` slash command, and provides the specialized `pymolt-migrator` subagent for guided migrations.
- **Deterministic JSON Output**: Every report command supports `--json` with strict stdout (data) and stderr (diagnostics) separation.
- **Typed Exit Codes**: `0` (Success/Info), `1` (Behavior change / negative finding), `2` (Usage error), `3` (Environment error).

### Installing the Claude Code Plugin
```bash
/plugin marketplace add zeelex/python_migrator
/plugin install pymolt
```
Installing wires the `pymolt mcp` server automatically, adds the `/pymolt:migrate [project-dir]` slash command, and activates the `pymolt-migrator` subagent.

### Model Context Protocol (MCP) Setup (Cursor, Windsurf, ChatGPT)
For other MCP clients, install the `pymolt[mcp]` package and register the stdio server:

```bash
uv tool install 'pymolt[mcp]'
```

```toml
# Editor MCP Config (e.g. ~/.codex/config.toml)
[mcp_servers.pymolt]
command = "pymolt"
args = ["mcp"]
```

### Recommended `AGENTS.md` / `CLAUDE.md` snippet:

```markdown
## Python Migrations
This repository uses PyMolt for codebase migration facts — do not rediscover them manually.
- Monorepo surfaces & version divergence: `pymolt scan . --json`
- Feasibility & risk on target Python: `pymolt assess . --target-python <X.Y> --risk --json --no-write`
- Behavioral ground truth: `pymolt contract report . --json`
  After modifying code, run `pymolt contract capture --when post-migration --mode tests -- <test cmd>` and re-check `pymolt contract report .`
```

---

## Behavioral Verification (`verify/`)

Static analysis detects declared signatures; `verify/` answers whether **code still behaves identically at runtime** after upgrading dependencies.

PyMolt attaches to your target application **without modifying any code in your repository**. A lightweight tracer is injected via `sitecustomize` / `.pth` + environment variables. The runtime watcher is **pure Python standard library (Python 3.6+)** with zero third-party dependencies, running inside any interpreter or container.

### Boundary Tracer Modes
- `setprofile` (3.6–3.11): Complete call-graph trace across the interpreter (ideal for test suites).
- `wrap` (`--wrap sym1,sym2`): Monkeypatches only specific target dependency symbols with import-hook preservation (ultra-low overhead for production/staging runs).
- `hybrid`: Combines `wrap` for named symbols with `setprofile` for C-extensions and dynamic dispatch.

### Guided Contract Workflow

```bash
# 1. Capture baseline behavior BEFORE migrating
pymolt contract capture --when baseline --mode tests -- pytest tests/

# 2. Perform migration (assess → codemods → edits)

# 3. Capture post-migration behavior
pymolt contract capture --when post-migration --mode tests -- pytest tests/

# 4. Compare behavior
pymolt contract report .
```

`contract diff --contract` compares **interaction shapes** (return types, exception types, dictionary structures) rather than volatile scalar values (timestamps, temporary paths), preventing false positives while catching true API signature and behavioral breaks.

---

## Design Note: What Happened to the TUI?

An interactive Terminal User Interface (TUI) was originally prototyped (`pymolt ui`) to visualize dependency graphs. However, practical migration experience quickly showed that a TUI offered minimal real-world value compared to **deterministic, pipeable CLI commands** (`--json`) and **native Model Context Protocol (MCP)** integration for AI pair-programming.

Visual TUI widgets added maintenance overhead without accelerating verification or evidence gathering. The TUI was intentionally removed, and PyMolt remains focused strictly on fast, headless CLI tools and structured MCP tool feeds.

---

## License & Support

Maintained as part of the PyMolt project. Documentation and machine-readable schema available at <https://pymolt.zeelex.me/llms.txt>.
