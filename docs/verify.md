# Captures and the verdict

`pymolt capture` records what your code does at the boundary with its dependencies. `pymolt verify`
compares two captures and decides `PASS`, `FAIL` or `INCONCLUSIVE`. This page covers how both
work and what each number means.

## Captures

A capture runs your command with a tracer injected through `sitecustomize` and the `PYMOLT_TRACE_*`
environment. Nothing is added to your project or its requirements.

### Modes

| `--mode` | Runs | Defaults |
|---|---|---|
| `tests` | your test suite, which exits on its own | records argument values for every call; full discovery |
| `command` | your app; Ctrl+C ends it and keeps what was recorded | records structure, not values (`shape`); samples which calls keep it; watches incrementally |
| `attach` | nothing: pymolt prints the command for you to run, e.g. in a container or on a server; finish with `--mode attach --collect` | as `command` |

The baseline is captured before any code change. The post-migration capture runs its command in the
target environment when setup named one (`setup --target-env`). With `setup --container`, the
baseline is captured inside that running container.

If the command exits non-zero, the capture is refused and the run is kept as a diagnostic artifact.
With `--accept-failing`, a run that recorded calls is kept anyway and stamped red. A run that
recorded no calls is always refused.

### Baseline replicate

`capture baseline --replicate` runs the baseline's command again, on the same code in the same
environment. `--runs 2` records the baseline and its replicate in one go. What differs between the
two runs is run-to-run noise, measured instead of guessed (see [Run noise](#run-noise)). A replicate
with another command, environment or sources is ignored. Without one, the report says the noise
was not measured, and `status` suggests `--runs 2`.

### What is recorded

For every call from your code into an installed dependency:
- the **callee**, as its canonical `module.Class.method`, whatever name your code used;
- the **call site**: file, line and function in your code, plus the line's text, so a rewritten
  site can be told apart from a call that vanished;
- the **arguments your code passed**: a parameter left at its default is recorded by name only, so
  a library that adds parameters does not change the fact;
- the **result** or the **raised exception**. The exception type is recorded on 3.12+, and on
  3.6–3.11 by the targeted wrappers of a long run. Under `setprofile` (a test suite on 3.6–3.11)
  the type is read from a one-off `sys.settrace` event when the exception reaches the frame that
  called the dependency. It is recorded without its type when C code swallows the exception
  (`hasattr`, `getattr` or `next` with a default). While another tool owns `sys.settrace`
  (coverage, a debugger), pymolt leaves it alone and reads the type only when the code that
  handles the exception makes a call. On 3.6 and 3.7, when a call ends inside a `with` or
  `try`/`finally` block with no return value, a `return None` cannot be told from a raise: it is
  recorded as "return or raise" unless an exception is seen;
- the duration and the thread.

A function written in C or Cython has no Python frame. pymolt wraps the public Cython functions of
dependency modules, on every Python, so their arguments and results are recorded in full. Any other
compiled call (a C function, a private Cython name, a method of a Cython extension type, a function
bound before the wrap) is recorded without its result, and its arguments are not compared. For those,
the first 2,000 calls per call site are recorded and the rest are counted. Past 1,000,000 calls a
site stops counting, and its count is marked as a lower bound.

When setup's base and target Python differ, captures also record calls into every standard-library
module your project imports, less a short noise list (`--stdlib` changes the list). The test runner
is never recorded.

Two more things are recorded for the comparison:
- the insertion order of every dict of 2 to 64 keys, next to its sorted keys;
- per traced process, before tracing starts, the environment it ran in: OS, kernel, machine,
  container, privileges, CPUs, resource limits, tmpdir, timezone, encodings and the hash seed
  setting. The hostname is not recorded.

Values are rendered at the most exact level that can be compared:
- scalars and containers as they are;
- datetimes, decimals, UUIDs, enums, classes and dataclasses canonically;
- numpy and pandas objects as type, shape and dtypes, with the values only when small;
- anything else as its type alone.

Recording a value never runs its code, so it cannot change what the program does. A dict, list or set
subclass is recorded from what it stores, not through its own methods (Flask's session stays
"not accessed"). A proxy or lazy object (werkzeug's `LocalProxy`, Django's `SimpleLazyObject`) is
recorded as its own type, without evaluating it.

`--privacy shape` keeps types and structure but drops scalar values.

### Tracers

| Python | Tracer |
|---|---|
| 3.12+ | `sys.monitoring`. Tests: full discovery, plus the lines of your code that ran, each one callback at its first run ([coverage](#coverage)). Long runs: incremental. A `CALL` event at your call sites arms only the functions your code actually calls. A second watcher covers properties, `x[k]`, `with`, operators and generators. It turns off 30 s after the last new contact, and back on whenever a line of your code runs for the first time, until nothing new happened for 5 s. While it is on, capture costs as much as before it first turned off. Each line of your code is watched once, then costs nothing. The capture metadata counts the times it came back on (`reopened`) and their total length (`reopen_seconds`). There is no warm-up window. |
| 3.6–3.11 | `sys.setprofile`. Tests: for the whole run, plus the functions of your code that started, with a one-off `sys.settrace` event per raise for its type (none while coverage or a debugger owns `sys.settrace`). Long runs: then targeted wrappers on the discovered functions (switch after 5 s with no new contact, at most 60 s) |
| 2.7 | not supported; the capture reports that the tracer failed to start |

### Sampling and counting

Every call is counted. In long runs, the arguments and results (their structure, under the default
`shape` privacy) are kept for the first 32 calls at each call site and for 10% of the rest
(`--sample-rate` changes the fraction). Because the counts are complete, a call that stopped
happening is still detected exactly.

Long runs aggregate in the traced process. Memory is bounded and there is no event queue to
overflow. On the load bench, output grew by about 2 MB a day at 200 requests/s
([results](../bench/load/RESULTS.md)). Capture metadata records what was dropped,
if anything, and why.

A test-suite capture writes every event to a trace file. When the writer falls behind, the traced
program waits for it instead of dropping events (`PYMOLT_TRACE_BACKPRESSURE=drop` restores
dropping). It stops waiting only when waiting cannot help, for example when the writer made no
progress for 10 s (`PYMOLT_TRACE_STALL_SECONDS`), and those losses are counted. The capture reports
the time spent waiting and warns from 1 s.

### Provenance

Each capture is stamped with its environment fingerprint, mode, privacy profile, sampling policy and
validity.
- **Replacing:** re-capturing archives the previous recording instead of deleting it.
- **Rollback:** a rollback that restored files sets the post-migration capture aside, in the
  archive, and `verify` names it. A rollback that restored no file keeps it.
- **Diagnostics:** failed, empty or malformed runs are kept under `contract_traces/diagnostics/`.
  They never replace active evidence.
- **Checkout path:** call sites are compared relative to the project root each capture was taken
  in, which its source snapshot records. Captures taken in different checkouts of the project (a
  copy, a second worktree, a CI workspace) compare as one project. A capture without a source
  snapshot records no root: a call it recorded in another checkout cannot be placed in this one
  and is UNCOMPARABLE.
- **Freshness:** the post-migration capture must match the environment on disk now, and every file
  it recorded must still have the recorded text (whatever changed it: a later step, a revert, a
  hand edit); a capture without a source snapshot cannot be checked and counts as not fresh. The
  verdict names the changed files, and `status` shows a saved verdict on such a capture as stale.
  The baseline
  must match the environment the migration started from. `pymolt apply` records that starting
  point. A baseline still counts as the starting point when only the *contents* of the same manifest
  changed between it and the apply, or, for a migration done by hand, since it (bumped pins).

## Facts and the diff

`verify` folds each capture into **facts**: (callee, call site, arguments) → the outcomes seen. It
then compares the two sides.

| Category | Meaning | Fails? |
|---|---|---|
| CHANGED | same call, different outcome | yes |
| DISAPPEARED / APPEARED | the call happens on one side only, at a call site whose code did not change | yes, unless the name no longer refers to a dependency there or the function never ran (then inconclusive), or the other side makes the same call there with other inputs (an [input change](#run-noise)) |
| REWRITTEN | the call site's line was edited; the call is compared with the one that replaced it | see [Edited lines](#edited-lines) |
| DRIFT | same outcome, but the library added or removed a parameter the call leaves at its default; or, on a hand-edited line, the same values in another container (list vs view) | no, reported |
| UNCOMPARABLE | the two sides could not record the call the same way: one dropped events, capped a compiled-call site, or recorded the call in another shape; or one, taken in another checkout, records no project root; or the call is a coroutine, whose awaited result is not recorded | makes the verdict inconclusive |

### Dependency objects and values

A returned value is compared by what your code can observe of it:
- **Values** compare by content: builtins and containers (each element on its own), arrays and
  frames, dates, enums, and records your project declares (a dataclass, attrs, pydantic or
  namedtuple class in your code).
- **Dependency objects** (*handles*: any other object a dependency returns, including a record a
  dependency declares) compare by their type, and by the attributes your code reads by name
  (`x.name`, `getattr(x, "name")`), as each capture's source snapshot shows them. A private
  attribute is compared only when your code reads it by name (`p._cursor`). An attribute your code only indexes with literal keys
  (`app.config["SEED"]`) compares under those keys. Unread state that differs is counted, not
  compared: the entry is DRIFT with "handle state not observed". The calls your code makes on the
  object are facts of their own. An attribute that exists on one side only is a change when the
  code of the side that lacks it reads it; when only the other side's code reads it (an edit
  moved the read to another name), or no code does, it is counted with the unread state.
- A capture without a source snapshot compares every object by its full state, as
  `verify --strict` does, and so does a diff with any project file that does not parse here
  (its reads are unknown). The report says which (`quality.objects`). The post-migration
  snapshot keeps only the files with calls in them; the read set takes the other files from the
  checkout, else from the baseline snapshot.
- Places that read attributes by a computed name (`getattr(x, name)`, `vars(x)`, `x.__dict__`,
  `asdict(x)`, `dir(x)`) cannot be projected. When an object's unread state differed, the verdict
  lists them; they do not block PASS, like the [unobservable uses](#the-verdict).
- A dependency record in a call's input, or as its receiver, is keyed by its type
  (`record-input`, counted). The calls on one record still pair with each other: a result that
  moved to another record fails. When the records themselves differ between the captures, a call
  with several results cannot be paired call by call and is a review item.

What a statement does with a call's result decides what of it is compared (from the source
snapshot, the same on both sides):

| The statement | Compared |
|---|---|
| discards it (`resp.raise_for_status()` on its own line) | outcome kind and raise type; an object other than one the call was handed (a lazy result, an `AsyncResult`, a `Deferred`: discarding one may mean its work did not run) by its type; a value, or the receiver or an argument handed back, not at all |
| unpacks or iterates it (`x, y = np.meshgrid(...)`, `for r in f()`) | the elements and their number, not the container type |
| passes it alone to `int`, `float`, `bool` or `len` (not rebound in the file) | the converted value: `int(bar.get_height())` makes 4.0 and 4 equal |
| tests its truth (`if f():`, `not f()`) | truthiness |
| anything else | the full result |

A pair equal only once projected is DRIFT (`projected`), with both types shown: the library did
change what it returns, and the plan's scoring still sees that. `--strict` compares the full
result.

Two floats are equal when they differ by at most 1024 units in the last place (ulps), at their
own precision: a float64 by float64 ulps, an array's float32 values by float32 ulps. Each value
scales by the larger of its own pair, an array element too (never by the array's largest), and
no pair further apart than 2^-10 relatively is ever within rounding (float16's 1024 ulps would
be all of it). Zero against non-zero, int against float, and values of different dtypes are
never within rounding. Such a pair is DRIFT at level `numeric`, with the largest distance in ulps
and relatively; the same holds for a call's input (`matched-modulo-numeric`: the same call given
the same input). The verdict says how many values needed it ("k values equal within
floating-point rounding (≤1024 ulps)"), and `verification.json` keeps `numeric_drift`.
`verify --float-ulps N` changes the bound (0 compares floats exactly); `--strict` makes each such
pair a review item. Masks apply first, and measured run noise as before; the bound never widens
with it.

A type that changed is judged by a ladder:
- **Values and raises**: strict. A class that moved within its package (same top-level package,
  same class name: `numpy.ma.core.MaskedArray` is `numpy.ma.MaskedArray`) is the only allowance.
  A type-only rendering of a data package's object (numpy, pandas, polars, pyarrow, torch, jax,
  datetime, pathlib, decimal, ...) is a value too.
- **Dependency objects**: a relocation is equal; otherwise the class relation is unknown (pymolt
  records no MRO yet). A change to a class of another top-level package (werkzeug's `Response`
  to `flask.wrappers.Response`) under the same conditions is a review item, "class changed
  across packages" (needs a person; `--strict` fails it). A change to another class of the same
  top-level package is drift
  (`type drift`) when your code never introspects either class: no `isinstance`, `issubclass`,
  `match`, `type(x) is C` or singledispatch names it or any class of its top-level package (an
  import alias resolved: with no MRO, a base class may be the one checked), no `except` names
  it, and nothing reads a type's name
  (`type(x).__name__`). Your code then
  sees the object through the calls it makes on it and the attributes it reads, both compared.
  Introspected, or when your code reads or probes (`hasattr`) off the call's result a name
  neither rendering holds (a class attribute, a property): it fails.
- **A decorator's wrapper** of the same function of yours, named differently by the new version
  (`WrappedAPIView` vs `View.as_view.<locals>.view`: a closure, or a name your code defines
  nowhere; never another function of yours), is drift unless your code reads a
  callable's `__name__` or `__qualname__`.
- **A type an edited line produced** (`go.Heatmapgl(...)` edited to `go.Heatmap(...)`) that
  reappears at an unedited call along the edit's data chain (`fig.data`, on the figure the
  edited line built) is judged with that edit: a review when the edit is one, a failure when it
  fails. An edit whose result is discarded carries nothing, and nothing is carried onto a class
  your code introspects.
- On an edited line, a new class is the edit's, judged by its claim ([Edited lines](#edited-lines)).

The outcomes at one fact (a call in a loop, over hidden receivers) are matched call by call,
with their counts, whenever every call was counted. A call that changed among N, a call more or
fewer (`call-count-changed`, one outcome or several), or counts that moved between outcome kinds
(`outcome-kinds-moved`) is a change. A value that moved between calls at a fact whose input is
not fully observed, and a call count that moved in a workload whose values vary between runs,
go through the [unmeasured-noise](#run-noise) review; a baseline replicate that shows the count
varying makes it noise.

Every relaxation counts what it absorbed: the fact diff's `absorbed` gives the entries each one
made equal where the full comparison would not have.

### Edited lines

A line diff of the source each capture stored decides who edited a call's line:
- **unedited**: compared strictly, as above;
- **edited by `pymolt apply`** (its ledger records each edit): strict. The plan claimed a drop-in
  replacement, so any different outcome fails. The ledger names the statements each step
  changed, not their lines. `verify` finds each statement in the two captures' source and
  credits it only with lines the same line diff marks as edited. A statement that later edits
  moved is still found; one that a later edit replaced is not, and that line counts as edited
  by hand. Where two steps' statements share a line, the stricter claim decides. Only steps
  whose last apply is still in effect count: after a rollback that restored files, or once a
  later apply of the same step replaced them, the earlier edits are judged by hand. When the
  post-migration capture predates a step (its source still has the statements from before
  the step), the report says so: capture post-migration again;
- **a call through a name an edit rebound** (an unedited `safe_join(...)` after the import
  changed from `flask` to `werkzeug.utils`): the edit chose the callee, so the call is judged as
  that edit's site;
- **edited by hand**: the old and new calls are paired only by the same symbol, by an old → new
  symbol from the plan or the ledger, or by a one-statement edit. An equal outcome is REWRITTEN
  equal. A different value is a review item, which makes the verdict inconclusive. A new raise
  fails. A call with no partner is counted as an unverified edited site; it does not block PASS.

A ledger entry is judged by its claim, whoever made the edit: pymolt, or an agent or a person
who recorded it (`apply --step ID --record`). Its old → new symbol pairs the calls. A
successor rewrite also records the keywords it renamed and the defaults it wrote out
([plan.md](plan.md#codemods)): before the inputs are compared, the new call's keywords take
their old names back, and a written-out default equal to the old one counts as left at its
default, so `shift(n=1)` compares with the baseline's `shift(periods=1)`. Only the rewritten
callable's own calls are taken back (the entry's old or new symbol, or that callable moved within
its package); another call on the statement keeps its keys, and its input change shows. A recorded
edit carries its step's claim: `preserve`, or `intended` expecting `representation` for a step
that moves to another object ([plan.md](plan.md#the-plan-as-steps)). `--claim` and `--expect`
can only tighten it; loosening it needs a person's attestation.

| Outcome on an edited line | hand edit | `preserve` | `intended` | `intended`, expectation attested by a person |
|---|---|---|---|---|
| equal | equal | equal | equal | equal |
| representation only | DRIFT | FAIL | DRIFT | ACCEPTED if it expects `representation`; else FAIL |
| different value | review | FAIL | review; FAIL if it expects `representation` | ACCEPTED if it expects `return:<Type>` and gets one; else FAIL |
| type changed, values not visible | review | FAIL | review | ACCEPTED if it expects `return:<Type>` and gets one; not comparable if it expects `representation`; else FAIL |
| new raise | FAIL | FAIL | FAIL | ACCEPTED if it expects `raise:<Type>` and gets one; else FAIL |
| no partner | unverified | unverified | unverified | unverified |

Values are not visible when the call was compared by type only, or when a value on either side
was recorded as opaque, truncated or cyclic. Then an `intended` claim expecting `representation`
cannot be checked, so the change is a review item, as for a hand edit. It names what settles it:
a capture with `--privacy values` where the values can be recorded, a baseline replicate, or a
person's `pymolt verify . --accept ID`. When a person attested that expectation, the change is
not comparable instead: nothing visible contradicts the claim or bears it out, so it neither
fails nor is accepted. It needs evidence: capture both sides with `--privacy values`.

For every outcome, `preserve` is at least as strict as `intended`, and `intended` at least as
strict as a hand edit, so recording an edit never judges it more leniently than leaving it
unrecorded. Only a person's attestation does (an `intended` claim whose expectation a person
attested, or `verify --accept`), and a new raise fails unless one expects it. An ACCEPTED entry
counts as compared equal, and the report prints each one with who accepted it and when.

### Accepting a change

Each failing or review finding has an id, printed after it. `pymolt verify . --accept ID` lets a
person accept that change as intended: pymolt prints the finding and a nonce, and the person types
the nonce back (see [agents.md](agents.md#what-a-person-attests)). The accept is bound to the
finding, the statement it is on, a fingerprint of its new outcomes and both captures. On a line an
`intended` step covers, it is that step's; on an unedited line or a hand edit it makes an ad-hoc
step `x-<id>` that exists in the ledger only. A line a `preserve` claim covers cannot be accepted.

Later verdicts honour the accept while it holds. It expires when the outcome changes, the
statement changes, or either capture is replaced, and the finding stands again with the reason.
`--strict` reviews every accepted change instead.

The report prints the edited sites as equal, representation-only, need review and unverified,
then failed and accepted when there are any.

### Run noise

Two runs of the same code differ in ports, ids, timestamps and hash order. `verify` compares in
layers:
- the outcome kind (return or raise) and the raise type: always strictly;
- the structure (types, keys, string format): always, after masking;
- the values: only where nothing shows them to be volatile.

A value is masked as volatile when an entropy source returned it on that side (uuid4, random,
secrets, datetime.now, tempfile names), when it is a reading of the capture's own clock, the
project root, the interpreter's prefix (a venv's `site-packages` path) or the Python version, or
when an input carried such a value into an output.
- **Fresh inputs.** A call site whose inputs never recur (a port, an id) is judged as one group.
  Its calls pair by their masked input and compare by value.
- **Input changes.** On an unedited line, an input that changed with no visible cause is a review
  item: what reached the call changed, which is not proof that the dependency did. A new raise or
  a new type still fails. Three causes are visible:
  - a string literal of an edited line (`def chart(style="seaborn-v0_8-whitegrid")` edited from
    `"seaborn-whitegrid"`): the call is judged as that hand edit;
  - a parameter bound on one side only, to None (numpy 2's `linspace(device=None)`): an added
    default, drift (`added-default-none`, counted; a review under `--strict`);
  - floats within rounding: the same input (`matched-modulo-numeric`).
  - This includes an input seen on one side only, at a call both sides make with the same outcome
    kinds and types and the same signature, such as a poll that saw one more intermediate value.
- **The replicate.** A difference is noise only where the replicate shows the same kind of
  difference at that call site. A site the replicate shows stable is judged strictly. An outcome
  too rare for the replicate to have shown is never called deterministic.
  - Calls whose counts moved between inputs at one site (one value more often in one run,
    another in the other) count as input variation, but only for the input fields that moved.
  - The replicate runs at the baseline's timing. When the two sides ran a different Python or
    tracer, their per-call overhead differs. An input or presence difference the replicate
    repeats exactly then still fails, but is called reproducible in the replicate, not
    deterministic.
  - The replicate measures the baseline capture only. Against another recording
    (`verify --baseline`), record that side a second time and verify the two runs against each
    other: what differs there is run-to-run noise.
- **Dict key order.** A different order is a review item. It fails only with proof that the order
  is deterministic: a replicate with random hash seeds that agreed on 5 or more keys, a pinned hash
  seed, or a known cause, such as the field order Python 3.9 gives a TypedDict.
- **Environment.** A difference at a call that reads an aspect of the environment that differs
  between the captures (a curated list of readers) is never noise and never a pass: it needs
  evidence, a post-migration capture in the baseline's environment.

The report leads with findings, not with every compared fact:
- a ledger: failing, review, expected by construction, explained, not comparable;
- the failing findings, grouped by symbol and call site, each with a sample;
- the review items, each with the action that settles it;
- the explained differences, by mechanism, with a count and an example.

Each comparison states its level:
- `exact`: the values were compared;
- `numeric`: the values were compared, floats within rounding;
- `type`: one side was opaque, or `--profile shape` was used;
- `kind`: from counts only, return versus raise type, when values were not sampled.

Two captures from different Python versions are compared fairly:
- an untyped raise (3.6–3.11: swallowed by C code, or handled without a call while another tool
  owns `sys.settrace`) matches any exception type;
- C-level raise facts are left out when only one side's tracer records them;
- a compiled call recorded only by the 3.12+ tracer, which sees every compiled call, is no
  evidence of a change when the other side ran the 3.6–3.11 tracer, which sees builtin C
  functions but not Cython functions or type calls: returning, it is explained; raising, it
  cannot be compared. A callable the older tracer recorded as a compiled call elsewhere in its
  capture is one it sees, so a new raise of it fails. The captures do not record what kind of
  compiled callable was called, so a builtin that the older side never recorded stays not
  comparable;
- a decorator's call is attributed to the `def` line before 3.11 and to its `@` line after. It
  is compared within its decorated statement, including several decorators from one factory
  stacked on one function, and a closure whose module moved within its package
  (`flask.scaffold.decorator` vs `flask.sansio.scaffold.Scaffold.route.<locals>.decorator`);
- a comprehension's call runs in a `<listcomp>` frame before 3.12 and in its function after
  (PEP 709): the function is read from the source snapshot when no other call on that line
  names it;
- a callee that moved to another module and binds its argument under another name
  (`abort(status=403)` vs `abort(code=403)`) pairs at its call site by the argument values;
- a call compiled code made while another call on your line ran (a C function or type calling
  back into Python: `RandomState(0)` calling `concatenate`, markupsafe's C `escape` building a
  `Markup`), and numpy's dispatchers (`_round_dispatcher`), are not calls your code made: they
  are not compared, only counted (`via`). A call your line names, directly or through an
  import or assignment alias (`mse(y, p)` for `mean_squared_error`), is always compared, and so
  is every call on a line that calls through a variable or parameter. `--strict` drops nothing;
- an inherited constructor (`Response.__init__` for `PlainTextResponse(...)`) also confirms the
  subclass's static contact.

## Coverage

`scan` lists every place your code touches a dependency: a **contact** (file, line, symbol,
kind). The dependency symbols those contacts name (`module.Class.method`) are the **static
symbols**, one per symbol however many contacts name it.

- **Confirmed**: a static symbol a capture exercised.
- **BLIND**: a static symbol no capture exercised. A green suite with many BLIND symbols proves
  little.
- **Dynamic-only**: a symbol the captures saw but the static map did not list, typically a method on
  an object a dependency returned.

Each contact gets one status from the recording `verify` judges (the post-migration capture, or
the only one):

| Status | Meaning |
|---|---|
| recorded | The trace has a call of the contact's symbol on its statement. A multi-line statement counts by its lines; a decorator, a default value or a base class by its `def` or `class` header |
| ran, not recorded | The statement ran and the trace has nothing there: a tracer gap |
| not run | The statement never ran: a test or run gap |
| unobservable | No trace can record it (see below) |

An operator names two methods: `a > b` names the left operand's `__gt__` and the right
operand's `__lt__`, which Python calls only when the first returns NotImplemented. At one
statement the two count as one contact, recorded when either was.

- **Coverage** = (recorded + ran, not recorded) / the contacts that are not unobservable: how much
  of the dependency surface the run reached, the analogue of pytest's line coverage.
- **Recall** = recorded / (recorded + ran, not recorded): how much of what ran the tracer caught.
  It should be about 100%. Less is a tracer limit or a bug; the contacts it missed are in the JSON
  report (`contract.contacts`, status `ran-not-recorded`).

Whether a statement ran comes from the capture. The numbers say how (**granularity**):

| Granularity | Source |
|---|---|
| line | Python 3.12+: each line of your code that ran. It costs one callback per line, at its first run |
| function | Python 3.6–3.11: the functions of your code that started. A contact in a function that started counts as ran even on a branch not taken, so recall is a lower bound. Also used on 3.12 for code objects too large for line events (about 8,000 instructions) |
| file | A capture without this record (an older capture, a watch-mode capture, `PYMOLT_TRACE_EXECUTED=0`): a contact counts as ran when the trace recorded a call site in its file |
| symbol | A trace without call sites: a contact counts as recorded when its symbol was observed anywhere |

The record is in each process's capture metadata (`executed`). Below 3.12, a long-run capture
records functions only until it switches from discovery to the targeted wrappers, and the report
notes it. When contacts were judged at different granularities, the headline gives the coarsest
and the count for each. When a contact counts as ran only because its function or file ran, the
report, `--agent` (`coverage_bounded`) and `status` give coverage as "at most" (an upper bound)
and recall as "at least" (a lower bound): such a miss may be a branch not taken, not a tracer gap.

**Unobservable** contacts are left out of both numbers and counted on their own line:
- attribute reads, base classes and constants (`Loader=yaml.SafeLoader`, `class V(dep.Base)`,
  `dep.DEFAULT`) of a symbol no capture observed: reading a name runs no dependency code there.
  Of a symbol observed elsewhere, such a contact counts as recorded when its line ran, and as not
  run when it did not;
- chains through a function's return value (`typer.prompt().strip`), which have no static name;
- dependencies that are not installed where the capture ran;
- protocol use that resolves to interpreter code (`A | B` on two classes runs `type.__or__`).

The test runner's symbols (`pytest`, `pluggy` and similar, e.g. `@pytest.fixture` in a test) are
not your project's dependency surface: captures do not trace the runner, so they are left out of
every count, table and verdict reason, and a note counts them. A target that names the runner
lifts this, and they count like any dependency.

BLIND symbols are split the same way: not run, ran but not recorded, unobservable. The report also
shows **symbol matching**: the share of static symbols the exact string join confirms, then the
share canonical identity confirms (what *confirmed* uses).

## The verdict

Comparability is checked first. If either capture is invalid or stale, lacks event-loss counters
or dropped events, skipped instrumentation, or ran partly untraced, or if the two used different
commands, targets, modes, privacy profiles or sampling rates, the verdict is INCONCLUSIVE, even
when facts changed. Sampled captures must also have complete call counts. A recording passed with
`--baseline` or `--post-migration` answers the same from its `<recording>.meta.json` sidecar: a
hand-written trace, or JSONL merged with `cat` without its sidecars, has no event-loss counters.

- **FAIL**: a compared fact failed anywhere in the recording, or a `--probe-python` replay failed.
  A replay compares with the recording it replays, so it fails the verdict even when only one
  capture exists or the recording has no event-loss counters (it must be otherwise sound).
- **PASS** requires all of the following, over the whole recorded surface (after `apply` too):
  - no fact failed, none was uncomparable, and there is no review item;
  - at least one fact compared equal (or was accepted). Noise a replicate measured, a call
    explained away as implicit and an unverified edited site do not fail, but they show nothing
    unchanged: a diff made only of them is INCONCLUSIVE;
  - no static symbol is BLIND that a capture could record. A symbol whose every contact no
    capture can observe (an attribute read, a constant such as `yaml.SafeLoader`, a chain through
    a return value) does not keep the verdict from PASS: the verdict lists it instead ("N
    dependency use(s) cannot be observed by a capture, so this verdict does not cover them"), and
    `--agent` gives it as `unobservable_uses`;
  - every dynamic-only symbol had all its facts compared equal.
- **INCONCLUSIVE**: anything else. The reasons name the missing evidence, for example "2 static
  dependency symbol(s) were not exercised (1 not run by the workload; 1 ran but not recorded)" or
  "capture freshness could not be established". Review
  items land here: differences pymolt cannot judge on its own, such as a hand edit's new value, a
  value that varies between runs, or an environment difference. Each names what settles it.

An INCONCLUSIVE verdict also says who settles it. Each reason is one of two kinds:
- **needs a person** (`needs_review`): a review item. A person fixes the code, or accepts an
  intended change with `pymolt verify . --accept ID`. A difference a baseline replicate measured
  and still could not settle is one too. So are BLIND symbols whose line ran without a recorded
  call: capturing again records the same, so a person reviews each use. (Symbols whose every
  contact is unobservable are no reason at all: the verdict lists them, see PASS above.)
- **needs evidence** (`needs_evidence`): more or better captures settle it. BLIND symbols a test
  can still reach (not run, or "ran" only by their function or file), a fact
  that could not be compared, a capture that is missing, invalid, stale or unlike the other, a
  value that may be run-to-run noise while no baseline replicate was measured, an environment
  difference between the two captures, a diff in which no fact compared equal.

`.pymolt/verification.json` keeps `inconclusive_kind`: `needs_review` when any reason needs a
person, else `needs_evidence`, and null for PASS and FAIL. `inconclusive_causes` lists every
reason with its kind and what settles it; `verify` prints what settles the first one. Both kinds
exit 4. Some facts that could not be compared stay so however often you capture (a compiled call
past the first 2,000 at its call site, compiled calls the two captures' backends record
differently): what settles them says so.

### Scope after `apply`

When the post-migration code came from `pymolt apply`, the verdict is the one the same captures get
without a plan. The static symbols, confirmed, BLIND, dynamic-only, coverage and recall count the
whole recorded surface, and a behaviour change in any recorded call fails.

The API paths the plan found changed only focus the report:
- *Impact focus* gives the number of those paths and the static symbols on them: confirmed, BLIND
  and dynamic-only, and the coverage of their contacts.
- The BLIND and dynamic-only tables list the symbols on the plan's paths first. The INCONCLUSIVE
  reasons name the BLIND symbols inside the plan's predicted impacts apart from those outside them.
- The version diff's counts and listed changes cover the plan's paths. Behaviour changes outside
  them are listed as changes outside the plan's predicted impacts.

### Predicted risks

When the migration was made with `plan` and `apply`, `verify` scores each risk the plan predicted:
- **hit**: a failing change (CHANGED, DISAPPEARED, or REWRITTEN with a different outcome) was
  observed for that symbol at that call site. The mechanism the plan named is not checked;
- **prevented**: the risk's step fixed it and the behaviour stayed the same. A fix is credited
  when the step is in effect and, in the post-migration code, its change removed or replaced
  the risk's statement (or the import that binds its symbol). A step in effect that did not
  touch the statement is `recorded-not-fixing`, and a step skipped or rolled back leaves the risk
  unfixed: both score as a hit or a miss, never prevented. A step a person recorded can prevent
  a risk as well as one pymolt applied;
- **accepted**: a person accepted the change at that site;
- **miss**: the call site was compared and nothing changed, so the prediction did not come true;
- **not exercised**: no capture compared that call site.

Failing entries that no prediction covers are listed as **unpredicted**. The scores are also
given per step.

### One step

Every `verify` judges each step in effect on the same comparison, and saves the judgements in
`.pymolt/verification.json`, where `status` reads them. A step's entries are the facts on the
lines its ledger entries cover and at its planned sites. It is `failed` when one fails,
`needs_review` when one is a review item a person must look at, `needs_evidence` when a site was
not compared, an edited site could not be paired, a fact could not be compared, or a difference
may be noise no baseline replicate measured yet, `not_in_capture` when the capture predates it,
and `verified` otherwise. A step that needs evidence says what settles it (`settle`), and
`status` names it: a baseline replicate for noise, a test that reaches its sites, or why
capturing again does not help. `pymolt verify . --step ID` prints one step's judgement and exits
by it; it takes no new capture. Only a verdict on the baseline and post-migration captures judges
steps.

### Further oracles

| Option | Adds |
|---|---|
| `--probe-python` | replays the recorded calls in a target interpreter, each in its own process |
| `--profile exact\|shape\|custom`, `--comparator module:callable` | how values are compared |

## Exit codes

| Code | Meaning |
|---|---|
| 0 | passed |
| 1 | a behaviour change, or the captured command failed |
| 2 | invalid invocation |
| 3 | an environment or service failure |
| 4 | insufficient evidence |
