# Legacy evaluation scorecard

pymolt `3829b01+dirty`, run 2026-11-16T05:52:42+00:00, total 17.4 min, parallel 3.

✓ ok · ~ degraded · ✗ failed · — not reached. Each cell: outcome, wall time, key numbers.

| | scan | setup | assess | capture baseline | target env | plan | apply | capture post | verify |
|---|---|---|---|---|---|---|---|---|---|
| hyper | ✓ 13 edges, 236 contacts (3s) | ✓ requirements-pymolt-eval.txt (1s) | ✓ bronze; resolved; 5 unchanged, 2 upgrade (36s) | ✓ 2465 events, 6.5x (23s) | ~ import_hyper: FAILS | ✓ 0/0 inconclusive, 6 rewrites (8s) | ✓ 3 files | ✗ refused, 0 events (2s) | ~ INCONCLUSIVE, trust 83% (4s) |
| lasagne | ✓ 18 edges, 1702 contacts (4s) | ✓ requirements-pymolt-eval.txt (1s) | ✓ bronze; resolved; 2 upgrade, 1 unchanged (45s) | ✓ 2859 events, 3.0x (83s) | ~ import_lasagne: FAILS | ✓ 0/14 inconclusive, 0 rewrites (14s) | ✓ no-op | ✗ refused, 0 events (1s) | ~ INCONCLUSIVE, trust 75% (7s) |
| theano | ✓ 21 edges, 7253 contacts (25s) | ✓ requirements.txt (2s) | ✓ bronze; resolved; 10 upgrade, 4 unchanged, 1 added (68s) | ✓ 20185 events, 11.8x (2.4m) | ~ import_theano: FAILS | ✓ 0/90 inconclusive, 3 rewrites (4.6m) | ✓ 3 files (2s) | ✗ refused, 257 events (3s) | ~ INCONCLUSIVE, trust 29% (8s) |
| alphalens | ✓ 23 edges, 313 contacts (5s) | ✓ requirements-pymolt-eval.txt (2s) | ✓ bronze; resolved; 14 added, 11 removed, 31 upgrade, 6 unchanged (113s) | ✓ 14349 events, 8.1x (3.6m) | ✓ import_alphalens: ok (19s) | ✓ 0/188 inconclusive, 5 rewrites (106s) | ✓ 3 files (1s) | ✗ refused, 52 events (6s) | ~ INCONCLUSIVE, trust 68% (7s) |
| vibora | ✓ 6 edges, 16 contacts (3s) | ✓ requirements.txt (2s) | ✓ gold; resolved; 1 unchanged (30s) | ✓ 96620 events, 2.7x (84s) | ~ import_vibora: FAILS (15s) | ✓ 0/0 inconclusive, 3 rewrites (15s) | ✓ 3 files (2s) | ✗ refused, 1875 events (3s) | ~ INCONCLUSIVE, trust 0% (5s) |
| zipline | ✓ 269 edges, 3904 contacts (10s) | ✓ requirements-pymolt-eval.txt (1s) | ✓ bronze; resolved; 29 upgrade, 27 added, 8 removed, 12 unchanged (2.3m) | ✓ 94511 events, 9.2x (3.8m) | ~ import_zipline: FAILS (10s) | ~ 3/335 inconclusive, 22 rewrites (5.4m) | ✓ 19 files (2s) | ✗ refused, 0 events (9s) | ~ INCONCLUSIVE, trust 52% (10s) |

## Baseline environment (not pymolt's job)

- hyper: skipped (1s), marker present, probe passed
- lasagne: skipped (6s), marker present, probe passed
- theano: skipped (6s), marker present, probe passed
- alphalens: skipped (8s), marker present, probe passed
- vibora: ok (261s)
- zipline: skipped (7s), marker present, probe passed
