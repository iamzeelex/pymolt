# Limits and status

## Where pymolt cannot help yet

### Getting to a comparison

- **Targets that do not build or import.** `assess` stops at resolution. When a dependency fails to
  build or import on the target (for example hyperframe < 4, `numpy.distutils`, a Cython extension
  built against numpy 2, Theano), there is no post-migration capture and nothing to compare. All six
  legacy projects stop here today ([below](#where-it-was-tried)).
- **Code no run exercises.** Only calls the tests or the app make are compared. A dependency symbol
  no run reaches is BLIND, and the verdict stays INCONCLUSIVE. This holds after `apply` too, whether
  or not the plan changed that symbol ([verify.md](verify.md#scope-after-apply)).
- **Python 2.7** is not supported: a capture reports that the tracer failed to start.

### Fixing what it finds

- **`apply` rewrites names, not behaviour.** It applies checked rewrite templates for moved, renamed
  and removed APIs, and fixes for Python-version breakage. A behaviour change (a new exception, a
  changed default, a different result) is found by `verify` and left to you. A rule that cannot be
  checked is listed for manual review instead of applied ([plan.md](plan.md#codemods)).

- **A step rolled back alone keeps what came after it, within limits.** `rollback --step` undoes
  only that step's change in a file that moved on, but refuses when a later edit touched the
  step's lines or the statements right around them (a hand edit there could depend on it), and an
  import the step wrote stays while anything uses it. Applying the step again re-bases it, and an
  import its recipe inserts may then land in another place among the imports than in a straight
  apply ([commands.md](commands.md#pymolt-rollback-dir)).

### Judging a difference

Each of these is a review item, so the verdict is INCONCLUSIVE until a person decides
([verify.md](verify.md#run-noise)):
- **A hand edit's new value.** When you changed a line and its call now returns something else,
  pymolt cannot tell whether you meant it. Only a new exception fails.
- **A value that varies between runs**, when no baseline replicate measured the noise. Capture the
  baseline with `--runs 2`. A replicate still cannot vouch for an outcome too rare for it to have
  shown (1 raise in 76 calls).
- **An input that changed with no visible cause** on an unedited line, including an input one side
  passed and the other did not, at a call both sides make.
- **A dict whose key order changed**, unless the order is shown to be deterministic.
- **An environment difference** between the two captures (OS, resource limits, privileges) at a
  call that reads it. Capture both sides on the same platform.

Two cases go the other way. Both still fail, although they may be noise:
- a change in the *kind* of a rare outcome (a call that raised once in 76 now returns);
- a value that depends on timing (a poll, a count of reports), when the replicate records the
  call's input in full and both baseline runs agreed on it. The replicate runs at the baseline's
  timing, and a different Python or tracer shifts it. The report then calls the difference
  reproducible in the replicate, not deterministic.

- **Attestations stop accidents, not an adversarial agent.** An agent with a pseudo-terminal can
  type the nonce, and one that can read your home directory can read the attestation key
  ([agents.md](agents.md#what-a-person-attests)). Attestations are per machine: on another
  machine an attested change is judged as unattested.

### What the tracer does not see

- **Some exception types on 3.6–3.11 test suites.** `setprofile` does not pass the exception to the
  tracer, so pymolt reads it from a one-off `sys.settrace` event when the exception reaches the
  frame that called the dependency. A raise that C code swallows before any Python frame sees it
  (`hasattr`, `getattr` or `next` with a default, `list()` over an iterator) is recorded without
  its type. While another tool owns `sys.settrace` (coverage on 3.6–3.11, a debugger), pymolt
  leaves it alone and reads the type only when the code that handles the exception makes a call,
  so a raise handled without any call (`except ValueError: pass`) is recorded without its type.
  On 3.6 and 3.7, a `return None` inside a `with` or `try`/`finally` block stays "return or raise".
  On Flask's test suite every raise was typed: 203 on 3.8 and 3.11, 214 on 3.6 (Flask 2.0.3).
  Under `coverage run` on 3.11, 106 of the 203 were. Long runs record the type once the targeted
  wrappers take over. 3.12+ always records it.
- **Compiled calls.** A C function, a private Cython function, or a method of a Cython extension type
  is recorded without its result, and its arguments are not compared. Past 2,000 calls per call
  site, such calls are only counted. The public Cython functions of dependency modules are wrapped
  and recorded in full.
- **Late dunder contacts in long runs (3.12+).** Watching for dunder, property and generator calls
  turns off 30 s after the last new contact. A line of your code that runs for the first time turns
  it back on until nothing new happened for 5 s (`PYMOLT_TRACE_INCREMENTAL_REOPEN`; 0 keeps it off).
  Still missed:
  - such a call on a line that already ran with other objects (`x[k]` on dicts for an hour, then
    on a DataFrame);
  - one made more than 5 s after the last new line or contact (`dep.slow()[k]` when `slow()` takes
    longer);
  - code of yours imported after the 30 s through a line that already ran
    (`importlib.import_module(name)`) and called only by a dependency;
  - on 3.12, a function of yours over about 8,000 bytecode instructions: watching its lines would
    make its first run slow there.
- **Values behind a proxy or a lazy object** (werkzeug's `LocalProxy`, Django's `SimpleLazyObject`)
  are recorded as the proxy's type: reading through it would evaluate it. A dict or list subclass
  whose own methods compute what they return (werkzeug's `MultiDict`) is recorded from what it
  stores (every value list). One that keeps its items elsewhere and defines its own `__len__`
  (werkzeug's `CombinedMultiDict`) is recorded like any object, from its attributes.
- **Classes made at run time (3.12+).** A dependency class created without an import, whose
  instances reach only a call site that stopped being watched before the class existed, is missed.

### PyMolt itself

- It runs on 3.12. On 3.13, 14 of its tests fail, so CI tests on 3.12 only. Two groups of them
  touch results:
  - pymolt on 3.13 anchors 3.6-era code (such as `async` used as a name) by tokens instead of
    its syntax tree, which survives less reformatting;
  - on a 3.13 target under `sys.monitoring`, a raise from C code is recorded without its type,
    and a call that fails to bind is not recorded.

  Neither has been looked into yet.
- The tracer is written to load on Python 3.4–3.5. That is not tested.

## Overhead

| Workload | Measured |
|---|---|
| Test suites, Python 3.6 in emulated containers (`setprofile`), six legacy projects | 2.7–11.8× slower ([scorecard](../bench/legacy_eval/results/after-waveG/scorecard.md)) |
| A Flask + SQLAlchemy + pydantic service on Python 3.12 (`sys.monitoring`), saturated | 16.8% lower throughput once steady; 74–76% of normal throughput in the first 30 s; 0 dropped calls |
| Same service at 200 requests/s | p99 latency ×1.01; output about 2 MB per day |
| Your code calling a Cython function in a hot loop, 3.12 | 13–16 µs per call, every call recorded |

The service numbers are from one load-gate run on 2026-11-15, with 16.8% saturated loss. Later
runs measured 18.9% (wave G) and about 20% on a busy host (2026-11-17), against a gate of 20%. A
clean re-measure on a quiet host is still owed ([bench/load/RESULTS.md](../bench/load/RESULTS.md)).
The cost of capturing a loaded service on Python 3.6–3.11 (`setprofile`, then targeted wrappers)
has not been measured.

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
- the first run with masks and a replicate left 2 findings, but its post side lost 43% of its
  events.
- A full re-capture with every event-loss and recall fix in place (2026-11-17) lost no events on
  any of its three runs. The post side waited 1.4 s in total for its trace writer.
- That re-capture's verdict is FAIL. Verified again with the two fixes below, the same captures
  fail on one finding: msgpack bytes reordered because Python 3.9 changed a TypedDict's key
  order. This is a real change.
- Two `verify` bugs gave three more FAILs, now fixed:
  - Two `@app.route` decorators on one function. 3.8 attributes both calls to the `def` line and
    3.12 each to its own `@` line, and `verify` did not pair them.
  - Two values from one timing-sensitive test, which shift with the tracer's overhead. Both
    baseline runs agreed on them, so `verify` called them deterministic. One is now measured
    noise. The other, a polled value one side never saw, is a review item.
- 4 review items and 2 not-comparable entries remain:
  - three timing-dependent test values;
  - `typing_extensions.final`, which is the stdlib's `typing.final` on 3.12, so it is not traced;
  - a compiled gevent method whose raise only `sys.monitoring` sees.
- The replicate explains the per-run msgpack differences (node ids) and 409 differences in
  all. The rare gevent outcome raised on both sides this time.
- 121 of 175 static symbols are BLIND under this test subset.

Details: [scorecard](../bench/legacy_eval/results/pilot-locust-recapture/scorecard.md),
[findings](../bench/legacy_eval/locust.md).
