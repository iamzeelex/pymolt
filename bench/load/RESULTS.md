# Load gate: the numbers the docs cite

Raw results live in `bench/load/results/` (gitignored). This page keeps the run the README and
docs/verify.md quote, so a reader can check them. Re-measure with `python3 bench/load/run.py gate`
on a quiet host (see [README.md](README.md)).

## The cited run

- session `20261115T025955Z-gate`, 2026-11-15;
- code `feat/verify-incremental-watch` @ `9d5f6e8`, with 4 uncommitted files in `pymolt/`;
- Apple M4 (10 CPUs), macOS 27.2, service on Python 3.12.11 (`sys.monitoring`, incremental watch);
- the service: Flask + SQLAlchemy + pydantic, 16 client threads, 60 s per run.

| Criterion | Threshold | Measured | Result |
|---|---|---|---|
| Saturated throughput loss, steady state | ≤ 20% | 4250 → 3546 requests/s: 16.8% (median of 18.1%, 15.4%, 16.8%; no-capture spread 5.0%) | PASS |
| Dropped calls | 0 | 0 in all four capture runs | PASS |
| Output per day | < 1 GB | saturated 0.6–0.7 MB; paced at 200 requests/s 2.0 MB | PASS |
| Host fold (verify) | < 500 MiB | 82 MiB, 1.1 s | PASS |
| Paced p99 latency at 200 requests/s | ≤ 2× | 6.143 → 6.178 ms (×1.01) | PASS |
| Startup: throughput in the first 30 s | ≥ 50% of no-capture | 74.2%, 75.1%, 76.1% | PASS |
| Recall: (symbol, call site) pairs vs a full-discovery reference | ≥ 95% | 159/159 | PASS |

## Since then

The tracer has had 21 more commits up to the wave-2 tip (`3829b01`). The gate runs on that tip so
far (2026-11-16) do not replace the numbers above. One run had a clean tree, but its two
no-capture runs differed by 21%, so the baseline is unreliable. The others ran with uncommitted
changes from other work in the tree. They measured between 9% and 28% saturated loss, and only the
last passed (18.9% loss, 72.5–72.9% startup, p99 ×1.06, 1.3 MB/day saturated, 4.0 MB/day paced).
A clean re-measurement is still to do.
