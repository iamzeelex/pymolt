# bench/load: the phase-2 load gate

A reproducible load test for PyMolt's long-run capture: a small but realistic HTTP service,
driven by a seeded load generator, run with and without `pymolt contract capture`, scored
against the gate in [docs/design/tier1-observation-substrate.md](../../docs/design/tier1-observation-substrate.md#gate-phase-2-is-done-when).

Everything is stdlib Python except the service, which gets its own venv. Outputs go to
`bench/load/results/` (gitignored, as is `bench/load/.venv/`).

## Quick start

```bash
python3 bench/load/run.py setup            # uv venv bench/load/.venv (Python 3.12) + pinned deps; idempotent
python3 bench/load/run.py gate             # nocap/capture/nocap saturated + paced pair, ~8-10 min; exit 0 = PASS, 1 = FAIL
```

`run.py` runs on any Python 3.9+. It needs `uv` on PATH and the repo venv's
`.venv/bin/pymolt` (override with `--pymolt PATH` or `BENCH_PYMOLT`, for example a pristine
snapshot of a commit while `pymolt/` is being edited, see "Measuring a fixed commit").

### Subcommands

| command | what it does |
|---|---|
| `setup` | creates `bench/load/.venv` with uv (Python 3.12) and `uv pip sync`s `requirements.txt`; skips work when the lock hash is unchanged |
| `nocap --duration S --rate R` | service without capture + load |
| `hook --duration S --rate R` | control: service under a no-op `sys.setprofile` hook (`control/sitecustomize.py`), no pymolt |
| `capture --duration S --rate R [--steady-warmup auto\|S\|off] [--env K=V ...] [-- <pymolt capture args>]` | `pymolt contract capture --when baseline --mode command --project-dir <run>/svc --force <args> -- <svc python> app.py`, load, then SIGINT to the service process only, then waits for pymolt to finalize. `--steady-warmup` defaults to `off` here |
| `report <run-or-session-dir> [--nocap DIR ...] [--no-fold] [--refold]` | summary per run (`report.json`, `summary.md`); a capture/hook run is compared with the mean of the given nocap runs (default: every matching nocap run in its session); capture runs also get the host fold |
| `gate [--duration 60] [--paced-rate 200] [--repeat N] [--steady-warmup auto\|S\|off] [--env K=V ...] [--control] [--wait-quiet S] [-- <capture args>]` | `nocap-sat-0, capture-sat-1, [hook-sat-1], nocap-sat-1, ... capture-sat-N, nocap-sat-N`, then `nocap-paced, capture-paced`, then the PASS/FAIL table |

`--rate 0` means saturated (closed loop). Common options: `--threads` (default 16 saturated,
64 paced), `--seed` (load mix, default 42), `--warmup` (default 2 s, sent but not measured),
`--session DIR` (put several runs in one results dir so `report` can pair a capture with its
nocap run), `--mix name=weight,...` (diagnostics: drive selected endpoints only),
`--wait-quiet S` (wait up to S seconds before each run for other processes to drop below
150% CPU).

`--env` sets variables for the capture runs (e.g. `--env PYMOLT_TRACE_DISCOVER_QUIET=10`);
variables pymolt does not know are passed through and ignored. Anything after `--` is appended
to the `pymolt contract capture` command line (e.g. `-- --backend setprofile`). The gate itself
passes nothing: it measures capture's defaults (backend, mode, sink, sample rate). Do not set
`PYMOLT_TRACE_SAMPLE_RATE` through `--env`; `capture` sets it from `--sample-rate`, so pass that
after `--` if you want a different rate.

## Methodology

**Steady state.** A long-run capture starts in discover mode and switches to watch once no new
dependency symbol has appeared for `PYMOLT_TRACE_DISCOVER_QUIET` s (default 30), or after
`PYMOLT_TRACE_DISCOVER_MAX` s (default 300). Discover is a one-time startup cost, so the gate's
capture runs measure the steady state after it (`--steady-warmup auto`, the gate default):

1. The load starts and keeps running; the load generator does not count anything yet.
2. The runner polls the watcher's live sidecars (`trace-<pid>.facts.json` header, rewritten
   every flush interval, 10 s by default; any `.meta.json`) for `switched_at`.
3. `switched_at + 2 s` (or the first poll after it) the runner touches `<run>/measure.go`; the
   load generator then starts the measured window (60 s) without pausing the load.
4. No live metadata within 20 s (the JSONL sink only writes `.meta.json` at exit): fixed
   fallback of quiet + 30 s. Metadata saying the mode is not `auto`: no switch to wait for,
   the window starts right away. Hard cap: discover max + 30 s.

After the run the final `.meta.json` gives the switch time, reason and symbol count. The
discover window is reported, not gated: time from watcher start to the switch, the switch's
offset into the load, and the service's rps until then (from the load generator's per-second
counts). `steady_state` says whether the measured window really started after the switch.
The paced capture uses the same warmup. `--steady-warmup S` is a fixed warmup, `off` the old
2 s warmup.

**Noise.** Every saturated capture runs between two nocap runs (`nocap-sat-(i-1)`,
`capture-sat-i`, `nocap-sat-i`). The capture is compared with the mean of its two neighbours,
and the spread between them (`|before - after| / mean`) is printed. `--repeat N` runs N
captures (N + 1 nocap runs, neighbours shared); criterion 1 uses the median loss across them.

## What runs

- `service/app.py`: orders service. Flask + SQLAlchemy 2 (sqlite, WAL) + pydantic v2 +
  requests, served by waitress with 8 threads, plus a sweeper thread (expires stale pending
  orders, prunes the rate cache, every second). Each run copies it into a fresh
  `<run>/svc/` project dir, so the sqlite db, `.pymolt/` state and trace belong to that run.
  The data set is recreated from a fixed seed on every start.
  - `POST /orders`: pydantic validation (~13% invalid bodies -> 422), insert, 201.
  - `GET /orders/<id>`: 3% malformed ids -> 400, 7% unknown -> 404, else 200.
  - `GET /customers/<id>/summary`: SQL aggregates by status (Zipf-skewed customers, ~1% -> 404).
  - `GET /convert?amount=&to=`: TTL rate cache, miss -> stub upstream via `requests`;
    the stub answers 503 for CHF -> 502.
  - `POST /orders/<id>/pay`: ids divisible by 5 raise `RuntimeError` -> 500 (about 2% of all
    requests, logged with a traceback by Flask); expired -> 409.
  - `GET /healthz`.
  Written lean on purpose (request-scoped `scoped_session`, prebuilt statements, dict
  returns) so it makes ~5-10 dependency-boundary calls per request, close to the service
  behind the phase-0 numbers in the design doc. Writes are serialized by an in-process lock
  (sqlite has one writer; without it SQLITE_BUSY retries sleep 1-100 ms and throughput
  collapses as soon as a writer slows down).
- `stub_upstream.py`: FX-rate stub (stdlib `ThreadingHTTPServer`, 2 ms service time).
- `loadgen.py DURATION THREADS RATE --url URL`: threads with keep-alive `http.client`
  connections. Endpoint mix (fixed weights): get order 35, create 20, convert 20, summary 10,
  pay 10, health 5. A seeded RNG pre-builds the request sequence and a shared counter hands it
  out, so request k is the same in every run. RATE > 0 is an open loop: request i is due at
  `t0 + i/RATE`, a free thread takes the next slot. Only requests that start (saturated) or
  are due (paced) inside `[mark, mark + duration)` count, where `mark` is the end of the
  warmup: `--warmup` s, or later when `--warmup-until FILE` appears (capped by
  `--max-warmup`). Warmup traffic is summarized separately and in `per_second`.

Ports are free ports picked by the OS (`port=0`, reported through ready files). Every child
runs in its own process group and is killed on exit, error or Ctrl+C.

## Results layout

```
results/<UTC stamp>-<label>/          # one session (gate, or a standalone nocap/capture)
  gate.json, summary.md               # gate only: criteria, per-run summaries, provenance
  <run>/run.json                      # config, provenance, host load, timings, loadgen result, capture facts
  <run>/report.json, summary.md       # written by `report` / `gate`
  <run>/loadgen.json, *.log           # raw load result, service/capture/stub logs
  <run>/fold.json, fold.time          # capture runs: `contract report --json` output + /usr/bin/time -l
  <run>/svc/                          # the run's project dir (app.py, orders.db, .pymolt/)
```

A 60 s phase-1 saturated capture leaves ~20-60 MB of trace; delete old sessions freely.

## What the numbers mean

| field | meaning |
|---|---|
| `rps` | requests started (saturated) or due (paced) in the measured window that completed, / duration. Includes 4xx/5xx: they are part of the mix |
| `latency_ms` p50/p95/p99 | from send to full response |
| `latency_co_ms` (paced) | from the scheduled time to full response: corrected for coordinated omission, so a stalled server cannot hide queueing by slowing the client. The gate's tail criterion uses this |
| `send_lateness_ms` (paced) | how late the client sent; ~0 unless all client threads were busy (the client sleeps in shrinking steps because Darwin gives a sleep up to 50%/10 ms of timer leeway) |
| `missed_slots` (paced) | scheduled requests never sent before the run's 10 s grace ran out (server far below the offered rate) |
| `client_cpu_pct` | load generator CPU; near 100% means the client, not the service, is the limit |
| `rps_loss` | `1 - capture rps / mean(nocap rps)` against the neighbouring nocap runs (gate) or every nocap run with the same rate, duration and threads in the session (`report`) |
| `nocap_spread` | `(max - min) / mean` of those nocap runs: how much the host moved during the comparison |
| `warmup_s`, `warmup.rps` | when the measured window started (s after the load started) and the rps before it |
| `discover.*` (capture) | `mode`, `switch_reason` (quiet / max), `discovered` symbols, `discover_s` (watcher start to switch), `switch_after_load_start_s`, `discover_rps` (load rps before the switch), `discover_bytes`, `steady_state` (window started after the switch) |
| `events_written` / `events_seen` | JSONL events in the merged trace / events the runtime saw (`.meta.json` counters) |
| `calls_counted` | the first of `calls_counted`, `calls_seen`, `calls`, `tallied` the runtime reports, else `events_seen` (phase 1 has no separate call counter) |
| `dropped` | `counters.dropped_events` from the merged `.meta.json` (sampling + backpressure + write failures + shutdown drops); `None` when the metadata is incomplete |
| `out_bytes` | everything the capture left under `<svc>/.pymolt/` except logs, `contract_state.json` and the bundle: the trace, its `.meta.json`, any `.facts.json` or other sidecars (listed in `out_files`) |
| `bytes_startup` / `bytes_before_window` / `bytes_in_window` / `bytes_at_exit` | watcher output (sampled live in pymolt's work dir every 2 s) present when the service became ready / added before the measured window (discover) / added during the window, after letting a lagging writer drain / added at shutdown (metadata, final snapshot, sidecars) |
| `bytes_per_day` | `bytes at window start + in_window x 86400 / window_seconds + at_exit`: what the steady state writes scales with time, startup, discover and exit output happen once. A rewritten facts snapshot counts by its growth. Gate criterion 3a |
| `bytes_per_day_linear` | `out_bytes x 86400 / load_seconds`, the naive upper bound |
| `capture_wall_s` / `capture_finalize_s` | pymolt capture process lifetime / from service exit to pymolt exit (merge + metadata + site text) |
| `fold` | `/usr/bin/time -l pymolt contract report <svc> --json`: `max_rss_bytes` (gate 3b), `seconds`, exit code (non-zero exit codes are findings, not harness errors), whether the JSON parsed |
| `host_load` | load average and the other processes' summed %CPU right before the run, top consumers |

### Gate thresholds (`GATE` in run.py)

| # | criterion | PASS when |
|---|---|---|
| 1 | throughput | steady-state saturated capture rps >= 80% of the mean of its neighbouring nocap runs (median over `--repeat`) |
| 2 | completeness | `dropped == 0` in both capture runs (unknown counts as ERROR) |
| 3a | volume | max `bytes_per_day` of the capture runs < 1 GB (10^9 bytes) |
| 3b | host fold | max fold RSS < 500 MiB |
| 4 | tail latency | paced capture p99 (from schedule, steady state) <= 2 x paced nocap p99 |

Rows marked INFO (discover window, no-op hook control, an unconfirmed steady state) are
reported and never fail the gate.

Gate 5 (pandas 1.5 -> 2.2 correctness) and 6 (stdlib-only runtime, suite passes) are not load
measurements and are not checked here.

## Measuring a fixed commit

The capture uses whatever pymolt the chosen executable imports. With an editable repo venv
that is the working tree, including half-finished edits. To measure a commit instead:

```bash
SNAP=/tmp/pymolt-$(git rev-parse --short HEAD)
mkdir -p $SNAP && git archive HEAD | tar -x -C $SNAP && git rev-parse HEAD > $SNAP/.git-head
uv venv $SNAP/.venv --python 3.12 && uv pip install --python $SNAP/.venv/bin/python -e $SNAP
python3 bench/load/run.py --pymolt $SNAP/.venv/bin/pymolt gate
```

`run.json` records which pymolt ran (`provenance.pymolt_root`, `snapshot`, `git_head`) and,
for the working tree, the dirty `pymolt/` files plus a hash of their diff.

## Known caveats

- **GIL hand-offs dominate saturated capture numbers on macOS.** Any Python-level profile
  hook makes all 8 worker threads CPU-bound; every GIL-releasing call (each sqlite3 step,
  socket send, contended lock) then waits for a hand-off, and on Apple Silicon (P and E
  cores) under background load those waits stretch to tens of ms. Saturation is
  metastable: with all workers busy the hand-off waits feed on themselves and the same
  capture run can give 6 or 100 rps, while the same capture at a paced 200 rps (workers
  mostly idle) shows almost no cost. `run.py hook` (no-op hook, no pymolt) shows how much
  of a capture's loss is the platform and how much is pymolt (with the `sys.monitoring`
  backend the watcher installs no profile hook, so the control then only describes the
  setprofile path). Use `--repeat 3` and look at the per-capture losses before reading
  anything into one number.
- **Host noise.** Other processes (test suites, Spotlight, `mediaanalysisd`) move both the
  nocap and the capture numbers. The before/after nocap runs and their printed spread bound
  the drift during a comparison; `host_load` is recorded per run; `--wait-quiet S` waits for
  a calmer host. Nocap saturated rps on an M4 ranges ~1,300-4,500 depending on load.
- **The client shares the host.** The load generator is one Python process; `client_cpu_pct`
  shows when it gets close to a core. Paced runs use a 0.2 ms switch interval and stepped
  sleeps in the client to keep `send_lateness_ms` near zero.
- **Volume extrapolation from 60 s.** `bytes_per_day` assumes the window's growth continues and
  startup, discover and exit output happen once. A facts snapshot flushed every 10 s grows in
  steps, so a window shorter than a few flushes under-samples it; confirm with a longer run
  (`capture --duration 600 --steady-warmup auto`), which also covers the gate's "fold of a
  10-minute run" (the default gate folds 60 s runs).
- **Switch detection latency.** The switch is seen when the watcher next publishes metadata
  (the facts flush interval), so the window can start up to ~10 s after the switch. With the
  JSONL sink there is no live metadata and the fixed fallback applies; the final metadata
  still tells whether the window was in steady state.
- **Paced runs below capacity.** If capture cannot sustain the paced rate, the open loop falls
  behind, `missed_slots` > 0 and from-schedule latency grows with the backlog; the p99 then
  measures overload, not tail latency.
- **Determinism** stops at the request sequence and the data set: thread interleaving, total
  request count in saturated mode and the 500/409 split of `pay` (orders expire after 20 s)
  vary between runs.
