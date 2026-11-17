# Legacy evaluation: locust, full re-capture with the event-loss and recall fixes

`bench/legacy_eval/run.py run locust`, pymolt `01e9233`, 2026-11-17, 27.1 min
([scorecard](results/pilot-locust-recapture/scorecard.md)).
- Baseline: `python:3.8` linux/arm64 container, setprofile, plus a replicate.
- Post-migration: `python:3.12` linux/arm64 container, sys.monitoring.
- Same pytest command on all three runs; no code change (plan: 0 rewrites, apply: no-op).

## Captures

| Run | Backend | Events recorded | Dropped | Backpressure wait | Duration |
|---|---|---|---|---|---|
| baseline | setprofile | 948,134 | 0 | 0.0 s | 307.4 s |
| baseline replicate | setprofile | 980,605 | 0 | 0.0 s | 308.0 s |
| post-migration | sys.monitoring | 1,665,729 | 0 | 1.4 s | 263.6 s |

- The uncaptured baseline took 240.9 s (overhead 1.3×).
- The first pilot run (2026-11-16) recorded 1,645,835 post-side events and dropped 1,053,468.

## Verdict

**FAIL.** Static 175, confirmed 54, BLIND 121, dynamic-only 51, trust 65%.

| Ledger | Entries |
|---|---|
| failing | 6 (4 findings) |
| review | 3 |
| noise, measured by the replicate | 409 |
| explained | 293 |
| not comparable | 2 |
| equal | 6,428 |

Explained, by mechanism:

| Mechanism | Entries |
|---|---|
| attribution | 114 |
| implicit | 67 |
| backend | 48 (2 of them are the not-comparable entries) |
| clock | 38 |
| binding | 12 |
| source | 10 |
| environment | 2 |
| volatile-site | 2 |
| clock+environment | 2 |

The replicate explains the msgpack differences caused by per-run node ids: 4,780 `msgpack.packb`
and 4,778 `msgpack._cmsgpack.unpackb` calls.

## Failing findings

| # | Finding | Entries | Root cause | Class |
|---|---|---|---|---|
| 1 | `msgpack.packb` at `locust/rpc/protocol.py:14`, value changed | 3 | Python 3.9 orders a TypedDict's base fields first, so the stats dict reaches msgpack in another key order and the bytes differ (same keys and values) | real behaviour change |
| 2 | `flask.sansio.scaffold.decorator` at `locust/test/testcases.py:67`, presence changed | 1 | stacked decorators: see [F2](#f2-stacked-decorators-are-not-paired) | diff bug |
| 3 | `json.dumps` at `locust/runners.py:1402`, value changed (`{"TestUser": 8}` → `7`) | 1 | timing in one test, confirmed as deterministic: see [F3](#f3-timing-confirmed-as-deterministic) | noise (verify over-claims) |
| 4 | `unittest.case.TestCase.assertEqual` at `locust/test/test_runners.py:972`, presence changed | 1 | the same test and mechanism as #3 | noise (verify over-claims) |

### F2: stacked decorators are not paired

```python
@app.route("/post", methods=["POST"])   # 65
@app.route("/put", methods=["PUT"])     # 66
def manipulate():                        # 67
```

- Python 3.8–3.10 attribute both decorator applications to the `def` line. 3.11+ attribute each
  one to its own `@` line.
- Both calls have the same symbol and input, so the 3.8 side holds one fact with count 2 at
  line 67. The 3.12 side holds one fact each at lines 65 and 66.
- `_pair_qualname_equivalent` (`pymolt/verify/facts.py`) pairs one-to-one only. So the two new
  facts become DRIFT ("decorator wrapper internals"), and the old fact is DISAPPEARED, which
  fails.
- Every single-decorator route in `locust/web.py` pairs ("attribution-moved").

Reproduction, from the repository root:

```python
import sys; sys.path.insert(0, "tests/verify")
from test_fact_noise import fact, fs, failing, ctx_for
from pymolt.verify.facts import diff_facts
SRC = ('import flask\napp = flask.Flask("x")\n\n\n@app.route("/post", methods=["POST"])\n'
       '@app.route("/put", methods=["PUT"])\ndef manipulate():\n    return "ok"\n')
def deco(sym, line, text, count=1):
    return fact(sym, {"f": {"__callable__": "app.manipulate"}},
                (("return", {"__callable__": "app.manipulate"}),),
                func="<module>", line=line, text=text, count=count)
old = fs(deco("flask.sansio.scaffold.decorator", 7, "def manipulate():", 2),
         backends=("setprofile",))
new = fs(deco("flask.sansio.scaffold.Scaffold.route.<locals>.decorator", 5, '@app.route("/post", methods=["POST"])'),
         deco("flask.sansio.scaffold.Scaffold.route.<locals>.decorator", 6, '@app.route("/put", methods=["PUT"])'))
print([(e.category.name, e.fails) for e in diff_facts(old, new, context=ctx_for(SRC)).entries])
# [('DISAPPEARED', True), ('DRIFT', False), ('DRIFT', False)]
```

### F3: timing confirmed as deterministic

Findings #3 and #4 both come from `test_distributed_rebalanced_integration_run`. That test runs
a master and six workers with real sleeps, `WORKER_REPORT_INTERVAL` 0.3 s, rebalancing, and
`retry` loops.
- #3: after each dispatch step the master logs `reported_user_classes_count` (line 807, through
  `_format_user_classes_count_for_log`). During the rebalance, workers' reports overlap. Both
  baseline runs reported 8 users eight times, and the post side reported 7 eight times.
- #4: after three workers are killed, `assertEqual(3, len(master.clients.missing))` is retried
  every second. The missing count it saw was 0 five times, then 1, 2, 3 in both baseline runs,
  and 0 five times, then 1, 3 on the post side. The failing fact is the attempt that saw 2.

The test run alone in each container, uncaptured, through a stdin script that records those
values (3 runs per side):

| Side | Plain | With a no-op `sys.setprofile` hook |
|---|---|---|
| 3.8 | at most 6 users reported; missing goes 0 → 3 | 7 users reported 8 times; missing goes 0 → 1 → 3 |
| 3.12 | at most 6 users reported; missing goes 0 → 3 | at most 6 users reported; missing goes 0 → 3 |

The transient count and the partial detection follow per-call overhead. The overhead differs
between the two sides (setprofile on 3.8, sys.monitoring on 3.12) but not between the two
baseline runs. So the two runs agreeing is no evidence of determinism.
- At the `json.dumps` site, the two baseline runs differ in how many calls logged 4, 5 or 6
  users. Every value occurs in both runs, though, so the replicate's diff pairs them as equal
  (`equal@complete`). Count shifts among recurring inputs are not counted as input variation.
- At the `assertEqual` site, the replicate shows `equal@hidden` (16 calls), and a presence
  difference is confirmed by any equal pair.

## Review items

| Item | Root cause | Class |
|---|---|---|
| `assertGreaterEqual(len(rows), 130)` at `locust/test/test_stats.py:461`: 148 → 150 | rows written by a timed run (`test_csv_stats_writer_full_history`) | noise |
| `assertLessEqual(master.user_count, 10)` at `locust/test/test_runners.py:1892`: 5 → 6 | users spawned at the moment of the check (`test_can_call_stop_endpoint_if_currently_swarming`) | noise |
| `typing_extensions.final` at `locust/user/users.py:137`: "not reached" | typing_extensions 4.8.0 on 3.12 re-exports `typing.final`, and stdlib `typing` is not traced on either side. The call still happens, in the stdlib. Coverage already notes that the symbol resolves to interpreter code; the fact diff does not use that. | tracer/diff gap |

- In the replicate, the two input-changed sites had one call per run, equal (148 and 148, 5 and
  5). The receiver (`self`) is opaque, so the replicate's evidence is `equal@hidden`. Confirming an
  input change needs `equal@complete`, so the items stay in review.
- Their action still reads "capture a baseline replicate", although one exists.

## Not comparable

`gevent._gevent_cqueue.Queue.get` at `locust/test/test_runners.py:69` and `:82`, in the test's
mocked RPC client.
- It is a method of a Cython extension type. sys.monitoring records it as a compiled call: 49 and
  123 calls, 8 and 4 of them raising `GreenletExit`. setprofile does not see it.
- A raise that only one backend can record is not comparable. This is the documented limit on
  compiled calls (`docs/limits.md`). Class: tracer.

## Earlier findings

| Earlier | Now |
|---|---|
| gevent `Group.join` rare outcome (FAIL in the first pilot run) | Not a finding this time. At `spawn_users` (`runners.py:248`) each side made 2 calls and 1 of them raised, so they compare equal at type level. The kind rule is unchanged: a run where only one side raises fails again. |
| flask decorator (FAIL after the cap fix) | Still FAILs, cause above (F2). |
| `collections.defaultdict.items` / `.values` at `runners.py:1402-1403` | Recorded on both sides (134/135 and 127/128 calls), equal. |
| `msgpack._cmsgpack.unpackb`: 3,292 not comparable, then review | Compared by value; noise measured by the replicate. |
| `urllib3.response.HTTPResponse.close` at `locust/test/util.py:82` (`gc.collect()`) | Recorded in all three runs (35, 36, 36 calls), equal. |

## Open issues, by how much they block PASS on an equivalent migration

1. **Stacked decorators of one factory on one function (F2).** A false FAIL on every 3.8–3.10 →
   3.11+ migration whose code stacks the same decorator factory (two `@app.route` on one view).
   It is deterministic.
2. **Timing confirmed as deterministic (F3).** A false FAIL wherever a test's values depend on
   timing that the tracer's overhead shifts. Two runs on one backend cannot measure that.
   - Count shifts among recurring inputs are not treated as input variation.
   - Presence is confirmed from `equal@hidden` pairs.
3. **A rare outcome's kind is judged strictly.** This was latent this run: the outcome happened on
   both sides. It is a false FAIL whenever a rare raise lands on one side only.
4. **BLIND: 121 of 175 static symbols.** These are outside this test subset, and they alone keep
   the verdict from PASS.
5. **A dependency function that is the stdlib's own on the new Python** (`typing_extensions.final`
   → `typing.final`). This leaves a review item saying "not reached", with an action that does not
   settle it.
6. **Timing-dependent assertion inputs with an opaque receiver.** They stay in review even with a
   replicate, and the action asks for a replicate that exists.
7. **A compiled method that raises, seen by sys.monitoring only.** Not comparable (a documented
   limit).

Minor: the `verify --agent` summary prints the first 3 failing findings (`views.py`,
`failing[:3]`), so #3 above is missing from it, and nothing says that more exist.
