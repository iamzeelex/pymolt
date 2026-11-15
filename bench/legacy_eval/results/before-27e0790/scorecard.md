# Legacy evaluation scorecard

pymolt `27e0790`, run 2026-11-15T20:44:26+00:00, total 22.5 min, parallel 3.

✓ ok · ~ degraded · ✗ failed · — not reached. Each cell: outcome, wall time, key numbers.

| | scan | setup | assess | capture baseline | target env | plan | apply | capture post | verify |
|---|---|---|---|---|---|---|---|---|---|
| hyper | ✓ 4 edges, 238 contacts (8s) | ✓ requirements-pymolt-eval.txt (4s) | ✓ bronze; resolved; 5 unchanged, 2 upgrade (69s) | ✓ 1496 events, 5.8x (38s) | ~ import_hyper: FAILS | ~ 2/2 inconclusive, 0 rewrites (7s) | ✓ no-op | ✗ refused, 0 events (2s) | ~ INCONCLUSIVE, trust 0% (6s) |
| lasagne | ✓ 13 edges, 1703 contacts (15s) | ✓ requirements-pymolt-eval.txt (4s) | ✓ bronze; resolved; 2 upgrade, 1 unchanged (89s) | ✓ 1961 events, 2.3x (113s) | ~ import_lasagne: FAILS | ~ 2/2 inconclusive, 0 rewrites (11s) | ✓ no-op | ✗ refused, 0 events (1s) | ~ INCONCLUSIVE, trust 0% (12s) |
| theano | ✓ 13 edges, 7272 contacts (2.6m) | ✓ requirements.txt (4s) | ✓ bronze; resolved; 10 upgrade, 4 unchanged, 1 added (2.3m) | ✓ 830 events, 3.0x (63s) | ~ import_theano: FAILS | ~ 11/11 inconclusive, 0 rewrites (108s) | ✓ no-op | ✗ refused, 4 events (1s) | ~ INCONCLUSIVE, trust 1% (98s) |
| alphalens | ✓ 11 edges, 319 contacts (7s) | ✓ requirements-pymolt-eval.txt (4s) | ✓ bronze; resolved; 14 added, 11 removed, 31 upgrade, 6 unchanged (3.2m) | ✓ 11697 events, 8.4x (7.1m) | ✓ import_alphalens: ok (20s) | ~ 31/31 inconclusive, 0 rewrites (34s) | ✓ no-op (2s) | ✗ refused, 0 events (10s) | ~ INCONCLUSIVE, trust 7% (7s) |
| vibora | ✓ 1 edges, 16 contacts (9s) | ✓ requirements.txt (5s) | ✓ gold; resolved; 1 unchanged (78s) | ✗ refused, 0 events (77s) | ~ import_vibora: FAILS (24s) | ✓ 0/0 inconclusive, 0 rewrites (1s) | ✗ migration plan needs evidence: a valid baseline capture is r (1s) | ✗ refused, 0 events (2s) | ~ INCONCLUSIVE, trust 0% (7s) |
| zipline | ✓ 269 edges, 3917 contacts (58s) | ✓ requirements-pymolt-eval.txt (3s) | ✓ bronze; resolved; 29 upgrade, 27 added, 8 removed, 12 unchanged (4.1m) | ✓ 50244 events, 8.4x (6.0m) | ~ import_zipline: FAILS (5s) | ~ 41/41 inconclusive, 1 rewrites (104s) | ✓ 1 files (2s) | ✗ refused, 0 events (4s) | ~ INCONCLUSIVE, trust 2% (61s) |

## Baseline environment (not pymolt's job)

- hyper: skipped (2s), marker present, probe passed
- lasagne: skipped (11s), marker present, probe passed
- theano: skipped (10s), marker present, probe passed
- alphalens: skipped (13s), marker present, probe passed
- vibora: ok (448s)
- zipline: skipped (19s), marker present, probe passed
