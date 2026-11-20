# Load gate: the numbers the docs cite

Raw results live in `bench/load/results/` (gitignored). This page keeps the run the README and
docs/verify.md quote, so a reader can check them. Re-measure with `python3 bench/load/run.py gate`
on a quiet host (see [README.md](README.md)).

## The cited run

- session `20261120T053716Z-gate`, 2026-11-20, `--repeat 3 --wait-quiet 120`;
- code: the 0.2 release branch with the hidden verify options (`446829a`), clean `pymolt/` tree;
- Apple M4 (10 CPUs), macOS 27.2, service on Python 3.12.11 (`sys.monitoring`, incremental watch);
- the service: Flask + SQLAlchemy + pydantic (flask 3.1.3, sqlalchemy 2.1.8, pydantic 2.14.3),
  60 s per run.

| Criterion | Threshold | Measured | Result |
|---|---|---|---|
| Saturated throughput loss, steady state | ≤ 20% | 4134 → 3333 requests/s: 19.3% (median of 19.3%, 19.0%, 20.5%; no-capture spread 0.2–2.8%) | PASS |
| Dropped calls | 0 | 0 in all four capture runs | PASS |
| Output per day | < 1 GB | saturated 1.3 MB; paced at 200 requests/s 1.2 MB | PASS |
| Host fold (verify) | < 500 MiB | 106–107 MiB, 1.2 s | PASS |
| Paced p99 latency at 200 requests/s | ≤ 2× | 6.062 → 6.366 ms (×1.05) | PASS |
| Startup: throughput in the first 30 s | ≥ 50% of no-capture | 71.4%, 72.1%, 72.1% | PASS |
| Recall: (symbol, call site) pairs vs a full-discovery reference | ≥ 95% | 160/160 | PASS |

The saturated loss passes by 0.7 points, and one of the three repeats (20.5%) was over the gate:
the margin is thin. The earlier cited run (2026-11-15, `9d5f6e8` with uncommitted files) measured
16.8%; the tracer gained exception events, decorator pairing, late implicit contacts and the
receiver-lifetime fix since.
