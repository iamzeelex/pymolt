# Tier 1: observation substrate and in-process aggregation (phase 2)

Status: in progress (2026-11-14). Builds on [fact model v2](tier1-fact-model-v2.md).

## Why

Measured on a Flask + SQLAlchemy + pydantic + requests service (waitress, 8 threads, py3.12) with the
phase-0 runtime:

| | value |
|---|---|
| saturated throughput, no capture → capture | 2138 → 312 rps (−85%), 0 drops after the batched writer |
| paced 200 rps p99 | 50 ms → 657 ms |
| volume | ~1,650 events/s, ~580 B/event → ~82 GB/day raw JSONL |
| host `contract report` on a 592 MB trace | 5 GB RSS |

The research (phase-0 tier-1 study) found that the cost is pymolt's own per-event work, not the hook:
a bare `setprofile` hook costs ~1.6×, targeted `sys.monitoring` ~1.1–1.3×, and normalizing values
plus writing raw events dominates once the hook is cheap.

## Gate (phase 2 is done when)

Measured with `bench/load/` (in the repo) against the same service, py3.12:

1. **Throughput:** saturated rps with capture ≥ 80% of no-capture (≤ 20% loss) in the default
   long-run mode.
2. **Completeness:** 0 dropped calls. Every boundary call is counted even when its values are not
   sampled.
3. **Volume:** extrapolated output < 1 GB/day; host fold of a 10-minute run < 500 MB RSS.
4. **Tail latency:** paced 200 rps p99 ≤ 2× no-capture.
5. **Correctness:** the toy pandas 1.5 → 2.2 end-to-end still gives PASS (equivalent) / FAIL
   (real change), and on 3.12+ the changed call shows the real exception type (`TypeError`), not
   `<unknown>`.
6. Runtime stays pure stdlib and parses with `ast.parse(..., feature_version=(3, 6))`; full suite passes.

## Backends

| Python | discover (wide) | watch (narrow, long runs) |
|---|---|---|
| 3.12+ | `MonitoringBackend` (new, `_monitoring.py`): `sys.monitoring` tool id, global `PY_START`/`PY_RETURN`/`PY_UNWIND`; returns `DISABLE` for code that is not a dependency | `MonitoringBackend` with `set_local_events` only on the watch-set code objects; no global events |
| ≤3.11 | existing `SetProfileBackend` (per-code decision cache, opcode raise bit) | existing `WrapBackend` (targeted wrappers on the watch set) |

- `PY_UNWIND` carries the exception object, so 3.12+ raises are typed.
- Code-swap (`func.__code__` trampolines) is **deferred**. Wrapping on import covers aliases bound
  after the watcher starts, which is the normal case under `sitecustomize`. Code-swap mutates
  dependency code objects, and that needs its own corpus test before it can ship.
- `PYMOLT_TRACE_BACKEND=auto` picks `monitoring` on 3.12+ and `setprofile` below.

## Discover → watch controller

`PYMOLT_TRACE_MODE`:
- `discover`: wide for the whole run. This is the default for `--mode tests`, where the test suite
  is the oracle and must see everything.
- `watch`: narrow from the start, on `PYMOLT_TRACE_IMPACT_TARGETS`.
- `auto` (default for `command`/`attach`, i.e. long runs): discover until the set of observed
  dependency symbols saturates, then switch to watch on observed ∪ impact targets.
  - Saturation means no new symbol for `PYMOLT_TRACE_DISCOVER_QUIET` seconds (default 30) or
    `PYMOLT_TRACE_DISCOVER_MAX` seconds (default 300).
  - The switch is recorded in the metadata: mode, discovered symbol count, switch time.

The known recall limit: a symbol first called after the switch is missed in watch mode. The
metadata reports the switch so the host can say so. Periodic re-discovery is out of scope here.

## Event additions (schema stays v2, fields are additive)

- `dur`: seconds from call to return/raise (monotonic clock).
- `tid`: `threading.get_ident()` of the calling thread.
- On 3.12+ `raised` is the exception type `module.Qualname`. Below 3.12 it stays `"<unknown>"`.

## Sampling and counting

- The sampling decision is made at call start, before any normalization (phase 0).
- **Unsampled calls are still counted.** The backend keeps a light pending record (no inputs) and on
  completion calls `sink.tally(qualname, sym, site, kind, raised=None, dur=None)`. `JsonlSink.tally`
  only bumps a counter. The aggregating sink adds the call to that (sym, site) slot's counts and
  outcome-kind counts.
- Sink method signatures (all keyword-compatible with today's callers):
  `on_return(qualname, inputs, result_obj, site=None, admitted=False, sym=None, symq=None, own=None, dur=None, tid=None)`,
  `on_raise(qualname, inputs, exc_typename, site=None, admitted=False, sym=None, symq=None, own=None, dur=None, tid=None)`,
  `tally(qualname, sym, site, kind, raised=None, dur=None)`.

## Aggregating sink (`FactSink`, `_sinks.py`)

Selected by `PYMOLT_TRACE_SINK=facts|jsonl`. The default is `facts` for `command`/`attach` and
`jsonl` for `tests`. It writes `trace-{pid}.facts.json`, rewritten atomically on a flush interval
(`PYMOLT_TRACE_FLUSH_SECONDS`, default 10) and at exit. There is no event queue, so nothing can be
dropped for backpressure.

```json
{"schema": "pymolt.facts-snapshot/1", "pid": 123, "python": "3.12.11", "executable": "...",
 "started_at": 0.0, "ended_at": 0.0, "mode": "auto", "switched_at": 0.0, "discovered": 42,
 "counters": {"calls": 0, "sampled": 0, "widened": 0, "evicted": 0, ...},
 "facts": [{"q": "...", "sym": "...", "symq": null, "own": null,
            "where": {"file": "...", "line": 0, "func": "..."},
            "in": {...},
            "outcomes": [{"t": "return", "result": ..., "count": 3},
                         {"t": "raise", "raised": "TypeError", "count": 1}],
            "count": 4, "dur": {"n": 4, "sum": 0.01, "min": 0.001, "max": 0.004},
            "first_ts": 0.0, "last_ts": 0.0}],
 "tallies": [{"q": "...", "sym": "...", "where": {...}, "count": 1000,
              "returns": 990, "raises": {"TypeError": 10}, "dur": {...}}]}
```

- A fact is keyed by `(sym or q, where.file, where.line, json(in))`. It has a bounded outcome list:
  at most 8 distinct outcomes, and the rest fold into one `{"t": "other", "count": n}`.
- Per (sym, site) at most `PYMOLT_TRACE_FACTS_PER_SITE` (default 32) distinct inputs. Beyond that, new
  inputs go to a single widened fact with `"in": {"__widened__": true}`, whose outcomes are reduced
  to their type (`{"__type__": ...}`), and `counters.widened` is incremented.
- Global cap `PYMOLT_TRACE_MAX_FACTS` (default 50,000). Past it, the fact with the smallest count is
  evicted Space-Saving style and its count is kept as `counters.evicted`. The host must treat an
  evicting snapshot as incomplete (a quality stamp, not a silent loss).
- Tallies hold every call, sampled or not: completeness comes from tallies, values from facts.

## Host

- `capture_trace` merges per-process `trace-*.facts.json` into one snapshot next to the trace
  (`<trace>.facts.json`) by summing counts. The JSONL path is unchanged.
- `facts.load_facts` accepts a facts snapshot, mapping `outcomes` to the outcome distribution with
  counts. `diff_facts` works unchanged.
- Report and verdict consume it unchanged. A snapshot with `evicted > 0` or `widened > 0` adds a note.
  Eviction blocks PASS (incomplete evidence); widening compares widened facts at type level only.

## Out of scope for phase 2
Value-class lens and saturation metrics (phase 3), request trajectories and `rid` (phase 3: needs
WSGI/ASGI entry detection), code-swap, re-discovery, 2.7/3.4 support (phase 4).
