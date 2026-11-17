"""``sys.monitoring`` (PEP 669) backend, Python 3.12+.

INJECTED RUNTIME — pure stdlib. Imported only on 3.12+, but it ships in the same bundle as the
3.6 runtime, so it must PARSE on 3.6 (no walrus, no ``match``, no PEP 604 unions, no
``from __future__ import annotations``).

One tool id, two modes, the same identity / inputs / sampling code path as the setprofile
backend (``FrameBackend``), so a fact recorded here is identical to one recorded there.

discover (wide)
    Global ``PY_START`` + ``PY_UNWIND``. ``PY_START`` returns ``DISABLE`` for a code object that
    is not a dependency (or is a module body), so after warm-up code outside the dependencies
    costs nothing. For a dependency the caller (near-side) check runs per call, exactly as in
    setprofile. ``PY_RETURN`` is armed lazily, as a LOCAL event, on a code object the first
    time a call into it is recorded: returns of dependency code that was never a boundary
    contact are never delivered.

watch (narrow, long runs)
    Local ``PY_START`` (+ ``PY_RETURN``) only on the watch-set code objects: the codes recorded
    during discover plus the impact targets resolved to code objects (functions, every
    wrapper in a ``__wrapped__`` chain, static/class methods, property accessors, a class's own
    methods and the constructor that runs). Targets in modules not imported yet resolve on
    import. ``module.*`` targets use a global ``PY_START`` restricted to the prefix (DISABLE
    elsewhere).

incremental (3.12+ default for long runs: no discover window)
    Tool A arms the watch set as the program runs: a global ``CALL`` event whose callback
    returns ``DISABLE`` at every call site that is not our code (so dependency internals cost
    one callback per call SITE, ever) and, at a call site in our code, resolves the callable to
    the Python code that will run (function, bound method, class -> its ``__init__``/``__new__``
    and a Python metaclass ``__call__``, ``functools.partial``, static/class method, callable
    instance -> its type's ``__call__``; a C callable's first argument when that is a
    function, for ``map(dep.f, xs)``). A dependency code is armed once with local
    ``PY_START``/``PY_RETURN`` and recorded by the same callback as watch mode. A call site
    that saw the same callee twice is DISABLEd; one that saw two callees (polymorphic) never
    is. C callables are not recorded (no Python frame), the same as discover.
    Tool B (a second tool id) owns the calls that never pass a CALL instruction in our code:
    dependency dunders other than ``__init__``/``__new__``/``__call__`` (``x[k]``, ``for``,
    ``with``, operators, ``__get__`` descriptors), a metaclass's ``__new__``/``__init__``
    (class creation), ``property`` accessors, generator-likes
    (one start per generator, so a generator a dependency created and our code iterates is
    seen), and ``module.*`` impact prefixes. Its global ``PY_START`` DISABLEs every other code.
    ``DISABLE`` is per tool, so B never blocks A's lazy local events, and each code object is
    owned by exactly one tool (one cached classification), so no call is recorded twice.
    Known recall limits vs discover: a dependency callable invoked by C code with our frame as
    caller but not passed as the C call's first argument (``sorted(xs, key=dep.f)``, a class
    in ``map(Dep, xs)``), and a call site that turns polymorphic after it was DISABLEd.

Impact targets (``watch_targets``) are armed in EVERY mode: in discover they are recorded on
top of the discover target (an impact target outside it would otherwise be invisible until
the switch).

``PY_UNWIND`` is global in both modes: CPython does not allow it as a local event, nor can it
be disabled (returning DISABLE raises into the program and removes the callback). Its
callback is a dict check when no recorded call is in flight. It carries the exception, so a
raise is reported with its type (``module.Qualname``, builtins bare: ``TypeError``).

Generators: ``PY_START`` fires once per generator, at its first start (``PY_RESUME`` is not
subscribed), and the call is emitted there (see ``_backend``). ``PY_YIELD`` is not subscribed:
a suspension is not an outcome.

The switch (``enter_watch``) never loses or doubles a call: the watch codes get their local
events while the global ``PY_START`` is still on (a code with both fires once per event), the
global events are dropped only afterwards, every call in flight is a recorded code (its local
``PY_RETURN`` stays armed) and ``PY_UNWIND`` never changes. Callbacks run with the GIL; the
shared state is a few dicts updated atomically, plus a lock taken only the first time a code
object is recorded.

Coexistence: another tool (coverage.py, a debugger, cProfile) uses its own tool id; ids are
tried in the order 4, 3, 2 (PROFILER_ID, cProfile's), 5 and never 0/1 (debugger/coverage).
``DISABLE`` is per tool. A disabled location stays disabled across ``free_tool_id`` (verified
3.12-3.14), so re-acquiring an id this process used before calls ``restart_events()`` (it
re-arms other tools' disabled events once; they disable them again).
"""
import functools
import sys
import time
import types

from ._backend import (
    _C_SITE_MAX,
    _CODE_CACHE_MAX,
    _HEAPTYPE,
    C_RETURN_UNOBSERVED,
    NO_ARG,
    OUTCOME_RAISE,
    OUTCOME_RETURN,
    UNTYPED_RAISE,
    FrameBackend,
    PrefixMatcher,
    _monotonic,
    generator_marker,
    install_warning_capture,
    is_compiled_callable,
    never_target,
    remove_warning_capture,
    same_c_callable,
)
from ._identity import _wrapped_of
from ._normalize import typename
from ._rawthreads import allocate_lock, stack_key
from ._wrap import _partition_impact_targets, _WrapFinder

_getframe = sys._getframe
_FUNCTION = types.FunctionType
_METHOD = types.MethodType
_TYPE_DICT = type.__dict__["__dict__"]
_TYPE_MRO = type.__dict__["__mro__"]
_MISSING = object()

TOOL_IDS = (4, 3, 2, 5)
TOOL_NAME = "pymolt"
_USED_IDS = set()   # tool ids this process's runtime held before (their DISABLEs persist)

DISCOVER = "discover"
WATCH = "watch"
INCREMENTAL = "incremental"

# Reached through a CALL instruction (a class call, a callable instance): tool A arms them.
_CTOR_NAMES = frozenset(("__init__", "__new__", "__call__"))
_K_NONE, _K_C, _K_PY = 0, 1, 2     # what a CALL reaches: nothing of ours / compiled / traced Python
_SITE_STABLE = 2        # same callee this many times at one call site -> DISABLE the site
_SITES_MAX = 200000
_SETTLE_GRACE = 1.0     # seconds the start de-duplication stays on after tool B is freed


def _implicit_name(name):
    """A dunder run by the interpreter itself (subscript, iteration, ``with``, operators,
    descriptors), not by a CALL instruction in the caller."""
    return (len(name) > 4 and name[:2] == "__" and name[-2:] == "__"
            and name not in _CTOR_NAMES)


def _owner_class(code, g):
    """The class whose namespace defines ``code`` (by ``co_qualname``), or None."""
    qual = getattr(code, "co_qualname", None)
    if not qual or "<" in qual or "." not in qual or g is None:
        return None, None
    parts = qual.split(".")
    obj = g.get(parts[0])
    for part in parts[1:-1]:
        if not issubclass(type(obj), type):
            return None, None
        obj = _TYPE_DICT.__get__(obj, type).get(part)
    if not issubclass(type(obj), type):
        return None, None
    return obj, parts[-1]


def _is_meta_ctor(code, g):
    """``__new__``/``__init__`` of a metaclass: run by class creation (``class X(Base):`` goes
    through the C ``__build_class__``), not by a CALL to something we can resolve."""
    if code.co_name not in ("__new__", "__init__"):
        return False
    owner, _name = _owner_class(code, g)
    return owner is not None and issubclass(owner, type)


def _is_accessor(code, g):
    """True if ``code`` is a ``property``'s fget/fset/fdel (run by attribute access)."""
    obj, name = _owner_class(code, g)
    if obj is None:
        return False
    value = _TYPE_DICT.__get__(obj, type).get(name)
    if not issubclass(type(value), property):
        return False
    for acc in (value.fget, value.fset, value.fdel):
        for _ in range(8):
            if acc is None:
                break
            if type(acc) is _FUNCTION and acc.__code__ is code:
                return True
            acc = _wrapped_of(acc)
    return False


def _mro_value(cls, name):
    for owner in _TYPE_MRO.__get__(cls, type):
        value = _TYPE_DICT.__get__(owner, type).get(name, _MISSING)
        if value is not _MISSING:
            return value
    return None


def _call_target(obj):
    """What identifies a call site's callee across calls, cheaply: the function's code, the
    class or callable type itself, or ``(type, name)`` for a C callable (a bound builtin
    method is a new object per call). Compared with ``is`` unless it is a tuple."""
    for _ in range(8):
        t = type(obj)
        if t is _FUNCTION:
            return obj.__code__
        if t is _METHOD or t is staticmethod or t is classmethod:
            obj = obj.__func__
            continue
        if t is functools.partial:
            obj = obj.func
            continue
        if issubclass(t, type):
            return obj
        if type(_mro_value(t, "__call__")) is _FUNCTION:
            return t
        return (t, getattr(obj, "__name__", None))
    return None


def _frame_of(code):
    """The frame executing ``code`` that triggered the current callback (``_getframe(1)``
    unless something wrapped the callback; bounded walk)."""
    frame = _getframe(2)
    for _ in range(8):
        if frame is None or frame.f_code is code:
            return frame
        frame = frame.f_back
    return None


def raised_label(exc):
    """The exception type as ``module.Qualname`` (builtins bare, like every type label)."""
    try:
        return typename(exc)
    except Exception:
        return "<unknown>"


# ── impact target -> code objects ────────────────────────────────────────────

def _function_codes(obj, out, seen, depth=0):
    """Every code object a call through ``obj`` starts in: the function and each function
    down its ``__wrapped__`` chain (a decorator's wrapper runs first)."""
    for _ in range(16):
        t = type(obj)
        if t is _METHOD:
            obj = obj.__func__
            continue
        if t is _FUNCTION:
            code = obj.__code__
            if id(code) not in seen:
                seen.add(id(code))
                out.append(code)
        elif issubclass(t, (staticmethod, classmethod)):
            obj = obj.__func__
            continue
        elif issubclass(t, property):
            if depth < 2:
                for acc in (obj.fget, obj.fset, obj.fdel):
                    if acc is not None:
                        _function_codes(acc, out, seen, depth + 1)
            return
        elif t is functools.partial:
            obj = obj.func
            continue
        nxt = _wrapped_of(obj)
        if nxt is None or nxt is obj:
            return
        obj = nxt


def target_codes(obj):
    """Code objects to watch for an impact target object (empty: nothing Python-level)."""
    out = []
    seen = set()
    if issubclass(type(obj), type):
        try:
            ns = _TYPE_DICT.__get__(obj, type)
            for value in list(ns.values()):
                if not issubclass(type(value), type):
                    _function_codes(value, out, seen)
            # The constructor that actually runs, possibly inherited from a base.
            for name in ("__init__", "__new__"):
                for owner in _TYPE_MRO.__get__(obj, type):
                    value = _TYPE_DICT.__get__(owner, type).get(name, _MISSING)
                    if value is not _MISSING:
                        _function_codes(value, out, seen)
                        break
        except Exception:
            pass
        return out
    try:
        _function_codes(obj, out, seen)
    except Exception:
        pass
    return out


class _CodeTargets:
    """Resolve exact impact symbols to code objects, now for loaded modules and on import for
    the rest (``_wrap._WrapFinder`` calls ``_wrap_module``). Unresolvable targets are recorded
    in ``skipped`` (honesty marker), never silently dropped."""

    def __init__(self, symbols, on_codes):
        self._on_codes = on_codes
        self._by_module = {}        # modname -> [(remainder, full symbol)]
        self._done = set()
        self.skipped = []
        self._skip_seen = set()
        for sym in (s for s in (symbols or ()) if s):
            parts = sym.split(".")
            if len(parts) < 2:
                self._skip(sym, "no-module")
                continue
            for i in range(1, len(parts)):
                self._by_module.setdefault(".".join(parts[:i]), []).append(
                    (tuple(parts[i:]), sym))

    @property
    def modules(self):
        return set(self._by_module)

    def _skip(self, full, reason):
        if full not in self._skip_seen:
            self._skip_seen.add(full)
            self.skipped.append((full, reason))

    def resolve_loaded(self):
        for modname in list(self._by_module):
            module = sys.modules.get(modname)
            if module is not None:
                self._wrap_module(modname, module)

    def _wrap_module(self, modname, module):
        for remainder, full in list(self._by_module.get(modname, ())):
            if full in self._done:
                continue
            obj = module
            ok = True
            for part in remainder:
                try:
                    obj = getattr(obj, part)
                except Exception:
                    ok = False
                    break
            if not ok:
                if len(remainder) == 1:
                    self._skip(full, "attr-missing")
                continue                    # a more specific module candidate may resolve
            self._done.add(full)
            codes = target_codes(obj)
            if not codes:
                self._skip(full, "no-python-code")   # C callable / constant: not observable
                continue
            self._on_codes(codes, full)


def _acquire_tool_id(mon):
    for tool in TOOL_IDS:
        try:
            mon.use_tool_id(tool, TOOL_NAME)
        except ValueError:
            continue                        # in use by another tool
        return tool
    raise RuntimeError("no free sys.monitoring tool id (tried " + repr(TOOL_IDS) + ")")


class MonitoringBackend(FrameBackend):
    """PEP 669 backend: ``discover`` (global, DISABLE outside dependencies) or ``watch``
    (local events on the watch set). ``enter_watch`` switches a running discover backend."""

    name = "monitoring"

    def __init__(self, target, sink, exclude=(), source=None, include_internal=False,
                 mode=DISCOVER, watch_targets=()):
        FrameBackend.__init__(self, target, sink, exclude, source, include_internal)
        self.mode = WATCH if mode == WATCH else DISCOVER
        self._watch_targets = tuple(watch_targets or ())
        self._watch_codes = {}      # id(code) -> code
        self._impact_codes = {}     # id(code) -> code (resolved from impact targets)
        self._watch_prefix = None   # PrefixMatcher for ``module.*`` impact targets
        self._local = {}            # id(code) -> (code, local event set we armed)
        self._local_lock = allocate_lock()
        self._target_sets = []      # _CodeTargets, one per armed batch of impact targets
        self._finders = []
        self._tool = None
        self._mon = getattr(sys, "monitoring", None)
        self.switched = False
        if mode == INCREMENTAL:
            self.mode = INCREMENTAL
        # incremental mode
        self._tool2 = None          # tool B: global PY_START for implicitly-run codes
        self._sites = {}            # (id(code), offset) -> [code, callee target, count, poly]
        self._deps = {}             # id(code) -> (code, is dependency, module) for callees
        self._ctors = {}            # id(class) -> (class, [(code, function)])
        self._kinds = {}            # id(code) -> (code, owned by tool B)
        self._kind_lock = allocate_lock()
        self._arm_lock = allocate_lock()
        self._implicit = {}         # tool B's per-code decision entries
        self.armed = 0              # codes armed by call-site resolution
        self.last_armed_at = None   # monotonic: last code armed (A) or first recorded (A or B)
        self._ckinds = {}           # id(callable) -> (callable, kind, sym, callee function | None)
        self._csites = {}           # (id(code), offset) -> [code, same-callee count, callee]
        self._lastcall = {}         # tid -> (code, offset, func): last traced Python call
        self._craise = {}           # tid -> (info, code, offset): raise awaiting its type
        self._callname = {}         # tid -> (callee code, declared sym): decorator wrappers
        self._renames = False
        self._b_active = True       # tool B owns implicitly-run codes (until settle)
        self.settled_at = None      # wall time tool B was freed
        self.settle_reason = None
        self.handed_over = 0        # tool B codes moved to tool A's local events at settle

    # ── lifecycle ──────────────────────────────────────────────────────────────
    @property
    def uses_profiling(self):
        """True while a process-wide event (beyond PY_UNWIND) is armed."""
        return self.mode in (DISCOVER, INCREMENTAL) or self._watch_prefix is not None

    @property
    def skipped(self):
        out = []
        for targets in self._target_sets:
            out.extend(targets.skipped)
        return out

    def start(self):
        mon = self._mon
        if mon is None:
            raise RuntimeError("sys.monitoring is unavailable (Python < 3.12)")
        tool = _acquire_tool_id(mon)
        tool2 = None
        try:
            if self.mode == INCREMENTAL:
                try:
                    tool2 = _acquire_tool_id(mon)
                except RuntimeError:
                    raise RuntimeError("incremental mode needs two free sys.monitoring tool ids")
            if tool in _USED_IDS or tool2 in _USED_IDS:
                mon.restart_events()        # our earlier DISABLEs on these ids persist
            ev = mon.events
            mon.register_callback(tool, ev.PY_START, self._on_start)
            mon.register_callback(tool, ev.PY_RETURN, self._on_return)
            mon.register_callback(tool, ev.PY_UNWIND, self._on_unwind)
            mon.register_callback(tool, ev.CALL, self._on_call)
            mon.register_callback(tool, ev.C_RETURN, self._on_c_return)
            mon.register_callback(tool, ev.C_RAISE, self._on_c_raise)
            mon.register_callback(tool, ev.RAISE, self._raise_callback())
            if tool2 is not None:
                mon.register_callback(tool2, ev.PY_START, self._on_implicit)
            self._tool = tool
            self._tool2 = tool2
            _USED_IDS.add(tool)
            if tool2 is not None:
                _USED_IDS.add(tool2)
            if self._watch_targets:
                self._arm_targets(self._watch_targets)
            self._set_global()
        except BaseException:
            self._tool = self._tool2 = None
            if tool2 is not None:
                self._release(tool2, local_events=False)
            self._release(tool)
            raise
        install_warning_capture(self)

    def stop(self):
        tool, tool2 = self._tool, self._tool2
        if tool is None:
            return
        self._tool = self._tool2 = None
        remove_warning_capture(self)
        if tool2 is not None:
            self._release(tool2, local_events=False)
        self._release(tool)
        self._pending.clear()
        self._cstack.clear()
        self._lastcall.clear()
        self._craise.clear()
        self._callname.clear()
        self._warned.clear()

    def _release(self, tool, local_events=True):
        mon = self._mon
        try:
            mon.set_events(tool, 0)
        except Exception:
            pass
        local = []
        if local_events:            # only tool A arms local events
            with self._local_lock:
                local = list(self._local.values())
                self._local = {}
        for code, _events in local:
            try:
                mon.set_local_events(tool, code, 0)
            except Exception:
                pass
        ev = mon.events
        for event in (ev.PY_START, ev.PY_RETURN, ev.PY_UNWIND, ev.CALL, ev.C_RETURN, ev.C_RAISE,
                      ev.RAISE):
            try:
                mon.register_callback(tool, event, None)
            except Exception:
                pass
        for finder in self._finders:
            try:
                sys.meta_path.remove(finder)
            except ValueError:
                pass
        self._finders = []
        try:
            mon.free_tool_id(tool)
        except Exception:
            pass

    # ── watch set ──────────────────────────────────────────────────────────────
    def enter_watch(self, extra_targets=()):
        """Switch a running discover backend to watch on observed ∪ the impact targets
        (those given at construction, plus ``extra_targets``).

        Returns False if already watching (idempotent). Safe to call from any thread."""
        if self._tool is None:
            return False
        with self._obs_lock:
            if self.mode == WATCH:
                return False
            self.mode = WATCH       # a code recorded from now on is armed for watch itself
            codes = list(self.observed.values())
        # Every observed / impact code joins the watch set BEFORE the decision cache is
        # dropped, so no concurrent call into one is misjudged as unwatched.
        for code in codes:
            self._watch_codes[id(code)] = code
        if extra_targets:
            self._arm_targets(extra_targets)
        self._callee = {}           # watch decisions differ from discover's: rebuilt lazily
        for code in codes:
            self._watch_code(code)
        self._set_global()          # only now: drop the global PY_START (unless prefixes)
        self.switched = True
        return True

    def _set_global(self):
        ev = self._mon.events
        # Compiled calls and binding failures (CALL + C_RETURN / C_RAISE + RAISE) are observed
        # in discover and incremental mode; watch mode keeps its narrow, local-events-only cost.
        calls = (ev.CALL | ev.C_RETURN | ev.C_RAISE | ev.RAISE
                 if self.mode in (DISCOVER, INCREMENTAL) else 0)
        if self.mode == INCREMENTAL:
            self._mon.set_events(self._tool, ev.PY_UNWIND | calls)
            self._mon.set_events(self._tool2, ev.PY_START)
            return
        start = self.mode == DISCOVER or self._watch_prefix is not None
        self._mon.set_events(self._tool, (ev.PY_START if start else 0) | ev.PY_UNWIND | calls)

    def _arm_targets(self, targets):
        symbols, prefixes = _partition_impact_targets(targets)
        batch = _CodeTargets(symbols, self._watch_resolved)
        self._target_sets.append(batch)
        batch.resolve_loaded()
        modules = batch.modules
        if modules:
            finder = _WrapFinder(batch, modules)
            sys.meta_path.insert(0, finder)
            self._finders.append(finder)
        if prefixes:
            # Only after the exact targets are in the watch set: with a prefix matcher an
            # unwatched code may be DISABLEd.
            old = list(self._watch_prefix.prefixes) if self._watch_prefix is not None else []
            self._watch_prefix = PrefixMatcher(old + list(prefixes), self._exclude)

    def _watch_resolved(self, codes, full):
        restart = False
        for code in codes:
            ent = self._callee.get(id(code))
            if ent is not None and ent[0] is code and ent[6] is not None:
                restart = True      # DISABLEd before it was known (outside the target, or
                                    # called by its module body before the import finished)
            self._impact_codes[id(code)] = code
            self._watch_code(code)
        if restart and self._tool is not None:
            try:
                self._mon.restart_events()  # a disabled location ignores new local events
            except Exception:
                pass

    def _watch_code(self, code):
        self._watch_codes[id(code)] = code
        self._callee.pop(id(code), None)
        ev = self._mon.events
        if self.mode == INCREMENTAL and self._b_active and self._b_owns(code):
            return      # tool B's global PY_START records it (any call path)
        if generator_marker(code) is not None:
            self._arm_local(code, ev.PY_START)          # recorded at start, nothing pending
        else:
            self._arm_local(code, ev.PY_START | ev.PY_RETURN)

    def _arm_local(self, code, events):
        tool = self._tool
        if tool is None:
            return
        with self._local_lock:
            cur = self._local.get(id(code))
            merged = events | (cur[1] if cur is not None else 0)
            if cur is not None and cur[1] == merged:
                return
            try:
                self._mon.set_local_events(tool, code, merged)
            except Exception:
                return
            self._local[id(code)] = (code, merged)

    def _on_new_code(self, code, ent):
        if self.mode == WATCH:
            self._watch_code(code)
        else:
            if self.mode == INCREMENTAL:
                self.last_armed_at = _monotonic()   # a new contact: not settled yet
                if not self._b_active and self._b_owns(code):
                    # Recorded by tool B while it was being freed: hand it over itself.
                    self._watch_code(code)
                    return
            if ent[5] is None:      # discover / incremental: arm the return to complete it
                self._arm_local(code, self._mon.events.PY_RETURN)

    # ── per-code decision ──────────────────────────────────────────────────────
    def _decide(self, code, frame):
        callee = self._callee
        if len(callee) >= _CODE_CACHE_MAX:
            callee.clear()
        module = frame.f_globals.get("__name__", "") or ""
        body = code.co_name == "<module>"
        prefix = self._watch_prefix
        watched = id(code) in self._watch_codes or (
            prefix is not None and not never_target(module)
            and bool(prefix.matches(module, frame)))
        disable = None
        if self.mode == INCREMENTAL:
            # Only armed codes reach tool A's PY_START; never DISABLE (it would block arming).
            hit = id(code) in self._watch_codes and not body
        elif self.mode == WATCH:
            hit = watched and not body
            if not hit and prefix is not None:
                disable = self._mon.DISABLE   # only a global PY_START reaches unwatched code
        else:
            hit = not body and (watched or (self._target_module(module, frame)
                                            and code.co_name != "__del__"))
            if not hit:
                disable = self._mon.DISABLE
        # [code, in_target, module, is_module_body, identity, generator marker, DISABLE|None]
        ent = [code, hit, module, body, None, generator_marker(code), disable]
        callee[id(code)] = ent
        return ent

    # ── callbacks (must never raise into the program) ──────────────────────────
    def _on_start(self, code, offset):
        try:
            ent = self._callee.get(id(code))
            frame = _getframe(1)
            if frame.f_code is not code:
                frame = _frame_of(code)
                if frame is None:
                    return None
            if ent is None:
                ent = self._decide(code, frame)
            if not ent[1]:
                return ent[6]
            if self.near is not None:          # the near-side check, inlined (hot path)
                back = frame.f_back
                if back is None:
                    return None
                near = self._near_codes.get(id(back.f_code))
                if not (near[1] if near is not None else self._code_is_near(back)):
                    return None
            if self._inh_codes and id(code) in self._inh_codes:
                # stdlib code armed for a dependency class's inherited method: only a call on
                # a receiver of such a class is a contact, named after that class
                sym = self._inherited_sym(code, frame.f_locals.get(code.co_varnames[0]))
                if sym is None:
                    return None
                self._record(frame, ent, sym)
                return None
            if self._renames:
                cn = self._callname.pop(stack_key(), None)
                if cn is not None and cn[0] is code:
                    self._record(frame, ent, cn[1])
                    return None
            self._record(frame, ent)
        except Exception:
            pass
        return None

    def _on_return(self, code, offset, retval):
        try:
            if self._pending:
                frame = _getframe(1)
                if frame.f_code is not code:
                    frame = _frame_of(code)
                info = self._pending.pop(id(frame), None)
                if info is not None and info[0] is code:
                    self._finish(info, OUTCOME_RETURN, retval,
                                 self._take_warnings(frame, code) if self._warned else None)
        except Exception:
            pass
        return None     # never DISABLE: armed only on codes we record

    def _on_unwind(self, code, offset, exc):
        try:
            if self._pending:
                frame = _getframe(1)
                if frame.f_code is not code:
                    frame = _frame_of(code)
                info = self._pending.pop(id(frame), None)
                if info is not None and info[0] is code:
                    self._finish(info, OUTCOME_RAISE, raised_label(exc),
                                 self._take_warnings(frame, code) if self._warned else None)
        except Exception:
            pass
        return None     # PY_UNWIND cannot be disabled

    # ── incremental: code ownership ────────────────────────────────────────────
    def _b_owns(self, code, g=None, module=None):
        """True if tool B (global PY_START) records calls into ``code``: generator-likes,
        implicitly-run dunders, ``property`` accessors, ``module.*`` impact prefixes.
        Decided once per code object, under a lock, so both tools always agree."""
        ent = self._kinds.get(id(code))
        if ent is not None and ent[0] is code:
            return ent[1]
        owned = generator_marker(code) is not None or _implicit_name(code.co_name)
        prefix = self._watch_prefix
        if not owned and module is not None and prefix is not None:
            owned = bool(prefix.matches(module, None))
        if not owned and g is not None:
            try:
                owned = _is_accessor(code, g) or _is_meta_ctor(code, g)
            except Exception:
                owned = False
        with self._kind_lock:
            ent = self._kinds.get(id(code))
            if ent is not None and ent[0] is code:
                return ent[1]
            if len(self._kinds) >= _CODE_CACHE_MAX:
                self._kinds.clear()
            self._kinds[id(code)] = (code, owned)
        return owned

    # ── incremental: settle (tool B off) ───────────────────────────────────────
    def settle(self, reason="quiet"):
        """Free tool B once the watch set stopped growing: its global PY_START keeps firing on
        dependency dunders/properties/generators that only dependencies call. Every code B
        recorded (and every impact target it owned) is handed to tool A's local events, as
        in watch mode, before B is freed; from then on A arms any dependency code a CALL in
        our code reaches, dunders and generators included. Tool A's CALL stays on.

        Recall caveat: a protocol, dunder or property contact FIRST used after the settle is
        not seen (it passes no CALL instruction in our code). Returns True if this settled.
        Not done with ``module.*`` impact targets (they need B's global PY_START)."""
        mon = self._mon
        with self._arm_lock:
            tool2 = self._tool2
            if self.mode != INCREMENTAL or tool2 is None or self._tool is None:
                return False
            if self._watch_prefix is not None:
                self.note = "settle-skipped: module.* impact targets need the second tool"
                return False
            self._dedupe_until = float("inf")   # both tools may deliver a start meanwhile
            self._b_active = False              # A now owns every code (arming and CALLs)
            with self._obs_lock:
                codes = list(self.observed.values())
            codes.extend(self._impact_codes.values())
            handed = 0
            for code in codes:
                if self._b_owns(code):
                    self._watch_code(code)      # local PY_START (+ PY_RETURN), like watch mode
                    handed += 1
            try:
                mon.set_events(tool2, 0)
            except Exception:
                pass
            self._tool2 = None
            self._release(tool2, local_events=False)
            self._dedupe_until = _monotonic() + _SETTLE_GRACE
            self.handed_over = handed
            self.settled_at = time.time()
            self.settle_reason = reason
        return True

    # ── incremental: tool A's CALL callback ────────────────────────────────────
    def _on_call_arm(self, code, offset, callable_, arg0):
        try:
            if self.near is not None:
                nent = self._near_codes.get(id(code))
                if nent is None:
                    frame = _getframe(1)
                    if frame.f_code is not code:
                        frame = _frame_of(code)
                        if frame is None:
                            return None
                    near = self._code_is_near(frame)
                else:
                    near = nent[1]
                if not near:
                    return self._mon.DISABLE    # not our code: this site never fires again
            key = (id(code), offset)
            site = self._sites.get(key)
            target = _call_target(callable_)
            if site is not None and site[0] is code:
                if not site[3]:
                    same = site[1] is target or (type(target) is tuple and site[1] == target)
                    if same:
                        site[2] += 1
                        if site[2] >= _SITE_STABLE:
                            return self._mon.DISABLE    # monomorphic and settled
                        return None
                    site[3] = True                      # polymorphic: never disabled
                self._discover_callable(callable_, arg0)
                return None
            if len(self._sites) >= _SITES_MAX:
                self._sites.clear()
            self._discover_callable(callable_, arg0)
            self._sites[key] = [code, target, 1, False]
        except Exception:
            pass
        return None

    # ── compiled calls and binding failures (CALL / C_RETURN / C_RAISE / RAISE) ──
    def _on_call(self, code, offset, callable_, arg0):
        """Every call made from our code: the arming of incremental mode, then the compiled-call
        and binding-failure observation. A site is DISABLEd only when neither needs it again."""
        disable = None
        if self.mode == INCREMENTAL:
            disable = self._on_call_arm(code, offset, callable_, arg0)
        try:
            seen = self._observe_call(code, offset, callable_, arg0)
        except Exception:
            seen = None
        if seen:
            return None
        if self.mode == INCREMENTAL:
            if disable is not None:
                self._quiet_site(code, offset)
            return disable
        return self._mon.DISABLE if seen is False else None

    def _quiet_site(self, code, offset):
        key = (id(code), offset)
        site = self._csites.get(key)
        if site is None or site[0] is not code:
            self._csites[key] = [code, _SITE_STABLE, None]
        else:
            site[1] = _SITE_STABLE

    def _is_near(self, code):
        nent = self._near_codes.get(id(code))
        if nent is not None and nent[0] is code:
            return nent[1]
        frame = _frame_of(code)
        if frame is None:
            return None
        return self._code_is_near(frame)

    def _dep_function(self, func):
        module = func.__globals__.get("__name__", "") or ""
        return not never_target(module) and func.__code__.co_name != "__del__" and (
            id(func.__code__) in self._impact_codes or self._target_module(module, None))

    def _call_kind(self, callable_):
        """(object, kind, sym, callee) of what a CALL reaches, decided once per object."""
        obj = callable_
        if type(obj) is _METHOD:
            obj = obj.__func__
        ent = self._ckinds.get(id(obj))
        if ent is not None and ent[0] is obj:
            return ent
        kind, sym, callee, rename = _K_NONE, None, None, None
        try:
            t = type(obj)
            if t is functools.partial:
                inner = obj.func
                if type(inner) is _METHOD:
                    inner = inner.__func__
                if type(inner) is _FUNCTION and self._dep_function(inner):
                    kind, callee = _K_PY, inner
            elif t is _FUNCTION:
                if self._dep_function(obj):
                    kind, callee = _K_PY, obj
                    rename = self._declared_name(obj)
            elif is_compiled_callable(obj):
                sym = self._c_sym(obj)
                if sym is not None:
                    kind = _K_C
            elif issubclass(t, type):
                pairs = self._ctor_codes(obj)
                if pairs:
                    func = pairs[-1][1]         # __init__ (it follows __new__ in the list)
                    if self._dep_function(func):
                        kind, callee = _K_PY, func
                elif not (obj.__flags__ & _HEAPTYPE):
                    module = getattr(obj, "__module__", None)
                    if (isinstance(module, str) and module and module != "builtins"
                            and not never_target(module) and self._in_target(module)):
                        sym, kind = module + "." + obj.__qualname__, _K_C
            else:
                call = _mro_value(t, "__call__")
                if type(call) is _FUNCTION and self._dep_function(call):
                    kind, callee = _K_PY, call
        except Exception:
            kind, sym, callee, rename = _K_NONE, None, None, None
        if len(self._ckinds) >= _CODE_CACHE_MAX:
            self._ckinds.clear()
        ent = (obj, kind, sym, callee, rename)
        self._ckinds[id(obj)] = ent
        return ent

    @staticmethod
    def _declared_name(func):
        """``module.qualname`` the callable declares when its code is a closure body
        (``<locals>``) and the callable says it is something else: a ``functools.wraps``
        wrapper is its wrapped function, not ``decorator.<locals>.wrapper``."""
        code_qual = getattr(func.__code__, "co_qualname", None)
        qual = getattr(func, "__qualname__", None)
        module = getattr(func, "__module__", None)
        if (code_qual and "<locals>" in code_qual and isinstance(qual, str)
                and "<locals>" not in qual and qual != code_qual
                and isinstance(module, str) and module):
            return module + "." + qual
        return None

    def _observe_call(self, code, offset, callable_, arg0):
        """True: this site must keep firing (a compiled dependency callable or a traced Python
        one); False: it never will matter (not our code, or settled on nothing of ours);
        None: nothing of ours yet."""
        near = True
        if self.near is not None:
            near = self._is_near(code)
            if near is None:
                return None
        if not near:
            return False
        ent = self._call_kind(callable_)
        kind = ent[1]
        key = (id(code), offset)
        tid = stack_key()
        csym = None
        if kind == _K_NONE:
            csym = self._inherited_call(callable_, arg0)
        if kind != _K_C and csym is None:
            # Nothing compiled to record. A traced Python callable is only watched for a
            # binding failure, so a site that reached the same callee twice settles (DISABLE,
            # like incremental mode's arming): a bind failing on a later call is not seen.
            if kind == _K_PY:
                if len(self._lastcall) > 4096:
                    self._lastcall.clear()  # one slot per greenlet that ever called
                self._lastcall[tid] = (code, offset, ent[3])
                if ent[4] is not None:
                    if len(self._callname) > 4096:
                        self._callname.clear()
                    self._callname[tid] = (ent[3].__code__, ent[4])
                    self._renames = True
            if kind == _K_PY and ent[4] is not None:
                # A callee that declares a name of its own (a decorator wrapper: its code is
                # shared, or generated and swapped in on first call, as networkx's argmap
                # does) is named by this event, so its site never settles: once CALL stopped
                # firing, later calls would take the code's name (locust: 2 calls of 6,572
                # named ``Process.memory_info``, the rest the wrapper's closure name).
                return True
            site = self._csites.get(key)
            if site is None or site[0] is not code:
                if len(self._csites) >= _SITES_MAX:
                    self._csites.clear()
                self._csites[key] = [code, 1, ent[0]]
                return None
            if site[2] is ent[0]:
                site[1] += 1
                return False if site[1] >= _SITE_STABLE else None
            site[1], site[2] = 1, ent[0]
            return None
        site = self._csites.get(key)
        if site is not None:
            site[1], site[2] = 0, None       # a compiled callable here: this site never settles
        frame = _getframe(2)
        if frame.f_code is not code:
            frame = _frame_of(code)
            if frame is None:
                return True
        n = self._ccount.get(key, 0)
        if n >= _C_SITE_MAX:
            return False        # a hot loop is witnessed, not replayed: this site stops here
        if n == 0 and len(self._ccount) >= _CODE_CACHE_MAX:
            self._ccount.clear()
        self._ccount[key] = n + 1
        info = self._c_start(frame, csym or ent[2],
                             NO_ARG if arg0 is self._mon.MISSING else arg0)
        stack = self._cstack.get(tid)
        if stack is None:
            stack = self._cstack[tid] = []
        elif len(stack) >= 256:
            del stack[:]
        stack.append((callable_, code, offset, info))
        return True

    def _on_c_return(self, code, offset, callable_, arg0):
        try:
            key = stack_key()
            stack = self._cstack.get(key)
            if stack:
                top = stack[-1]
                if same_c_callable(top[0], callable_) and top[1] is code and top[2] == offset:
                    stack.pop()
                    if not stack:
                        del self._cstack[key]       # greenlets come and go: no empty leftovers
                    self._finish(top[3], OUTCOME_RETURN, C_RETURN_UNOBSERVED)
                    return None
            return None     # C_RETURN / C_RAISE cannot be DISABLEd: they stop with the site's CALL
        except Exception:
            return None

    def _on_c_raise(self, code, offset, callable_, arg0):
        try:
            tid = stack_key()
            stale = self._craise.pop(tid, None)
            if stale is not None:           # its RAISE never came (StopIteration): untyped
                self._finish(stale[0], OUTCOME_RAISE, UNTYPED_RAISE)
            stack = self._cstack.get(tid)
            if stack:
                top = stack[-1]
                if same_c_callable(top[0], callable_) and top[1] is code and top[2] == offset:
                    stack.pop()
                    if not stack:
                        del self._cstack[tid]
                    self._craise[tid] = (top[3], code, offset)   # RAISE types it
        except Exception:
            pass
        return None

    def _raise_callback(self):
        """The RAISE callback, a closure: it runs on every raise in the process (it cannot be
        DISABLEd), so the common case, a non-TypeError with nothing awaiting a type, is one
        call and one test, no attribute lookups."""
        craise = self._craise
        slow = self._on_raise
        te = TypeError

        def on_raise(code, offset, exc):
            if craise or isinstance(exc, te):
                slow(code, offset, exc)

        return on_raise

    def _on_raise(self, code, offset, exc):
        """A raise in our code: it types a compiled call's raise (``C_RAISE`` has no exception),
        and a TypeError at a call to a traced Python callable that left no frame behind
        (``tb_next`` is None) is that call failing to bind its arguments. RAISE cannot be
        DISABLEd and is global, so the common case (not a TypeError, nothing awaiting a type)
        is two checks."""
        try:
            if not self._craise and not isinstance(exc, TypeError):
                return None
            if self.near is not None and not self._is_near(code):
                return None
            tid = stack_key()
            pending = self._craise.get(tid)
            if pending is not None:
                del self._craise[tid]
                if pending[1] is code and pending[2] == offset:
                    self._finish(pending[0], OUTCOME_RAISE, raised_label(exc))
                    return None
                self._finish(pending[0], OUTCOME_RAISE, UNTYPED_RAISE)
            if isinstance(exc, TypeError):
                last = self._lastcall.get(tid)
                if last is not None and last[0] is code and last[1] == offset:
                    del self._lastcall[tid]
                    tb = exc.__traceback__
                    if tb is not None and tb.tb_next is None:
                        frame = _getframe(1)
                        if frame.f_code is not code:
                            frame = _frame_of(code)
                        if frame is not None:
                            self._record_binding(frame, last[2])
        except Exception:
            pass
        return None

    def _inherited_call(self, callable_, arg0):
        """FU6: a call from our code to stdlib (or other non-target) code whose receiver is an
        instance of a target class. A Python method is armed (its start is recorded, named
        after the receiver's class) and None returned; a builtin method descriptor on a
        dependency subclass of a builtin returns its symbol, to be recorded like a C call."""
        t = type(callable_)
        recv = arg0
        if t is _METHOD:
            func = callable_.__func__
            recv = callable_.__self__
            t = type(func)
        else:
            func = callable_
        if t is _FUNCTION:
            code = func.__code__
            if (recv is self._mon.MISSING or code.co_argcount < 1
                    or code.co_varnames[0] not in ("self", "cls")
                    or id(code) in self._inh_codes):
                return None
            if self._dep_function(func) or self._inherited_sym(code, recv) is None:
                return None
            self._arm_inherited(code)
            return None
        if recv is self._mon.MISSING or not hasattr(func, "__objclass__"):
            return None
        return self._inherited_descriptor_sym(func, recv)

    def _arm_inherited(self, code):
        with self._arm_lock:
            if id(code) in self._inh_codes:
                return
            self._inh_codes[id(code)] = code
            self._impact_codes[id(code)] = code     # tool B (incremental) records it too
            ent = self._callee.get(id(code))
            restart = ent is not None and ent[0] is code and len(ent) > 6 and ent[6] is not None
            self._watch_code(code)
            if restart:
                try:
                    self._mon.restart_events()      # a DISABLEd location ignores local events
                except Exception:
                    pass

    def _record_binding(self, frame, func):
        ci = self._identity.code_identity(func.__code__, func.__globals__.get("__name__", ""),
                                          func.__globals__)[0]
        self._binding_failure(frame, ci.sym, ci.symq)

    def _discover_callable(self, callable_, arg0):
        pairs = self._resolve(callable_)
        if pairs is None:
            # A C callable (``map``, ``filter``, ``functools.reduce`` ...): its first
            # argument is what it will call.
            t = type(arg0)
            if t is _FUNCTION or t is _METHOD or t is functools.partial:
                pairs = self._resolve(arg0)
        if pairs:
            for code, func in pairs:
                self._arm_callee(code, func)

    def _resolve(self, obj):
        """[(code, function)] a call to ``obj`` starts in, or None for a C callable."""
        for _ in range(8):
            t = type(obj)
            if t is _FUNCTION:
                return [(obj.__code__, obj)]
            if t is _METHOD or t is staticmethod or t is classmethod:
                obj = obj.__func__
                continue
            if t is functools.partial:
                obj = obj.func
                continue
            if issubclass(t, type):
                return self._ctor_codes(obj)
            call = _mro_value(t, "__call__")
            if type(call) is _FUNCTION:
                return [(call.__code__, call)]
            return None
        return None

    def _ctor_codes(self, cls):
        ent = self._ctors.get(id(cls))
        if ent is not None and ent[0] is cls:
            return ent[1]
        pairs = []
        meta_call = _mro_value(type(cls), "__call__")
        if type(meta_call) is _FUNCTION:
            pairs.append((meta_call.__code__, meta_call))
        for name in ("__new__", "__init__"):
            value = _mro_value(cls, name)
            if issubclass(type(value), (staticmethod, classmethod)):
                value = value.__func__
            if type(value) is _FUNCTION:
                pairs.append((value.__code__, value))
        if len(self._ctors) >= _CODE_CACHE_MAX:
            self._ctors.clear()
        self._ctors[id(cls)] = (cls, pairs)     # holds the class: its id cannot recycle
        return pairs

    def _arm_callee(self, code, func):
        if id(code) in self._watch_codes:
            return
        ent = self._deps.get(id(code))
        if ent is None or ent[0] is not code:
            module = func.__globals__.get("__name__", "") or ""
            dep = not never_target(module) and code.co_name != "__del__" and (
                id(code) in self._impact_codes or self._target_module(module, None))
            if len(self._deps) >= _CODE_CACHE_MAX:
                self._deps.clear()
            ent = (code, dep, module)
            self._deps[id(code)] = ent
        if not ent[1] or (self._b_active and self._b_owns(code, func.__globals__, ent[2])):
            return
        with self._arm_lock:            # cold path: once per newly reached dependency code
            if id(code) in self._watch_codes:
                return
            self._watch_code(code)
            self.armed += 1
            self.last_armed_at = _monotonic()

    # ── incremental: tool B's PY_START callback ────────────────────────────────
    def _on_implicit(self, code, offset):
        try:
            ent = self._implicit.get(id(code))
            frame = _getframe(1)
            if frame.f_code is not code:
                frame = _frame_of(code)
                if frame is None:
                    return None
            if ent is None or ent[0] is not code:
                ent = self._decide_implicit(code, frame)
            if not ent[1]:
                return ent[6]
            if self.near is not None:
                back = frame.f_back
                if back is None:
                    return None
                near = self._near_codes.get(id(back.f_code))
                if not (near[1] if near is not None else self._code_is_near(back)):
                    return None
            self._record(frame, ent)
        except Exception:
            pass
        return None

    def _decide_implicit(self, code, frame):
        g = frame.f_globals
        module = g.get("__name__", "") or ""
        body = code.co_name == "<module>"
        hit = False
        if (not body and not never_target(module) and code.co_name != "__del__"
                and self._b_owns(code, g, module)):
            prefix = self._watch_prefix
            hit = (id(code) in self._impact_codes
                   or (prefix is not None and bool(prefix.matches(module, frame)))
                   or self._target_module(module, frame))
        if len(self._implicit) >= _CODE_CACHE_MAX:
            self._implicit.clear()
        ent = [code, hit, module, body, None, generator_marker(code),
               None if hit else self._mon.DISABLE]
        self._implicit[id(code)] = ent
        return ent

