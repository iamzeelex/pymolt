# Legacy evaluation scorecard

pymolt `3d7354f+dirty`, run 2026-11-16T10:13:55+00:00, total 20.0 min, parallel 1.

✓ ok · ~ degraded · ✗ failed · — not reached. Each cell: outcome, wall time, key numbers.

| | scan | setup | assess | capture baseline | target env | plan | apply | capture post | verify |
|---|---|---|---|---|---|---|---|---|---|
| locust | ✓ 59 edges, 569 contacts (5s) | ✓ requirements-pymolt-eval.txt (1s) | ✓ bronze; resolved; 13 added, 12 upgrade, 1 unchanged (7s) | ✓ 1071337 events, 1.2x (5.3m) | ✓ import_locust: ok (9s) | ✓ 0/0 inconclusive, 0 rewrites (31s) | ✓ no-op (9s) | ~ 1645835 events (4.6m) | ~ INCONCLUSIVE, trust 65% (62s) |

## Baseline environment (not pymolt's job)

- locust: skipped (0s), marker present, probe passed
