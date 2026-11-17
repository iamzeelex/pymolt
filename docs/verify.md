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

### Baseline replicate

`capture baseline --replicate` runs the baseline's command again, on the same code in the same
environment. `--runs 2` records the baseline and its replicate in one go. What differs between the
two runs is run-to-run noise, measured instead of guessed (see [Run noise](#run-noise)). A replicate
with another command, environment or sources is ignored. Without one, the report says the noise
was not measured, and `status` suggests `--runs 2`.

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

A function written in C or Cython has no Python frame. pymolt wraps the public Cython functions of
dependency modules, on every Python, so their arguments and results are recorded in full. Any other
compiled call (a C function, a private Cython name, a method of a Cython extension type, a function
bound before the wrap) is recorded without its result, and its arguments are not compared. For those,
the first 2,000 calls per call site are recorded and the rest are counted. Past 1,000,000 calls a
site stops counting, and its count is marked as a lower bound.

When setup's base and target Python differ, captures also record calls into every standard-library
module your project imports, less a short noise list (`--stdlib` changes the list). The test runner
is never recorded.

Two more things are recorded for the comparison:
- the insertion order of every dict of 2 to 64 keys, next to its sorted keys;
- per traced process, before tracing starts, the environment it ran in: OS, kernel, machine,
  container, privileges, CPUs, resource limits, tmpdir, timezone, encodings and the hash seed
  setting. The hostname is not recorded.

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

A test-suite capture writes every event to a trace file. When the writer falls behind, the traced
program waits for it instead of dropping events (`PYMOLT_TRACE_BACKPRESSURE=drop` restores
dropping). It stops waiting only when waiting cannot help, for example when the writer made no
progress for 10 s (`PYMOLT_TRACE_STALL_SECONDS`), and those losses are counted. The capture reports
the time spent waiting and warns from 1 s.

### Provenance

Each capture is stamped with its environment fingerprint, mode, privacy profile, sampling policy and
validity.
- **Replacing:** re-capturing archives the previous recording instead of deleting it.
- **Rollback:** a rollback that restored files sets the post-migration capture aside, in the
  archive, and `verify` names it. A rollback that restored no file keeps it.
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
| UNCOMPARABLE | the two sides could not record the call the same way: one dropped events, capped a compiled-call site, or recorded the call in another shape | makes the verdict inconclusive |

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

### Run noise

Two runs of the same code differ in ports, ids, timestamps and hash order. `verify` compares in
layers:
- the outcome kind (return or raise) and the raise type: always strictly;
- the structure (types, keys, string format): always, after masking;
- the values: only where nothing shows them to be volatile.

A value is masked as volatile when an entropy source returned it on that side (uuid4, random,
secrets, datetime.now, tempfile names), when it is a reading of the capture's own clock, the
project root or the Python version, or when an input carried such a value into an output.
- **Fresh inputs.** A call site whose inputs never recur (a port, an id) is judged as one group.
  Its calls pair by their masked input and compare by value.
- **Input changes.** On an unedited line, an input that changed with no visible cause is a review
  item: what reached the call changed, which is not proof that the dependency did. A new raise or
  a new type still fails.
- **The replicate.** A difference is noise only where the replicate shows the same kind of
  difference at that call site. A site the replicate shows stable is judged strictly. An outcome
  too rare for the replicate to have shown is never called deterministic.
- **Dict key order.** A different order is a review item. It fails only with proof that the order
  is deterministic: a replicate with random hash seeds that agreed on 5 or more keys, a pinned hash
  seed, or a known cause, such as the field order Python 3.9 gives a TypedDict.
- **Environment.** A difference at a call that reads an aspect of the environment that differs
  between the captures (a curated list of readers) is a review item, never noise and never a pass.

The report leads with findings, not with every compared fact:
- a ledger: failing, review, expected by construction, explained, not comparable;
- the failing findings, grouped by symbol and call site, each with a sample;
- the review items, each with the action that settles it;
- the explained differences, by mechanism, with a count and an example.

Each comparison states its level:
- `exact`: the values were compared;
- `type`: one side was opaque, or `--profile shape` was used;
- `kind`: from counts only, return versus raise type, when values were not sampled.

Two captures from different Python versions are compared fairly:
- an untyped raise (≤3.11) matches any exception type;
- C-level raise facts are left out when only one side's tracer records them;
- a decorator's call is attributed to the `def` line before 3.11 and to its `@` line after. It
  is compared within its decorated statement, including several decorators from one factory
  stacked on one function.

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
  - no fact failed, none was uncomparable, and there is no review item;
  - no static symbol is BLIND, of any contact kind (attribute reads and constants included);
  - every dynamic-only symbol had all its facts compared equal.
- **INCONCLUSIVE**: anything else. The reasons name the missing evidence, for example "1 static
  dependency symbol(s) were not exercised" or "capture freshness could not be established". Review
  items land here: differences pymolt cannot judge on its own, such as a hand edit's new value, a
  value that varies between runs, or an environment difference. Each names what settles it.

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
