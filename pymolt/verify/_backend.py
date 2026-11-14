"""Trace-backend abstraction + version-driven selection.

INJECTED RUNTIME — pure stdlib, Python 3.6+ (see ``_normalize`` header for the rules).
One interface for observing the call boundary, with implementations underneath:

  - ``SetProfileBackend`` : ``sys.setprofile``, the whole 3.6-3.11 band, pure Python.
  - ``MonitoringBackend`` : ``sys.monitoring`` (PEP 669), 3.12+. Extension point —
                            deliberately stubbed (no target version to validate against),
                            ``select_backend`` delegates to setprofile for now.

The backend knows nothing about the on-disk format: it calls ``sink.on_call`` /
``sink.on_return`` / ``sink.on_raise``. That decouples *how we observe* from *where we write*.

Raise capture, honestly (verified empirically against CPython 3.12):
``sys.setprofile`` does NOT deliver a Python ``exception`` event. A Python function that
raises is reported as a ``return`` with ``arg=None`` — indistinguishable from ``return None``
— and ``sys.exc_info()`` is already clear at that point. The only runtime raise signal
setprofile exposes is ``c_exception`` (a C callable raised). Consequently Python-side
exceptions surface as *result* changes, not *raise* changes. This is non-blocking for a gate
(still BEHAVIOR_CHANGED) and matches the spec's Known Gaps. The Sink API still accepts
``on_raise`` so artifacts produced by other means carry raises uniformly.
"""
import inspect
import os
import sys
import threading

from ._normalize import AMBIGUOUS_OUTCOME

# A frozenset, not the tuple CPython exposes: a tuple scan on every event cost ~25% of the
# hook's time.
_BUILTIN_MODULES = frozenset(sys.builtin_module_names)

# ── target matching ──────────────────────────────────────────────────────────
# A target is "what counts as a dependency boundary". Three shapes, all driven from
# PYMOLT_TRACE_TARGET (the CLI / env): one prefix ("flask"), several ("flask,werkzeug"),
# or ALL installed dependencies ("all" / "*"). An optional exclude set drops your own
# package(s). Matchers are pure stdlib so they run inside an old target interpreter.

def _site_roots():
    """Directories where installed third-party distributions live (site/dist-packages).

    Stdlib lives elsewhere and editable/source-run app code lives in the project tree, so
    "file under one of these roots" cleanly means "an installed dependency"."""
    roots = set()
    try:
        import site
        try:
            for d in site.getsitepackages():
                roots.add(d)
        except Exception:
            pass
        try:
            roots.add(site.getusersitepackages())
        except Exception:
            pass
    except Exception:
        pass
    try:
        import sysconfig
        paths = sysconfig.get_paths()
        for k in ("purelib", "platlib"):
            if paths.get(k):
                roots.add(paths[k])
    except Exception:
        pass
    return tuple(os.path.normpath(r) + os.sep for r in roots if r)


def _stdlib_roots():
    """Directories where the standard library lives (to exclude stdlib from 'our code')."""
    roots = set()
    try:
        import sysconfig
        paths = sysconfig.get_paths()
        for k in ("stdlib", "platstdlib"):
            if paths.get(k):
                roots.add(paths[k])
    except Exception:
        pass
    return tuple(os.path.normpath(r) + os.sep for r in roots if r)


class PrefixMatcher(object):
    """Match modules under one or more explicit top-level prefixes (minus excludes)."""

    def __init__(self, prefixes, exclude=()):
        self.prefixes = tuple(p for p in prefixes if p)
        self.exclude = tuple(exclude)

    def matches(self, module, frame=None):
        if not module:
            return False
        if module.split(".", 1)[0] in self.exclude:
            return False
        for p in self.prefixes:
            if module == p or module.startswith(p + "."):
                return True
        return False


def _module_file(module):
    """Where an imported module lives — for callables that carry no frame.

    A C function has no Python frame to read ``co_filename`` from, so the only
    way to place it is the module object it came from.
    """
    mod = sys.modules.get(module)
    return getattr(mod, "__file__", "") or ""


class SitePackagesMatcher(object):
    """Match ANY installed dependency: module whose source file is under site-packages.

    Excludes stdlib (not under site-packages) and an editable/source-run app (its files are
    in the project tree, not site-packages) for free; ``exclude`` drops named top-levels too.
    Decisions are cached per module name to keep the hot path to a dict lookup.
    """

    def __init__(self, exclude=()):
        self.exclude = tuple(exclude)
        self._roots = _site_roots()
        self._cache = {}

    def matches(self, module, frame=None):
        if not module or not self._roots:
            return False
        top = module.split(".", 1)[0]
        if top in self.exclude or top in _BUILTIN_MODULES:
            return False  # C builtins (builtins, posix, ...) are never a dependency
        cached = self._cache.get(module)
        if cached is not None:
            return cached
        # Frameless callers (C functions) fall back to the module's own file.
        filename = frame.f_code.co_filename if frame is not None else _module_file(module)
        ok = bool(filename) and filename.startswith(self._roots)
        self._cache[module] = ok
        return ok


def _split(value):
    """Normalize a comma-string / sequence / None into a tuple of non-empty prefixes."""
    if not value:
        return ()
    if isinstance(value, str):
        return tuple(p.strip() for p in value.split(",") if p.strip())
    return tuple(value)


def build_matcher(target, exclude=()):
    """Build a matcher from a target spec: a matcher, a list, or a string
    (``"all"``/``"*"`` -> all deps; ``"a,b"`` -> several prefixes; ``"a"`` -> one)."""
    if hasattr(target, "matches"):
        return target
    if isinstance(target, (list, tuple, set)):
        return PrefixMatcher(list(target), exclude)
    spec = (target or "").strip()
    if spec in ("*", "all", "ALL"):
        return SitePackagesMatcher(exclude)
    if "," in spec:
        return PrefixMatcher([p.strip() for p in spec.split(",") if p.strip()], exclude)
    return PrefixMatcher([spec], exclude)


class AppMatcher(object):
    """The NEAR side of the boundary: *our* code (the project being migrated).

    The boundary we record is "our code -> dependency"; internal dependency<->dependency
    traffic is NOT a contract we care about. This identifies the near side:

      - with an explicit ``source`` (e.g. "flasgger") -> caller top-level under that prefix;
      - otherwise by elimination -> a caller that is NOT an installed dependency
        (not under site-packages) and NOT stdlib/builtin. This auto-detects an
        editable/source-run app without naming it.
    """

    def __init__(self, source=()):
        self.source = tuple(source)
        self._dep_roots = _site_roots() + _stdlib_roots()
        self._cache = {}

    def matches(self, module, frame=None):
        if not module:
            return False
        if self.source:
            return any(module == p or module.startswith(p + ".") for p in self.source)
        top = module.split(".", 1)[0]
        if top in _BUILTIN_MODULES:
            return False
        if frame is None:
            return False
        cached = self._cache.get(module)
        if cached is not None:
            return cached
        filename = frame.f_code.co_filename or ""
        # "our code" = a real file that is NOT under site-packages or the stdlib.
        # `<frozen importlib._bootstrap>` and friends are the interpreter's own
        # bootstrap: they have no real file, so the path test above cannot reject
        # them, and left alone they masquerade as the project's code — which made
        # every C call the import machinery happens to make look like a contact.
        ok = (bool(filename) and not filename.startswith("<frozen ")
              and not filename.startswith(self._dep_roots))
        self._cache[module] = ok
        return ok


class Sink(object):
    """Structural receiver interface. A plain base class (not ``typing.Protocol``,
    which is 3.8+) for 3.6/3.7 reach. Implementations provide the three hooks."""

    def on_call(self, qualname, inputs, site=None):
        raise NotImplementedError

    def on_return(self, qualname, inputs, result_obj, site=None):
        raise NotImplementedError

    def on_raise(self, qualname, inputs, exc_typename, site=None):
        raise NotImplementedError


class TraceBackend(object):
    """Base interface. ``start()``/``stop()`` are symmetric; re-entrancy is the caller's
    responsibility."""

    name = "base"

    def __init__(self, target, sink, exclude=(), source=None, include_internal=False):
        # far side (callee = dependency): prefix / "all"/"*" / comma list / sequence / matcher.
        self.matcher = build_matcher(target, exclude)
        # near side (caller = our code). None => boundary filtering off (record any call into a
        # dependency, including internal dep<->dep traffic) for a deep audit.
        self.near = None if include_internal else AppMatcher(_split(source))
        self.sink = sink

    def _in_target(self, module, frame=None):
        return self.matcher.matches(module, frame)

    def start(self):  # pragma: no cover - interface
        raise NotImplementedError

    def stop(self):  # pragma: no cover - interface
        raise NotImplementedError


# ── return-event outcome classification ──────────────────────────────────────
# setprofile reports a frame unwound by an exception as ``return`` with ``arg=None`` —
# indistinguishable from ``return None`` by the event alone. The frame's last executed
# opcode tells them apart: a real return stops on RETURN_*, a generator suspension on
# YIELD_*, anything else means the frame is unwinding. Verified on 2.7, 3.4-3.14
# (3.12+ ``co_code`` is de-instrumented). On <=3.7 a ``return`` inside try/finally or
# ``with`` stops on the same cleanup opcodes an exception propagating through the block
# does, so those cases are reported as ambiguous rather than guessed.
OUTCOME_RETURN = 0
OUTCOME_RAISE = 1
OUTCOME_AMBIGUOUS = 2

# The exception TYPE is not observable from a profile hook; the raise itself is.
UNTYPED_RAISE = "<unknown>"


def _opcode_sets():
    try:
        import dis
        opmap = dis.opmap
    except Exception:  # pragma: no cover - dis is stdlib everywhere
        return frozenset(), frozenset(), frozenset()
    ret = frozenset(opmap[n] for n in ("RETURN_VALUE", "RETURN_CONST", "RETURN_GENERATOR")
                    if n in opmap)
    yld = frozenset(opmap[n] for n in ("YIELD_VALUE", "YIELD_FROM") if n in opmap)
    amb = frozenset()
    if sys.version_info < (3, 8):
        amb = frozenset(opmap[n] for n in ("END_FINALLY", "WITH_CLEANUP", "WITH_CLEANUP_START",
                                           "WITH_CLEANUP_FINISH") if n in opmap)
    return ret, yld, amb


_RETURN_OPS, _YIELD_OPS, _AMBIGUOUS_OPS = _opcode_sets()


def frame_outcome(frame, arg):
    """Classify a profile ``return`` event as OUTCOME_RETURN / _RAISE / _AMBIGUOUS."""
    if arg is not None or not _RETURN_OPS:
        return OUTCOME_RETURN  # an unwinding frame always reports None
    try:
        lasti = frame.f_lasti
        if lasti < 0:
            return OUTCOME_AMBIGUOUS
        op = frame.f_code.co_code[lasti]
        if not isinstance(op, int):  # Python 2: co_code is a str
            op = ord(op)
    except Exception:
        return OUTCOME_AMBIGUOUS
    if op in _RETURN_OPS or op in _YIELD_OPS:
        return OUTCOME_RETURN
    if op in _AMBIGUOUS_OPS:
        return OUTCOME_AMBIGUOUS
    return OUTCOME_RAISE


# Per-code decision caches are keyed by ``id(code)`` and hold a reference to the code
# object itself, so an id can never be recycled while its entry is alive. They are bounded
# so dynamically generated code (templating engines, exec in a loop) cannot grow them
# without limit: past the cap the cache is simply rebuilt.
_CODE_CACHE_MAX = 200000


class SetProfileBackend(TraceBackend):
    """``sys.setprofile`` backend — events at call boundaries, not lines.

    Hook cheapness is load-bearing. Every decision that depends only on a code object
    (is the callee a dependency? is the caller our code?) is made once per code object and
    cached by ``id(code)``; the hot path is a dict lookup, with no per-event string work.
    Sampling is decided before any introspection (``getargvalues``/normalize). The whole
    hook body is guarded so an internal error can never propagate into the traced program.
    """

    name = "setprofile"

    def __init__(self, target, sink, exclude=(), source=None, include_internal=False):
        TraceBackend.__init__(self, target, sink, exclude, source, include_internal)
        self._prev = None
        self._pending = {}  # id(frame) -> (qualname, inputs, site)
        self._lock = threading.Lock()
        self.c_event_count = 0  # classified internal C traffic, intentionally not persisted
        self.skip_wrapped = False  # hybrid: skip calls a WrapBackend wrapper already records
        self._callee = {}  # id(code) -> (code, in_target, module, is_module_body)
        self._near_codes = {}  # id(code) -> (code, is_near)
        self._admit = getattr(sink, "admit", None)

    # ── per-code decisions ─────────────────────────────────────────────────────
    def _callee_entry(self, frame):
        code = frame.f_code
        ent = self._callee.get(id(code))
        if ent is None:
            if len(self._callee) >= _CODE_CACHE_MAX:
                self._callee.clear()
            module = frame.f_globals.get("__name__", "") or ""
            ent = (code, bool(self._in_target(module, frame)), module,
                   code.co_name == "<module>")
            self._callee[id(code)] = ent
        return ent

    def _code_is_near(self, frame):
        code = frame.f_code
        ent = self._near_codes.get(id(code))
        if ent is None:
            if len(self._near_codes) >= _CODE_CACHE_MAX:
                self._near_codes.clear()
            module = frame.f_globals.get("__name__", "") or ""
            ent = (code, bool(self.near.matches(module, frame)))
            self._near_codes[id(code)] = ent
        return ent[1]

    @staticmethod
    def _from_wrapper(caller_frame):
        """True if the caller is a WrapBackend wrapper (so the call is already recorded there)."""
        return (caller_frame is not None
                and caller_frame.f_code.co_name in ("wrapper", "init_wrapper")
                and caller_frame.f_globals.get("__name__", "").endswith("_wrap"))

    def _caller_is_near(self, caller_frame):
        """True if the *caller* is our code (near side). With boundary filtering off
        (``self.near is None``) there is no near constraint -> always True."""
        if self.near is None:
            return True
        if caller_frame is None:
            return False
        return self._code_is_near(caller_frame)

    def _c_caller_ok(self, frame):
        """Caller gate for C events (here `frame` IS the calling frame): near side in
        boundary mode, else the dependency itself (internal C traffic) for a full audit."""
        if self.near is not None:
            return self._code_is_near(frame)
        return self._callee_entry(frame)[1]

    def _hook(self, frame, event, arg):
        try:
            if event == "call":
                ent = self._callee_entry(frame)
                if not ent[1] or ent[3]:
                    return self._hook  # not a dependency, or a module body (import, not a contact)
                back = frame.f_back
                if self.skip_wrapped and self._from_wrapper(back):
                    return self._hook  # hybrid: a WrapBackend wrapper already recorded this call
                if not self._caller_is_near(back):  # caller = our code?
                    return self._hook  # internal dep<->dep traffic, not our boundary
                self._record_call(frame, ent[2])
            elif event == "return":
                if self._pending:
                    info = self._pending.pop(id(frame), None)
                    if info is not None:
                        self._emit_outcome(info, frame, arg)
            elif event == "c_call":
                # Routine C traffic. Explicitly classified so the hook never falls into the
                # Python-frame path or crashes; intentionally not persisted.
                if self._c_caller_ok(frame):
                    self.c_event_count += 1
            elif event == "c_exception":
                # The C-level raise signal: a C callable the dependency invoked raised. The
                # CALLEE has to be the traced dependency: gating only on the caller recorded
                # every C raise our code provoked anywhere (`_io.BufferedReader.seek` during an
                # import) as a contact of a dependency it never touched. Callables with no
                # `__module__` — most builtin method descriptors — cannot be attributed at
                # all, and an unattributable call is not a contract.
                if not self._c_caller_ok(frame):
                    return self._hook
                cmod = getattr(arg, "__module__", "") or ""
                if not cmod or cmod.split(".", 1)[0] in _BUILTIN_MODULES:
                    return self._hook
                if not self._in_target(cmod):
                    return self._hook
                self.sink.on_raise(self._c_qualname(arg), {"c_level": True}, "Exception",
                                   self._site(frame))
            # "c_return" carries nothing we persist: fall through.
        except Exception:
            pass  # the hook must never break the traced program
        return self._hook

    def _emit_outcome(self, info, frame, arg):
        qual, inputs, site = info
        outcome = frame_outcome(frame, arg)
        admitted = self._admit is not None
        if outcome == OUTCOME_RAISE:
            if admitted:
                self.sink.on_raise(qual, inputs, UNTYPED_RAISE, site, admitted=True)
            else:
                self.sink.on_raise(qual, inputs, UNTYPED_RAISE, site)
            return
        result = arg if outcome == OUTCOME_RETURN else AMBIGUOUS_OUTCOME
        if admitted:
            self.sink.on_return(qual, inputs, result, site, admitted=True)
        else:
            self.sink.on_return(qual, inputs, result, site)

    @staticmethod
    def _c_qualname(cfunc):
        mod = getattr(cfunc, "__module__", "") or ""
        name = getattr(cfunc, "__qualname__", None) or getattr(cfunc, "__name__", "c_call")
        return "{0}.{1}".format(mod, name) if mod else name

    @staticmethod
    def _site(caller_frame):
        """Where in *our* code the call originated: {file, line, func}. The caller frame's
        current line is exactly the call site (the line invoking the dependency)."""
        if caller_frame is None:
            return None
        code = caller_frame.f_code
        return {"file": code.co_filename, "line": caller_frame.f_lineno, "func": code.co_name}

    def _record_call(self, frame, module):
        # Sampling first: an unsampled call must not pay for getargvalues/normalize.
        if self._admit is not None and not self._admit():
            return
        from ._normalize import normalize  # local import keeps cold-path cost off the reject path
        code = frame.f_code
        qual = "{0}.{1}".format(module, code.co_name)
        try:
            ai = inspect.getargvalues(frame)
            bound = {n: normalize(ai.locals.get(n)) for n in ai.args}
            inputs = {"bound": bound}
            if ai.varargs:
                inputs["varargs"] = normalize(ai.locals.get(ai.varargs))
            if ai.keywords:
                inputs["kwargs"] = normalize(ai.locals.get(ai.keywords))
        except Exception:
            inputs = {"unbound": True}
        self._pending[id(frame)] = (qual, inputs, self._site(frame.f_back))

    def start(self):
        self._prev = sys.getprofile()
        sys.setprofile(self._hook)
        threading.setprofile(self._hook)  # catch threads spawned after start

    def stop(self):
        sys.setprofile(self._prev)
        threading.setprofile(None)
        self._pending.clear()


class MonitoringBackend(TraceBackend):
    """PEP 669 (``sys.monitoring``), Python 3.12+. Extension point — not implemented.

    Left deliberately stubbed: none of the target versions (3.6-3.11) has it, and writing
    speculative code with no target to validate against would violate the project's honesty
    rule. When implemented, subscribe to CALL / PY_RETURN / PY_RAISE events.
    """

    name = "monitoring"

    def start(self):  # pragma: no cover
        raise NotImplementedError(
            "monitoring backend not implemented; on 3.12+ subscribe to "
            "sys.monitoring events CALL / PY_RETURN / PY_RAISE."
        )

    def stop(self):  # pragma: no cover
        raise NotImplementedError


def select_backend(target, sink, prefer="auto", exclude=(), source=None, include_internal=False):
    """Choose a backend. Today the whole 3.6-3.11 band -> setprofile.

    ``target`` (far side / callee) is a prefix / "all" / comma-list / sequence / matcher;
    ``exclude`` drops named top-levels. ``source`` (near side / caller = our code) narrows the
    boundary; ``include_internal=True`` records every call into a dependency, incl. internal
    dep<->dep traffic. ``prefer="monitoring"`` forces the PEP 669 branch (raises <3.12).
    """
    if prefer in ("wrap", "hybrid"):
        # Targeted wrap: ``target`` is the list of dependency symbols to wrap (not a prefix).
        # "hybrid" also runs setprofile for completeness (the C-remainder + dynamic usage).
        from ._wrap import HybridBackend, WrapBackend
        cls = HybridBackend if prefer == "hybrid" else WrapBackend
        return cls(_split(target), sink, source=source, include_internal=include_internal)
    if prefer == "monitoring":
        return MonitoringBackend(target, sink, exclude, source, include_internal)
    # "setprofile" and "auto" both land here for now (3.6-3.11 band).
    return SetProfileBackend(target, sink, exclude, source, include_internal)
