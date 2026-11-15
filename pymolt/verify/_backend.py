"""Trace-backend abstraction + version-driven selection.

INJECTED RUNTIME — pure stdlib, Python 3.6+ (see ``_normalize`` header for the rules).
One interface for observing the call boundary, with implementations underneath:

  - ``SetProfileBackend`` : ``sys.setprofile``, the whole 3.6-3.11 band, pure Python.
  - ``MonitoringBackend`` : ``sys.monitoring`` (PEP 669), 3.12+ (``_monitoring``). Typed raises
                            (``PY_UNWIND`` carries the exception) and ``DISABLE`` for code that
                            is not a dependency, so the steady-state cost outside dependencies
                            is zero.

Both share ``FrameBackend``: the per-code decision and identity caches, the caller (near-side)
check, the v2 inputs, sampling + counting, and the discovery bookkeeping the discover -> watch
controller reads. So ``sym``/``symq``/``own``/``bound``/``defaulted`` are produced by one code
path whichever hook delivered the event.

The backend knows nothing about the on-disk format: it calls ``sink.on_return`` /
``sink.on_raise`` for a sampled call and ``sink.tally`` for an unsampled one. That decouples
*how we observe* from *where we write*.

Raise capture under setprofile, honestly (verified empirically against CPython 3.12):
``sys.setprofile`` does NOT deliver a Python ``exception`` event. A Python function that
raises is reported as a ``return`` with ``arg=None``; the frame's last opcode tells the two
apart (``frame_outcome``), but the exception TYPE is not observable, so a setprofile raise is
``"<unknown>"``. The monitoring backend reports the type.

Generators, coroutines and async generators are recorded ONCE per call, by every backend: at
the generator's first start (when its arguments are still exactly what the caller passed),
as a ``return`` whose result is the generator kind (it normalizes exactly like the generator
object the wrap backend sees: ``{"__opaque__": "generator"}``), with ``dur`` = None (creating
a generator is not the work it does). Resumptions are not calls and suspensions are not
outcomes: what a generator yields is not part of its call's fact.

Every recorded call carries ``dur`` (``time.perf_counter`` seconds, call -> return/raise) and
``tid`` (``threading.get_ident()``), passed only to sinks whose ``on_return``/``on_raise``
accept them.
"""
import inspect
import os
import sys
import threading
import time
import types

from . import _normalize as _nz
from ._identity import (
    SYMQ_UNWRAPPED,
    IdentityResolver,
    bind_inputs,
    fallback_identity,
    frame_inputs,
    receiver_owner,
    sink_identity_fields,
    wrapper_call,
)
from ._normalize import AMBIGUOUS_OUTCOME, OutcomeMarker

# Monotonic, high resolution (``time.monotonic`` is ~15 ms on older Windows builds).
_now = time.perf_counter
_monotonic = time.monotonic
_get_ident = threading.get_ident

HAS_MONITORING = sys.version_info >= (3, 12) and hasattr(sys, "monitoring")

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


class AnyMatcher(object):
    """Matches when any of its matchers does (the discover target widened by impact targets)."""

    def __init__(self, *matchers):
        self.matchers = tuple(m for m in matchers if m is not None)

    def matches(self, module, frame=None):
        for m in self.matchers:
            if m.matches(module, frame):
                return True
        return False


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
        # The decision is cached per MODULE, so it is made from the module's own file: the
        # first frame seen may run generated code (dataclass / namedtuple methods are exec'd in
        # the module's globals with ``co_filename == "<string>"``), and deciding from it marked
        # whole dependency modules (pydantic.fields, sqlalchemy.orm.attributes) as not ones.
        filename = _module_file(module)
        if not filename and frame is not None:
            filename = frame.f_code.co_filename
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
        # "our code" = a real file that is NOT under site-packages or the stdlib.
        # `<frozen importlib._bootstrap>` and friends are the interpreter's own
        # bootstrap: they have no real file, so the path test cannot reject
        # them, and left alone they masquerade as the project's code — which made
        # every C call the import machinery happens to make look like a contact.
        frame_file = frame.f_code.co_filename or ""
        if frame_file.startswith("<frozen "):
            ok = False
        else:
            # Per-module decision from the module's own file (see SitePackagesMatcher): a
            # dependency's generated code (``"<string>"``) must not make it "our code".
            filename = _module_file(module) or frame_file
            ok = bool(filename) and not filename.startswith(self._dep_roots)
        self._cache[module] = ok
        return ok




class Sink(object):
    """Structural receiver interface. A plain base class (not ``typing.Protocol``,
    which is 3.8+) for 3.6/3.7 reach. Implementations provide the hooks.

    ``qualname`` is the v1 legacy name (``<frame module>.<co_name>``); ``sym`` the canonical
    runtime identity, ``symq`` its quality ("unwrapped" / "fallback", None when exact) and
    ``own`` the receiver's class when it differs from the class prefix of ``sym``; ``dur`` the
    call's duration in seconds and ``tid`` the calling thread. Backends pass each keyword only
    to sinks whose signature accepts it. ``tally`` (optional) counts a call whose values were
    not sampled."""

    def on_call(self, qualname, inputs, site=None):
        raise NotImplementedError

    def on_return(self, qualname, inputs, result_obj, site=None, admitted=False, sym=None,
                  symq=None, own=None, dur=None, tid=None):
        raise NotImplementedError

    def on_raise(self, qualname, inputs, exc_typename, site=None, admitted=False, sym=None,
                 symq=None, own=None, dur=None, tid=None):
        raise NotImplementedError

    def tally(self, qualname, sym, site, kind, raised=None, dur=None):
        raise NotImplementedError


_CALL_FIELDS = ("dur", "tid")


def sink_call_fields(sink):
    """Which per-call keywords (``dur``, ``tid``) BOTH ``on_return`` and ``on_raise`` accept.

    Decided once per backend, like ``sink_identity_fields``: today's sinks keep receiving
    exactly today's call (an unknown keyword would raise inside the hook and lose the event)."""
    accepted = set(_CALL_FIELDS)
    try:
        for name in ("on_return", "on_raise"):
            params = inspect.signature(getattr(sink, name)).parameters
            if any(p.kind == p.VAR_KEYWORD for p in params.values()):
                continue
            accepted &= set(params)
    except Exception:
        return frozenset()
    return frozenset(accepted)


def sink_admit_accepts_key(sink):
    """True if ``sink.admit`` takes ``key=`` (a novelty-first sink: the first calls of each
    (symbol, call site) are admitted before the sample rate applies). Decided once per
    backend; a sink without it keeps receiving ``admit()`` / ``admit(force=...)``."""
    fn = getattr(sink, "admit", None)
    if fn is None or not callable(fn):
        return False
    try:
        params = inspect.signature(fn).parameters
    except Exception:
        return False
    return "key" in params or any(p.kind == p.VAR_KEYWORD for p in params.values())


def sink_tally(sink):
    """The sink's ``tally(qualname, sym, site, kind, raised=None, dur=None)``, adapted to what
    it accepts, or None when the sink has no tally (an unsampled call is then only counted by
    ``admit``, as before)."""
    fn = getattr(sink, "tally", None)
    if fn is None or not callable(fn):
        return None
    try:
        params = inspect.signature(fn).parameters
        if (not any(p.kind == p.VAR_KEYWORD for p in params.values())
                and not ("raised" in params and "dur" in params)):
            def positional(qualname, sym, site, kind, raised=None, dur=None):
                return fn(qualname, sym, site, kind)
            return positional
    except Exception:
        pass
    return fn


class TraceBackend(object):
    """Base interface. ``start()``/``stop()`` are symmetric; re-entrancy is the caller's
    responsibility."""

    name = "base"

    def __init__(self, target, sink, exclude=(), source=None, include_internal=False):
        # far side (callee = dependency): prefix / "all"/"*" / comma list / sequence / matcher.
        self.matcher = build_matcher(target, exclude)
        self._exclude = tuple(exclude or ())
        # near side (caller = our code). None => boundary filtering off (record any call into a
        # dependency, including internal dep<->dep traffic) for a deep audit.
        self.near = None if include_internal else AppMatcher(_split(source))
        self.sink = sink
        self.note = None  # a backend-selection note for the metadata (e.g. a fallback)

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

_TALLY_KIND = {OUTCOME_RETURN: "return", OUTCOME_RAISE: "raise",
               OUTCOME_AMBIGUOUS: "ambiguous"}


def _opcode_sets():
    try:
        import dis
        opmap = dis.opmap
    except Exception:  # pragma: no cover - dis is stdlib everywhere
        return frozenset(), frozenset(), frozenset(), None
    ret = frozenset(opmap[n] for n in ("RETURN_VALUE", "RETURN_CONST", "RETURN_GENERATOR")
                    if n in opmap)
    yld = frozenset(opmap[n] for n in ("YIELD_VALUE", "YIELD_FROM") if n in opmap)
    amb = frozenset()
    if sys.version_info < (3, 8):
        amb = frozenset(opmap[n] for n in ("END_FINALLY", "WITH_CLEANUP", "WITH_CLEANUP_START",
                                           "WITH_CLEANUP_FINISH") if n in opmap)
    return ret, yld, amb, opmap.get("RESUME")


_RETURN_OPS, _YIELD_OPS, _AMBIGUOUS_OPS, _RESUME = _opcode_sets()


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


# ── generators: one call, recorded at the first start ────────────────────────
_CO_GENERATOR = 0x20
_CO_COROUTINE = 0x80
_CO_ASYNC_GENERATOR = 0x200

# Normalize exactly like the generator objects themselves (``typename`` of a builtins type).
GENERATOR_RESULT = OutcomeMarker("generator")
COROUTINE_RESULT = OutcomeMarker("coroutine")
ASYNC_GENERATOR_RESULT = OutcomeMarker("async_generator")


_GENERATOR_TYPES = frozenset(t for t in (
    getattr(types, "GeneratorType", None), getattr(types, "CoroutineType", None),
    getattr(types, "AsyncGeneratorType", None)) if t is not None)


def generator_marker(code):
    """The call result recorded for a generator-like code object, or None for a function."""
    flags = code.co_flags
    if flags & _CO_ASYNC_GENERATOR:
        return ASYNC_GENERATOR_RESULT
    if flags & _CO_COROUTINE:
        return COROUTINE_RESULT
    if flags & _CO_GENERATOR:
        return GENERATOR_RESULT
    return None


def first_start(frame):
    """True if a profile ``call`` event for a generator frame is its first start, False for a
    resumption (``next``/``send``/``throw`` after a yield).

    <=3.10: a frame that has not run yet has ``f_lasti == -1``. 3.11+: the frame stops on
    ``RESUME`` whose oparg's low two bits are 0 at the start (1 after yield, 2 after yield
    from, 3 after await; 3.13+ adds a depth bit). Verified on 3.8-3.14. Undecidable -> True
    (record rather than lose the call)."""
    try:
        lasti = frame.f_lasti
        if lasti < 0:
            return True
        if _RESUME is None:
            return False
        co = frame.f_code.co_code
        return co[lasti] == _RESUME and (co[lasti + 1] & 3) == 0
    except Exception:
        return True


# Per-code decision caches are keyed by ``id(code)`` and hold a reference to the code
# object itself, so an id can never be recycled while its entry is alive. They are bounded
# so dynamically generated code (templating engines, exec in a loop) cannot grow them
# without limit: past the cap the cache is simply rebuilt.
_CODE_CACHE_MAX = 200000
_PENDING_MAX = 100000

# The runtime's own modules are never a dependency, whatever the target says (a "all"
# target with the runtime installed under site-packages would otherwise record its own
# writer / controller threads).
_SELF_PKG = __name__.rsplit(".", 1)[0]
_SELF_MODULES = frozenset(_SELF_PKG + "." + n for n in (
    "_backend", "_monitoring", "_wrap", "_sinks", "_normalize", "_identity",
    "boundary_tracer", "sitecustomize", "_install_hook"))


def privacy_normalizer():
    """``normalize`` bound to the privacy profile read ONCE (per backend), not per value:
    ``normalize``'s own ``os.environ`` lookup was ~2/3 of the cost of normalizing a scalar.
    It resolves ``_normalize.normalize`` per call, so a patched normalize is still used."""
    privacy = os.environ.get("PYMOLT_TRACE_PRIVACY", "values").strip().lower()

    def norm(value):
        return _nz.normalize(value, _privacy=privacy)
    return norm


class FrameBackend(TraceBackend):
    """What every frame-observing backend shares (setprofile, sys.monitoring).

    Hook cheapness is load-bearing. Every decision that depends only on a code object
    (is the callee a dependency? is the caller our code? its canonical ``sym``, parameter
    names and defaults, whether it may be a generic decorator wrapper) is made once per code
    object and cached by ``id(code)``; the hot path is a dict lookup, with no per-event string
    work. The only per-call introspection beyond reading the frame's locals is for generic
    wrapper frames (closure cells), cached per inner function. Sampling is decided at call
    start, before any introspection/normalize; an unsampled call keeps a light pending record
    (no inputs) and is tallied when it completes.

    Discovery bookkeeping (read by the discover -> watch controller): ``observed`` holds every
    code object recorded at least once, ``observed_syms`` every recorded ``sym``, and
    ``last_new_at`` the monotonic time a new ``sym`` was last seen. It is touched only the first
    time a code object / symbol is recorded."""

    def __init__(self, target, sink, exclude=(), source=None, include_internal=False):
        TraceBackend.__init__(self, target, sink, exclude, source, include_internal)
        self._pending = {}  # id(frame) -> (code, q, inputs, site, sym, symq, own, t0, tid, sampled)
        # id(code) -> [code, in_target, module, is_module_body, CodeIdentity | None,
        #              generator marker | None, ...backend extras]; the identity slot is
        # filled lazily on the first RECORDED call (most code objects in the process are
        # never a boundary contact and never pay for it).
        self._callee = {}
        self._near_codes = {}  # id(code) -> (code, is_near)
        self._admit = getattr(sink, "admit", None)
        self._admit_key = sink_admit_accepts_key(sink)
        fields = sink_identity_fields(sink)          # v1 sinks keep the v1 call
        self._sym_kw = "sym" in fields
        self._own_kw = "own" in fields
        call_fields = sink_call_fields(sink)         # today's sinks keep today's call
        self._dur_kw = "dur" in call_fields
        self._tid_kw = "tid" in call_fields
        self._tally = sink_tally(sink)
        self._normalize = privacy_normalizer()
        self._identity = IdentityResolver(self._in_target)
        self._obs_lock = threading.Lock()
        self.observed = {}          # id(code) -> code
        self.observed_syms = set()
        self.first_seen_at = None   # monotonic
        self.last_new_at = None     # monotonic

    def _target_module(self, module, frame):
        """Far-side decision for a module (cached per code by the callers)."""
        if module in _SELF_MODULES:
            return False
        return bool(self._in_target(module, frame))

    # ── per-code decisions ─────────────────────────────────────────────────────
    def _code_identity(self, ent, frame):
        """The per-code identity, computed once and stored in the decision-cache entry."""
        try:
            ci, cacheable = self._identity.code_identity(ent[0], ent[2], frame.f_globals)
        except Exception:
            ci, cacheable = fallback_identity(ent[0], ent[2]), True
        if cacheable:
            ent[4] = ci
        return ci

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

    def _caller_is_near(self, caller_frame):
        """True if the *caller* is our code (near side). With boundary filtering off
        (``self.near is None``) there is no near constraint -> always True."""
        if self.near is None:
            return True
        if caller_frame is None:
            return False
        return self._code_is_near(caller_frame)

    @staticmethod
    def _site(caller_frame):
        """Where in *our* code the call originated: {file, line, func}. The caller frame's
        current line is exactly the call site (the line invoking the dependency)."""
        if caller_frame is None:
            return None
        code = caller_frame.f_code
        return {"file": code.co_filename, "line": caller_frame.f_lineno, "func": code.co_name}

    # ── recording ──────────────────────────────────────────────────────────────
    def _call_identity(self, frame, ent):
        """(identity, sym, symq, locals | None, inner | None) of the call starting in
        ``frame``, BEFORE the sampling decision (the sampling key needs ``sym``).

        Cheap: the per-code identity is cached; only a generic wrapper (its inner function)
        or a factory-made method (its receiver's class) reads the frame's locals. Nothing is
        normalized here."""
        ci = ent[4]
        if ci is None:
            ci = self._code_identity(ent, frame)
        if not ci.cells and not ci.via_recv:
            return ci, ci.sym, ci.symq, None, None   # the common path: no locals
        try:
            loc = frame.f_locals
            if ci.cells:
                # Generic decorator wrapper: attribute the call to the single wrapped
                # dependency function (its arguments are bound to ITS signature below).
                inner = self._identity.wrapped_inner(loc, ci.cells)
                if inner is not None:
                    return ci, inner.sym, SYMQ_UNWRAPPED, loc, inner
            if ci.via_recv:
                # A factory-made method (``cls.mean = mean``): name it after the class the
                # receiver reaches it through, with that function's defaults.
                rci = self._identity.receiver_identity(ci, ent[0], loc.get(ci.recv))
                if rci is not None:
                    ci = rci
            return ci, ci.sym, ci.symq, loc, None
        except Exception:
            return ci, ci.sym, ci.symq, None, None

    def _call_inputs(self, frame, ent, ci, sym, symq, loc, inner):
        """(sym, symq, own, inputs) of a SAMPLED call: the v2 inputs, normalized.

        A generic wrapper's pass-through args are bound to its inner function's signature,
        so the event looks the same whether or not the decorator is present; if that bind
        fails the call keeps the wrapper's own identity, as it always did."""
        normalize = self._normalize
        own = None
        try:
            if loc is None:
                loc = frame.f_locals
            if inner is not None:
                try:
                    args, kwargs = wrapper_call(loc, ci)
                    inputs = bind_inputs(inner.sig, args, kwargs, normalize)
                    if inner.recv is not None and args:
                        own = receiver_owner(args[0], inner.recv, inner.prefix)
                    return sym, symq, own, inputs
                except Exception:
                    own = None
                    sym, symq = ci.sym, ci.symq
                    if ci.via_recv:
                        rci = self._identity.receiver_identity(ci, ent[0], loc.get(ci.recv))
                        if rci is not None:
                            ci = rci
                            sym, symq = ci.sym, ci.symq
            inputs = frame_inputs(loc, ci, normalize)
            if ci.recv is not None:
                own = receiver_owner(loc.get(ci.recv), ci.recv, ci.prefix)
        except Exception:
            return sym, symq, None, {"unbound": True}
        return sym, symq, own, inputs

    def _record(self, frame, ent):
        """Record one boundary call at its start (the near-side check already passed).

        A function call becomes a pending entry completed by its return/raise event; a
        generator-like call is complete at its first start and is emitted at once."""
        ci, sym, symq, loc, inner = self._call_identity(frame, ent)
        site = self._site(frame.f_back)
        # Sampling before any normalize: an unsampled call never pays for its values. The
        # key (symbol, call site) lets a novelty-first sink admit each new one first.
        admit = self._admit
        if admit is None:
            sampled = True
        elif self._admit_key:
            if site is not None:
                sampled = bool(admit(key=(sym or ci.q, site["file"], site["line"])))
            else:
                sampled = bool(admit(key=(sym or ci.q, None, None)))
        else:
            sampled = bool(admit())
        if sampled:
            sym, symq, own, inputs = self._call_inputs(frame, ent, ci, sym, symq, loc, inner)
        else:
            own = inputs = None
        code = ent[0]
        if id(code) not in self.observed or sym not in self.observed_syms:
            self._observe(code, sym, ent)
        tid = _get_ident()
        marker = ent[5]
        if marker is not None:
            self._finish((code, ci.q, inputs, site, sym, symq, own, None, tid, sampled),
                         OUTCOME_RETURN, marker)
            return False
        pending = self._pending
        if len(pending) >= _PENDING_MAX:
            pending.clear()  # completions lost (threads killed mid-call): never grow unbounded
        # t0 last: the duration is the callee's, not the cost of recording its inputs.
        pending[id(frame)] = (code, ci.q, inputs, site, sym, symq, own, _now(), tid, sampled)
        return True

    def _finish(self, info, outcome, value):
        """Complete a recorded call: ``on_return``/``on_raise`` if sampled, else ``tally``.
        ``value`` is the result (return), the raised type label (raise) or ignored
        (ambiguous)."""
        t0 = info[7]
        dur = (_now() - t0) if t0 is not None else None
        if not info[9]:
            tally = self._tally
            if tally is not None:
                tally(info[1], info[4], info[3], _TALLY_KIND[outcome],
                      raised=value if outcome == OUTCOME_RAISE else None, dur=dur)
            return
        kw = {"admitted": True} if self._admit is not None else {}
        if self._sym_kw:
            kw["sym"] = info[4]
            kw["symq"] = info[5]
        if info[6] is not None and self._own_kw:
            kw["own"] = info[6]
        if self._dur_kw:
            kw["dur"] = dur
        if self._tid_kw:
            kw["tid"] = info[8]
        if outcome == OUTCOME_RAISE:
            self.sink.on_raise(info[1], info[2], value, info[3], **kw)
            return
        result = value if outcome == OUTCOME_RETURN else AMBIGUOUS_OUTCOME
        self.sink.on_return(info[1], info[2], result, info[3], **kw)

    # ── discovery bookkeeping ──────────────────────────────────────────────────
    def _observe(self, code, sym, ent):
        new_code = False
        with self._obs_lock:
            if id(code) not in self.observed:
                self.observed[id(code)] = code
                new_code = True
            if sym not in self.observed_syms:
                self.observed_syms.add(sym)
                now = _monotonic()
                if self.first_seen_at is None:
                    self.first_seen_at = now
                self.last_new_at = now
        if new_code:
            self._on_new_code(code, ent)

    def _on_new_code(self, code, ent):
        """Hook: a code object was recorded for the first time (monitoring arms its return)."""


class SetProfileBackend(FrameBackend):
    """``sys.setprofile`` backend — events at call boundaries, not lines.

    The whole hook body is guarded so an internal error can never propagate into the traced
    program. ``sys.setprofile`` is per thread: ``start`` hooks the calling thread and threads
    started later; ``retire`` (the discover -> watch switch) makes every thread's hook stop
    recording new calls and remove itself at its next event once that thread has no call in
    flight, so no call is lost or recorded twice across the switch.
    """

    name = "setprofile"

    def __init__(self, target, sink, exclude=(), source=None, include_internal=False):
        FrameBackend.__init__(self, target, sink, exclude, source, include_internal)
        self._prev = None
        self._lock = threading.Lock()
        self.c_event_count = 0  # classified internal C traffic, intentionally not persisted
        self.skip_wrapped = False  # hybrid: skip calls a WrapBackend wrapper already records
        self._retired = False
        self._stopped = False
        self._start_tid = None
        self._inflight = {}  # tid -> pending calls of that thread (written by that thread only)

    # ── per-code decisions ─────────────────────────────────────────────────────
    def _callee_entry(self, frame):
        code = frame.f_code
        ent = self._callee.get(id(code))
        if ent is None:
            if len(self._callee) >= _CODE_CACHE_MAX:
                self._callee.clear()
            module = frame.f_globals.get("__name__", "") or ""
            ent = [code, self._target_module(module, frame), module,
                   code.co_name == "<module>", None, generator_marker(code)]
            self._callee[id(code)] = ent
        return ent

    @staticmethod
    def _from_wrapper(caller_frame):
        """True if the caller is a WrapBackend wrapper (so the call is already recorded there)."""
        return (caller_frame is not None
                and caller_frame.f_code.co_name in ("wrapper", "init_wrapper")
                and caller_frame.f_globals.get("__name__", "").endswith("_wrap"))

    def _c_caller_ok(self, frame):
        """Caller gate for C events (here `frame` IS the calling frame): near side in
        boundary mode, else the dependency itself (internal C traffic) for a full audit."""
        if self.near is not None:
            return self._code_is_near(frame)
        return self._callee_entry(frame)[1]

    def _hook(self, frame, event, arg):
        try:
            if event == "call":
                if self._retired:
                    # Retired (discover -> watch): only an OBSERVED code entered without its
                    # wrapper is still recorded (the call raced the switch: its callee was
                    # looked up before the wrap); anything else unhooks an idle thread.
                    ent = self._callee.get(id(frame.f_code))
                    if (ent is None or ent[0] is not frame.f_code
                            or id(ent[0]) not in self.observed or self._stopped):
                        if not self._inflight.get(_get_ident()):
                            self._unhook()
                        return self._hook
                else:
                    ent = self._callee_entry(frame)
                if not ent[1] or ent[3]:
                    return self._hook  # not a dependency, or a module body (import, not a contact)
                if ent[5] is not None and not first_start(frame):
                    return self._hook  # a generator resuming: not a call
                back = frame.f_back
                if self.skip_wrapped and self._from_wrapper(back):
                    return self._hook  # hybrid: a WrapBackend wrapper already recorded this call
                if not self._caller_is_near(back):  # caller = our code?
                    return self._hook  # internal dep<->dep traffic, not our boundary
                if self._record(frame, ent):
                    tid = _get_ident()
                    self._inflight[tid] = self._inflight.get(tid, 0) + 1
            elif event == "return":
                if self._pending:
                    info = self._pending.pop(id(frame), None)
                    if info is not None:
                        tid = info[8]
                        self._inflight[tid] = self._inflight.get(tid, 1) - 1
                        if info[0] is frame.f_code:
                            self._emit_outcome(info, frame, arg)
            elif self._retired:
                pass
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
                cq = self._c_qualname(arg)  # a C callable's own module.qualname: exact
                kw = {"sym": cq, "symq": None} if self._sym_kw else {}
                if self._tid_kw:
                    kw["tid"] = _get_ident()
                self.sink.on_raise(cq, {"c_level": True}, "Exception", self._site(frame), **kw)
            # "c_return" carries nothing we persist: fall through.
        except Exception:
            pass  # the hook must never break the traced program
        return self._hook

    def _emit_outcome(self, info, frame, arg):
        outcome = frame_outcome(frame, arg)
        self._finish(info, outcome, UNTYPED_RAISE if outcome == OUTCOME_RAISE else arg)

    @staticmethod
    def _c_qualname(cfunc):
        mod = getattr(cfunc, "__module__", "") or ""
        name = getattr(cfunc, "__qualname__", None) or getattr(cfunc, "__name__", "c_call")
        return "{0}.{1}".format(mod, name) if mod else name

    def _unhook(self):
        """Remove the hook from the CURRENT thread (``sys.setprofile`` is per thread)."""
        if sys.getprofile() == self._hook:
            sys.setprofile(self._prev if _get_ident() == self._start_tid else None)

    def start(self):
        self._retired = self._stopped = False
        self._start_tid = _get_ident()
        self._prev = sys.getprofile()
        sys.setprofile(self._hook)
        threading.setprofile(self._hook)  # catch threads spawned after start

    def retire(self):
        """Stop recording new calls (discover -> watch). Calls in flight still complete; each
        thread's hook removes itself at its next call event once that thread has none in
        flight, except that an observed code entered without its wrapper (the call looked its
        callee up before the wrap) is still recorded, so the switch loses no call."""
        self._retired = True
        threading.setprofile(None)
        try:
            if not self._inflight.get(_get_ident()):
                self._unhook()
        except Exception:
            pass

    def narrow(self, prefixes):
        """Keep recording, but only into ``prefixes`` (watch mode's ``module.*`` targets)."""
        self.matcher = PrefixMatcher(list(prefixes), self._exclude)
        self._callee = {}

    def stop(self):
        self._retired = self._stopped = True  # hooks in other threads remove themselves
        if sys.getprofile() == self._hook:
            sys.setprofile(self._prev)
        threading.setprofile(None)
        self._pending.clear()
        self._inflight.clear()


def _monitoring_cls():
    from ._monitoring import MonitoringBackend
    return MonitoringBackend


def select_backend(target, sink, prefer="auto", exclude=(), source=None, include_internal=False):
    """Choose a backend.

    ``prefer``: "auto" -> ``sys.monitoring`` on 3.12+, setprofile below; "monitoring" on
    <3.12 falls back to setprofile with ``note = "monitoring-unavailable"`` (never raises);
    "setprofile" forces setprofile; "wrap"/"hybrid" use the targeted wrap backends.

    ``target`` (far side / callee) is a prefix / "all" / comma-list / sequence / matcher;
    ``exclude`` drops named top-levels. ``source`` (near side / caller = our code) narrows the
    boundary; ``include_internal=True`` records every call into a dependency, incl. internal
    dep<->dep traffic.
    """
    if prefer in ("wrap", "hybrid"):
        # Targeted wrap: ``target`` is the list of dependency symbols to wrap (not a prefix).
        # "hybrid" also runs setprofile for completeness (the C-remainder + dynamic usage).
        from ._wrap import HybridBackend, WrapBackend
        cls = HybridBackend if prefer == "hybrid" else WrapBackend
        return cls(_split(target), sink, source=source, include_internal=include_internal)
    if prefer in ("auto", "monitoring"):
        if HAS_MONITORING:
            return _monitoring_cls()(target, sink, exclude, source, include_internal)
        backend = SetProfileBackend(target, sink, exclude, source, include_internal)
        if prefer == "monitoring":
            backend.note = "monitoring-unavailable"
        return backend
    return SetProfileBackend(target, sink, exclude, source, include_internal)
