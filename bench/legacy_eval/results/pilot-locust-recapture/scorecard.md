# Legacy evaluation scorecard

pymolt `01e9233`, run 2026-11-17T20:08:07+00:00, total 27.1 min, parallel 1.

✓ ok · ~ degraded · ✗ failed · — not reached. Each cell: outcome, wall time, key numbers.

| | scan | setup | assess | capture baseline | target env | plan | apply | capture post | verify |
|---|---|---|---|---|---|---|---|---|---|
| locust | ✓ 59 edges, 569 contacts (5s) | ✓ requirements-pymolt-eval.txt (1s) | ✓ bronze; resolved; 13 added, 12 upgrade, 1 unchanged (8s) | ✓ 948134 events, 1.3x (5.4m) | ✓  | ✓ 0/0 inconclusive, 0 rewrites (43s) | ✓ no-op (10s) | ✓ 1665729 events (4.8m) | ✓ FAIL, trust 65% (2.4m) |

## Baseline environment (not pymolt's job)

- locust: skipped (1s), marker present, probe passed
