# Legacy evaluation scorecard

pymolt `7c1e0fc`, run 2026-11-20T07:18:45+00:00, total 667.9 min, parallel 1.

Note: hyper re-run alone: Docker Desktop stopped during its first baseline capture (docker cp could not reach the daemon). Wall times of lasagne and theano (and the total) are not comparable: the host slept or Docker stalled during the run (setup took 17.7 min); their outcomes and numbers stand.

✓ ok · ~ degraded · ✗ failed · — not reached. Each cell: outcome, wall time, key numbers.

| | scan | setup | assess | capture baseline | target env | plan | apply | capture post | verify |
|---|---|---|---|---|---|---|---|---|---|
| hyper | ✓ 13 edges, 236 contacts (5s) | ✓ requirements-pymolt-eval.txt (1s) | ✓ bronze; resolved; 5 unchanged, 2 upgrade (52s) | ✓ 1861 events, 8.4x (28s) | ~ import_hyper: FAILS | ✓ 0/0 inconclusive, 6 rewrites (6s) | ✓ 3 files (1s) | ✗ refused, 0 events (2s) | ~ INCONCLUSIVE, coverage 46%, recall 89% (function) (3s) |
| lasagne | ✓ 18 edges, 1703 contacts (3s) | ✓ requirements-pymolt-eval.txt (17.7m) | ✓ bronze; resolved; 2 upgrade, 1 unchanged (106.2m) | ✓ 2843 events (111.1m) | ~ import_lasagne: FAILS | ✓ 0/14 inconclusive, 0 rewrites (10.9m) | ✓ no-op | ✗ refused, 0 events | ~ INCONCLUSIVE, coverage 13%, recall 43% (function) (10.0m) |
| theano | ✓ 21 edges, 7262 contacts (21s) | ✓ requirements.txt (1s) | ✓ bronze; resolved; 10 upgrade, 4 unchanged, 1 added (63s) | ✓ 13824 events, 10.8x (119s) | ~ import_theano: FAILS (2s) | ✓ 0/90 inconclusive, 3 rewrites (190.1m) | ✓ 3 files (17.6m) | ✗ refused, 162 events (2s) | ~ INCONCLUSIVE, coverage 2%, recall 34% (function) (31.6m) |
| alphalens | ✓ 23 edges, 313 contacts (3s) | ✓ requirements-pymolt-eval.txt (1s) | ✓ bronze; resolved; 14 added, 11 removed, 31 upgrade, 6 unchanged (107s) | ✓ 14324 events, 8.0x (3.2m) | ✓ import_alphalens: ok (18s) | ✓ 0/188 inconclusive, 5 rewrites (88s) | ✓ 3 files (1s) | ✗ refused, 27 events (6s) | ~ INCONCLUSIVE, coverage 69%, recall 72% (function) (7s) |
| vibora | ✓ 6 edges, 16 contacts (2s) | ✓ requirements.txt (1s) | ✓ gold; resolved; 1 unchanged (29s) | ✓ 62356 events, 2.3x (70s) | ~ import_vibora: FAILS (15s) | ✓ 0/0 inconclusive, 3 rewrites (12s) | ✓ 3 files (2s) | ✗ refused, 1029 events (2s) | ~ INCONCLUSIVE, coverage 4%, recall 0% (function) (5s) |
| zipline | ✓ 269 edges, 3906 contacts (8s) | ✓ requirements-pymolt-eval.txt (1s) | ✓ bronze; resolved; 29 upgrade, 27 added, 8 removed, 12 unchanged (2.2m) | ✓ 66535 events, 8.5x (3.0m) | ~ import_zipline: FAILS (6s) | ~ 3/335 inconclusive, 22 rewrites (5.1m) | ✓ 19 files (3s) | ✗ refused, 0 events (8s) | ~ INCONCLUSIVE, coverage 0%, recall None% (function) (9s) |

## Baseline environment (not pymolt's job)

- hyper: skipped (2s), marker present, probe passed
- lasagne: skipped (923s), marker present, probe passed
- theano: skipped (5s), marker present, probe passed
- alphalens: skipped (6s), marker present, probe passed
- vibora: skipped (1s), marker present, probe passed
- zipline: skipped (6s), marker present, probe passed
