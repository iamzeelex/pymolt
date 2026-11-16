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

Compiled (C / Cython) dependency callables called from our code are recorded too, as
``in = {"__c_call__": true}`` (+ ``arg0`` under sys.monitoring) with an unobserved result
(``C_RETURN_UNOBSERVED``) or a raise. setprofile sees ``builtin_function_or_method`` and
method descriptors through ``c_call`` / ``c_return`` / ``c_exception``, but NOT Cython
functions (``cython_function_or_method``: not an exact ``PyCFunction``) nor class
instantiation, and it cannot see a call that fails while BINDING its arguments (no frame, no
event): both are sys.monitoring-only (``CALL`` + ``C_RETURN`` / ``C_RAISE`` / ``RAISE``), and
for Cython functions below 3.12 ``SetProfileBackend`` wraps them (see ``_wrap.CompiledWrap``).

Generators, coroutines and async generators are recorded ONCE per call, by every backend: at
the generator's first start (when its arguments are still exactly what the caller passed),
as a ``return`` whose result is the generator kind (it normalizes exactly like the generator
object the wrap backend sees: ``{"__opaque__": "generator"}``), with ``dur`` = None (creating
a generator is not the work it does). Resumptions are not calls and suspensions are not
outcomes: what a generator yields is not part of its call's fact.

Every recorded call carries ``dur`` (``time.perf_counter`` seconds, call -> return/raise) and
``tid`` (``threading.get_ident()``), passed only to sinks whose ``on_return``/``on_raise``
accept them.

Runtime warnings (``install_warning_capture``): while a backend has a call in flight, a
``DeprecationWarning`` / ``PendingDeprecationWarning`` / ``FutureWarning`` issued through
``warnings.warn`` is attributed to that call and passed to the sink as ``warnings``
(``[{"category", "message", "count"}]``, message capped at 300 chars), sampled or tallied.
The in-flight calls are the thread's own stack: the frames between the ``warn()`` caller and
the innermost frame a backend has pending (setprofile / monitoring) or a wrapper frame (wrap)
ARE the thread-local stack of recorded calls, so attribution costs nothing on the call path;
only a ``warn()`` walks it. ``warnings.warn`` is wrapped, not replaced: the original always
runs, with ``stacklevel`` shifted by the one frame the wrapper adds, so the filters, the
"once"/"default" registries and the reported file/line are exactly what they were.
"""
import functools
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
from ._sinks import add_warning, warning_list

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


def _norm_dist(name):
    """PEP 503 normalization (``Foo_Bar.baz`` -> ``foo-bar-baz``) without ``re``."""
    out = []
    for ch in name.lower():
        out.append("-" if ch in "-_." else ch)
    return "-".join(part for part in "".join(out).split("-") if part)


def _dist_header(path):
    """``(Name, Version)`` from a dist-info METADATA / egg-info PKG-INFO, or None."""
    for leaf in ("METADATA", "PKG-INFO"):
        try:
            with open(os.path.join(path, leaf), encoding="utf-8", errors="replace") as fh:
                name = version = None
                for line in fh:
                    if not line.strip():
                        break  # end of the header block
                    if line.startswith("Name:"):
                        name = line[5:].strip()
                    elif line.startswith("Version:"):
                        version = line[8:].strip()
                if name and version:
                    return name, version
        except (OSError, ValueError):
            continue
    return None


def _dist_tops(path):
    """Top-level import names a distribution provides (top_level.txt, else its RECORD)."""
    tops = set()
    try:
        with open(os.path.join(path, "top_level.txt"), encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    tops.add(line.strip().replace("/", "."))
        return tops
    except (OSError, ValueError):
        pass
    try:
        with open(os.path.join(path, "RECORD"), encoding="utf-8") as fh:
            for line in fh:
                first = line.split(",", 1)[0].split("/", 1)[0]
                if not first or first.endswith((".dist-info", ".egg-info", ".data")) \
                        or first.startswith(".."):
                    continue
                if first.endswith(".py"):
                    first = first[:-3]
                elif "." in first:  # foo.cpython-312-darwin.so -> foo
                    first = first.split(".", 1)[0]
                tops.add(first)
    except (OSError, ValueError):
        pass
    return tops


def loaded_distributions(modules=None, roots=None, limit=1000):
    """``{distribution: version}`` for the installed distributions with a module loaded.

    What a capture actually ran against, as opposed to what a manifest says (an unpinned
    ``pandas`` resolves to whatever is newest today): the host takes a migration's
    ``from`` side from the baseline's record. Read from the ``*.dist-info`` /
    ``*.egg-info`` directories in the site roots by plain file reads, never by importing
    ``importlib.metadata`` or ``pkg_resources``, so it behaves the same on 3.6 and costs a
    directory listing plus one small read per loaded distribution. A distribution whose
    name is not its import name (``yaml`` -> PyYAML) is found through its top_level.txt or
    RECORD, read only while some loaded module is still unattributed."""
    roots = _site_roots() if roots is None else roots
    modules = sys.modules if modules is None else modules
    tops = set()
    for name, module in list(modules.items()):
        if "." in name or module is None:
            continue
        path = getattr(module, "__file__", None)
        if not path:
            continue
        path = os.path.normpath(path)
        if any(path.startswith(root) for root in roots):
            tops.add(name)
    if not tops:
        return {}
    infos = []  # (normalized stem name, metadata dir)
    for root in roots:
        try:
            entries = sorted(os.listdir(root))
        except OSError:
            continue
        for entry in entries:
            if entry.endswith(".dist-info"):
                stem = entry[:-len(".dist-info")]
            elif entry.endswith(".egg-info"):
                stem = entry[:-len(".egg-info")]
            else:
                continue
            infos.append((_norm_dist(stem.split("-", 1)[0]), os.path.join(root, entry)))
    found = {}
    pending = set(tops)
    unmatched = []
    by_name = {}
    for norm, path in infos:
        by_name.setdefault(norm, path)
    for top in sorted(tops):
        path = by_name.get(_norm_dist(top))
        if path is None:
            continue
        header = _dist_header(path)
        if header is not None:
            found[header[0]] = header[1]
            pending.discard(top)
    if pending:
        matched = set(by_name.get(_norm_dist(t)) for t in tops)
        unmatched = [path for _norm, path in infos if path not in matched]
    for path in unmatched:
        if not pending or len(found) >= limit:
            break
        provided = _dist_tops(path) & pending
        if not provided:
            continue
        header = _dist_header(path)
        if header is not None:
            found[header[0]] = header[1]
            pending.difference_update(provided)
    return found


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


ENV_STDLIB = "PYMOLT_TRACE_STDLIB"   # comma list of stdlib top-level modules to treat as targets

# Test-runner packages are never a boundary target: a project's tests call them (monkeypatch,
# fixtures, hooks) and their inputs (tmp paths, node ids) change every run. They come back only
# when ``PYMOLT_TRACE_TARGET`` names one of them (naming any lifts the exclusion for the whole
# family; the target prefixes themselves still decide which modules match).
TEST_RUNNER_PACKAGES = frozenset((
    "_pytest", "pytest", "pluggy", "py", "iniconfig", "_hypothesis_pytestplugin", "xdist"))

# Where a stdlib module's callables report their ``__module__`` from, when it is not the
# module's own name (``os.path.join`` is ``posixpath.join``). A leading underscore is tried too
# (``datetime`` -> ``_datetime``, ``asyncio`` -> ``_asyncio``).
_STDLIB_ALIASES = {
    "os": ("posix", "nt", "posixpath", "ntpath", "genericpath"),
}


def stdlib_targets(raw=None):
    """The stdlib top-level modules ``PYMOLT_TRACE_STDLIB`` (or ``raw``) asks to be recorded
    like dependencies: a frozenset of top-level names, empty when unset (nothing changes)."""
    if raw is None:
        raw = os.environ.get(ENV_STDLIB, "")
    if not isinstance(raw, str):
        raw = ",".join(str(r) for r in raw)
    return frozenset(p.strip().split(".", 1)[0] for p in raw.split(",") if p.strip())


def _stdlib_hit_names(targets):
    """Every top-level module name that counts as one of the stdlib ``targets``."""
    names = set(targets)
    for name in targets:
        names.add("_" + name)
        names.update(_STDLIB_ALIASES.get(name, ()))
    return frozenset(names)


def _runner_exclusions(explicit_tops):
    """The test-runner packages to exclude, none of them when the target names one."""
    if any(t in TEST_RUNNER_PACKAGES for t in explicit_tops):
        return ()
    return tuple(sorted(TEST_RUNNER_PACKAGES))


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
    """Match modules under one or more explicit top-level prefixes (minus excludes), plus the
    stdlib modules ``PYMOLT_TRACE_STDLIB`` names (``stdlib`` overrides the environment)."""

    def __init__(self, prefixes, exclude=(), stdlib=None):
        self.prefixes = tuple(p for p in prefixes if p)
        tops = [p.split(".", 1)[0] for p in self.prefixes]
        self.exclude = tuple(exclude) + _runner_exclusions(tops)
        self.stdlib = stdlib_targets() if stdlib is None else stdlib_targets(stdlib)
        self._stdlib_names = _stdlib_hit_names(self.stdlib)

    def matches(self, module, frame=None):
        if not module:
            return False
        top = module.split(".", 1)[0]
        if top in self.exclude:
            return False
        if top in self._stdlib_names:
            return True
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

    def __init__(self, exclude=(), stdlib=None):
        self.exclude = tuple(exclude) + _runner_exclusions(())
        self.stdlib = stdlib_targets() if stdlib is None else stdlib_targets(stdlib)
        self._stdlib_names = _stdlib_hit_names(self.stdlib)
        self._roots = _site_roots()
        self._cache = {}

    def matches(self, module, frame=None):
        if not module:
            return False
        top = module.split(".", 1)[0]
        if top in self.exclude:
            return False
        if top in self._stdlib_names:
            return True   # a stdlib module this capture asked to record (PYMOLT_TRACE_STDLIB)
        if not self._roots or top in _BUILTIN_MODULES:
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
        if module in _SELF_MODULES:
            return False   # the injected runtime is not the project
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


_CALL_FIELDS = ("dur", "tid", "warnings")


def sink_call_fields(sink):
    """Which per-call keywords (``dur``, ``tid``, ``warnings``) BOTH ``on_return`` and
    ``on_raise`` accept.

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
    """The sink's ``tally(qualname, sym, site, kind, raised=None, dur=None, warnings=None)``,
    adapted to what it accepts, or None when the sink has no tally (an unsampled call is then
    only counted by ``admit``, as before). The adapter always takes every keyword."""
    fn = getattr(sink, "tally", None)
    if fn is None or not callable(fn):
        return None
    try:
        params = inspect.signature(fn).parameters
    except Exception:
        params = None
    if params is not None and any(p.kind == p.VAR_KEYWORD for p in params.values()):
        return fn
    timing = params is None or ("raised" in params and "dur" in params)
    warns = params is not None and "warnings" in params
    if timing and warns:
        return fn
    if timing:
        def without_warnings(qualname, sym, site, kind, raised=None, dur=None, warnings=None):
            return fn(qualname, sym, site, kind, raised=raised, dur=dur)
        return without_warnings

    def positional(qualname, sym, site, kind, raised=None, dur=None, warnings=None):
        return fn(qualname, sym, site, kind)
    return positional


def sink_binding(sink):
    """True if the sink's ``on_raise`` accepts ``binding`` (a raise while binding arguments)."""
    fn = getattr(sink, "on_raise", None)
    if fn is None:
        return False
    try:
        params = inspect.signature(fn).parameters
    except Exception:
        return False
    return "binding" in params or any(p.kind == p.VAR_KEYWORD for p in params.values())


# ── compiled (C / Cython) callables ──────────────────────────────────────────
# A compiled callable has no Python frame: the setprofile ``c_call`` events and the
# sys.monitoring ``CALL`` / ``C_RETURN`` / ``C_RAISE`` events are what sees it. Neither
# delivers the RESULT or the exception type, nor the arguments (monitoring gives ``arg0``), so
# the event is ``in = {"__c_call__": true[, "arg0": ...]}`` and a return's result is this
# marker (undecidable to the diff, like any outcome the runtime cannot classify).
C_RETURN_UNOBSERVED = OutcomeMarker("unobserved:c-return")
NO_ARG = object()
_C_CALL_TYPES = tuple(getattr(types, n) for n in (
    "BuiltinFunctionType", "MethodDescriptorType", "WrapperDescriptorType",
    "MethodWrapperType", "ClassMethodDescriptorType") if hasattr(types, n))
_C_TYPE_NAMES = frozenset(("cython_function_or_method", "fused_cython_function"))
_HEAPTYPE = 1 << 9


def is_compiled_callable(obj):
    """True for a C / Cython callable object (not a Python function, class or method)."""
    t = type(obj)
    return t in _C_CALL_TYPES or t.__name__ in _C_TYPE_NAMES


def mro_lookup(cls, name):
    """The raw class-dict value ``name`` resolves to on ``cls`` (None if absent)."""
    for owner in cls.__mro__:
        value = owner.__dict__.get(name, NO_ARG)
        if value is not NO_ARG:
            return value
    return None


def same_c_callable(a, b):
    """True if two events name the same compiled callable. The profile hook is handed a
    freshly made bound builtin method at ``c_call`` and again at ``c_return``: same
    ``__self__`` and name, not the same object."""
    if a is b:
        return True
    try:
        me = getattr(a, "__self__", NO_ARG)
        return (me is not NO_ARG and me is getattr(b, "__self__", None)
                and getattr(a, "__name__", None) == getattr(b, "__name__", 0))
    except Exception:
        return False


def c_identity(obj):
    """``(module, qualname)`` of a compiled callable, or None when it cannot be named.

    The module is the callable's own ``__module__``; most method descriptors have none, so it
    comes from what owns them: the module a function is bound to, the class a classmethod is
    bound to, the type of the instance a bound method belongs to, or ``__objclass__``."""
    qual = getattr(obj, "__qualname__", None) or getattr(obj, "__name__", None)
    if not isinstance(qual, str) or not qual:
        return None
    module = getattr(obj, "__module__", None)
    if not (isinstance(module, str) and module):
        module = None
        owner = getattr(obj, "__self__", None)
        if owner is None:
            owner = getattr(obj, "__objclass__", None)
            if owner is not None:
                module = getattr(owner, "__module__", None)
        elif isinstance(owner, types.ModuleType):
            module = owner.__name__
        elif isinstance(owner, type):
            module = owner.__module__
        else:
            # A method bound to an instance is named after the instance's class (a dependency
            # subclass of a builtin: ``dep.MyDict.get``, not ``dep.dict.get``).
            module = type(owner).__module__
            name = getattr(obj, "__name__", None)
            if isinstance(name, str) and name:
                qual = type(owner).__qualname__ + "." + name
    if not (isinstance(module, str) and module):
        return None
    return module, qual


# The import machinery is never a boundary (``PYMOLT_TRACE_STDLIB=importlib`` included): its
# frames are what runs while a dependency is being imported, not a contact with it.
_IMPORT_MACHINERY = ("importlib._bootstrap", "_frozen_importlib")


def never_target(module):
    """True for a module that is never a dependency boundary (this runtime, the import
    machinery)."""
    return module in _SELF_MODULES or module.startswith(_IMPORT_MACHINERY)


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
    """``normalize_input`` (call inputs: generic objects stay opaque; results use ``normalize``)
    bound to the privacy profile read ONCE (per backend), not per value:
    ``normalize``'s own ``os.environ`` lookup was ~2/3 of the cost of normalizing a scalar.
    It resolves ``_normalize.normalize`` per call, so a patched normalize is still used."""
    privacy = os.environ.get("PYMOLT_TRACE_PRIVACY", "values").strip().lower()

    def norm(value):
        return _nz.normalize_input(value, _privacy=privacy)
    return norm


# ── runtime warnings, attributed to the recorded call in flight ──────────────
# The categories that announce an API change; the message is capped (a hint, not a dump).
WARN_CATEGORIES = (DeprecationWarning, PendingDeprecationWarning, FutureWarning)
WARN_MESSAGE_MAX = 300
ENV_WARNINGS = "PYMOLT_TRACE_WARNINGS"   # "0" / "off": do not wrap ``warnings.warn``
_WARN_DEPTH = 256           # frames walked from the warn() caller to the call in flight
_C_STACK_MAX = 256          # compiled calls in flight per thread (a lost completion never grows it)
_WARNED_MAX = 10000         # per backend: accumulators awaiting their call's completion

_warn_lock = threading.Lock()
_warn_owners = [()]         # [tuple of backends capturing], read lock-free by the hook
_warn_saved = [None, None]  # [the original warnings.warn, our wrapper] while installed
_category_labels = {}


def _category_label(category):
    label = _category_labels.get(category)
    if label is None:
        mod = getattr(category, "__module__", None)
        name = getattr(category, "__qualname__", None) or getattr(category, "__name__", "?")
        label = name if mod in (None, "builtins", "exceptions") else mod + "." + name
        if len(_category_labels) < 256:
            _category_labels[category] = label
    return label


def warning_key(message, category=None):
    """``(category label, message)`` of a deprecation-family warning, else None.

    Mirrors ``warnings.warn``'s own reading (a ``Warning`` instance gives its class; no
    category means ``UserWarning``) without running user code: a Warning instance's text is
    its single string argument, a non-string message is named by its type."""
    if isinstance(message, Warning):
        category = type(message)
        args = getattr(message, "args", ())
        text = args[0] if len(args) == 1 and isinstance(args[0], str) else category.__name__
    elif isinstance(category, type):
        text = message if isinstance(message, str) else "<" + type(message).__name__ + ">"
    else:
        return None
    if not issubclass(category, WARN_CATEGORIES):
        return None
    if len(text) > WARN_MESSAGE_MAX:
        text = text[:WARN_MESSAGE_MAX]
    return (_category_label(category), text)


def _capture_warning(owners, message, category, frame):
    """Attribute one warning to the innermost recorded call in flight (across backends)."""
    key = warning_key(message, category)
    if key is None:
        return
    best = None
    for owner in owners:
        hit = owner._warning_frame(frame)
        if hit is not None and (best is None or hit[0] < best[0]):
            best = (hit[0], hit[1], owner)
    if best is not None:
        best[2]._add_warning(best[1], key)


def _forward_stacklevel(stacklevel, kwargs, caller, c_impl=True):
    """The ``stacklevel`` that makes the original ``warn`` attribute the warning to the frame
    it would have chosen without our wrapper frame in between.

    ``stacklevel <= 1`` means the caller itself (one step past the wrapper: 2). With
    ``skip_file_prefixes`` (3.12+; the level is then at least 2) the first step from the
    wrapper already skips the caller when the caller's own file is a skipped prefix, so the
    level is shifted only when it is not. "Is a skipped prefix" is decided exactly as the
    implementation decides it: C ``_warnings`` matches ``PyUnicode_Tailmatch(name, prefix,
    0, -1, -1)``, i.e. ``name[:-1].startswith(prefix)``; the pure-Python fallback uses
    ``name.startswith(prefix)``."""
    prefixes = kwargs.get("skip_file_prefixes") if kwargs else None
    if prefixes:
        level = stacklevel if stacklevel > 2 else 2
        name = caller.f_code.co_filename if caller is not None else ""
        if c_impl:
            name = name[:-1]
        for prefix in prefixes:
            if isinstance(prefix, str) and name.startswith(prefix):
                return level
        return level + 1
    return (stacklevel if stacklevel > 1 else 1) + 1


def _make_warn_hook(original):
    c_impl = isinstance(original, types.BuiltinFunctionType)

    def warn(message, category=None, stacklevel=1, source=None, **kwargs):
        caller = sys._getframe(1)
        owners = _warn_owners[0]
        if owners:
            try:
                _capture_warning(owners, message, category, caller)
            except Exception:
                pass  # capture must never change what warn() does
        try:
            level = _forward_stacklevel(stacklevel, kwargs, caller, c_impl)
        except Exception:
            level = stacklevel  # a bad stacklevel: let the original raise its own error
        return original(message, category, level, source, **kwargs)

    try:
        functools.update_wrapper(warn, original)
    except Exception:
        pass
    warn.__pymolt_warn_hook__ = True
    return warn


def _warnings_disabled():
    raw = os.environ.get(ENV_WARNINGS, "1").strip().lower()
    return raw in ("0", "false", "no", "off")


def install_warning_capture(owner):
    """Start attributing warnings to ``owner``'s calls in flight (idempotent). The first
    owner wraps ``warnings.warn``. ``owner`` provides ``_warning_frame(frame)`` ->
    ``(depth, frame) | None`` and ``_add_warning(frame, key)``."""
    if _warnings_disabled():
        return False
    try:
        import warnings
    except Exception:
        return False
    with _warn_lock:
        owners = _warn_owners[0]
        if not any(o is owner for o in owners):
            _warn_owners[0] = owners + (owner,)
        if _warn_saved[1] is None:
            original = warnings.warn
            hook = _make_warn_hook(original)
            warnings.warn = hook
            _warn_saved[0], _warn_saved[1] = original, hook
    return True


def remove_warning_capture(owner):
    """Stop attributing warnings to ``owner``. The last owner restores ``warnings.warn``,
    unless something wrapped our wrapper since (it then stays, as a pass-through)."""
    with _warn_lock:
        _warn_owners[0] = tuple(o for o in _warn_owners[0] if o is not owner)
        if _warn_owners[0] or _warn_saved[1] is None:
            return
        try:
            import warnings
            if warnings.warn is _warn_saved[1]:
                warnings.warn = _warn_saved[0]
                _warn_saved[0] = _warn_saved[1] = None
        except Exception:
            pass


def warned_add(warned, frame, key):
    """Count ``key`` on the accumulator of the call running in ``frame``."""
    ent = warned.get(id(frame))
    if ent is None or ent[0] is not frame.f_code:
        if len(warned) >= _WARNED_MAX:
            warned.clear()  # completions lost (threads killed mid-call): never grow unbounded
        ent = warned[id(frame)] = (frame.f_code, {})
    add_warning(ent[1], key)


def warned_take(warned, frame, code):
    """The ``warnings`` list of the call that ran ``code`` in ``frame`` (None if none)."""
    ent = warned.pop(id(frame), None)
    if ent is None or ent[0] is not code:
        return None
    return warning_list(ent[1])


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
        self._warn_kw = "warnings" in call_fields
        self._tally = sink_tally(sink)
        self._warned = {}   # id(frame) -> (code, {(category, message): count}) of calls in flight
        self._normalize = privacy_normalizer()
        self._identity = IdentityResolver(self._in_target)
        self._obs_lock = threading.Lock()
        self.observed = {}          # id(code) -> code
        self.observed_syms = set()
        self.first_seen_at = None   # monotonic
        self.last_new_at = None     # monotonic
        # While two instrumentations can both deliver one start (the incremental settle), a
        # start is recorded once: both callbacks for an event run back to back in the same
        # thread, so the thread's last recorded frame (held, so its id cannot be reused)
        # identifies the duplicate exactly. Off (0.0) otherwise: holding frames costs memory.
        self._dedupe_until = 0.0
        self._last_start = {}       # tid -> frame of the last recorded start
        self._ccache = {}           # id(callable) -> (callable, sym | None): compiled decisions
        self._cstack = {}           # tid -> [(callable, caller frame, info)] compiled in flight
        self._bind_kw = sink_binding(sink)
        self._inh = {}              # (id(callable | code), id(receiver class)) -> (obj, cls, sym)
        self._inh_codes = {}        # id(code) -> code: stdlib code armed for inherited-method calls

    def _target_module(self, module, frame):
        """Far-side decision for a module (cached per code by the callers)."""
        if never_target(module):
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

    # ── runtime warnings ───────────────────────────────────────────────────────
    def _warning_frame(self, frame):
        """``(depth, frame)`` of the innermost call this backend has in flight on the stack
        above ``frame`` (the ``warn()`` caller), or None. The walk stops at our own code: a
        warning our code issues, or one raised under a call of ours this backend is not
        recording, is not the dependency call's."""
        pending = self._pending
        if not pending:
            return None
        near = self.near
        depth = 0
        while frame is not None and depth < _WARN_DEPTH:
            info = pending.get(id(frame))
            if info is not None and info[0] is frame.f_code:
                return depth, frame
            if near is not None and self._code_is_near(frame):
                return None
            frame = frame.f_back
            depth += 1
        return None

    def _add_warning(self, frame, key):
        warned_add(self._warned, frame, key)

    def _take_warnings(self, frame, code):
        try:
            return warned_take(self._warned, frame, code)
        except Exception:
            return None

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

    def _record(self, frame, ent, override=None):
        """Record one boundary call at its start (the near-side check already passed).

        A function call becomes a pending entry completed by its return/raise event; a
        generator-like call is complete at its first start and is emitted at once."""
        if self._dedupe_until:
            if _monotonic() < self._dedupe_until:
                tid = _get_ident()
                if self._last_start.get(tid) is frame:
                    return False            # the same start, delivered by the other tool
                self._last_start[tid] = frame
            else:
                self._dedupe_until = 0.0
                self._last_start = {}
        ci, sym, symq, loc, inner = self._call_identity(frame, ent)
        if override is not None and override != sym:
            sym, symq = override, None      # an inherited method, named after the receiver
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
        if self._warned:
            self._warned.pop(id(frame), None)  # a lost completion's frame id, reused
        # t0 last: the duration is the callee's, not the cost of recording its inputs.
        pending[id(frame)] = (code, ci.q, inputs, site, sym, symq, own, _now(), tid, sampled)
        return True

    def _finish(self, info, outcome, value, warns=None, binding=False):
        """Complete a recorded call: ``on_return``/``on_raise`` if sampled, else ``tally``.
        ``value`` is the result (return), the raised type label (raise) or ignored
        (ambiguous); ``warns`` the warnings attributed to the call while it ran."""
        t0 = info[7]
        dur = (_now() - t0) if t0 is not None else None
        if not info[9]:
            tally = self._tally
            if tally is not None:
                tally(info[1], info[4], info[3], _TALLY_KIND[outcome],
                      raised=value if outcome == OUTCOME_RAISE else None, dur=dur,
                      warnings=warns or None)
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
        if warns and self._warn_kw:
            kw["warnings"] = warns
        if outcome == OUTCOME_RAISE:
            if binding and self._bind_kw:
                kw["binding"] = True
            self.sink.on_raise(info[1], info[2], value, info[3], **kw)
            return
        result = value if outcome == OUTCOME_RETURN else AMBIGUOUS_OUTCOME
        self.sink.on_return(info[1], info[2], result, info[3], **kw)

    # ── inherited methods ──────────────────────────────────────────────────────
    # A dependency class inherits methods from the stdlib (``HTTPHeaderDict.get`` is
    # ``Mapping.get``); the callee's code lives outside every target, yet the CALL from our
    # code is a boundary contact. It is recorded when the receiver's class is a target class
    # and resolves the method to that very code, named after the receiver's class.
    def _inherited_sym(self, code, recv):
        """``<receiver class module>.<qualname>.<method>`` for non-target ``code`` called on
        ``recv``, or None. Decided once per (code, receiver class)."""
        t = recv if isinstance(recv, type) else type(recv)
        key = (id(code), id(t))
        ent = self._inh.get(key)
        if ent is not None and ent[0] is code and ent[1] is t:
            return ent[2]
        sym = None
        try:
            module = t.__module__
            if (isinstance(module, str) and module and module != "builtins"
                    and not never_target(module) and self._in_target(module)
                    and code.co_name != "__del__"):
                value = mro_lookup(t, code.co_name)
                if isinstance(value, (staticmethod, classmethod)):
                    value = value.__func__
                if getattr(value, "__code__", None) is code:
                    sym = module + "." + t.__qualname__ + "." + code.co_name
        except Exception:
            sym = None
        if len(self._inh) >= _CODE_CACHE_MAX:
            self._inh.clear()
        self._inh[key] = (code, t, sym)
        return sym

    def _inherited_descriptor_sym(self, desc, recv):
        """The same for a builtin method descriptor (``dict.get``) called on ``recv``, an
        instance of a dependency subclass of its class."""
        t = type(recv)
        key = (id(desc), id(t))
        ent = self._inh.get(key)
        if ent is not None and ent[0] is desc and ent[1] is t:
            return ent[2]
        sym = None
        try:
            module = t.__module__
            if (isinstance(module, str) and module and module != "builtins"
                    and not never_target(module) and self._in_target(module)
                    and desc.__name__ != "__del__" and mro_lookup(t, desc.__name__) is desc):
                sym = module + "." + t.__qualname__ + "." + desc.__name__
        except Exception:
            sym = None
        if len(self._inh) >= _CODE_CACHE_MAX:
            self._inh.clear()
        self._inh[key] = (desc, t, sym)
        return sym

    # ── compiled calls ─────────────────────────────────────────────────────────
    def _c_sym(self, obj):
        """The ``module.qualname`` a compiled callable is recorded under, or None when it is
        not a dependency's (builtins, the stdlib and anything the matcher rejects). Decided
        once per callable object (the entry holds it, so its id cannot be recycled)."""
        ent = self._ccache.get(id(obj))
        if ent is not None and ent[0] is obj:
            return ent[1]
        sym = None
        try:
            ident = c_identity(obj)
            if ident is not None:
                module = ident[0]
                if (module != "builtins" and not never_target(module)
                        and self._in_target(module)):
                    sym = module + "." + ident[1]
        except Exception:
            sym = None
        if len(self._ccache) >= _CODE_CACHE_MAX:
            self._ccache.clear()
        self._ccache[id(obj)] = (obj, sym)
        return sym

    def _c_start(self, frame, sym, arg0=NO_ARG):
        """Begin one compiled call made from ``frame`` (the near-side check passed): the
        pending record, in the shape ``_finish`` completes."""
        site = self._site(frame)
        admit = self._admit
        if admit is None:
            sampled = True
        elif self._admit_key:
            sampled = bool(admit(key=(sym, site["file"], site["line"])))
        else:
            sampled = bool(admit())
        inputs = None
        if sampled:
            inputs = {"__c_call__": True}
            if arg0 is not NO_ARG:
                try:
                    inputs["arg0"] = self._normalize(arg0)
                except Exception:
                    inputs["arg0"] = {"__opaque__": "unrenderable"}
        if sym not in self.observed_syms:
            with self._obs_lock:
                if sym not in self.observed_syms:
                    self.observed_syms.add(sym)
                    now = _monotonic()
                    if self.first_seen_at is None:
                        self.first_seen_at = now
                    self.last_new_at = now
        return (None, sym, inputs, site, sym, None, None, _now(), _get_ident(), sampled)

    def _binding_failure(self, frame, sym, symq=None):
        """Record a call that failed while binding its arguments: the callee never started."""
        info = (None, sym, {"__c_call__": True}, self._site(frame), sym, symq, None, None,
                _get_ident(), True)
        self._finish(info, OUTCOME_RAISE, "TypeError", binding=True)

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
        self.compiled_wrap = True  # wrap the Cython functions of dependency modules (see start)
        self._compiled = None
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
            body = code.co_name == "<module>"
            # [.., generator marker, may be an inherited method]: a non-target method (first
            # parameter self/cls) can still be a dependency class's inherited method.
            ent = [code, self._target_module(module, frame) and code.co_name != "__del__",
                   module, body, None, generator_marker(code),
                   (not body and code.co_argcount > 0 and not never_target(module)
                    and code.co_varnames[0] in ("self", "cls"))]
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
                override = None
                if not ent[1]:
                    if not ent[6] or self._retired:
                        return self._hook  # not a dependency
                    back = frame.f_back
                    if not self._caller_is_near(back):
                        return self._hook
                    override = self._inherited_sym(frame.f_code, frame.f_locals.get(
                        frame.f_code.co_varnames[0]))
                    if override is None:
                        return self._hook  # not a dependency class's inherited method
                elif ent[3]:
                    return self._hook  # a module body (import, not a contact)
                if ent[5] is not None and not first_start(frame):
                    return self._hook  # a generator resuming: not a call
                back = frame.f_back
                if self.skip_wrapped and self._from_wrapper(back):
                    return self._hook  # hybrid: a WrapBackend wrapper already recorded this call
                if not self._caller_is_near(back):  # caller = our code?
                    return self._hook  # internal dep<->dep traffic, not our boundary
                if self._record(frame, ent, override):
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
                # A C callable of a dependency, called from our code (``c_call``'s frame is the
                # CALLER). Everything else is routine C traffic, classified and not persisted.
                if self._c_caller_ok(frame):
                    self.c_event_count += 1
                    sym = self._c_sym(arg)
                    if sym is not None and not (self.skip_wrapped and self._from_wrapper(frame)):
                        tid = _get_ident()
                        stack = self._cstack.get(tid)
                        if stack is None:
                            stack = self._cstack[tid] = []
                        elif len(stack) >= _C_STACK_MAX:
                            del stack[:]
                        stack.append((arg, frame, self._c_start(frame, sym)))
            elif event == "c_return":
                stack = self._cstack.get(_get_ident())
                if stack and stack[-1][1] is frame and same_c_callable(stack[-1][0], arg):
                    self._finish(stack.pop()[2], OUTCOME_RETURN, C_RETURN_UNOBSERVED)
            elif event == "c_exception":
                # A C callable raised. The exception TYPE is not observable from a profile
                # hook (as for a Python raise), so the raise is untyped.
                stack = self._cstack.get(_get_ident())
                if stack and stack[-1][1] is frame and same_c_callable(stack[-1][0], arg):
                    self._finish(stack.pop()[2], OUTCOME_RAISE, UNTYPED_RAISE)
                    return self._hook
                # No matching c_call (the hook started mid-call): the legacy raise-only fact.
                # The CALLEE has to be the traced dependency: gating only on the caller recorded
                # every C raise our code provoked anywhere (`_io.BufferedReader.seek` during an
                # import) as a contact of a dependency it never touched. Callables with no
                # `__module__` cannot be attributed at all.
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
        except Exception:
            pass  # the hook must never break the traced program
        return self._hook

    def _emit_outcome(self, info, frame, arg):
        outcome = frame_outcome(frame, arg)
        warns = self._take_warnings(frame, info[0]) if self._warned else None
        self._finish(info, outcome, UNTYPED_RAISE if outcome == OUTCOME_RAISE else arg, warns)

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
        install_warning_capture(self)
        if self.compiled_wrap:
            try:
                from ._wrap import CompiledWrap
                self._compiled = CompiledWrap(self.sink, self.matcher)
                self._compiled.near = self.near
                self._compiled.start()
            except Exception:
                self._compiled = None

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
        self._ccache = {}

    def stop(self):
        self._retired = self._stopped = True  # hooks in other threads remove themselves
        if sys.getprofile() == self._hook:
            sys.setprofile(self._prev)
        threading.setprofile(None)
        remove_warning_capture(self)
        if self._compiled is not None:
            try:
                self._compiled.stop()
            except Exception:
                pass
            self._compiled = None
        self._pending.clear()
        self._inflight.clear()
        self._cstack.clear()
        self._warned.clear()


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
