# Wave G: generality, fixing the runtime evidence skew

Status: done, 2026-11-15 (results at the end). Base: `wave2/fixers-and-successors` (3829b01). The problem statement and evidence are in `bench/generalization/FINDINGS.md`.

The API-change detection generalizes across ecosystems. The **runtime evidence layer** does not: the tracer, the value renderers, verify's pairing and the warning capture. This wave fixes G1–G8 and keeps G9–G11 for later.

## Acceptance
- **`bench/generalization` matrix (all 10 toys):**
  - **no equiv scenario FAILs.** PASS is the goal. INCONCLUSIVE is acceptable only when the replacement can't be observed (a plain attribute, an untraced stdlib method), and only with that reason stated. Requiring PASS where the evidence can't be seen would push pymolt into claiming more than it knows;
  - every change scenario is FAIL, with its `expected_changed` symbol either CHANGED or "now raises";
  - A recall (BEHAVIOR entries excluded) is at least 0.8 per toy;
  - pydantic gets a baseline (G1);
  - expected warnings are found, C-level ones included where the capture can observe them (G7).
- **No regression:** `bench/legacy_eval` scorecard vs `results/after-wave2`, `bench/e2e_pandas` 4/4, delta parity (P 1.00, R 0.95), the full suite, and the 3.6 parse of every injected-runtime file.
- **Overhead:** `bench/load` steady-state loss stays within 20% (the phase-2 gate).

## Contracts

### Runtime events (R1a writes them; V and H read them)
- **A compiled callable** (C/Cython: `builtin_function_or_method`, `method_descriptor`, `cython_function_or_method`, `wrapper_descriptor`) called from the project's code is recorded as a normal event.
  - `sym` is `__module__` + `__qualname__`, falling back to the owner type's module and qualname plus the name.
  - `site` comes from the caller frame. `t` is `return` or `raise`, with the rendered result or the exception type.
  - **`in` is `{"__c_call__": true}`** plus `arg0` when the backend knows it (sys.monitoring `CALL` gives the callable and `arg0`). Other arguments are not observable.
  - It is pure stdlib and 3.6-safe. Sources:
    - sys.monitoring (3.12+): `CALL` + `C_RETURN` / `C_RAISE`;
    - setprofile: `c_call` / `c_return` / `c_exception`;
    - wrap: module-level C functions can be wrapped.
- **A binding failure:** when a call to a traced Python callable raises before the callee's frame starts (TypeError for a bad signature), it is recorded as `t: "raise"`, `raised: "TypeError"`, with **`"binding": true`**, the callee's `sym`, and the caller's site.
  - sys.monitoring: `CALL` then `RAISE` in the caller frame with no `PY_START`.
  - setprofile: not observable, which is documented.
  - wrap: already observed, as the wrapper sees the raise; mark it `binding` when the TypeError comes from binding.

### Boundary selection (R1b writes it into the runtime; H sets it from the host)
- **`PYMOLT_TRACE_STDLIB`:** comma-separated stdlib top-level modules (`asyncio,collections,datetime,shutil,…`) treated as dependencies for this capture. Calls into them from the project's code are recorded like dependency calls. Without the variable, the stdlib is not recorded, as today.
- **Test-runner packages are never boundary targets** unless named in `PYMOLT_TRACE_TARGET`: `_pytest`, `pytest`, `pluggy`, `py`, `iniconfig`, `_hypothesis_pytestplugin`, `xdist`.
- **Warnings (G7):** an **observer filter** is installed at the front of `warnings.filters` for the deprecation family, with action `always`, routed to a recording hook. The hook records the warning, attributes it the way the current capture does, and then **re-applies the program's own filter decision** for that warning (ignore / default / once / module / error), so visible behaviour is unchanged. That covers warnings raised from C, which bypass `warnings.warn`. Keep the existing `warnings.warn` wrapper only if it is still needed, and never double-count.

### Renderer (R2 writes it; V compares it)
`verify/_normalize.py` gets a **generic structural renderer** for objects that today render `__opaque__`. It must never run user code: no properties, `__getattr__`, `__repr__`, `__eq__` or iteration of non-builtin types. It reads:
- the instance `__dict__` through the C getset (as `_dict_slot` does);
- `__slots__` values through member descriptors only;
- attrs (`__attrs_attrs__` names);
- namedtuple (`_fields` on a tuple subclass);
- pydantic v1 (`__fields__` names) and v2 (`__pydantic_fields__` names), with values from `__dict__`.

It produces `{"__object__": "<module>.<qualname>", "attrs": {name: rendered, ...}}`. Rules: public names only, depth- and size-capped, and the existing caps apply. A cap hit marks it truncated, so it compares at type level.

### Comparison (V owns `verify/facts.py`)
- **Pairing (G3):**
  - at the same site (file, function and line text, or line), a fact whose **input differs** but whose **symbol is the same** pairs with its partner and compares outcomes;
  - at an **edited line**, leftover facts pair by position even when the symbol differs (this exists today; find out why urllib3's `getheader` → `headers.get` and `Retry` did not pair);
  - a `__c_call__` input matches by symbol and site only.
- **Binding raises (G4):** compare as an outcome change (return → raise TypeError), never as DISAPPEARED. Name the change "now raises TypeError while binding its arguments".
- **Relocated types:** for `__type__`, `__object__` and `__opaque__` markers, the same top-level package plus the same qualname counts as equal at type level, as a relocation note, not CHANGED (SQLAlchemy `declarative_base`).
- **`__object__` markers** compare attribute by attribute: exact when both sides are complete, type level otherwise.

### Successors (S owns `delta/probe.py`'s successor parts, the engine's successor logic, and `delta/fetch.py`)
- Same-object candidates rank the object's **own home first**: `obj.__module__` + `__qualname__`, then that module's top-level package. This home is searched even when it's in another package, so `jinja2.escape` → `markupsafe.escape`, not `jinja2.compiler.escape`.
- **Rename successors from deprecation prose on the new side:** the new version still has the old name, deprecated, and its warning, docstring or `__deprecated__` names the successor. Examples: pydantic v2 `.dict()` → `model_dump`, `.json()` → `model_dump_json`, `parse_obj` → `model_validate`.
- **From intermediate releases** (`--online`, existing fetch): Django `force_text` → `force_str` (3.x), Jinja2 `contextfunction` → `pass_context` (3.0).
- A successor must exist on the new side and be compatible. One that is not the same object is a rename: `rename-call` / `replace-attr-path` with `confidence: medium`, and **never** auto-applied without its signature checked (existing `successor_same_signature`).

## Work packages and file ownership

The injected runtime is pure stdlib and parses at 3.6 (`CLAUDE.md`).

| WP | Owns | Issues |
|---|---|---|
| **R1a** | `verify/_monitoring.py`, `verify/_wrap.py`; in `verify/_backend.py`, the setprofile backend's event handling (`c_call` etc.) and the shared call-recording path; tests `tests/verify/test_monitoring.py`, `test_backend.py`, `test_wrap.py` and new ones | G1, G4 (runtime) |
| **R1b** | in `verify/_backend.py`, the matcher / target classes and `install_warning_capture`; `verify/boundary_tracer.py`'s env parsing; tests | G2 (runtime), G6, G7 |
| **R2** | `verify/_normalize.py`; tests `tests/verify/test_normalize.py` | G5 (renderer) |
| **V** | `verify/facts.py` (pairing, comparison, markers); tests `tests/verify/test_facts.py`, `test_fact_diff_integration.py` | G3, G4 (host), relocated types |
| **H** | `verify/service.py` (passes `PYMOLT_TRACE_STDLIB` for captures, computed from stdlib sites and from the stdlib imports on edited lines); `pyversion/` if needed; `bench/generalization/run.py` and `toys/django/toy.json`; G10 investigation in `delta/envs.py` (read; fix only if trivial) | G2 (host), bench fixes, G10 |
| **S** | in `delta/probe.py`, the same-object ranking; in `delta/engine.py`, the successor functions; `delta/fetch.py` successor prose; tests `tests/delta/` | G8 |

## Round 2 (after R1a, R1b, R2, V, H, S)

Round 1's results are in `bench/generalization` runs under the session scratchpad (`gH`, `gR1a`, `gR1b`, `gS`). They showed one regression and the gaps below.

### Comparison rules added (V)
- **Handle vs value.** A returned `__object__` whose type is, on either side, the receiver (`self`) of an *operation-like* traced call is a handle. What the project gets from it is recorded by those later calls, so attribute differences on a handle are DRIFT, never CHANGED.
  - **Operation-like** means the call binds something beyond `self`, or has defaulted parameters. A call with only `self` bound is indistinguishable from a property read (`click.testing.Result.output`), so it does not make its receiver a handle.
  - **Values** keep exact attribute comparison: click's `Result.exit_code` 0 → 2 is CHANGED.
  - **Why:** R2's generic renderer exposed pandas' `DataFrameGroupBy` internals (`group_keys`, `mutated`, `grouper`), which differ across versions while `groupby(...)[...].sum().to_dict()` returns the same dict. That made a false CHANGED in the pandas equiv scenario, the reference toy.
- **Sentinels.** A sentinel (`_NoDefault.no_default`, `_MISSING_TYPE`, `NOTSET`, …) on one side and a concrete value on the other is DRIFT "sentinel resolved". This mirrors `predict._call_time_default`.
- **An edited line with no recorded call on the post side** (the replacement is a plain attribute or an untraced stdlib method) is rewritten-unpaired: INCONCLUSIVE, with the reason given, never DISAPPEARED. The line text comes from the current source.
- **Module `__getattr__(name)` facts** (PEP 562) are attribute accesses, presented as `<module>.<name>`. They keep their warnings and never decide APPEARED/DISAPPEARED.
- **Closures** at the same site with the same `<locals>` leaf under a renamed enclosing function compare at type level.
- **Unobserved outcomes** (`unobserved:c-return`, `raised: "<unknown>"`) compare as unobservable, never CHANGED.

### Runtime (R1a)
- **Inherited stdlib methods on dependency objects:** a near call into stdlib code whose receiver is an instance of a target type is a dependency call named by the receiver type, for example `urllib3._collections.HTTPHeaderDict.get`.
  - **So the receiver, not the callee, decides such a call.** Under 3.12, sys.monitoring sees `d.items()` as a CALL of `dict.items` with `d` as its first argument. A call site used to stop firing (settle) once it saw the same callee twice. A site reached by plain dicts first then never saw a later `collections.defaultdict`: locust `runners.py:1402-1403` had 126 baseline calls and none on the post side.
  - Now a site keeps firing while a class of a target module inherits its callee: a builtin method, a builtin method bound to its receiver, or a stdlib method. That costs about 0.2 µs per call. The site still settles when no target class inherits the callee, and on the project's own methods.
  - A callee no target class inherited is decided again after imports, and when a call elsewhere shows a target receiver. One case is still missed: a dependency class made at run time without an import, whose instances reach only a site that settled before the class existed.
- **Setprofile cannot see Cython functions** (no `c_call` for `cython_function_or_method`). sys.monitoring sees them only as compiled calls: `arg0` and no result. So both backends wrap the public callables of compiled dependency modules (`CompiledWrap`), and a Cython call is recorded with its bound inputs and its result on both sides of a migration. Under sys.monitoring this runs in every mode (discover, watch, incremental) and stops with the backend.
  - **Recorded once.** The wrapper's code is in the runtime's own module, so sys.monitoring DISABLEs its `PY_START` and `CALL`, and neither incremental tool arms it. Its call of the Cython function is not a compiled call from the project's code (with `include_internal` too). An impact target that resolves to a wrapper is observed through it: arming the wrapper's code, which every wrapper shares, would record every wrapped call twice.
  - **Still a `__c_call__`:** a Cython callable reached without the wrapper, that is, bound before the wrap, a private (`_x`) name, a method of an extension type (`cdef class`, whose type is immutable), or outside the target. The host keeps `call-shape:<side>` for these and for older traces: one wrapped fact pairs with the compiled-call fact at the same symbol and site, and the others there are UNCOMPARABLE, never DISAPPEARED.
  - **As under setprofile:** dependency code that a wrapped Cython function calls back is no longer a contact at the project's call site. Before, the project's frame was its caller, since Cython has no frame. Wrapped calls have no per-site cap, as under setprofile: a hot loop of Cython calls from the project's code is recorded call by call, like a Python dependency call.
  - **Cost** (3.12, sys.monitoring discover):
    - Project → Cython in a hot loop (`msgpack.unpackb`, 200,000 calls): 13 µs per call with a memory sink, 16 µs with JSONL. Before, it was 0.8 µs, with 2,000 calls recorded and the rest counted. A recorded Python dependency call costs 3.8 µs. Most of the difference is `inspect.Signature.bind` over msgpack's 15 parameters. setprofile pays 88 µs per call for the same loop.
    - Dependency code calling its own Cython (pandas → `pandas._libs.lib`): every such call passes through a wrapper. A caller code decided once as not the project's calls straight through: 0.11 µs per call instead of 0.03 µs (0.55 µs without that gate).
    - Workloads: pandas (452 wrapped callables, 360,000 internal calls) +3%; SQLAlchemy ORM +1%.
  - **Wave G:** no scorecard differences against the base launched from the same checkout. Post traces are identical except for timestamp noise. No toy calls a Cython function from its own code: the base post traces hold no Cython `__c_call__`, only stdlib and numpy C methods.
  - **Locust:** the post side was re-captured with the base and the new runtime, against the same 3.8 baseline, and verified with the same host code.
    - `msgpack._cmsgpack.unpackb` at `locust/rpc/protocol.py:18`: 2,000 `__c_call__` events plus 2,571 counted past the cap became 4,614 events with bound inputs and results, none capped. Its 3,292 UNCOMPARABLE facts now compare by value, in two REVIEW entries:
      - 3,231 old / 3,004 new facts pair once volatile input values are masked. Their results differ, because the messages carry per-run node ids (host name plus uuid4).
      - 13 / 13 facts at an unpaired site, with equal types.
    - Not comparable 3,294 → 2, equal 6,222 → 6,257. The other moves are run noise at the HTTP-test sites, whose inputs carry random server ports. The failing findings shared by both runs are unchanged.
- **Per-site cap on compiled calls.** The first 2,000 calls of a compiled callable at one site are recorded. Later calls are counted, not recorded.
  - Each process's sidecar states this in `c_site_cap`: the muted calls, and the capped sites with their counts (at most 1,000 sites, the most muted first). The account is always written, so a sidecar without one comes from an older runtime.
  - setprofile counts exactly, since it sees every C call anyway. sys.monitoring keeps a capped site firing only to count it (about 1 µs per call). After 1,000,000 muted calls it stops the site, and that count becomes a lower bound (`exact: false`).
  - The cap key differs: setprofile caps per symbol and line, sys.monitoring per call instruction. A polymorphic site under sys.monitoring can therefore mute a second callable entirely. The account lists it by symbol.
  - The host marks a fact missing at a capped site as UNCOMPARABLE `c-site-capped:<side>`, never DISAPPEARED or APPEARED. So is an outcome missing only on the capped side of the same compiled call. A pair whose inputs differ is never softened.
  - An older one-process trace with a compiled-call site that holds 2,000 calls is presumed capped there: the whole site under sys.monitoring, and that symbol under setprofile. Nothing is inferred across processes, since their calls add up without any of them reaching the cap.
  - The runtime's own modules (`_rawthreads` included) are never the project's code. Before this, sys.monitoring recorded the JSONL writer's `deque.popleft`, and two of locust's three capped sites were the tracer itself.
- **Open:** a warning raised by a C method itself (`datetime.utcnow`, `Element.getchildren`) is observed but not attributed to the `__c_call__` fact. The wiring is designed (R1b's report) and waits on the owner.

### Host (H)
- During a Python move, trace every stdlib module the project imports, minus a justified noise list.
- The python delta's touched spellings include runtime-observed stdlib symbols, and stdlib SIGNATURE facts come from probing both sides.

### Delta and bench (S)
- **G10:** a package absent from the baseline is absent on the old side; that is not a reason to build a throwaway env.
- **DEPRECATED** facts with a verified, same-signature successor produce rewrites through the existing kinds.
- **Runner:** `--online`; no crash without the pandas row; metric E matches by resolved object.

## Round 3: edit provenance decides how a difference is judged

Round 2 left seven equivalent scenarios failing, and most of those failures were artifacts. verify paired leftover calls on edited lines **by position**, so it compared calls that don't correspond. For example, SQLAlchemy `ResultProxy.scalar()` → 3 was compared with `Connection.execute` → CursorResult, and `Query.get(1)` was compared with `Session.get(Book, 999)`. Where the pairs did correspond, the differences were real at the call boundary but expected: the person had also edited the consuming code. urllib3 `items()` changed from a list to a view and `get_host` from a tuple to `Url`. networkx went from `matrix` to `ndarray`, with `A * A` edited to `A @ A`.

The rules below came from my proposal and an independent second opinion, reconciled. Both are in the session scratchpad (`design/roundG3-mine.md`).

### Three provenance tiers
A line-mapped diff of the baseline source against the post source classifies every fact's line.
1. **Unedited:** compared strictly. A different outcome is CHANGED, so FAIL; this covers click `exit_code`, pandas `mean` and pydantic `schema()`. A tool that downgrades a boundary change at untouched code because the tests didn't notice it defeats its purpose.
   - **A call that vanished:**
     - if the old and new static contact maps show the code no longer refers to a dependency there (the name now binds to project code), it is INCONCLUSIVE, "no longer a dependency call here";
     - if the enclosing function never ran on the new side, it is INCONCLUSIVE, "not reached";
     - otherwise it is FAIL, for example `row.keys()`.
   - **Downstream of an edit:** a fact whose receiver or input type changed to a type that an edited site on the same side returns is judged in the hand-edit tier.
2. **pymolt's own edit** (in the apply ledger): strict. pymolt claimed a drop-in replacement, so any difference falsifies the claim and FAILs. An equal outcome is REWRITTEN equal; that is the pandas `append` → `concat` case.
3. **Hand edit** (an edited line with no ledger entry): facts are paired by anchors only.
   - **Equal:** REWRITTEN equal.
   - **Representation only** (the same values in a different container or type family: matrix vs ndarray, list vs view, tuple vs namedtuple): DRIFT, "representation changed".
   - **Different value:** a **review item**, so the verdict is INCONCLUSIVE with the old and new outcome shown, never a silent PASS.
   - **A new raise where the old side returned:** FAIL.
   - **Unpaired:** an "unverified edited site". It does not block PASS, but it lowers trust and is counted, like a BLIND contact.

The report always prints `N edited sites: a equal, b representation-only, c need review, d unverified`.

### Anchors (positional pairing across different symbols is removed)
Facts pair only within the same file and normalized function, through one of:
1. the same symbol;
2. an old → new symbol from the apply ledger or the plan's delta successors;
3. a one-statement edit whose sides are both `return <call>`, or both `<same target> = <call>`: the outermost call (found by leaf name) carries the statement's value.

Within a candidate set, facts match on scalar argument values present on both sides, then by order when that is unique. An ambiguous match stays unpaired.

### What verify needs (new inputs)
- **A source snapshot:** each capture stores the text and sha256 of every project file that has facts.
- **An apply ledger:** `apply` records each edit as file, old and new line ranges, pattern kind, rule id, claim (same-object / same-signature / verified-rule / python-fixer), and old and new symbol.
- **Old and new static contact maps**, built from the snapshots.
- **Type relocations from the plan:** the old public spelling resolves to the new type, for example pydantic `ValidationError` now living in `pydantic_core`.

Without snapshots (older baselines) verify falls back to round 2's line-text rules, minus positional pairing.

### Noise removed
- `__del__`: finalizers run at GC time.
- Import machinery: `importlib._bootstrap*`.
- Implicit class-creation hooks at class-body sites (`__init_subclass__`, `__set_name__`, a metaclass namespace's `__setitem__`): never verdict drivers.
- Generic rendering of static C types (generator, code, frame): they stay opaque.
- Decorator wrappers: named by the callable's declared `__qualname__` under monitoring (networkx `argmap`).

### Follow-up, not in this wave
A reviewed-change record, keyed by symbol, site and an outcome fingerprint, that turns a reviewed FAIL into ACCEPTED, printed, and expiring when the fingerprint changes.

### Verdict scope (found in round 3, fixed)
Once a plan was applied, the verdict read only the impact-filtered diff: the plan's predicted symbols. An unpredicted behaviour change therefore could not FAIL verify, so verify only confirmed the predictor. The django change scenario's `escape` (`&#39;` → `&#x27;`) and jinja2's `Template.render` were found in `all_fact_diff` and still came out PASS or INCONCLUSIVE.

The filter dates from v0.1.0, where it was a *focus* on an Axiom impact set. It became the verdict when the applied plan became the default impact set.

Now:
- **FAIL evidence** comes from the whole surface (`all_fact_diff`).
- **Coverage** (BLIND, trust) stays scoped to the impacted surface.
- **The report** lists "behaviour changes outside the plan's predicted impacts" (`unpredicted_changes`, `data.changes_outside_plan`).

### Known limit (follow-up)
Time-dependent outcomes, for example `gzip.compress`, which embeds mtime, compare as CHANGED. That got more frequent once stdlib modules are traced. The principled fix is a determinism re-run of the baseline workload, which marks unstable facts so they compare at type level.

## Round 4: detection recall, overhead, consistency

- **API-change recall (S):**
  - a parameter that loses its default is SIGNATURE;
  - a parameter that is removed but absorbed by `**kwargs` is SIGNATURE when the new code's own `raise` message says so;
  - a `**kw` that exists only to refuse makes every removed parameter SIGNATURE;
  - same-class aliases and module siblings count in the same-object search;
  - touched spellings come from the site: `<receiver type>.<attr as written>`, and inherited dependency attributes through project classes (`delta/touched.py`).
- **Ground-truth labels** follow the taxonomy written in `bench/generalization/SPEC.md`, with identity evidence in each toy's `verified`.
- **Overhead (R1a).** Wave G first measured 28.4% saturated loss (gate: ≤ 20%). The causes:
  - the generic object renderer on big results (about 5–6 points);
  - the global RAISE callback (about 2 points).

  Fixes:
  - a symbol whose object result renders over 4 KB keeps that result as a witness and records later results at type level;
  - `MAX_OBJECTS` is 64 and nested objects render to depth 3;
  - the RAISE callback is a single test in the common case.

  The RAISE callback stays, because PY_UNWIND would lose binding failures caught in the same frame (PyYAML). The gate's `dropped=None` was a harness path bug.
- **Trust consistency.** Test-runner packages are not traced, so their static contacts are left out of trust, with a note. Naming one as a target brings the whole family back. Before this fix, hyper's trust fell from 80% to 68% for nothing; now it is 83%.

## Results

**Generalization matrix** (`bench/generalization/results/waveG`; `wave2` is the before):

| toy | A recall | equiv | change (expected symbol) |
|---|---|---|---|
| click | 2/3 (`**attrs` forwarding, a known limit) | PASS | FAIL (`CliRunner.invoke`) |
| django | 7/7 | PASS | FAIL (`escape`) |
| jinja2 | 4/5 (a name in a string, a known limit) | INCONCLUSIVE | FAIL (`Template.render`) |
| networkx | 7/7 | PASS | FAIL (`node_link_data`) |
| pandas | 2/2 | PASS | FAIL (`mean`) |
| pydantic | 9/9 | INCONCLUSIVE | FAIL (`parse_obj`, `schema`) |
| pyyaml | 4/4 | PASS | FAIL (`load`) |
| sqlalchemy | 8/9 (C slot, a known limit) | INCONCLUSIVE | FAIL (`RowProxy.keys`) |
| stdlib | 10/11 (attribute of a replaced result, a known limit) | INCONCLUSIVE | FAIL (`fromisoformat`) |
| urllib3 | 5/5 | INCONCLUSIVE | FAIL, but not on `HTTPResponse.stream` (G9; it FAILs on time-dependent `gzip.compress`) |

**What this means:**
- No equivalent scenario FAILs. Every INCONCLUSIVE names its reason: review items on hand edits, or untraced static symbols.
- 9 of 10 change scenarios FAIL on the expected symbol.
- In `wave2`, only pandas had the expected verdicts in both scenarios.

**Gates:**
- the full suite (1863 passed);
- `bench/e2e_pandas` 4/4;
- delta parity unchanged (local API P 1.00 / R 0.95);
- `bench/load` PASS (18.9% saturated loss, median of 3, about 1 point of margin; dropped 0; recall 100%; tail ×1.06; startup at least 72%);
- every injected-runtime file parses at 3.6;
- no changed file has more ruff findings than at HEAD.

**Legacy eval:** `bench/legacy_eval/results/after-waveG`, compared with `after-wave2`:
- vibora's baseline captures again (0 → 96k events; ujson was invisible), and its plan and apply now run;
- theano's baseline grows from 830 to 20k events, and zipline confirms 31 → 38 symbols.
- **Trust changes, explained:**
  - **hyper (80 → 83%):** after the test-runner fix. Before that fix it read 68%: pytest contacts were counted although pytest is no longer traced.
  - **alphalens (82 → 68%) and lasagne (100 → 75%):** the scope got wider. DEPRECATED facts now carry successors, and the impact set has always included successor paths (`replacement_path`), so the successors' own static contacts joined the trust scope. lasagne's new BLIND is `numpy.random.randint`, a C method whose identity does not resolve in the 3.6 container. That identity gap for C methods predates this wave; it is not lost evidence.

**Open after Wave G:**
- a determinism re-run for time-dependent outcomes;
- G9 (stream outcomes);
- C-method warning attribution (designed, waiting for the owner's decision);
- a reviewed-change record (ACCEPTED);
- the gate's margin, which is only about 1 point.
