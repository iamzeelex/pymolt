# Captures and the verdict

`pymolt capture` records what your code does at the boundary with its dependencies. `pymolt verify`
compares two captures and decides `PASS`, `FAIL` or `INCONCLUSIVE`. This page covers how both
work and what each number means.

## Captures

A capture runs your command with a tracer injected through `sitecustomize` and the `PYMOLT_TRACE_*`
environment. Nothing is added to your project or its requirements.

### Modes

| `--mode` | Runs | Defaults |
|---|---|---|
| `tests` | your test suite, which exits on its own | records argument values for every call; full discovery |
| `command` | your app; Ctrl+C ends it and keeps what was recorded | records structure, not values (`shape`); samples which calls keep it; watches incrementally |
| `attach` | nothing: pymolt prints the command for you to run, e.g. in a container or on a server; finish with `--mode attach --collect` | as `command` |

The baseline is captured before any code change. The post-migration capture runs its command in the
target environment when setup named one (`setup --target-env`). With `setup --container`, the
baseline is captured inside that running container.

If the command exits non-zero, the capture is refused and the run is kept as a diagnostic artifact.
With `--accept-failing`, a run that recorded calls is kept anyway and stamped red. A run that
recorded no calls is always refused.

### What is recorded

For every call from your code into an installed dependency:
- the **callee**, as its canonical `module.Class.method`, whatever name your code used;
- the **call site**: file, line and function in your code, plus the line's text, so a rewritten
  site can be told apart from a call that vanished;
- the **arguments your code passed**: a parameter left at its default is recorded by name only, so
  a library that adds parameters does not change the fact;
- the **result** or the **raised exception**. The exception type is recorded on 3.12+, and on
  3.6–3.11 by the targeted wrappers of a long run. Under `setprofile` alone (a test suite on
  3.6–3.11) a capture records that a call raised, not what it raised;
- the duration and the thread.

A function written in C or Cython has no Python frame. pymolt records the call and whether it
raised, but not its result, and its arguments are not compared. Below 3.12 the public Cython
functions of dependency modules are wrapped, so their arguments and results are recorded in full.

When setup's base and target Python differ, captures also record calls into every standard-library
module your project imports, less a short noise list (`--stdlib` changes the list). The test runner
is never recorded.

Values are rendered at the most exact level that can be compared:
- scalars and containers as they are;
- datetimes, decimals, UUIDs, enums, classes and dataclasses canonically;
- numpy and pandas objects as type, shape and dtypes, with the values only when small;
- anything else as its type alone.

`--privacy shape` keeps types and structure but drops scalar values.

### Tracers

| Python | Tracer |
|---|---|
| 3.12+ | `sys.monitoring`. Tests: full discovery. Long runs: incremental. A `CALL` event at your call sites arms only the functions your code actually calls. A startup-only watcher covers properties, `x[k]`, `with`, operators and generators, and turns itself off 30 s after the last new contact. There is no warm-up window. |
| 3.6–3.11 | `sys.setprofile`. Tests: for the whole run. Long runs: then targeted wrappers on the discovered functions (switch after 5 s with no new contact, at most 60 s) |
| 2.7 | not supported; the capture reports that the tracer failed to start |

### Sampling and counting

Every call is counted. In long runs, the arguments and results (their structure, under the default
`shape` privacy) are kept for the first 32 calls at each call site and for 10% of the rest
(`--sample-rate` changes the fraction). Because the counts are complete, a call that stopped
happening is still detected exactly.

Long runs aggregate in the traced process. Memory is bounded and there is no event queue to
overflow. On the load bench, output grew by about 2 MB a day at 200 requests/s
([results](../bench/load/RESULTS.md)). Capture metadata records what was dropped,
if anything, and why.

### Provenance

Each capture is stamped with its environment fingerprint, mode, privacy profile, sampling policy and
validity.
- **Replacing:** re-capturing archives the previous recording instead of deleting it.
- **Diagnostics:** failed, empty or malformed runs are kept under `contract_traces/diagnostics/`.
  They never replace active evidence.
- **Freshness:** the post-migration capture must match the environment on disk now. The baseline
  must match the environment the migration started from. `pymolt apply` records that starting
  point. For a migration done by hand, a baseline still counts as the starting point when only the
  *contents* of the same manifest changed since (bumped pins).

## Facts and the diff

`verify` folds each capture into **facts**: (callee, call site, arguments) → the outcomes seen. It
then compares the two sides.

| Category | Meaning | Fails? |
|---|---|---|
| CHANGED | same call, different outcome | yes |
| DISAPPEARED / APPEARED | the call happens on one side only, at a call site whose code did not change | yes, unless the name no longer refers to a dependency there or the function never ran (then inconclusive) |
| REWRITTEN | the call site's line was edited; the call is compared with the one that replaced it | see [Edited lines](#edited-lines) |
| DRIFT | same outcome, but the library added or removed a parameter the call leaves at its default; or, on a hand-edited line, the same values in another container (list vs view) | no, reported |
| UNCOMPARABLE | the outcome could not be compared (e.g. its distribution was not recorded) | makes the verdict inconclusive |

### Edited lines

A line diff of the source each capture stored decides who edited a call's line:
- **unedited**: compared strictly, as above;
- **edited by `pymolt apply`** (its ledger records each edit): strict. The plan claimed a drop-in
  replacement, so any different outcome fails;
- **edited by hand**: the old and new calls are paired only by the same symbol, by an old → new
  symbol from the plan, or by a one-statement edit. An equal outcome is REWRITTEN equal. A
  different value is a review item, which makes the verdict inconclusive. A new raise fails. A
  call with no partner is counted as an unverified edited site; it does not block PASS.

The report prints the edited sites as equal, representation-only, need review and unverified.

Each comparison states its level:
- `exact`: the values were compared;
- `type`: one side was opaque, or `--profile shape` was used;
- `kind`: from counts only, return versus raise type, when values were not sampled.

Two captures from different Python versions are compared fairly:
- an untyped raise (≤3.11) matches any exception type;
- C-level raise facts are left out when only one side's tracer records them.

## Coverage

- **Static symbols**: `scan` lists every place your code touches a dependency. Each dependency
  symbol those contacts name (`module.Class.method`) is one static symbol, however many call
  sites name it. These are the denominator.
- **Confirmed**: a static symbol a capture exercised.
- **BLIND**: a static symbol no capture exercised. A green suite with many BLIND symbols proves
  little.
- **Dynamic-only**: a symbol the captures saw but the static map did not list, typically a method on
  an object a dependency returned.
- **Trust**: confirmed over the static symbols reached by behavioural contacts (calls, decorators,
  context managers, protocol use) in files the run executed. The report shows the steps from the raw string
  match to this number. It leaves out, with a note, contacts that resolve to interpreter code,
  chains through a function's return value, and dependencies that are not installed where the
  capture ran.

## The verdict

Comparability is checked first. If either capture is invalid or stale, lacks event-loss counters
or dropped events, skipped instrumentation, or ran partly untraced, or if the two used different
commands, targets, modes, privacy profiles or sampling rates, the verdict is INCONCLUSIVE, even
when facts changed. Sampled captures must also have complete call counts.

- **FAIL**: a compared fact failed anywhere in the recording, or a sandbox replay, golden snapshot
  or canary gate failed.
- **PASS** requires all of the following:
  - no fact failed, none was uncomparable, and no edited site needs review;
  - no static symbol is BLIND, of any contact kind (attribute reads and constants included);
  - every dynamic-only symbol had all its facts compared equal.
- **INCONCLUSIVE**: anything else. The reasons name the missing evidence, for example "1 static
  dependency symbol(s) were not exercised" or "capture freshness could not be established".

### Scope after `apply`

When the post-migration code came from `pymolt apply`, coverage is narrowed to the API paths the
plan found changed. The report prints this as *Impact focus* with the full surface next to it.
- **Narrowed:** the static symbols, confirmed, BLIND, dynamic-only and trust. A symbol outside the
  plan's paths that no run exercised does not block PASS.
- **Not narrowed:** FAIL. A behaviour change in any recorded call fails the verdict. The report
  lists the ones outside the plan's paths as changes outside the plan's predicted impacts.

Without an applied plan, coverage counts every static symbol.

### Predicted risks

When the migration was made with `plan` and `apply`, `verify` scores each risk the plan predicted:
- **hit**: a failing change (CHANGED, DISAPPEARED, or REWRITTEN with a different outcome) was
  observed for that symbol at that call site. The mechanism the plan named is not checked;
- **prevented by the plan**: the plan rewrote the call and the behaviour stayed the same;
- **miss**: the call site was compared and nothing changed, so the prediction did not come true;
- **not exercised**: no capture compared that call site.

Failing entries that no prediction covers are listed as **unpredicted**.

### Further oracles

| Option | Adds |
|---|---|
| `--golden-before`, `--golden-after` | golden snapshots compared alongside the captures |
| `--policy`, `--metrics` | canary gates: error rate, p95/p99 latency, output-shape mismatch, dependency-call drift, dropped events |
| `--auto-rollback` | runs `rollback` when a required canary gate fails (opt-in) |
| `--shadow` | classifies read/idempotent operations and leaves unsafe or unknown writes out |
| `--probe-python` | replays the recorded calls in a target interpreter, in a sandbox |
| `--profile exact\|shape\|custom`, `--comparator module:callable` | how values are compared |
| `--cascade` | includes a node-level cascade report |

## Exit codes

| Code | Meaning |
|---|---|
| 0 | passed |
| 1 | a behaviour change, or the captured command failed |
| 2 | invalid invocation |
| 3 | an environment or service failure |
| 4 | insufficient evidence |
