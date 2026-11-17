# Limits and status

## Where pymolt cannot help yet

### Getting to a comparison

- **Targets that do not build or import.** `assess` stops at resolution. When a dependency fails to
  build or import on the target (for example hyperframe < 4, `numpy.distutils`, a Cython extension
  built against numpy 2, Theano), there is no post-migration capture and nothing to compare. All six
  legacy projects stop here today ([below](#where-it-was-tried)).
- **Code no run exercises.** Only calls the tests or the app make are compared. A dependency symbol
  no run reaches is BLIND, and the verdict stays INCONCLUSIVE.
- **Python 2.7** is not supported: a capture reports that the tracer failed to start.

### Fixing what it finds

- **`apply` rewrites names, not behaviour.** It applies checked rewrite templates for moved, renamed
  and removed APIs, and fixes for Python-version breakage. A behaviour change (a new exception, a
  changed default, a different result) is found by `verify` and left to you. A rule that cannot be
  checked is listed for manual review instead of applied ([plan.md](plan.md#codemods)).

### Judging a difference

Each of these is a review item, so the verdict is INCONCLUSIVE until a person decides
([verify.md](verify.md#run-noise)):
- **A hand edit's new value.** When you changed a line and its call now returns something else,
  pymolt cannot tell whether you meant it. Only a new exception fails.
- **A value that varies between runs**, when no baseline replicate measured the noise. Capture the
  baseline with `--runs 2`. A replicate still cannot vouch for an outcome too rare for it to have
  shown (1 raise in 76 calls).
- **An input that changed with no visible cause** on an unedited line.
- **A dict whose key order changed**, unless the order is shown to be deterministic.
- **An environment difference** between the two captures (OS, resource limits, privileges) at a
  call that reads it. Capture both sides on the same platform.

One case goes the other way: a change in the *kind* of a rare outcome (a call that raised once in 76
now returns) still fails, although it may be noise.

### What the tracer does not see

- **Exception types on 3.6–3.11 test suites.** A test-suite capture records that a call raised, not
  what it raised. Long runs record the type once the targeted wrappers take over. 3.12+ always
  records it.
- **Compiled calls.** A C function, a private Cython function, or a method of a Cython extension type
  is recorded without its result, and its arguments are not compared. Past 2,000 calls per call
  site, such calls are only counted. The public Cython functions of dependency modules are wrapped
  and recorded in full.
- **Late dunder contacts in long runs (3.12+).** Watching for dunder, property and generator calls
  stops 30 s after the last new contact, so one first used after that is missed.
- **Values behind a proxy or a lazy object** (werkzeug's `LocalProxy`, Django's `SimpleLazyObject`)
  are recorded as the proxy's type: reading through it would evaluate it. A dict or list subclass
  whose own methods compute what they return (werkzeug's `MultiDict`) is recorded from what it
  stores (every value list). One that keeps its items elsewhere and defines its own `__len__`
  (werkzeug's `CombinedMultiDict`) is recorded like any object, from its attributes.
- **Classes made at run time (3.12+).** A dependency class created without an import, whose
  instances reach only a call site that stopped being watched before the class existed, is missed.

### Coverage after `apply`

When the post-migration code came from `pymolt apply`, BLIND, dynamic-only and trust count only the
API paths the plan found changed, so a symbol outside them that no run exercised does not block
PASS. A behaviour change in any recorded call still fails ([verify.md](verify.md#scope-after-apply)).

### PyMolt itself

- It runs on 3.12. On 3.13, three tests currently fail.
- The tracer is written to load on Python 3.4–3.5. That is not tested.

## Overhead

| Workload | Measured |
|---|---|
| Test suites, Python 3.6 in emulated containers (`setprofile`), six legacy projects | 2.7–11.8× slower ([scorecard](../bench/legacy_eval/results/after-waveG/scorecard.md)) |
| A Flask + SQLAlchemy + pydantic service on Python 3.12 (`sys.monitoring`), saturated | 16.8% lower throughput once steady; 74–76% of normal throughput in the first 30 s; 0 dropped calls |
| Same service at 200 requests/s | p99 latency ×1.01; output about 2 MB per day |
| Your code calling a Cython function in a hot loop, 3.12 | 13–16 µs per call, every call recorded |

The service numbers are from one load-gate run on 2026-11-15. The tracer has changed since, and
they have not been re-measured cleanly ([bench/load/RESULTS.md](../bench/load/RESULTS.md)).

In long runs, pymolt records the structure of arguments and results by default, not their values.
It records them for the first 32 calls at each call site and for 10% of the rest, and it counts
every call ([verify.md](verify.md#sampling-and-counting)). A test-suite capture whose trace writer
falls behind makes the program wait rather than drop events.

## Where it was tried

**Six legacy projects**, Python 3.6 → 3.12: hyper, vibora, theano, lasagne, alphalens, zipline.
- All six capture a baseline.
- Plans with rewrites were applied to five of them. lasagne's plan has none.
- None reaches a compared post-migration capture. The target fails to import on five. alphalens
  imports, but its post-migration test run was refused.

Details: [scorecard](../bench/legacy_eval/results/after-waveG/scorecard.md),
[issues](../bench/legacy_eval/KEY_ISSUES.md).

**locust** (gevent, 3.8 → 3.12, no code change) is the noisy real-project pilot:
- without noise handling, the diff reported 14,065 failing entries;
- with masks and a replicate, 2 findings remained: msgpack bytes reordered because Python 3.9
  changed a TypedDict's key order (a real change), and a rare gevent outcome (1 raise in 76 calls);
- the post side of that run lost 43% of its events. Since the event-loss and recall fixes, a
  re-captured post side loses none, and not-comparable entries fell from 3,309 to 2. The msgpack
  calls now compare by value and land in review, because each message carries a per-run node id.
  A full re-capture with every fix in place has not been run yet.
