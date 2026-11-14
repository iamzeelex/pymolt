# Tier 1 — fact model v2 (phase 1)

Status: in progress (2026-11-14). Supersedes the exact-string join between the static contact
map and runtime `q`, and the exact `(q, inputs)` set difference used for the verdict.

## Why

Measured on phase-0 traces:

| problem | evidence |
|---|---|
| static and runtime names never meet | trust 1% (2/197) on pymolt's own tests; ~44% after naive canonicalization |
| runtime name drops the class | `q = module.co_name`: every `__init__` in a module collides |
| decorator wrappers rename the callee | pandas 1.5 wraps `concat`, 2.2 wraps `Series.to_dict` → `pandas.util._decorators.wrapper` |
| library-added defaults change the fact key | pandas 2 adds parameters → same call "disappears" and "appears" → false FAIL |
| verdict shows counts only | `disappeared: 6` — never which call, where, old → new |
| most values are opaque | 29–42% of value leaves (datetime, Decimal, DataFrame, Response…) |

## Acceptance

1. **Equivalent migration passes.** The toy app (requests + pandas 1.5 on 3.11 → pandas 2.2 on
   3.12, `Series.append` rewritten to `pd.concat`) yields no behaviour change; the rewritten site is
   reported as REWRITTEN with equal outcome; library signature changes are reported as DRIFT.
2. **Real changes fail precisely.** Fabricated changes (int→float result, different length, a call
   that now raises, a different return type) each FAIL naming symbol, site and old → new outcome.
3. **Trust is real.** On pymolt's own codemods+ingestion tests, trust over behavioural contacts in
   executed files is ≥ 75% (prototype: 85%).
4. Injected runtime stays pure stdlib and 3.6-syntax compatible; v1 traces still load.

## Event schema v2 (injected runtime → host)

Additive; every v1 field keeps its meaning.

```json
{"v": 2, "t": "return" | "raise",
 "q":   "<frame module>.<co_name>",                 // v1 legacy name, unchanged
 "sym": "<defining module>.<qualname>",             // canonical runtime identity
 "symq": "unwrapped" | "receiver" | "fallback",     // absent when exact; never part of a fact key
 "own": "<module>.<qualname>",                      // receiver's class; absent unless it differs
                                                    // from the class prefix of sym (see below)
 "in": {"bound": {...}, "varargs": ..., "kwargs": ...,
        "defaulted": ["param", ...]},               // names left at their default value
 "result": ..., "raised": "...", "where": {"file", "line", "func"}, "ts": ...}
```

### Canonical identity (`sym`)
Computed once per code object (cached with the existing per-code decision cache):

1. 3.11+: `module + "." + code.co_qualname` — except generated or nested code (`co_filename`
   starting with `<`, or `<locals>` in `co_qualname`, e.g. dataclass methods named
   `__create_fn__.<locals>.__init__`): a unique per-module index hit (step 2) wins there. The index
   names a `<locals>` function found directly in a class namespace by its place (`Cls.attr`), so
   pre-3.10 dataclasses agree too.
2. Older: a per-module index built lazily from `sys.modules[module]` — functions, classes and their
   methods (plain, static, class, property accessors), nested classes — mapping code objects to
   `__qualname__`. Unique hit → that qualname.
3. Generic decorator wrappers (code with free variables and `*args/**kwargs`): read the frame's
   closure cells; exactly one function-valued cell → follow `__wrapped__` chains (`inspect.unwrap`)
   → identity of the inner function, `symq: "unwrapped"`. This is per call (cells differ per
   decorated function); results are cached per inner function.
4. Methods generated elsewhere and attached to a class (`cls.mean = mean`; co_qualname has
   `<locals>` or the index misses) are named through the receiver: walk `type(self).__mro__` for the
   class holding a function with this exact code → `Class.__module__.__qualname__ + "." + co_name`,
   `symq: "receiver"`, with that function's defaults. Cached per (code, receiver type).
5. Otherwise `module.co_name`, `symq: "fallback"`.

The wrap backend already holds the real callable: `sym` = its unwrapped `__module__.__qualname__`.

### Receiver (`own`)
An inherited constructor or method (`httpx.ConnectError(...)` runs a base class's `__init__`
defined elsewhere) has the base's `sym`, which can never match the static class. When the callee's
first positional parameter is `self`/`cls`, the event carries `own` = `module.qualname` of
`type(self)` (of the class itself for `cls`: classmethods, `__new__`), but only when it differs from
the class prefix of `sym` (`sym` minus its last segment). Per call: one `type()` plus a per-type
cache. Unwrapped wrapper frames use the inner function's first parameter; the wrap backend the
instance under construction / the first positional argument.

### Caller-passed inputs
- `bound` holds only arguments whose value is **not identical** (`is`) to the parameter's default.
  Parameters at their default are listed by name in `defaulted`. Explicitly passing a value equal to
  the default is indistinguishable and harmless (same value).
- For an unwrapped wrapper frame, `varargs/kwargs` are bound to the inner function's signature with
  `bind_partial`, so both versions produce the same `bound` form whether or not a decorator is
  present.
- Defaults come from the function object (index hit / unwrapped inner); when unknown, all bound
  arguments are kept and `defaulted` is omitted.

### Value renderers (`_normalize`)
Before the opaque fallback, in this order (all stdlib, duck-typed, guarded):

| value | rendering (values) | privacy=shape |
|---|---|---|
| datetime / date / time | `{"__datetime__": iso}` | `{"__shape__": type}` |
| timedelta | `{"__timedelta__": total_seconds}` | `{"__shape__": type}` |
| Decimal | `{"__decimal__": str}` | `{"__shape__": type}` |
| UUID | `{"__uuid__": str}` | `{"__shape__": type}` |
| Enum member | `{"__enum__": "mod.Cls.NAME"}` | same (structural) |
| class / function object | `{"__type__": "mod.Qual"}` / `{"__callable__": "mod.Qual"}` | same |
| dataclass instance | `{"__dataclass__": type, "fields": [[name, value], ...]}` | fields at shape |
| allow-listed array/frame (numpy ndarray, pandas Series/DataFrame/Index) | `{"__structure__": type, "shape": [...], "dtype(s)": ..., "columns": [...], "values": ...}` — values only when size ≤ 200 | without `values` |
| object with int `status_code` | `{"__structure__": type, "status_code": n}` | same |

Anything else stays `{"__opaque__": type}`.

### Runtime metadata
The sink metadata records `python` (`x.y.z`) and `executable`, so the host can resolve static
symbols in the interpreter that ran the capture.

## Host

### Static identity (`verify/identity_resolver.py`)
Static contact targets are public import paths (`requests.Request`, `pandas.concat`). They are
resolved **in the capture's interpreter** (subprocess, project as cwd; it imports dependencies, so it
never runs in pymolt's own process): `{path: {"sym", "kind": function|class|module|other,
"aliases": [...]}}` or `{path: {"error"}}`. Match rule: runtime `sym == static sym`, or the static
object is a class and runtime `sym` starts with `class_sym + "."`.

### Contact classes and trust
- behavioural: `call`, `decorator`, `context-manager`, `protocol`
- existence: `attribute-read`, `class-base`, `imported-constant` (an import that resolves proves them;
  they are not trace evidence)

`trust` = confirmed / static, over **behavioural contacts in files the trace executed**. The
previous all-contacts number stays available as `all_*`.

### Facts (`verify/facts.py`)
- **Fact key**: `(sym or q, site, input)`; `site = (relative file, func, line text)` when the trace
  metadata carries line text, else `(relative file, func, line)`. `input` = `in` without `defaulted`.
- **Outcome**: `("return", result)` or `("raise", type)`; a fact holds the outcome distribution.
- **Line text** is captured at capture time (`site_text` in the trace metadata: `"file:line" → text`),
  because the project's code changes between baseline and post-migration.

### Fact diff
| category | rule | fails? |
|---|---|---|
| equal | same key, same outcomes (at the comparable level) | no |
| CHANGED | same key, outcome distribution differs | yes |
| REWRITTEN | site text changed; old and new calls at the same `(file, func)` compared by outcome | only if outcomes differ |
| DISAPPEARED / APPEARED | key on one side only, at an unchanged site | yes |
| DRIFT | same symbol and site, `defaulted` differs (library signature change) | no; reported |
| type-level | either side opaque / ambiguous / nondeterministic → compared by type only | if types differ |

Comparison level is reported per fact (`exact` / `type`). The verdict and `contract report` list
every non-equal fact with symbol, site, and old → new.

## Out of scope for phase 1
Value-class lens and saturation (phase 3), basins and statistical verdict (phase 3), sys.monitoring /
code-swap backends and in-process aggregation (phase 2), HMAC privacy profile, C-call identity facts.
