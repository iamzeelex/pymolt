"""Targeted-wrap backend — observe only the named dependency symbols (prod-grade overhead).

INJECTED RUNTIME — pure stdlib, Python 3.6+ (see ``_normalize`` header for the rules).

Where ``SetProfileBackend`` taxes *every* call in the process, this wraps only the specific
dependency callables your code uses (the list comes from ``verify gaps`` or is given explicitly).
Each target is replaced, in memory, by a TRANSPARENT wrapper: it records args / return / raise /
call-site, then calls the real object and returns/raises exactly as before. No source edits; the
swap is reversible (``stop()`` restores originals). Overhead is paid only on the wrapped
functions, only when they're called — nothing on the rest of the process.

Two install paths, both needed because the watcher activates at interpreter startup *before* the
dependency is imported:
  - eager-patch any target module already in ``sys.modules``;
  - a ``sys.meta_path`` finder that wraps a target module right after it loads — so the wrap
    happens before user code binds ``from dep import name`` (the aliasing pitfall).

Honesty: symbols that can't be wrapped (not callable, read-only/C type, attr missing) are
recorded in ``skipped`` rather than silently dropped. Feeds the same Sink as the other backends
(``on_return``/``on_raise`` with the ``site``, ``dur``, ``tid``; ``tally`` for an unsampled
call), so the JSONL / diff / contract are unchanged. A raise is labelled ``module.Qualname``
(builtins bare), exactly as the monitoring backend labels it.

Watch after discover (``patch_aliases=True``): by the time the controller switches, user and
dependency modules have long bound ``from dep import func`` aliases, which patching the
defining module alone would miss. The backend then also patches every module-global alias of
a wrapped module-level function (one pass over ``sys.modules`` at start), and restores them on
``stop()``. Late ``from dep import func`` imports read the patched attribute.
"""
import dis
import functools
import importlib.util
import inspect
import os
import sys
import types

from ._backend import (
    _GENERATOR_TYPES,
    _WARN_DEPTH,
    AppMatcher,
    SetProfileBackend,
    _get_ident,
    _now,
    _split,
    _stdlib_roots,
    install_warning_capture,
    privacy_normalizer,
    remove_warning_capture,
    sink_admit_accepts_key,
    sink_binding,
    sink_call_fields,
    sink_tally,
    warned_add,
    warned_take,
)
from ._identity import (
    SYMQ_FALLBACK,
    SYMQ_RECEIVER,
    bind_inputs,
    callable_identity,
    callable_receiver,
    class_member,
    receiver_owner,
    sink_identity_fields,
    unwrap,
)
from ._normalize import OutcomeMarker, typename

_WRAP_MARK = "__pymolt_wrapped__"
_FUNCTION = types.FunctionType
_MODULE = types.ModuleType
_getframe = sys._getframe
_NOT_NEAR_MAX = 200000
# The code objects of the two wrapper ``def``s (every wrapper made from one ``def`` shares
# its code): a frame running one is a wrapped call in flight (warning attribution).
_WRAPPER_CODES = []


def _note_wrapper_code(code):
    for known in _WRAPPER_CODES:
        if known is code:
            return
    _WRAPPER_CODES.append(code)


_CALL_OPS = frozenset(n for n in (
    "CALL_FUNCTION", "CALL_FUNCTION_KW", "CALL_FUNCTION_EX", "CALL_METHOD", "CALL_METHOD_KW",
    "PRECALL", "CALL", "CALL_KW", "CALL_FUNCTION_EX") if n in dis.opmap)
_CALL_OPCODES = frozenset(dis.opmap[n] for n in _CALL_OPS)
_CACHE_OP = dis.opmap.get("CACHE")
_AT_CALL = {}       # (id(code), f_lasti) -> (code, is a call instruction)
_AT_CALL_MAX = 50000


def _at_call(frame):
    """True if ``frame`` is executing a call instruction: a compiled function reached through
    a comparison, a property or a Cython-internal call has the project's frame behind it
    too, but not at a call (``a == b`` runs a compiled ``__eq__`` that calls ``self.dict()``).
    Decided once per (code, instruction)."""
    code = frame.f_code
    lasti = frame.f_lasti
    key = (id(code), lasti)
    ent = _AT_CALL.get(key)
    if ent is not None and ent[0] is code:
        return ent[1]
    try:
        co = code.co_code
        while _CACHE_OP is not None and lasti > 0 and co[lasti] == _CACHE_OP:
            lasti -= 2          # 3.11+: f_lasti may sit on the instruction's inline cache
        ok = co[lasti] in _CALL_OPCODES
    except Exception:
        ok = True
    if len(_AT_CALL) >= _AT_CALL_MAX:
        _AT_CALL.clear()
    _AT_CALL[key] = (code, ok)
    return ok


def _alias_index():
    """{id(function): [(module, name)]} of every module-global function binding."""
    index = {}
    for module in list(sys.modules.values()):
        if not isinstance(module, _MODULE):
            continue
        try:
            items = list(module.__dict__.items())
        except Exception:
            continue
        for name, value in items:
            if type(value) is _FUNCTION:
                index.setdefault(id(value), []).append((module, name))
    return index


def _restore_setattr(holder, attr, original):
    def undo():
        setattr(holder, attr, original)
    return undo


def _restore_delattr(holder, attr):
    def undo():
        try:
            delattr(holder, attr)
        except AttributeError:
            pass
    return undo


class WrapBackend(object):
    """Wrap a fixed set of dependency symbols and report their boundary contacts."""

    name = "wrap"

    def __init__(self, symbols, sink, source=None, include_internal=False, patch_aliases=False):
        self.sink = sink
        fields = sink_identity_fields(sink)           # v1 sinks keep the v1 call
        self._sym_kw = "sym" in fields
        self._own_kw = "own" in fields
        call_fields = sink_call_fields(sink)          # today's sinks keep today's call
        self._dur_kw = "dur" in call_fields
        self._tid_kw = "tid" in call_fields
        self._warn_kw = "warnings" in call_fields
        self._tally = sink_tally(sink)
        self._warned = {}   # id(wrapper frame) -> (code, {(category, message): count})
        self._admit_key = sink_admit_accepts_key(sink)
        self._bind_kw = sink_binding(sink)
        self._normalize = privacy_normalizer()
        self.patch_aliases = patch_aliases
        self._aliases = None                          # built once at start when patching aliases
        self.near = None if include_internal else AppMatcher(_split(source))
        # id(code) -> code of callers that are never a boundary contact (not our code, or a
        # wrapper itself): a wrapper called from one calls through without recording anything.
        # Dependency-internal traffic through a wrapped Cython function (pandas calling its own
        # ``lib.is_list_like``) then costs a frame read and a dict lookup, not the record path.
        self._not_near = {}
        self._require_call = False  # only a call instruction in the caller is a contact
        self._restore = []          # closures that undo each patch
        self._finder = None
        self.skipped = []           # (symbol, reason) — honesty markers
        self._done = set()          # full symbols already wrapped (dedup across candidates)
        self._skip_seen = set()
        # Register each symbol under every candidate module prefix with the remaining attribute
        # chain, so "flask.app.Flask.add_url_rule" resolves once flask.app loads (module=flask.app,
        # chain=("Flask","add_url_rule")). The most specific module that resolves wins; rest dedup.
        self._by_module = {}        # modname -> list of (remainder_tuple, full_symbol)
        for sym in (s for s in (symbols or []) if s):
            parts = sym.split(".")
            if len(parts) < 2:
                self._mark_skip(sym, "no-module")
                continue
            for i in range(1, len(parts)):
                self._by_module.setdefault(".".join(parts[:i]), []).append((tuple(parts[i:]), sym))

    def _mark_skip(self, full, reason):
        if full not in self._skip_seen:
            self._skip_seen.add(full)
            self.skipped.append((full, reason))

    # ── lifecycle ──────────────────────────────────────────────────────────────
    def start(self):
        if self.patch_aliases:
            try:
                self._aliases = _alias_index()
            except Exception:
                self._aliases = None
        for modname in list(self._by_module):
            mod = sys.modules.get(modname)
            if mod is not None:
                self._wrap_module(modname, mod)        # already imported -> patch now
        self._finder = _WrapFinder(self, set(self._by_module))
        sys.meta_path.insert(0, self._finder)          # patch the rest on import
        install_warning_capture(self)

    def stop(self):
        remove_warning_capture(self)
        if self._finder is not None:
            try:
                sys.meta_path.remove(self._finder)
            except ValueError:
                pass
            self._finder = None
        for undo in reversed(self._restore):
            try:
                undo()
            except Exception:
                pass
        self._restore = []
        self._warned.clear()

    # ── runtime warnings ───────────────────────────────────────────────────────
    def _warning_frame(self, frame):
        """``(depth, wrapper frame)`` of the innermost boundary call this backend has in
        flight above ``frame`` (the ``warn()`` caller), or None. A wrapper whose caller is
        not our code is dependency-internal traffic (never recorded): the walk goes on to
        an outer wrapper. It stops at our own code (see ``FrameBackend._warning_frame``)."""
        codes = _WRAPPER_CODES
        if not codes:
            return None
        near = self.near
        depth = 0
        while frame is not None and depth < _WARN_DEPTH:
            code = frame.f_code
            if any(code is c for c in codes):
                if frame.f_locals.get("backend") is self:
                    caller = frame.f_back
                    if near is None or (caller is not None and near.matches(
                            caller.f_globals.get("__name__", "") or "", caller)):
                        return depth, frame
            elif near is not None and near.matches(
                    frame.f_globals.get("__name__", "") or "", frame):
                return None
            frame = frame.f_back
            depth += 1
        return None

    def _add_warning(self, frame, key):
        warned_add(self._warned, frame, key)

    def _take_warnings(self):
        """The warnings attributed to the calling wrapper's call (its frame is the caller)."""
        try:
            frame = sys._getframe(1)
            return warned_take(self._warned, frame, frame.f_code)
        except Exception:
            return None

    # ── wrapping ───────────────────────────────────────────────────────────────
    @staticmethod
    def _resolve(module, remainder):
        """Walk an attribute chain on a module. Returns (holder, attr, obj, ok)."""
        obj = module
        for part in remainder[:-1]:
            try:
                obj = getattr(obj, part)
            except AttributeError:
                return None, None, None, False
        attr = remainder[-1]
        try:
            return obj, attr, getattr(obj, attr), True
        except AttributeError:
            return obj, attr, None, False

    def _wrap_module(self, modname, module):
        for remainder, full in list(self._by_module.get(modname, [])):
            if full in self._done:
                continue
            if remainder[-1] == "__del__":
                self._mark_skip(full, "finalizer")      # runs at GC time: not a contact
                self._done.add(full)
                continue
            holder, attr, obj, ok = self._resolve(module, remainder)
            if not ok:
                if len(remainder) == 1:                 # module loaded but the attr is absent
                    self._mark_skip(full, "attr-missing")
                continue                                # else a more specific candidate may resolve
            if getattr(obj, _WRAP_MARK, False):
                self._done.add(full)
                continue
            if isinstance(obj, type):
                self._wrap_class(obj, full)
            elif callable(obj):
                self._wrap_attr(holder, attr, obj, full)
            else:
                self._mark_skip(full, "not-callable")
            self._done.add(full)

    def _wrap_attr(self, holder, attr, real_obj, qualname):
        """Wrap a module-level function OR a class method (instance / static / class)."""
        raw = getattr(holder, "__dict__", {}).get(attr)
        if isinstance(raw, staticmethod):
            inner, rebind = raw.__func__, staticmethod
        elif isinstance(raw, classmethod):
            inner, rebind = raw.__func__, classmethod
        else:
            inner, rebind = real_obj, None
        backend = self
        # Canonical identity, once per wrapped symbol: the unwrapped callable's own name,
        # plus its receiver parameter (self/cls) for the per-call ``own``.
        ident = callable_identity(inner)
        if ident[0] is None:
            ident = (qualname, SYMQ_FALLBACK)
        if isinstance(holder, type) and (ident[1] == SYMQ_FALLBACK or "<locals>" in ident[0]):
            ident = self._class_identity(holder, inner, ident)
        ident = ident + callable_receiver(inner, ident[0])

        not_near = self._not_near

        @functools.wraps(inner)
        def wrapper(*a, **kw):
            if not_near:
                code = _getframe(1).f_code
                if not_near.get(id(code)) is code:
                    return inner(*a, **kw)      # never a boundary contact: nothing to record
            t0 = _now()
            try:
                result = inner(*a, **kw)
            except BaseException as exc:
                backend._safe_record(qualname, inner, a, kw, None, exc, ident, _now() - t0,
                                     backend._take_warnings() if backend._warned else None)
                raise
            backend._safe_record(qualname, inner, a, kw, result, None, ident, _now() - t0,
                                 backend._take_warnings() if backend._warned else None)
            return result

        _note_wrapper_code(wrapper.__code__)
        setattr(wrapper, _WRAP_MARK, True)
        wrapper.__wrapped__ = inner
        new = rebind(wrapper) if rebind is not None else wrapper
        had_own = attr in getattr(holder, "__dict__", {})
        original = holder.__dict__.get(attr) if had_own else None
        try:
            setattr(holder, attr, new)
        except (TypeError, AttributeError):
            self._mark_skip(qualname, "readonly")       # C / extension type -> hybrid's setprofile
            return
        if had_own:
            self._restore.append(_restore_setattr(holder, attr, original))
        else:
            self._restore.append(_restore_delattr(holder, attr))
        if rebind is None and self._aliases and isinstance(holder, _MODULE):
            self._patch_aliases(holder, attr, real_obj, new)

    def _patch_aliases(self, holder, attr, real_obj, new):
        """Rebind every other module-global name bound to ``real_obj`` (``from dep import f``
        in user code, re-exports in package ``__init__``) to the wrapper, reversibly."""
        for module, name in self._aliases.get(id(real_obj), ()):
            if module is holder and name == attr:
                continue
            try:
                if module.__dict__.get(name) is not real_obj:
                    continue                            # rebound since the index was built
                setattr(module, name, new)
            except Exception:
                continue
            self._restore.append(_restore_setattr(module, name, real_obj))

    @staticmethod
    def _class_identity(holder, inner, ident):
        """A factory-made method attached to ``holder`` (``cls.mean = mean``): named after the
        class that holds it, exactly as the setprofile backend names it via the receiver."""
        try:
            base = unwrap(inner)
            code = getattr(base, "__code__", None)
            if code is not None:
                owner, _func = class_member(holder, code)
                if owner is not None:
                    return (owner + "." + code.co_name, SYMQ_RECEIVER)
        except Exception:
            pass
        return ident

    def _wrap_class(self, cls, qualname):
        backend = self
        real_init = cls.__init__
        had_own = "__init__" in cls.__dict__
        # The constructor that actually runs (possibly inherited) — the same identity the
        # setprofile backend sees for this construction. A C-level __init__ has no frame;
        # name it after the class.
        ident = callable_identity(real_init)
        if ident[0] is None:
            ident = ("{0}.{1}.__init__".format(cls.__module__, cls.__qualname__),
                     SYMQ_FALLBACK)
        # The receiver is the instance under construction (``bound_args[0]``).
        ident = ident + ("self", ident[0].rsplit(".", 1)[0])

        not_near = self._not_near

        @functools.wraps(real_init)
        def init_wrapper(self_obj, *a, **kw):
            if not_near:
                code = _getframe(1).f_code
                if not_near.get(id(code)) is code:
                    real_init(self_obj, *a, **kw)   # never a boundary contact
                    return
            bound_args = (self_obj,) + a            # include self so it binds to the real signature
            t0 = _now()
            try:
                real_init(self_obj, *a, **kw)
            except BaseException as exc:
                backend._safe_record(qualname, real_init, bound_args, kw, None, exc, ident,
                                     _now() - t0,
                                     backend._take_warnings() if backend._warned else None)
                raise
            # construction "result" is the instance -> shape becomes opaque:<Class>
            backend._safe_record(qualname, real_init, bound_args, kw,
                                 OutcomeMarker(typename(self_obj)), None, ident,
                                 _now() - t0,
                                 backend._take_warnings() if backend._warned else None)

        _note_wrapper_code(init_wrapper.__code__)
        setattr(init_wrapper, _WRAP_MARK, True)
        init_wrapper.__wrapped__ = real_init
        try:
            cls.__init__ = init_wrapper
        except (TypeError, AttributeError):
            self._mark_skip(qualname, "readonly-class")
            return
        if had_own:
            self._restore.append(_restore_setattr(cls, "__init__", real_init))
        else:
            self._restore.append(_restore_delattr(cls, "__init__"))

    # ── recording ──────────────────────────────────────────────────────────────
    def _safe_record(self, qualname, real, args, kwargs, result, exc,
                     ident=(None, None, None, None), dur=None, warns=None):
        """Tracing must never alter the wrapped API's return/raise behavior."""
        try:
            self._record(qualname, real, args, kwargs, result, exc, ident, dur, warns)
        except Exception:
            note = getattr(self.sink, "note_failure", None)
            if note is not None:
                try:
                    note("sink_failures")
                except Exception:
                    pass

    def _record(self, qualname, real, args, kwargs, result, exc,
                ident=(None, None, None, None), dur=None, warns=None):
        # caller frame: 0=_record, 1=_safe_record, 2=wrapper, 3=the code that
        # made the call (our boundary near side)
        try:
            caller = sys._getframe(3)
        except ValueError:
            caller = None
        if caller is not None and _WRAPPER_CODES:
            ccode = caller.f_code
            for known in _WRAPPER_CODES:
                if ccode is known:
                    self._never_near(ccode)
                    return      # a wrapped callable called by compiled code that a wrapper
                                # called: dependency-internal, whatever the near matcher says
        if self._require_call and caller is not None and not _at_call(caller):
            return          # C-mediated (``==``, a property, compiled code calling a method)
        if self.near is not None:
            mod = caller.f_globals.get("__name__", "") if caller is not None else ""
            if caller is None or not self.near.matches(mod, caller):
                if caller is not None:
                    self._never_near(caller.f_code)
                return                                  # internal dep<->dep call, not our boundary
        site = None
        if caller is not None:
            code = caller.f_code
            site = {"file": code.co_filename, "line": caller.f_lineno, "func": code.co_name}
        if type(result) in _GENERATOR_TYPES:
            dur = None          # creating a generator is not its work (as setprofile/monitoring)
        sym = ident[0] or qualname
        raised = typename(exc) if exc is not None else None
        # Sampling before normalization; raises are always retained. An unsampled call is
        # still counted (tally), without its values.
        admit = getattr(self.sink, "admit", None)
        if admit is None:
            sampled = True
        elif self._admit_key:
            key = (sym, site["file"], site["line"]) if site is not None else (sym, None, None)
            sampled = admit(key=key, force=exc is not None)
        else:
            sampled = admit(force=exc is not None)
        if not sampled:
            if self._tally is not None:
                self._tally(qualname, sym, site, "raise" if exc is not None else "return",
                            raised=raised, dur=dur, warnings=warns or None)
            return
        inputs = self._inputs(real, args, kwargs)
        extra = {"admitted": True} if admit is not None else {}
        if self._sym_kw:
            extra["sym"] = sym
            extra["symq"] = ident[1] if ident[0] else SYMQ_FALLBACK
        if self._own_kw and ident[2] is not None and args:
            own = receiver_owner(args[0], ident[2], ident[3])
            if own is not None:
                extra["own"] = own
        if self._dur_kw:
            extra["dur"] = dur
        if self._tid_kw:
            extra["tid"] = _get_ident()
        if warns and self._warn_kw:
            extra["warnings"] = warns
        if exc is not None:
            if self._bind_kw and self._binding_failure(real, args, kwargs, exc):
                extra["binding"] = True
            self.sink.on_raise(qualname, inputs, raised, site, **extra)
        else:
            self.sink.on_return(qualname, inputs, result, site, **extra)

    def _never_near(self, code):
        """Remember a caller code whose calls are never recorded (decided per code, as the
        frame backends decide the near side), so later calls skip the record path."""
        not_near = self._not_near
        if len(not_near) >= _NOT_NEAR_MAX:
            not_near.clear()
        not_near[id(code)] = code   # holds the code: its id cannot be recycled

    _SIG_FAILED = object()

    def _binding_failure(self, real, args, kwargs, exc):
        """True if ``exc`` came from binding the arguments, not from the callee's body: a
        TypeError no deeper frame raised (the traceback ends at the wrapper) that the callee's
        signature itself rejects for these arguments."""
        try:
            if not isinstance(exc, TypeError):
                return False
            tb = exc.__traceback__
            if tb is not None and tb.tb_next is not None:
                return False
            sig = self._signature(real)
            if sig is self._SIG_FAILED:
                return False
            try:
                sig.bind(*args, **kwargs)
            except TypeError:
                return True
        except Exception:
            pass
        return False

    def _signature(self, real):
        """``inspect.signature`` once per callable (it is costly and was paid on every call)."""
        cache = self.__dict__.setdefault("_sig_cache", {})
        ent = cache.get(id(real))
        if ent is not None and ent[0] is real:  # entry holds `real`, so its id can't be recycled
            return ent[1]
        try:
            sig = inspect.signature(real)
        except Exception:
            sig = self._SIG_FAILED
        cache[id(real)] = (real, sig)
        return sig

    def _inputs(self, real, args, kwargs):
        """Bind to the signature when possible — the same v2 form as setprofile's: ``bound``
        without arguments at their default, ``defaulted``, ``varargs``/``kwargs``."""
        try:
            sig = self._signature(real)
            if sig is self._SIG_FAILED:
                raise TypeError("no signature")
            return bind_inputs(sig, args, kwargs, self._normalize)
        except Exception:
            norm = self._normalize
            return {"args": [norm(v) for v in args],
                    "kwargs": {k: norm(v) for k, v in kwargs.items()}}


class _WrapFinder(object):
    """Meta-path finder that wraps a target module immediately after it is imported."""

    def __init__(self, backend, target_modules):
        self.backend = backend
        self.targets = set(target_modules)
        self._busy = set()                              # re-entrancy guard for find_spec

    def find_spec(self, name, path=None, target=None):
        if name not in self.targets or name in self._busy:
            return None
        self._busy.add(name)
        try:
            spec = importlib.util.find_spec(name)       # the real spec, via the other finders
        except Exception:
            spec = None
        finally:
            self._busy.discard(name)
        if spec is None or spec.loader is None:
            return None
        spec.loader = _PatchingLoader(spec.loader, name, self.backend)
        return spec


class _PatchingLoader(object):
    """Delegates to the real loader, then wraps the target symbols once the module is executed."""

    def __init__(self, real, modname, backend):
        self._real = real
        self._modname = modname
        self._backend = backend

    def create_module(self, spec):
        if hasattr(self._real, "create_module"):
            return self._real.create_module(spec)
        return None

    def exec_module(self, module):
        self._real.exec_module(module)
        try:
            self._backend._wrap_module(self._modname, module)
        except Exception:
            pass

    def __getattr__(self, item):                        # delegate get_data/get_source/etc.
        return getattr(self._real, item)


_CYTHON_TYPES = frozenset(("cython_function_or_method", "fused_cython_function"))
_KEEP_DUNDERS = frozenset(("__init__", "__call__", "__new__"))


def _is_cython(obj):
    return type(obj).__name__ in _CYTHON_TYPES


_SITE_DIRS = ("site-packages", "dist-packages")


def stdlib_file(filename, roots):
    """True for a file of the standard library itself: under a stdlib root, not under that
    root's ``site-packages``. A venv's ``platstdlib`` is ``<venv>/lib/pythonX.Y`` and a system
    Python's stdlib is ``/usr/local/lib/pythonX.Y``: both CONTAIN the installed packages, so a
    plain prefix test takes every compiled dependency (pydantic v1's Cython) for stdlib."""
    for root in roots or ():
        if filename.startswith(root):
            head = filename[len(root):].split(os.sep, 1)[0]
            if head not in _SITE_DIRS:
                return True
    return False


class CompiledWrap(WrapBackend):
    """Wrap the Cython functions of dependency modules, under both frame backends.

    ``sys.setprofile`` below 3.12 reports only exact ``PyCFunction`` calls, never a
    ``cython_function_or_method`` (a compiled pydantic 1.10 gave zero events).
    ``sys.monitoring`` sees the call, but only as a compiled call: the callable and its first
    argument, no result. So both backends start this (``SetProfileBackend.start``,
    ``MonitoringBackend._start_compiled``) and a Cython call is recorded in one shape on both
    sides of a migration: the compiled module's module-level functions
    and the methods of the classes it defines (static / class / instance) are replaced, in
    memory, by the same transparent wrappers ``WrapBackend`` installs: full inputs and results,
    recorded only when the caller is the project's code. Underscore-prefixed names (hot
    internal dunders and helpers) are left alone, except ``__init__`` / ``__call__``. Methods of
    an extension type (``cdef class``) cannot be replaced (``skipped``: ``readonly``).

    Compiled modules are found at import (``ExtensionFileLoader.exec_module`` is wrapped while
    the backend runs) and at ``start`` (those already loaded). Reversible: ``stop`` restores."""

    name = "compiled-wrap"

    def __init__(self, sink, matcher, source=None, include_internal=False):
        WrapBackend.__init__(self, [], sink, source=source, include_internal=include_internal)
        self._matcher = matcher
        self._require_call = True
        self._stdlib = _stdlib_roots()   # a stdlib module is never wrapped in place
        self._scanned = set()
        self._loader_undo = None

    def start(self):
        try:
            from importlib.machinery import EXTENSION_SUFFIXES, ExtensionFileLoader
        except ImportError:
            return
        self._suffixes = tuple(EXTENSION_SUFFIXES)
        orig = ExtensionFileLoader.__dict__.get("exec_module")
        if orig is not None:
            backend = self

            def exec_module(loader, module):
                orig(loader, module)
                try:
                    backend.scan(module)
                except Exception:
                    pass

            ExtensionFileLoader.exec_module = exec_module
            self._loader_undo = _restore_setattr(ExtensionFileLoader, "exec_module", orig)
        for module in list(sys.modules.values()):
            try:
                self.scan(module)
            except Exception:
                pass

    def stop(self):
        if self._loader_undo is not None:
            try:
                self._loader_undo()
            except Exception:
                pass
            self._loader_undo = None
        WrapBackend.stop(self)

    def scan(self, module):
        """Wrap the compiled callables of ``module`` if it is a compiled dependency module."""
        if not isinstance(module, _MODULE):
            return
        modname = getattr(module, "__name__", "") or ""
        if modname in self._scanned:
            return
        filename = getattr(module, "__file__", None) or ""
        if not filename.endswith(self._suffixes):
            return
        self._scanned.add(modname)
        if stdlib_file(filename, self._stdlib):
            return          # the profile hook sees stdlib code; a wrapper would be visible to it
        if not self._matcher.matches(modname, None):
            return
        for name, obj in list(module.__dict__.items()):
            if _is_cython(obj) and getattr(obj, "__module__", None) == modname:
                if name[:1] != "_":
                    self._wrap_one(module, name, modname + "." + name)
            elif isinstance(obj, type) and getattr(obj, "__module__", None) == modname:
                for mname, raw in list(obj.__dict__.items()):
                    inner = raw.__func__ if isinstance(raw, (staticmethod, classmethod)) else raw
                    if not _is_cython(inner):
                        continue
                    if mname[:1] == "_" and mname not in _KEEP_DUNDERS:
                        continue
                    self._wrap_one(obj, mname, "{0}.{1}.{2}".format(modname, obj.__qualname__,
                                                                   mname))

    def _wrap_one(self, holder, attr, qualname):
        try:
            real = getattr(holder, attr)
            raw = holder.__dict__.get(attr)
            inner = raw.__func__ if isinstance(raw, (staticmethod, classmethod)) else raw
            if getattr(inner, _WRAP_MARK, False):
                return
            self._wrap_attr(holder, attr, real, qualname)
        except Exception:
            pass


class HybridBackend(object):
    """WrapBackend (precise, low-overhead) + SetProfileBackend (completeness for the C-remainder).

    ``wrap`` observes the listed symbols with full fidelity (incl. true Python raises) and ~no
    process tax; ``setprofile`` then covers the rest of the dependency boundary — the symbols wrap
    couldn't patch (C/extension-type methods, surfaced in ``skipped``) plus any dynamic/aliased
    usage not in the list. The two are de-duplicated: setprofile skips calls invoked by a wrap
    wrapper, so a wrapped symbol is never recorded twice (which would conflict on raises). The
    cost is setprofile's process tax — this is the completeness/audit mode, not the prod-cheap
    one (that is pure ``--wrap``). C-implemented methods still only expose raises via c_exception.
    """

    name = "hybrid"

    def __init__(self, symbols, sink, source=None, include_internal=False):
        self._symbols = [s for s in (symbols or []) if s]
        self._sink = sink
        self._source = source
        self._internal = include_internal
        self.wrap = WrapBackend(self._symbols, sink, source=source,
                                include_internal=include_internal)
        self.sp = None

    @property
    def skipped(self):
        return self.wrap.skipped

    def start(self):
        self.wrap.start()
        prefixes = sorted({s.split(".", 1)[0] for s in self._symbols})  # top-level dep packages
        if prefixes:
            self.sp = SetProfileBackend(prefixes, self._sink, source=self._source,
                                        include_internal=self._internal)
            self.sp.skip_wrapped = True                  # don't double-record what wrap owns
            self.sp.compiled_wrap = False                # explicit symbols only
            self.sp.start()

    def stop(self):
        if self.sp is not None:
            self.sp.stop()
        self.wrap.stop()


def _partition_impact_targets(targets):
    """Split explicit symbols from opt-in module prefixes.

    A trailing ``.*`` (or ``.``) is the unambiguous prefix notation.  Plain
    dotted names are treated as symbols and can therefore use wrapping without
    enabling a process-wide profile hook.
    """
    symbols = []
    prefixes = []
    seen_symbols = set()
    seen_prefixes = set()
    for raw in targets or ():
        value = (raw or "").strip()
        if not value:
            continue
        if value.endswith(".*"):
            prefix = value[:-2].rstrip(".")
        elif value.endswith("."):
            prefix = value.rstrip(".")
        elif "." not in value:
            # A top-level package cannot identify a callable, so it is naturally
            # a prefix even without the explicit wildcard notation.
            prefix = value
        else:
            if value not in seen_symbols:
                symbols.append(value)
                seen_symbols.add(value)
            continue
        if prefix and prefix not in seen_prefixes:
            prefixes.append(prefix)
            seen_prefixes.add(prefix)
    return symbols, prefixes


class TargetedBackend(object):
    """Instrument only the supplied impact set.

    Exact API symbols use transparent wrapping and install no profile hook.
    Explicit ``package.module.*`` entries use ``setprofile`` restricted to that
    module prefix.  Mixed sets combine both and de-duplicate wrapped calls.
    """

    name = "targeted"

    def __init__(self, targets, sink, source=None, include_internal=False, patch_aliases=False):
        self.symbols, self.prefixes = _partition_impact_targets(targets)
        self.wrap = None
        self.profile = None
        if self.symbols:
            self.wrap = WrapBackend(
                self.symbols, sink, source=source, include_internal=include_internal,
                patch_aliases=patch_aliases,
            )
        if self.prefixes:
            self.profile = SetProfileBackend(
                self.prefixes, sink, source=source, include_internal=include_internal,
            )
            self.profile.skip_wrapped = bool(self.wrap)
            self.profile.compiled_wrap = False

    @property
    def skipped(self):
        return self.wrap.skipped if self.wrap is not None else []

    @property
    def uses_profiling(self):
        return self.profile is not None

    def start(self):
        if self.wrap is not None:
            self.wrap.start()
        if self.profile is not None:
            try:
                self.profile.start()
            except BaseException:
                if self.wrap is not None:
                    self.wrap.stop()
                raise

    def stop(self):
        if self.profile is not None:
            self.profile.stop()
        if self.wrap is not None:
            self.wrap.stop()
