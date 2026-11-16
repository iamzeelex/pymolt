# Legacy evaluation scorecard

pymolt `9ef1336+dirty`, run 2026-11-16T01:11:34+00:00, total 13.7 min, parallel 3.

✓ ok · ~ degraded · ✗ failed · — not reached. Each cell: outcome, wall time, key numbers.

| | scan | setup | assess | capture baseline | target env | plan | apply | capture post | verify |
|---|---|---|---|---|---|---|---|---|---|
| hyper | ✓ 13 edges, 236 contacts (3s) | ✓ requirements-pymolt-eval.txt (1s) | ✓ bronze; resolved; 5 unchanged, 2 upgrade (36s) | ✓ 1496 events, 5.5x (20s) | ~ import_hyper: FAILS | ✓ 0/0 inconclusive, 6 rewrites (4s) | ✓ 3 files | ✗ refused, 0 events (2s) | ~ INCONCLUSIVE, trust 80% (3s) |
| lasagne | ✓ 18 edges, 1702 contacts (3s) | ✓ requirements-pymolt-eval.txt (1s) | ✓ bronze; resolved; 2 upgrade, 1 unchanged (44s) | ✓ 1961 events, 2.7x (73s) | ~ import_lasagne: FAILS | ✓ 0/14 inconclusive, 0 rewrites (10s) | ✓ no-op | ✗ refused, 0 events (1s) | ~ INCONCLUSIVE, trust 100% (7s) |
| theano | ✓ 21 edges, 7253 contacts (22s) | ✓ requirements.txt (2s) | ✓ bronze; resolved; 10 upgrade, 4 unchanged, 1 added (72s) | ✓ 830 events, 3.7x (42s) | ~ import_theano: FAILS | ✓ 0/88 inconclusive, 3 rewrites (3.9m) | ✓ 3 files | ✗ refused, 36 events (1s) | ~ INCONCLUSIVE, trust 24% (6s) |
| alphalens | ✓ 23 edges, 313 contacts (5s) | ✓ requirements-pymolt-eval.txt (1s) | ✓ bronze; resolved; 14 added, 11 removed, 31 upgrade, 6 unchanged (110s) | ✓ 11697 events, 7.6x (3.2m) | ✓ import_alphalens: ok (14s) | ✓ 0/174 inconclusive, 5 rewrites (81s) | ✓ 3 files (1s) | ✗ refused, 38 events (5s) | ~ INCONCLUSIVE, trust 82% (8s) |
| vibora | ✓ 6 edges, 16 contacts (3s) | ✓ requirements.txt (1s) | ✓ gold; resolved; 1 unchanged (29s) | ✗ refused, 0 events (46s) | ~ import_vibora: FAILS (13s) | ✓ 0/0 inconclusive, 3 rewrites (2s) | ✗ migration plan needs evidence: a valid baseline capture is r | ✗ refused, 0 events (1s) | ~ INCONCLUSIVE, trust 0% |
| zipline | ✓ 269 edges, 3904 contacts (9s) | ✓ requirements-pymolt-eval.txt (1s) | ✓ bronze; resolved; 29 upgrade, 27 added, 8 removed, 12 unchanged (2.3m) | ✓ 50244 events, 6.8x (2.5m) | ~ import_zipline: FAILS (5s) | ~ 4/309 inconclusive, 22 rewrites (4.2m) | ✓ 19 files (2s) | ✗ refused, 40 events (2s) | ~ INCONCLUSIVE, trust 45% (15s) |

## Baseline environment (not pymolt's job)

- hyper: skipped (1s), marker present, probe passed
- lasagne: skipped (6s), marker present, probe passed
- theano: skipped (5s), marker present, probe passed
- alphalens: skipped (8s), marker present, probe passed
- vibora: ok (252s)
- zipline: skipped (7s), marker present, probe passed
