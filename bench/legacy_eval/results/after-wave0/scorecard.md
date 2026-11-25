# Legacy evaluation scorecard

pymolt `27e0790+dirty` (Wave 0, uncommitted), run 2026-11-15T21:09:39+00:00, total 27.2 min, parallel 3.

✓ ok · ~ degraded · ✗ failed · — not reached. Each cell: outcome, wall time, key numbers.

| | scan | setup | assess | capture baseline | target env | plan | apply | capture post | verify |
|---|---|---|---|---|---|---|---|---|---|
| hyper | ✓ 13 edges, 238 contacts (11s) | ✓ requirements-pymolt-eval.txt (2s) | ✓ bronze; resolved; 5 unchanged, 2 upgrade (118s) | ✓ 1496 events, 5.0x (59s) | ~ import_hyper: FAILS | ~ 2/2 inconclusive, 0 rewrites (9s) | ✓ no-op | ✗ refused, 0 events (10s) | ~ INCONCLUSIVE, trust 0% (8s) |
| lasagne | ✓ 18 edges, 1703 contacts (21s) | ✓ requirements-pymolt-eval.txt (2s) | ✓ bronze; resolved; 2 upgrade, 1 unchanged (110s) | ✓ 1961 events, 2.7x (3.5m) | ~ import_lasagne: FAILS | ~ 2/2 inconclusive, 0 rewrites (15s) | ✓ no-op | ✗ refused, 0 events (12s) | ~ INCONCLUSIVE, trust 0% (12s) |
| theano | ✓ 21 edges, 7272 contacts (3.5m) | ✓ requirements.txt (3s) | ✓ bronze; resolved; 10 upgrade, 4 unchanged, 1 added (3.2m) | ✓ 830 events, 2.5x (91s) | ~ import_theano: FAILS (1s) | ~ 11/11 inconclusive, 0 rewrites (107s) | ✓ no-op | ✗ refused, 4 events (1s) | ~ INCONCLUSIVE, trust 1% (90s) |
| alphalens | ✓ 23 edges, 319 contacts (4s) | ✓ requirements-pymolt-eval.txt (2s) | ✓ bronze; resolved; 14 added, 11 removed, 31 upgrade, 6 unchanged (3.1m) | ✓ 11697 events, 6.8x (7.6m) | ✓ import_alphalens: ok (23s) | ~ 31/31 inconclusive, 0 rewrites (38s) | ✓ no-op (1s) | ✗ refused, 0 events (18s) | ~ INCONCLUSIVE, trust 7% (8s) |
| vibora | ✓ 6 edges, 16 contacts (5s) | ✓ requirements.txt (2s) | ✓ gold; resolved; 1 unchanged (80s) | ✗ refused, 0 events (89s) | ~ import_vibora: FAILS (31s) | ✓ 0/0 inconclusive, 0 rewrites | ✗ migration plan needs evidence: a valid baseline capture is r | ✗ refused, 0 events (10s) | ~ INCONCLUSIVE, trust 0% (10s) |
| zipline | ✓ 269 edges, 3917 contacts (41s) | ✓ requirements-pymolt-eval.txt (1s) | ✓ bronze; resolved; 29 upgrade, 27 added, 8 removed, 12 unchanged (5.5m) | ✓ 50244 events, 7.4x (6.1m) | ~ import_zipline: FAILS (6s) | ~ 41/41 inconclusive, 1 rewrites (2.3m) | ✓ 1 files (2s) | ✗ refused, 0 events (78s) | ~ INCONCLUSIVE, trust 2% (83s) |

## Baseline environment (not pymolt's job)

- hyper: skipped (3s), marker present, probe passed
- lasagne: skipped (15s), marker present, probe passed
- theano: skipped (10s), marker present, probe passed
- alphalens: skipped (8s), marker present, probe passed
- vibora: ok (556s)
- zipline: skipped (8s), marker present, probe passed
