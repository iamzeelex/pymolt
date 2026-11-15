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

Impact targets (``watch_targets``) are armed in BOTH modes: in discover they are recorded on
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
import threading
import types

from ._backend import (
    _CODE_CACHE_MAX,
    _SELF_MODULES,
    OUTCOME_RAISE,
    OUTCOME_RETURN,
    FrameBackend,
    PrefixMatcher,
    generator_marker,
)
from ._identity import _wrapped_of
from ._normalize import typename
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
        self._local_lock = threading.Lock()
        self._target_sets = []      # _CodeTargets, one per armed batch of impact targets
        self._finders = []
        self._tool = None
        self._mon = getattr(sys, "monitoring", None)
        self.switched = False

    # ── lifecycle ──────────────────────────────────────────────────────────────
    @property
    def uses_profiling(self):
        """True while a process-wide event (beyond PY_UNWIND) is armed."""
        return self.mode == DISCOVER or self._watch_prefix is not None

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
        try:
            if tool in _USED_IDS:
                mon.restart_events()        # our earlier DISABLEs on this id persist
            ev = mon.events
            mon.register_callback(tool, ev.PY_START, self._on_start)
            mon.register_callback(tool, ev.PY_RETURN, self._on_return)
            mon.register_callback(tool, ev.PY_UNWIND, self._on_unwind)
            self._tool = tool
            _USED_IDS.add(tool)
            if self._watch_targets:
                self._arm_targets(self._watch_targets)
            self._set_global()
        except BaseException:
            self._tool = None
            self._release(tool)
            raise

    def stop(self):
        tool = self._tool
        if tool is None:
            return
        self._tool = None
        self._release(tool)
        self._pending.clear()

    def _release(self, tool):
        mon = self._mon
        try:
            mon.set_events(tool, 0)
        except Exception:
            pass
        with self._local_lock:
            local = list(self._local.values())
            self._local = {}
        for code, _events in local:
            try:
                mon.set_local_events(tool, code, 0)
            except Exception:
                pass
        ev = mon.events
        for event in (ev.PY_START, ev.PY_RETURN, ev.PY_UNWIND):
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
        start = self.mode == DISCOVER or self._watch_prefix is not None
        self._mon.set_events(self._tool, (ev.PY_START if start else 0) | ev.PY_UNWIND)

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
        elif ent[5] is None:
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
            prefix is not None and module not in _SELF_MODULES
            and bool(prefix.matches(module, frame)))
        disable = None
        if self.mode == WATCH:
            hit = watched and not body
            if not hit and prefix is not None:
                disable = self._mon.DISABLE   # only a global PY_START reaches unwatched code
        else:
            hit = not body and (watched or self._target_module(module, frame))
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
                    self._finish(info, OUTCOME_RETURN, retval)
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
                    self._finish(info, OUTCOME_RAISE, raised_label(exc))
        except Exception:
            pass
        return None     # PY_UNWIND cannot be disabled
