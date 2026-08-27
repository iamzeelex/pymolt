# PyMolt benchmarks

Two independent benchmarks. Both write machine-readable JSON into `results/`.

## A. CLI: latency + payload size

```bash
uv run --with tiktoken python bench/bench_cli.py --repeats 5            # incl. assess (network)
uv run --with tiktoken python bench/bench_cli.py --offline-only         # deterministic subset
```

Measures wall-clock (warmup + N repeats, min/p50/max/stdev) and stdout size in
bytes and tokens (tiktoken `cl100k_base`) for `status`, `scan`, `contract map`
and `assess` across the `tests/artifacts/*` fixtures.

**What "cold" and "warm" mean for `assess`.** Cold = PyMolt's own
`.pymolt_cache/` removed. It does *not* clear uv's global cache
(`~/.cache/uv`), so a cold number here is "cold PyMolt cache, warm uv cache" —
the realistic second-project-on-this-machine case, not a fresh CI runner.

## B. MCP A/B: what PyMolt costs and saves an agent

```bash
python bench/bench_mcp.py --model sonnet --repeats 3
python bench/analyze.py
```

The same migration question is put to `claude -p --output-format json` twice:

| arm | setup | answers |
|---|---|---|
| `bare` | `Read,Grep,Glob,Bash` | what an agent costs without PyMolt |
| `mcp_only` | the same **plus** `mcp__pymolt__*` | will an agent reach for PyMolt on its own? |
| `plugin` | `mcp_only` **plus** a system prompt telling it to prefer those tools | what installing the plugin actually delivers |

The `plugin` arm is not a thumb on the scale: installing the PyMolt plugin ships
`plugin/commands/migrate.md` and `plugin/agents/pymolt-migrator.md`, both of
which instruct the agent to drive the funnel through the MCP tools. The system
prompt used here is the MCP server's own `_INSTRUCTIONS` text.

### Which numbers to trust

`total_tokens` from the result envelope is **not** a good metric: it is dominated
by `cache_read_input_tokens`, which swings with prompt-cache hits between runs.
Observed spread within a single arm (95k–157k on the same task) exceeded the gap
between arms. `bench/transcript_metrics.py` recovers two stable numbers from each
run's session transcript instead:

- **`peak_context_tokens`** — the largest context the model held (max over
  assistant turns of `input + cache_read + cache_creation`). How full the window got.
- **`tool_result_tokens`** — total volume pulled off disk into the context
  (every `tool_result` block). This is the number PyMolt is meant to shrink.

`num_turns`, `output_tokens` and wall time are stable as reported.
`total_cost_usd` inherits the cache jitter — quote it with care.

Controls that make the comparison honest:

- **Isolated workspace.** Each run gets a fresh copy of the fixture under
  `/tmp/pymolt_bench_ws/` with `.git`, `.pymolt*`, `CLAUDE.md` and `AGENTS.md`
  stripped, so neither arm can read this repo's docs, a previous run's
  `.pymolt/mcp/` reports, or a manifest an earlier `assess` wrote.
- **The bare arm keeps `Bash`.** It is free to run `uv pip compile` itself.
  The comparison is *cost to reach the answer*, not tool deprivation.
- **Arms interleave** (`bare, pymolt, bare, pymolt…`) so API-side drift hits
  both equally.
- **`--strict-mcp-config`** so no other MCP server on the machine leaks in.
- **T0 is a no-op prompt** (`Reply with exactly: OK`) to price the fixed
  overhead of merely attaching the server — that cost is paid on every turn of
  every session and belongs on PyMolt's side of the ledger.

### Tasks and ground truth

Fixture: `flasgger` (68 `.py`, ~8k LOC, 3 project roots).

| task | question | ground truth |
|---|---|---|
| `T0_noop` | none — overhead probe | n/a |
| `T1_recon` | project roots, manifests, Python-version evidence | 3 roots; `.python-version` = 3.6.1, `Dockerfile` `FROM python:3.6` — **verified directly against the files**, independently of PyMolt |
| `T2_upgrade` | which deps must move for Python 3.12, and to what | real uv resolve: 77 packages, 31 version changes |
| `T3_contact` | dependency contact surface | 67 files scanned, 227 contact points, 55 distinct symbols; top: flask 165, simplejson 9, jsonschema 7, werkzeug 6, flask_restful 5 |

**Token counts alone are not a result.** A cheap wrong answer beats an expensive
right one on every metric here, so `bench/score.py` grades every answer against
the ground truth above before its tokens are reported.

### Prompt ambiguities found (and how they are handled)

Two tasks turned out to admit more than one correct reading. Both were caught by
reading the losing arm's actual answer rather than trusting its score:

- **T2, first wording** asked which *declared* dependencies must change version.
  The `bare` arm built a real 3.12 venv, ran `pip check`, and correctly answered
  "none — every constraint is an unpinned `>=` floor". That scored 0.00 against a
  ground truth about *resolved* versions. The task was reworded to ask explicitly
  for two resolutions (3.9 vs 3.12) and re-run; the original numbers are kept in
  `results/mcp_sonnet.json` for cost only, not for correctness.
- **T3** asked for "distinct symbols … count them", which is either 227 call
  sites or 55 distinct symbols, and its top-5 can be ranked by either. The scorer
  credits whichever reading is closer, for every arm equally.

## Known fixture issues

- `tests/artifacts/blaze/.pymolt/env_config.json` holds `"base_python": "q"`
  from an old interactive session; it fails validation (PyMolt warns and falls
  back to defaults). `assess` on blaze then fails for a genuine reason —
  `uv pip compile requirements-310.txt --python-version 3.9` cannot resolve
  blaze's decade-old pins. That is a real dead-project condition, not a harness bug.
- `scan` on `pyfolio` returns an almost empty inventory: the project declares
  its dependencies only in `setup.py`/`setup.cfg`, and `install_requires` is
  not yet edge-classified (PyMolt says so in `notes`).
