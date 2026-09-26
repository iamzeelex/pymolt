# Benchmark results

Host: macOS 15.6.1, arm64 · repo `6702f5a` · 2026-08-26
Method and controls: [`bench/README.md`](README.md). Raw data: `bench/results/*.json`.

## A. CLI — latency and payload

5 repeats after a warmup; stdev under 1.5% everywhere except blaze `contract map` (2.4%).
Tokens counted with tiktoken `cl100k_base`.

| fixture | .py / LOC | command | p50 | JSON | tokens |
|---|---|---|---|---|---|
| pyfolio | 26 / 10,696 | `status` | 218 ms | 1.2 KB | 332 |
| | | `scan` | 223 ms | 0.8 KB | 209 |
| | | `contract map` | 2.17 s | 77.0 KB | 20,486 |
| | | `assess` cold → warm | 1112 → 571 ms | 13.0 KB | 3,810 |
| flasgger | 68 / 7,951 | `status` | 217 ms | 1.2 KB | 340 |
| | | `scan` | 226 ms | 14.2 KB | 3,484 |
| | | `contract map` | 1.76 s | 40.8 KB | 11,022 |
| | | `assess` cold → warm | 571 → 573 ms | 19.8 KB | 5,777 |
| blaze | 133 / 33,315 | `status` | 220 ms | 1.3 KB | 346 |
| | | `scan` | 231 ms | 70.0 KB | 17,856 |
| | | `contract map` | 10.53 s | 314.9 KB | 84,984 |
| | | `assess` | **fails (exit 3)** | — | — |

**Interpreter startup is 210 ms**, so `status` and `scan` are essentially free — the
analysis itself costs 7–20 ms. `contract map` is the only phase that scales with code
size: 1.76 s at 8k LOC, 10.53 s at 33k LOC (LibCST parse dominates).

Two honest caveats:

- **flasgger `assess` shows no cold/warm difference** (571 vs 573 ms) because clearing
  `.pymolt_cache/` does not clear uv's global cache. pyfolio does show the effect
  (1112 → 571 ms) because its resolve was not yet in uv's cache either. A true cold
  number needs a clean `~/.cache/uv`, which this run did not do.
- **blaze `assess` fails for a real reason**: `uv pip compile requirements-310.txt
  --python-version 3.9` cannot resolve its decade-old pins. Separately,
  `tests/artifacts/blaze/.pymolt/env_config.json` carries `"base_python": "q"` from an
  old interactive session and fails validation (PyMolt warns and falls back to defaults).

## B. MCP A/B — what PyMolt costs and saves an agent

Fixture: flasgger. Arms: `bare` (no PyMolt) · `mcp_only` (server attached, neutral prompt)
· `plugin` (server + the instruction the real plugin ships). Medians.
`peak ctx` = largest context the model held; `tool data` = tokens pulled off disk into
context; `score` = answer graded against ground truth (1.00 = correct).

### Sonnet, n=3

| task | arm | peak ctx | tool data | turns | wall | PyMolt calls | score |
|---|---|---|---|---|---|---|---|
| T0 noop | bare | 27,676 | 0 | 1 | 3.5 s | 0 | — |
| | mcp_only | 28,177 | 0 | 1 | 3.7 s | 0 | — |
| | plugin | 28,298 | 0 | 1 | 4.5 s | 0 | — |
| T1 recon | bare | 34,113 | 3,253 | 5 | 30.4 s | 0 | 1.00 |
| | mcp_only | 33,664 | 2,258 | 5 | 32.0 s | 0 | 1.00 |
| | plugin | 34,036 | 1,974 | 7 | 34.2 s | 1 | 1.00 |
| T2 upgrade | bare | 35,318 | 2,836 | 9 | 49.6 s | 0 | 0.24 |
| | mcp_only | 37,921 | 4,541 | 11 | 41.1 s | 0 | 0.24 |
| | plugin | 39,317 | 4,602 | 10 | 55.2 s | 4 | 0.24 (best 0.98) |
| T3 contact | bare | 62,259 | 20,075 | 14 | 123.0 s | 0 | 0.66 |
| | mcp_only | **32,948** | **1,516** | 5 | 24.5 s | 1 | **1.00** |
| | plugin | 33,862 | 1,752 | 5 | 28.6 s | 2 | **1.00** |

### Opus, n=1 (T3 lost to the account's monthly spend limit, all three arms)

| task | arm | peak ctx | tool data | turns | wall | score |
|---|---|---|---|---|---|---|
| T0 noop | bare / mcp_only / plugin | 28,537 / 29,019 / 29,142 | 0 | 1 | ~4 s | — |
| T1 recon | bare | 37,481 | 4,384 | 6 | 52.7 s | 1.00 |
| | mcp_only | 37,873 | 4,536 | 6 | 48.9 s | 1.00 |
| | plugin | **30,448** | **422** | **3** | **11.3 s** | 1.00 |
| T2 upgrade | bare | 32,981 | 1,517 | 7 | 41.2 s | 0.24 |
| | mcp_only | 33,507 | 1,514 | 6 | 40.5 s | 0.24 |
| | plugin | 36,451 | 1,992 | 9 | 55.5 s | 0.24 |

## Findings

**1. Attaching the server costs ~500 tokens per request.** Sonnet +501, Opus +482; the
plugin instruction adds ~120 more. Paid on every turn of every session.

**2. An agent does not reach for PyMolt on its own — unless the task is clearly out of
reach of grep.** `mcp_only` made **zero** PyMolt calls on T1 and T2 on both models: it
shells out instead. The one exception is T3 on Sonnet, where the contact surface is not
greppable and it found `contract_map` unaided. `ToolSearch` appears only where a PyMolt
call happens, confirming the tools are deferred: the model sees names, and loads schemas
only when it has decided to use one.

**3. Where PyMolt wins, it wins on every axis at once.** T3, Sonnet: **53% of the
context, 7.5% of the tool data, 5× faster, 2.8× fewer turns — and the correct answer**
(1.00 vs 0.66). `bare` reported 35–38 contact points against a true 227 call sites / 55
distinct symbols, because grepping imports does not find call sites. On Opus T1 the
plugin arm answered in **3 turns and 11.3 s against 6 turns and 52.7 s**, reading 422
tokens of data instead of 4,384 — 10× less — at identical correctness.

**4. Where it does not win: T2.** Nobody answers the two-resolution question well
(0.24 across all arms and both models). PyMolt *has* the answer — `assess` returns all 31
version changes — but the MCP contract truncates lists at `LIST_CAP = 20` and hands back
a `full_report_path` instead. **The agent opened that file in exactly 1 of 3 plugin runs.
That run scored 0.98 (31/31 packages); the two that answered from the truncated list
scored 0.24 (6/31).** The data exists and does not reach the model. This is the single
highest-leverage fix in the MCP layer.

**5. `bare` is not merely expensive, it is unboundedly expensive.** Worst observed
`bare` run: 1,068,876 tokens, 26 turns, 6m15s on T2. Worst PyMolt run on the same task:
323,194 tokens. The variance itself is the cost — an agent without a tool does not know
when to stop.

## A note on metrics

`total_tokens` from `claude -p --output-format json` is **not** usable for this
comparison: it is dominated by `cache_read_input_tokens`, and the spread within one arm
(95k–157k on a single task) exceeded the gap between arms. The numbers above come from
the session transcripts instead — `peak_context_tokens` and `tool_result_tokens` — which
are stable across repeats. Reported `total_cost_usd` inherits the same jitter.

---

# Round 2 — after the P0 (truncation) and P2 (positioning) changes

Same harness, same fixture, n=3, Sonnet. Re-run 2026-08-27 after: budgeted compact
answer lists (`_fit_answer`), summaries that state their own completeness, true
counts in `contract_report`, and the contract-first rewrite of tool descriptions,
`_INSTRUCTIONS`, README, CLI help panels and the plugin.

**The `bare` arm is the control** — it never touches PyMolt, so whatever moved in it
is drift between runs. Only changes that moved in the PyMolt arms *and not* in the
control are attributable to the fixes.

| task | metric | bare (control) | plugin before → after | attributable? |
|---|---|---|---|---|
| T3 | tool data | 20,075 → 22,190 (+11%) | 1,752 → **291** (−83%) | **yes** |
| T3 | turns | 14 → 18 | 5 → **3** | **yes** |
| T3 | wall | 123.0 s → 237.4 s | 28.6 s → **12.5 s** | **yes** |
| T3 | score | 0.66 → 0.68 | 1.00 → 1.00 | unchanged, correct |
| T2 | score | 0.24 → **0.98** | 0.24 → **0.98** | **no — control moved identically** |
| T2 | PyMolt calls (mcp_only) | n/a | **0 → 5** | **yes** |
| T2 | tool data (plugin) | 2,836 → 9,757 | 4,602 → 2,602 (−43%) | likely |
| T1 | score | 1.00 → 1.00 | 1.00 → **0.84** | **yes — regression** |
| T1 | PyMolt calls | n/a | 1 → **0** | **yes — regression** |
| T0 | overhead/request | 27,676 → 27,676 | +622 → **+848** | yes, cost of longer docstrings |

## What the round-2 data supports

**1. Compacting `contract_map` is a clean win.** 83% less data into context, 40%
fewer turns, 56% less wall time, correctness unchanged at 1.00 — while the control
moved the *other* way over the same interval. PyMolt now answers the contact-surface
question on **1.3% of the data and 5% of the time** the tool-less agent needs.

**2. Rewriting tool descriptions fixed discovery.** On T2 the neutral-prompt arm went
from **zero** PyMolt calls to five (`scan`, `setup_options`, `setup_apply`, `assess`),
with no instruction in the prompt. Describing tools by what an agent *cannot do for
itself* is what changed its behaviour.

**3. The truncation fix cannot be credited for T2's score.** The clip was a genuine
defect and removing it was correct — the full 31-row changed-set now ships and costs
fewer tokens than the clipped JSON did (453 vs 643). But the control improved by the
identical 0.24 → 0.98, so the movement is drift. Worse, the agent now receives the
whole list and *still* sometimes answers with 6 packages: the remaining T2 variance is
an interpretation problem (direct vs transitive), not a delivery one.

**4. The positioning change caused a regression on T1.** The plugin arm fell 1.00 →
0.84 and stopped calling `scan` (1 → 0), missing the `demo_app` project root in two
runs of three, while the control held at 1.00. Pushing the contract to the front
appears to have pulled attention off the cheap recon step. Attaching the server also
costs +848 tokens now, up from +622, because the rewritten descriptions are longer.

## Caveat

The control moved substantially between rounds with no code touching it (T2 score
0.24 → 0.98, T3 wall +93%). Treat any single-round A/B here at n=3 as indicative.
Only differences that separate from the control are claimed above.

---

# Round 2b — monorepo trigger restored in `scan` (NOT MEASURED)

Applied 2026-08-27 to address the T1 regression. **No benchmark run backs this
section** — it is recorded so the next run has a stated expectation to test against,
and it must not be quoted as a result.

The regression's symptom was specific: the agent reported `.` and
`etc/flasgger_package` (both carrying a `setup.py`) and dropped `demo_app`, whose only
marker is its own `requirements.txt`. The `scan` description now states that the repo
should be assumed a monorepo, and that a nested requirements-only directory is a
separate root. Two tests pin it: one on the description text, one asserting `scan`
actually returns such a nested root.

## Deterministic facts (no agent run needed)

Token cost of the tool descriptions, measured with tiktoken against `HEAD`:

| tool | before | after | delta |
|---|---|---|---|
| `scan` | 86 | 170 | **+84** |
| `assess` | 141 | 218 | +77 |
| `contract_map` | 62 | 143 | +81 |
| `contract_report` | 98 | 162 | +64 |
| **all 12 tools** | **1,312** | **1,618** | **+306** |
| server `_INSTRUCTIONS` | 148 | 295 | +147 |

**Tool descriptions reach the model; the server's `_INSTRUCTIONS` do not.** Between
round 1 and round 2 the docstring deltas summed to +222 tokens and the measured
per-request overhead rose +230 (501 → 731) — a near-exact match — while the +147 of
`_INSTRUCTIONS` left no trace. This explains why round 1's agents ignored the
"Prefer these tools over reading Dockerfiles" line written there: they never saw it.
Anything meant to steer an agent belongs in a docstring.

Also verified without an agent: `scan` on flasgger returns all three roots, including
`demo_app` with its `requirements.txt`. The tool's data was never wrong — the round-2
failure was purely that the agent stopped calling it.

## Expectation to test (not a result)

- Per-request overhead should rise by roughly the docstring delta, to **~815 tokens**
  (731 + 84), if the 1:1 relationship above holds.
- T1 score for the arms that call `scan` should return toward 1.00. **Unknown**
  whether the description alone makes the agent call `scan` again — round 2 showed
  the plugin arm dropping to zero `scan` calls, and no description change has yet
  been shown to reverse that.

Re-run with: `python bench/bench_mcp.py --model sonnet --repeats 3 --tasks T1_recon`

---

# Comparison: Bun's Zig → Rust rewrite

**Source:** [Bun in Rust](https://bun.com/blog/bun-in-rust) — Jarred Sumner, on rewriting
Bun (535,496 lines of Zig) into Rust with Claude, May 2026. Read as reported by the
author; nothing here is independently verified.

The closest public analogue to what this benchmark measures. Different scale, different
kind of migration — but the same load-bearing question: *how do you know the thing still
behaves the same afterwards?*

## What they did

| | |
|---|---|
| Scope | 1,448 `.zig` files → `.rs`; 535k lines |
| Duration | 11 days (May 3–14, 2026), 6,502 commits; peak 695 commits in one hour |
| Parallelism | ~64 Claude instances across 4 git worktrees, one engineer supervising |
| Token spend | 5.9B uncached input, 690M output, **72B cached reads** — ~$165,000 at API pricing |
| Oracle | Bun's existing TypeScript test suite: 1.39M `expect()` calls over 60,624 tests, **"0 tests skipped or deleted"** |
| Extra safety | 24/7 coverage-guided fuzzing (Fuzzilli), LeakSanitizer, CI on 6 platforms |
| Review | Per task: 1 implementer Claude, **2 adversarial reviewers** with no access to the implementer's rationale, 1 fixer |
| Outcome | 19 known regressions (all fixed), 128 bug fixes beyond the Zig baseline, ~4% of Rust marked `unsafe` |

## Where their findings and ours agree

**1. The oracle is the whole game.** Their rewrite was tractable because the test suite
was written in TypeScript and therefore survived the reimplementation untouched — a
behavioural oracle that existed *before* the migration and cost nothing to keep. "0 tests
skipped or deleted" is the same claim `_truncation_note` now makes in our MCP payloads:
state completeness explicitly, because silence reads as completeness.

**2. Agents take the shortcut unless something adversarial is watching.** They report
early Claudes "stubbed functions instead of fixing them" and wrote "paragraph-long
justifying comments" — which is why reviewers were told to assume the code is wrong and
were denied the implementer's reasoning. We hit the same failure in a smaller form: given
a list clipped at 20 items with a pointer to the full file, the agent answered from the
clipped list in 2 of 3 runs. Both are the same reflex — *treat what is in front of you as
the whole truth* — and both are fixed structurally, not by asking nicely.

**3. A single token number is meaningless at any scale.** Their spend is 72B cached reads
against 5.9B uncached — a 12:1 ratio. We abandoned `total_tokens` for exactly this reason:
`cache_read` dominated it, and the spread within one arm (95k–157k) exceeded the gap
between arms. Anyone quoting "tokens used" without separating cached from uncached is
quoting mostly cache.

**4. Don't let the agent decide whether to use the tool.** They wired `cargo check` in as
the work-queue generator — the compiler was a pipeline stage, not an option the model
could skip. Our round-1 finding was the same from the failure side: with the server merely
attached, the agent made **zero** PyMolt calls on 2 of 3 tasks. Tools that matter belong in
the workflow, not on a menu.

## Where their experience corrects ours

**Our overhead finding is close to irrelevant at real scale.** We measured attaching the
server at +501 tokens per request, rising to +848 after the docstring rewrite, and treated
that as a cost worth watching. Against 5.9B uncached input tokens it is noise. The metric
that matters is not what the tools cost to expose — it is what fraction of calls actually
use them, and what a wrong answer costs downstream. We should stop optimising the former.

**Mechanical porting beat clever porting.** They deliberately kept the Rust architecturally
close to the Zig, for comprehension and risk. The analogous discipline for PyMolt: return
data with honesty markers and let the engineer or agent decide — do not have the tool
render a verdict it cannot support. That is the same instinct behind refusing to call a
migration "feasible" from a green resolve.

## Where the two situations genuinely differ — and why it matters for PyMolt

Bun had the oracle for free. **The projects PyMolt targets do not.**

That is not a caveat, it is the entire product thesis, and this article is the strongest
external evidence for it we have. A rewrite of that ambition was possible *because*
1.39M assertions already pinned the behaviour. Take the oracle away and no amount of
parallel agents, adversarial review or fuzzing tells you whether the migration preserved
behaviour — there is nothing to compare against.

The dead Python projects in `tests/artifacts/` are precisely the no-oracle case: flasgger's
suite exercises part of its surface, blaze's dependencies no longer resolve at all. This is
why `contract report` returns **BLIND** coverage rather than pass/fail. It answers the
question Bun never had to ask: *how much of the behaviour do my tests actually pin?*
A project with high BLIND cannot run Bun's playbook, however many agents it throws at it.

Their fuzzing infrastructure is the interesting borrowable idea: Fuzzilli generated
coverage 24/7 and filed bugs automatically. PyMolt's `contract_capture` currently depends
on a human having a command worth running. Raising confirmed coverage without waiting for
someone to write tests is the natural next capability.

## Honest limits of this comparison

Their numbers describe one real migration of one large codebase, self-reported, with no
control group — you cannot tell from the article what Claude contributed versus what a
year of conventional work would have. Ours are n=3 on one 8k-line fixture with a control
arm, and even there the control drifted enough between rounds to invalidate one of our
attributions. Neither is statistics. The agreements above are worth more than either set of
numbers alone precisely because the two setups fail in such different ways.

---

# What does an agent-run migration actually cost?

Prices are Anthropic list rates as of 2026-08-27: Opus 5 $5/$25 per MTok, Sonnet 5
$3/$15 ($2/$10 introductory through 2026-08-31), Fable 5 $10/$50. Cache reads bill at
~0.1× input, cache writes at ~1.25×.

## The model is validated against a known answer

Bun's published spend — 5.9B uncached input, 690M output, 72B cached reads — repriced at
Fable 5 list rates comes to **$165,500** against their reported "approximately $165,000".
A 0.3% match, which also tells us they ran Fable 5. Derived from that:

| | |
|---|---|
| Per line of Zig replaced | **$0.309** |
| Per commit (6,502 commits) | **$25.45** |
| Cached reads as a share of all input | **92%** |
| Same job with no caching | **$813,500** — caching saved **$648,000** |

That last row is the one to internalise. Prompt caching was not a 10% optimisation on this
job; it was the difference between $165k and $813k. Any cost model that ignores the cache
ratio is wrong by roughly 5×.

## Anchors from our own measurements

Our benchmark runs, repriced from Sonnet to list rates (median of n=3):

| task | arm | turns | Sonnet 5 | Opus 5 | Fable 5 |
|---|---|---|---|---|---|
| T1 recon | bare | 8 | $0.164 | $0.273 | $0.546 |
| | plugin | 7 | $0.176 | $0.294 | $0.587 |
| T2 upgrade | bare | 14 | $0.332 | $0.554 | $1.107 |
| | plugin | 7 | $0.178 | $0.297 | $0.594 |
| T3 contact | bare | 18 | $0.690 | $1.149 | $2.299 |
| | plugin | **3** | **$0.074** | **$0.123** | **$0.246** |

Across 252 measured turns: **$0.025/turn (Sonnet 5), $0.042/turn (Opus 5), $0.084/turn
(Fable 5)**. On T3 the tool-less agent costs 9.3× what the PyMolt-equipped one does for a
worse answer.

## Projecting a whole migration

Two independent methods, because they disagree by an order of magnitude and that gap is
itself the honest answer.

- **Method A (bottom-up)** — our measured $/turn × an estimated turn count
  (recon + ~4 turns per affected call site + 4 verification cycles of ~40 turns).
- **Method B (top-down)** — Bun's measured $0.309/line, rescaled to the model, applied to
  the lines a migration actually touches (~20 lines read/written per affected call site).

Structural estimates come from PyMolt's own measurements of flasgger: **28.5 contact
points per 1,000 LOC**, and **40% of packages change version** on a 3.9 → 3.12 move (31 of
77). Human effort at 3–8 h per 1,000 LOC is an assumption, not a measurement.

| project | agent, Opus 5 (A → B) | human ($75/h, 3h/kLOC) | human ($150/h, 8h/kLOC) |
|---|---|---|---|
| 8,000 LOC | $23 → $284 | $1,800 (24 h) | $9,600 (64 h) |
| 50,000 LOC | $104 → $1,776 | $11,250 (0.9 mo) | $60,000 (2.5 mo) |
| 200,000 LOC | $393 → $7,103 | $45,000 (3.8 mo) | $240,000 (10 mo) |

**Why the two methods diverge 17×:** Method A prices an agent *answering questions about
code*, which is what we measured. Method B prices an agent *writing and debugging code
until CI is green*, which is what Bun measured. A real migration is the second thing.
Budget toward Method B; treat Method A as the floor.

## So is it worth it?

**Token spend is not the line item.** Even the pessimistic estimate for a 200k-LOC
migration — ~$7,100 — is a fraction of the $45,000–240,000 of engineer time it displaces,
and it arrives in days rather than months. At 50k LOC the ratio is 6× to 100× depending on
which end of each range you take. No plausible parameter choice makes the tokens the
expensive part.

**The real cost is a migration that silently changed behaviour.** A wrong answer does not
cost $0.12 — it costs an incident. That reframes what to optimise: not the price of the
tokens, but the probability that the migration is wrong and nobody notices. This is where
the benchmark and the cost model meet — the tool-less agent on T3 was both 9× more
expensive *and* wrong (0.68 vs 1.00), and its worst single run consumed 1.07M tokens
without converging.

**The change is not "cheaper migrations" — it is migrations that were never worth doing.**
At $11k–60k a legacy upgrade competes with feature work and loses; that is why dead Python
projects stay dead. At $100–2,000 the calculation is different, and the constraint moves
from budget to *confidence*: can you prove the thing still works? For a project with a
strong test suite the answer is easy. For everything else — which is most legacy code —
that is exactly the gap `contract report`'s BLIND number is built to expose, and the reason
this positioning is the right one.

## Assumptions, stated plainly

Measured: all token counts and $/turn figures; Bun's totals; flasgger's contact density and
package-change rate. Assumed: 4 turns per affected call site, 4 verification cycles, ~20
lines touched per call site, 3–8 engineer-hours per 1,000 LOC, and that flasgger's density
generalises. None of the projections has been validated against a completed migration —
they are a framework for reasoning about the order of magnitude, not a quote.
