# Limits and status

## The verdict

- **Scope after `apply`.** When the post-migration code came from `pymolt apply`, the verdict
  covers only the calls into the API paths the plan found changed. A behaviour change in a call to
  any other API appears in the full report (`all_fact_diff`) but does not fail the verdict, and
  BLIND symbols outside that set do not block PASS. Without an applied plan, the verdict covers
  every recorded call. See [verify.md](verify.md#scope-after-apply).
- **Only exercised calls are compared.** If the tests never call a dependency, there is nothing to
  compare, and the verdict stays INCONCLUSIVE.
- **Exception types on 3.6–3.11.** A test-suite capture records that a call raised, not what it
  raised. Long runs record the type once the targeted wrappers take over. 3.12+ always records it.
- **C-implemented dependency functions** are not recorded, except that `setprofile` notes when they
  raise.
- **Late dunder contacts in long runs (3.12+).** Watching for dunder, property and generator calls
  stops 30 s after the last new contact, so one of these that is first used after that is missed.

## Other commands

- **`assess` stops at resolution.** A target that resolves may still fail to build or import.
- **PyMolt itself** runs on 3.12. On 3.13, three tests currently fail.
- **The tracer** is written to load on Python 3.4–3.5. That is not tested.

## Overhead

| Workload | Measured |
|---|---|
| Test suites, Python 3.6 in emulated containers (`setprofile`), five legacy projects | 2.7–7.6× slower ([scorecard](../bench/legacy_eval/results/after-wave2/scorecard.md)) |
| A Flask + SQLAlchemy + pydantic service on Python 3.12 (`sys.monitoring`), saturated | 16.8% lower throughput once steady; 74–76% of normal throughput in the first 30 s; 0 dropped calls |
| Same service at 200 requests/s | p99 latency ×1.01; output about 2 MB per day |

The service numbers are from one load-gate run on 2026-11-15. The tracer has changed since, and
they have not been re-measured cleanly ([bench/load/RESULTS.md](../bench/load/RESULTS.md)).

In long runs, pymolt records the structure of arguments and results by default, not their values.
It records them for the first 32 calls at each call site and for 10% of the rest, and it counts
every call ([verify.md](verify.md#sampling-and-counting)).

## Legacy projects

Six legacy projects were migrated from Python 3.6 to 3.12: hyper, vibora, theano, lasagne,
alphalens and zipline.
- Five capture a baseline. vibora's was refused with no calls recorded.
- Plans with rewrites were applied to four of them.
- None reaches a compared post-migration capture. The target fails to import on four, vibora has no
  baseline, and alphalens's post-migration test run was refused.

Details: [scorecard](../bench/legacy_eval/results/after-wave2/scorecard.md),
[issues](../bench/legacy_eval/KEY_ISSUES.md).
