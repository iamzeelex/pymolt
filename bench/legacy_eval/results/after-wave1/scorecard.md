# Legacy evaluation scorecard

pymolt `2cdcdfb+dirty`, run 2026-11-15T22:48:00+00:00, total 30.7 min, parallel 3.

✓ ok · ~ degraded · ✗ failed · — not reached. Each cell: outcome, wall time, key numbers.

| | scan | setup | assess | capture baseline | target env | plan | apply | capture post | verify |
|---|---|---|---|---|---|---|---|---|---|
| hyper | ✓ 13 edges, 236 contacts (12s) | ✓ requirements-pymolt-eval.txt (4s) | ✓ bronze; resolved; 5 unchanged, 2 upgrade (2.4m) | ✓ 1496 events, 4.9x (91s) | ~ import_hyper: FAILS | ✓ 0/0 inconclusive, 0 rewrites (11s) | ✓ no-op (2s) | ✗ refused, 0 events (4s) | ~ INCONCLUSIVE, trust 80% (14s) |
| lasagne | ✓ 18 edges, 1702 contacts (23s) | ✓ requirements-pymolt-eval.txt (3s) | ✓ bronze; resolved; 2 upgrade, 1 unchanged (2.2m) | ✓ 1961 events, 2.7x (4.6m) | ~ import_lasagne: FAILS | ✓ 0/14 inconclusive, 0 rewrites (18s) | ✓ no-op (2s) | ✗ refused, 0 events (3s) | ~ INCONCLUSIVE, trust 100% (19s) |
| theano | ✓ 21 edges, 7253 contacts (3.9m) | ✓ requirements.txt (3s) | ✓ bronze; resolved; 10 upgrade, 4 unchanged, 1 added (3.4m) | ✓ 830 events, 4.2x (2.6m) | ~ import_theano: FAILS (3s) | ✓ 0/88 inconclusive, 2 rewrites (5.0m) | ✓ 2 files (1s) | ✗ refused, 36 events (3s) | ~ INCONCLUSIVE, trust 24% (13s) |
| alphalens | ✓ 23 edges, 313 contacts (9s) | ✓ requirements-pymolt-eval.txt (3s) | ✓ bronze; resolved; 14 added, 11 removed, 31 upgrade, 6 unchanged (4.3m) | ✓ 11697 events, 8.0x (7.2m) | ✓ import_alphalens: ok (19s) | ✓ 0/174 inconclusive, 0 rewrites (3.3m) | ✓ no-op (2s) | ✗ refused, 16 events (12s) | ~ INCONCLUSIVE, trust 82% (19s) |
| vibora | ✓ 6 edges, 16 contacts (14s) | ✓ requirements.txt (3s) | ✓ gold; resolved; 1 unchanged (74s) | ✗ refused, 0 events (73s) | ~ import_vibora: FAILS (28s) | ✓ 0/0 inconclusive, 0 rewrites (4s) | ✗ migration plan needs evidence: a valid baseline capture is r (1s) | ✗ refused, 0 events (3s) | ~ INCONCLUSIVE, trust 0% (2s) |
| zipline | ✓ 269 edges, 3904 contacts (85s) | ✓ requirements-pymolt-eval.txt (3s) | ✓ bronze; resolved; 29 upgrade, 27 added, 8 removed, 12 unchanged (5.0m) | ✓ 50244 events, 7.6x (6.1m) | ~ import_zipline: FAILS (6s) | ~ 4/309 inconclusive, 6 rewrites (6.7m) | ✓ 6 files (5s) | ✗ refused, 40 events (6s) | ~ INCONCLUSIVE, trust 45% (41s) |

## Baseline environment (not pymolt's job)

- hyper: skipped (4s), marker present, probe passed
- lasagne: skipped (16s), marker present, probe passed
- theano: skipped (14s), marker present, probe passed
- alphalens: skipped (19s), marker present, probe passed
- vibora: ok (553s)
- zipline: skipped (16s), marker present, probe passed
