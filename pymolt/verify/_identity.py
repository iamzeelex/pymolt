"""Canonical runtime identity (``sym``) and caller-passed inputs for boundary events.

INJECTED RUNTIME — pure stdlib, Python 3.4+ syntax (see ``_normalize`` header for the rules).
Loaded inside the target application's interpreter: no ``pymolt.*``, no third-party imports,
no f-strings. Callers guard every entry point; nothing here may raise into the traced program.

Identity of a called code object — computed ONCE per code object (the backend stores the
result in its per-code decision cache):

  1. 3.11+: ``<frame module>.<co_qualname>`` (exact) — except for generated or nested code
     (``co_filename`` starting with ``<`` or a ``<locals>`` qualname, e.g. dataclass methods
     named ``__create_fn__.<locals>.__init__``): there a unique index hit (step 2) wins, since
     the generator re-set the function's ``__qualname__`` to its place in the class.
  2. older: a lazy per-module index (code object -> ``__qualname__``) built from the frame's
     module globals: module-level functions, classes and their methods (plain, static, class,
     property accessors, ``__wrapped__`` chains), nested classes. A unique hit is exact. A miss
     rebuilds the index a bounded number of times while the module keeps growing.
  3. a decorator wrapper whose frame holds exactly one function-valued closure cell, that
     function being a dependency function, is attributed to that function (``__wrapped__``
     chain followed): ``symq = "unwrapped"``. Two shapes: a generic wrapper (free variables +
     ``*args``/``**kwargs``), and a signature-preserving one whose parameters are exactly the
     inner function's (psutil's ``memoize_when_activated``: ``wrapper(self)`` around
     ``memory_info(self)``). One wrapper code serves every function it decorated, so its own
     name would merge them. Per call (cells differ per decorated function); cached per inner
     function. A ``__class__`` cell (``super()``) is never a candidate. Both backends name a
     wrapper this way on every call, so the name never depends on which event saw the call.
  4. a method created by a factory and attached to classes elsewhere (pandas 1.5
     ``NDFrame._add_numeric_operations(cls)`` -> ``cls.mean = mean``): when the index misses or
     the name has ``<locals>`` and the code takes ``self``/``cls``, the receiver's MRO is walked
     for the class whose namespace holds a function with this very code under ``co_name``:
     ``<that class's module.qualname>.<co_name>``, ``symq = "receiver"``, with THAT function's
     defaults. One code object is shared by every class the factory decorated (DataFrame.mean
     and Series.mean), so this is cached per (code, receiver type), never per code.
  5. otherwise ``<frame module>.<co_name>``, ``symq = "fallback"``.

The function object found in step 2 (or step 3) supplies the parameter defaults, so
``bound`` keeps only arguments NOT identical (``is``) to their default and ``defaulted`` lists
the parameters left at it. Unknown defaults: every argument is kept, ``defaulted`` omitted.

Receiver (``own``): when the callee's first positional parameter is ``self``/``cls``, the
receiver's class ``module.qualname`` (``type(self)``, or the class itself for ``cls``) is
reported if it differs from the class prefix of ``sym`` — an inherited ``__init__`` defined on
a base elsewhere still names the class the caller constructed. Per call: one ``type()`` and a
per-type cache.

Introspection here never runs user code: module and class namespaces are read through the
raw ``__dict__`` mappings and exact type checks, never ``getattr``/``isinstance`` on arbitrary
values (a werkzeug ``LocalProxy`` in a module namespace would otherwise resolve itself).
"""
import inspect
import types
import weakref

SYMQ_UNWRAPPED = "unwrapped"
SYMQ_FALLBACK = "fallback"
SYMQ_RECEIVER = "receiver"

# Read once per code object (never per call), so tests can force the pre-3.11 index path.
HAS_CO_QUALNAME = hasattr((lambda: None).__code__, "co_qualname")

_CO_VARARGS = 0x04
_CO_VARKEYWORDS = 0x08
_FUNCTION = types.FunctionType
_METHOD = types.MethodType
_MISSING = object()

_MAX_WRAPPED = 16           # __wrapped__ chain guard
_INDEX_MAX_REBUILDS = 3     # per module; only while the module namespace keeps growing
_INDEX_MAX_MODULES = 4096
_INNER_CACHE_MAX = 20000

# C-level accessors: reading a class namespace / name through these can never be intercepted
# by a metaclass ``__getattribute__``.
_TYPE_DICT = type.__dict__["__dict__"]
_TYPE_QUALNAME = type.__dict__["__qualname__"]
_TYPE_NAME = type.__dict__["__name__"]
_TYPE_MODULE = type.__dict__["__module__"]

_RECEIVERS = ("self", "cls")
_OWNER_CACHE_MAX = 20000
_OWNERS = {}                # id(type) -> (held(type), "module.qualname" | None)
_RECV_CACHE_MAX = 20000
_TYPE_MRO = type.__dict__["__mro__"]
_TYPE_FLAGS = type.__dict__["__flags__"]
_HEAPTYPE = 512             # Py_TPFLAGS_HEAPTYPE: a class defined in Python (or a heap C type)

# ── what a decision cache keeps of the object it decided ─────────────────────
# A cache keyed by ``id(obj)`` must know ``obj`` again (an id is reused once its object
# dies), but must not keep it alive: a tracer may not change how long the traced program's
# objects live. A builtin method bound to a receiver holds the receiver, a closure its
# cells, a ``functools.partial`` its arguments; holding them ran their finalizers late or
# never and kept weak references alive. A class holds its namespace (methods, their
# closures, class attributes), and a class may be made at run time and dropped again
# (Flask's debug helper makes one per request, ``unittest.mock`` one per mock): the locust
# suite's ``isinstance`` over ``gc.get_objects()`` met a dead ``weakref.proxy`` that a
# cached per-request class still reached.
_WEAKREF = weakref.ref
_BUILTIN = types.BuiltinFunctionType
_MODULE = types.ModuleType
# Class-level builtin descriptors (``dict.items``, ``object.__init__``, ``dict.fromkeys``):
# they live as long as their class and take no weak reference.
_DESCRIPTOR_TYPES = (type(str.join), type(object.__init__), type(dict.__dict__["fromkeys"]))


def held(obj):
    """What a cache keeps of ``obj`` to know it again (``is_held``): ``obj`` itself when it
    lives as long as the interpreter or its class or module keeps it alive anyway (a static C
    type, a builtin descriptor, a builtin function of a module, a function its module's
    namespace names: ``_named_by_module``), else a weak reference (a class defined in Python
    among them), or None when ``obj`` takes none (never known again: every later object is a
    different one). Never runs user code."""
    t = type(obj)
    if t in _DESCRIPTOR_TYPES:
        return obj
    if issubclass(t, type):
        if not (_TYPE_FLAGS.__get__(obj, type) & _HEAPTYPE):
            return obj
    elif t is _BUILTIN:
        owner = obj.__self__
        if owner is None or type(owner) is _MODULE:
            return obj
    elif t is _FUNCTION and _named_by_module(obj):
        return obj
    try:
        return _WEAKREF(obj)
    except TypeError:
        return None


def _named_by_module(func):
    """True if ``func`` is what its module's namespace holds under its qualified name: a
    module function, or a method of a class of the module (also as a static or class
    method). A closure, a lambda, a function made at run time is not."""
    try:
        qual = func.__qualname__
        if type(qual) is not str or "<" in qual:
            return False
        parts = qual.split(".")
        obj = dict.get(func.__globals__, parts[0])
        for part in parts[1:]:
            if not issubclass(type(obj), type):
                return False
            obj = _TYPE_DICT.__get__(obj, type).get(part)
        t = type(obj)
        if t is staticmethod or t is classmethod:
            obj = obj.__func__
        return obj is func
    except Exception:
        return False


def is_held(ref, obj):
    """True if ``ref`` (from ``held``) is ``obj``, still alive (a dead reference is no object,
    not None; a None ``ref`` knows nothing)."""
    if ref is obj:
        return ref is not None
    return type(ref) is _WEAKREF and obj is not None and ref() is obj


class CodeIdentity:
    """Everything the hot path needs about one code object, computed once."""

    __slots__ = ("q", "sym", "symq", "argnames", "posnames", "varargs", "varkw",
                 "defaults", "cells", "same_sig", "recv", "prefix", "via_recv")

    def derived(self):
        """A copy, to specialise per receiver type."""
        other = CodeIdentity()
        for name in CodeIdentity.__slots__:
            setattr(other, name, getattr(self, name))
        return other


class InnerIdentity:
    """A wrapped dependency function reached through a generic wrapper's closure."""

    __slots__ = ("sym", "sig", "recv", "prefix", "params")


def _receiver_of(posnames, sym):
    """(receiver param name, class prefix of ``sym``) when the first parameter is self/cls."""
    if posnames and posnames[0] in _RECEIVERS and sym:
        return posnames[0], sym.rsplit(".", 1)[0]
    return None, None


def type_label(t):
    """``module.qualname`` of a class (per-type cache, C accessors only)."""
    ent = _OWNERS.get(id(t))
    if ent is not None:
        ref = ent[0]                        # ``is_held``, inline (per call)
        if ref is t or (type(ref) is _WEAKREF and ref() is t):
            return ent[1]
    try:
        mod = _TYPE_MODULE.__get__(t, type)
        qual = _TYPE_QUALNAME.__get__(t, type)
        label = mod + "." + qual if isinstance(mod, str) and mod else qual
    except Exception:
        label = None
    ref = held(t)
    if ref is not None:
        if len(_OWNERS) >= _OWNER_CACHE_MAX:
            _OWNERS.clear()
        _OWNERS[id(t)] = (ref, label)
    return label


def receiver_owner(value, recv, prefix):
    """The receiver's class label when it differs from the callee's class prefix, else None."""
    t = type(value)
    if recv == "cls" and issubclass(t, type):
        t = value                         # classmethod / __new__: the class itself
    label = type_label(t)
    if label is None or label == prefix:
        return None
    return label


def _function_with_code(obj, code):
    """The function object behind a class-namespace value whose code is ``code``, or None."""
    for _ in range(_MAX_WRAPPED):
        t = type(obj)
        if t is _FUNCTION:
            if obj.__code__ is code:
                return obj
        elif issubclass(t, (staticmethod, classmethod)):
            obj = obj.__func__
            continue
        elif issubclass(t, property):
            for acc in (obj.fget, obj.fset, obj.fdel):
                if acc is not None:
                    found = _function_with_code(acc, code)
                    if found is not None:
                        return found
            return None
        nxt = _wrapped_of(obj)
        if nxt is None or nxt is obj:
            return None
        obj = nxt
    return None


def class_member(cls, code):
    """(``module.qualname`` of the first class in ``cls``'s MRO whose namespace holds a
    function with ``code`` under ``co_name``, that function) or (None, None)."""
    name = code.co_name
    for owner in _TYPE_MRO.__get__(cls, type):
        value = _TYPE_DICT.__get__(owner, type).get(name, _MISSING)
        if value is _MISSING:
            continue
        func = _function_with_code(value, code)
        if func is not None:              # else overridden here: keep walking (super() call)
            label = type_label(owner)
            return (label, func) if label else (None, None)
    return None, None


def callable_receiver(obj, sym):
    """(receiver param name, class prefix) for a callable the wrap backend holds."""
    try:
        base = unwrap(obj)
        if type(base) is _METHOD:
            return None, None             # already bound: the receiver is not in the args
        if type(base) is not _FUNCTION:
            return None, None
        return _receiver_of(code_args(base.__code__)[1], sym)
    except Exception:
        return None, None


def code_args(code):
    """(named params in order, positional params, *args name, **kwargs name) of a code object.

    The same split ``inspect.getargs`` makes, without its per-call list building."""
    names = code.co_varnames
    npos = code.co_argcount            # includes positional-only (3.8+)
    nnamed = npos + code.co_kwonlyargcount
    varargs = varkw = None
    i = nnamed
    if code.co_flags & _CO_VARARGS:
        varargs = names[i]
        i += 1
    if code.co_flags & _CO_VARKEYWORDS:
        varkw = names[i]
    return tuple(names[:nnamed]), tuple(names[:npos]), varargs, varkw


def _wrapper_cells(code, argnames, posnames, varargs, varkw):
    """(closure cells to look in for a wrapped function, signature-preserving shape?).

    Generic wrapper: free variables + ``*args``/``**kwargs``. Signature-preserving wrapper:
    free variables, positional parameters only (the inner function must then have exactly
    these, checked per call). ``__class__`` (the ``super()`` cell) never holds the wrapped."""
    cells = tuple(n for n in code.co_freevars if n != "__class__")
    if not cells:
        return (), False
    if varargs or varkw:
        return cells, False
    if posnames and argnames == posnames:
        return cells, True
    return (), False


def wraps_inner(ci, inner):
    """True when ``inner`` (from a wrapper's closure) is what the wrapper stands for: any inner
    for a generic wrapper, an inner with exactly the wrapper's parameters otherwise."""
    return inner is not None and (not ci.same_sig or inner.params == ci.posnames)


def _defaults_of(func, code):
    """{param: default object} of ``func`` for ``code``; None when unknown."""
    if func is None:
        return None
    try:
        if func.__code__ is not code:
            return None
        out = {}
        defs = func.__defaults__
        if defs:
            pos = code.co_varnames[:code.co_argcount]
            for name, value in zip(pos[len(pos) - len(defs):], defs):
                out[name] = value
        kwdefs = func.__kwdefaults__
        if kwdefs:
            out.update(kwdefs)
        return out
    except Exception:
        return None


def _wrapped_of(obj):
    """``obj.__wrapped__`` without running user code (functions and lru_cache wrappers)."""
    t = type(obj)
    if t is _FUNCTION:
        return obj.__dict__.get("__wrapped__")
    if t is _METHOD:
        return _wrapped_of(obj.__func__)
    if _TYPE_NAME.__get__(t, type) == "_lru_cache_wrapper":
        try:
            d = object.__getattribute__(obj, "__dict__")
        except Exception:
            return None
        return d.get("__wrapped__") if type(d) is dict else None
    return None


def unwrap(obj):
    """Follow the ``__wrapped__`` chain (bounded, cycle-safe). Returns the innermost object."""
    seen = set()
    cur = obj
    for _ in range(_MAX_WRAPPED):
        seen.add(id(cur))
        nxt = _wrapped_of(cur)
        if nxt is None or id(nxt) in seen:
            break
        cur = nxt
    return cur


def _function_sym(func):
    """``<defining module>.<qualname>`` of a Python function: the module is the one whose
    globals the code runs in — exactly what a profile frame reports, so a wrapped call and a
    direct call of the same function agree (numpy-style ``__module__`` re-homing ignored)."""
    if type(func) is _METHOD:
        func = func.__func__
    if type(func) is not _FUNCTION:
        return None
    module = func.__globals__.get("__name__")
    qual = func.__qualname__
    if isinstance(module, str) and module and isinstance(qual, str) and qual:
        return module + "." + qual
    return None


def callable_identity(obj):
    """(sym, symq) of a callable held by the wrap backend: its ``__wrapped__``-unwrapped
    ``module.qualname``. ``(None, "fallback")`` when it has no usable name."""
    try:
        base = unwrap(obj)
        symq = SYMQ_UNWRAPPED if base is not obj else None
        sym = _function_sym(base)
        if sym is None:
            module = getattr(base, "__module__", None)
            qual = getattr(base, "__qualname__", None)
            if isinstance(module, str) and module and isinstance(qual, str) and qual:
                sym = module + "." + qual
        if sym is not None:
            return sym, symq
    except Exception:
        pass
    return None, SYMQ_FALLBACK


# ── inputs ─────────────────────────────────────────────────────────────────────

def frame_inputs(loc, ci, normalize):
    """v2 inputs of a frame at its ``call`` event (``loc`` = ``frame.f_locals``)."""
    bound = {}
    defaults = ci.defaults
    if defaults is None:
        for name in ci.argnames:
            bound[name] = normalize(loc.get(name))
        inputs = {"bound": bound}
    else:
        defaulted = []
        for name in ci.argnames:
            value = loc.get(name)
            if value is defaults.get(name, _MISSING):
                defaulted.append(name)
            else:
                bound[name] = normalize(value)
        inputs = {"bound": bound, "defaulted": defaulted}
    if ci.varargs is not None:
        inputs["varargs"] = normalize(loc.get(ci.varargs))
    if ci.varkw is not None:
        inputs["kwargs"] = normalize(loc.get(ci.varkw))
    return inputs


def bind_inputs(sig, args, kwargs, normalize):
    """Bind a call to ``sig`` (``bind_partial``) in the same v2 form as ``frame_inputs``:
    ``*args``/``**kwargs`` parameters go to ``varargs``/``kwargs`` (always present when the
    signature has them), named parameters to ``bound`` unless at their default."""
    arguments = sig.bind_partial(*args, **kwargs).arguments
    bound = {}
    defaulted = []
    inputs = {"bound": bound, "defaulted": defaulted}
    empty = inspect.Parameter.empty
    for name, param in sig.parameters.items():
        kind = param.kind
        if kind == param.VAR_POSITIONAL:
            inputs["varargs"] = normalize(tuple(arguments.get(name, ())))
        elif kind == param.VAR_KEYWORD:
            inputs["kwargs"] = normalize(dict(arguments.get(name, {})))
        elif name in arguments:
            value = arguments[name]
            if param.default is not empty and value is param.default:
                defaulted.append(name)
            else:
                bound[name] = normalize(value)
        elif param.default is not empty:
            defaulted.append(name)
    return inputs


def wrapper_call(loc, ci):
    """The (args, kwargs) a generic wrapper frame received, ready to re-bind to its inner."""
    args = [loc.get(name) for name in ci.posnames]
    if ci.varargs is not None:
        extra = loc.get(ci.varargs)
        if extra:
            args.extend(extra)
    kwargs = {}
    if ci.varkw is not None:
        kw = loc.get(ci.varkw)
        if kw:
            kwargs.update(kw)
    return args, kwargs


# ── per-module code -> qualname index (pre-3.11) ───────────────────────────────

def _index_add(obj, g, out, place=None):
    """Index ``obj`` (a namespace value) if it is, holds or wraps a function of module ``g``.

    ``place`` is ``Class.attr`` when ``obj`` sits directly in a class namespace: a generated
    method still named after its generator (``__create_fn__.<locals>.__init__``: dataclasses
    before 3.10) is indexed under its place in the class instead."""
    for _ in range(_MAX_WRAPPED):
        t = type(obj)
        if t is _FUNCTION:
            if obj.__globals__ is g:
                code = obj.__code__
                qual = obj.__qualname__
                if (place is not None and isinstance(qual, str) and "<locals>" in qual
                        and place.rsplit(".", 1)[-1] == code.co_name):
                    qual = place
                # functools.wraps copies the inner's __qualname__ onto the wrapper: a name
                # that does not end in the code's own name is not this code's identity.
                if isinstance(qual, str) and qual.rsplit(".", 1)[-1] == code.co_name:
                    cur = out.get(id(code))
                    if cur is None:
                        out[id(code)] = [code, qual, obj]
                    elif cur[1] != qual:
                        cur[1] = None          # one code object, two names: ambiguous
                        cur[2] = None
        elif issubclass(t, (staticmethod, classmethod)):
            obj = obj.__func__
            continue
        elif issubclass(t, property):
            for acc in (obj.fget, obj.fset, obj.fdel):
                if acc is not None:
                    _index_add(acc, g, out)
            return
        elif _TYPE_NAME.__get__(t, type) == "cached_property":
            try:
                inner = object.__getattribute__(obj, "__dict__").get("func")
            except Exception:
                return
            if inner is None:
                return
            obj = inner
            continue
        nxt = _wrapped_of(obj)
        if nxt is None or nxt is obj:
            return
        obj = nxt
        place = None                    # a wrapped inner function has a name of its own


def _index_class(cls, module, g, out, seen):
    if id(cls) in seen:
        return
    seen.add(id(cls))
    try:
        ns = _TYPE_DICT.__get__(cls, type)
        prefix = _TYPE_QUALNAME.__get__(cls, type) + "."
        items = list(ns.items())
    except Exception:
        return
    for name, value in items:
        try:
            if issubclass(type(value), type):
                ns2 = _TYPE_DICT.__get__(value, type)
                if (ns2.get("__module__") == module
                        and _TYPE_QUALNAME.__get__(value, type).startswith(prefix)):
                    _index_class(value, module, g, out, seen)
            else:
                _index_add(value, g, out, prefix + name if isinstance(name, str) else None)
        except Exception:
            pass


def build_index(module, g):
    """{id(code): [code, qualname | None (ambiguous), function | None]} for namespace ``g``."""
    out = {}
    seen = set()
    try:
        items = list(g.items())
    except Exception:
        return out
    for _name, value in items:
        try:
            if issubclass(type(value), type):
                owner = _TYPE_DICT.__get__(value, type).get("__module__")
                # Classes defined here, or re-homed onto a parent package (``__module__``
                # rewritten to the public path); imported foreign classes are skipped.
                if isinstance(owner, str) and (owner == module
                                               or module.startswith(owner + ".")):
                    _index_class(value, module, g, out, seen)
            else:
                _index_add(value, g, out)
        except Exception:
            pass
    return out


def _initializing(g):
    """True while the module is still executing its body (more definitions may follow)."""
    try:
        spec = g.get("__spec__")
        return bool(spec is not None and spec.__dict__.get("_initializing", False))
    except Exception:
        return False


class IdentityResolver:
    """Per-backend identity caches. ``in_target(module)`` decides whether a function found in
    a wrapper's closure is a dependency function (only those are attributed through the
    wrapper; a dependency decorator around the project's own function keeps its own name)."""

    def __init__(self, in_target=None):
        self._in_target = in_target
        self._index = {}    # module -> [namespace size, index, rebuilds, globals]
        self._inner = {}    # id(func) -> (held(func), InnerIdentity | None)
        self._inner_m = {}  # id(method.__func__) -> (held(func), InnerIdentity | None)
        # (id(code), id(receiver type)) -> (code, held(type), CodeIdentity | None)
        self._recv = {}

    # ── per code object ────────────────────────────────────────────────────────
    def code_identity(self, code, module, g):
        """(CodeIdentity, cacheable). ``g`` = the frame's globals (the module namespace).
        ``cacheable`` is False only for an index miss while the module is still importing."""
        module = module or ""
        argnames, posnames, varargs, varkw = code_args(code)
        ci = CodeIdentity()
        ci.q = module + "." + code.co_name                  # v1 legacy name, unchanged
        ci.argnames, ci.posnames, ci.varargs, ci.varkw = argnames, posnames, varargs, varkw
        ci.cells, ci.same_sig = _wrapper_cells(code, argnames, posnames, varargs, varkw)
        qual = None
        func = None
        cacheable = True
        if HAS_CO_QUALNAME:
            qual = code.co_qualname
            if module and g is not None:
                hit, cacheable = self._lookup(code, module, g)
                if hit is not None and hit[1] is not None:
                    # Generated code (dataclass ``__create_fn__.<locals>.__init__``, exec'd in
                    # the module's namespace) keeps the generator's name in co_qualname; the
                    # function's re-set ``__qualname__`` places it in its class.
                    synthetic = "<locals>" in qual or code.co_filename.startswith("<")
                    if hit[1] == qual or synthetic:
                        qual, func = hit[1], hit[2]
        elif module and g is not None:
            hit, cacheable = self._lookup(code, module, g)
            if hit is not None and hit[1] is not None:
                qual, func = hit[1], hit[2]
        if qual and module:
            ci.sym, ci.symq = module + "." + qual, None
        else:
            ci.sym, ci.symq = ci.q, SYMQ_FALLBACK
        ci.defaults = _defaults_of(func, code)
        ci.recv, ci.prefix = _receiver_of(posnames, ci.sym)
        # No function object (index miss) or a name no class reaches (``<locals>``): resolve
        # through the receiver at call time (step 4).
        ci.via_recv = ci.recv is not None and (func is None or "<locals>" in ci.sym)
        return ci, cacheable

    def receiver_identity(self, ci, code, value):
        """The identity of ``code`` as reached through receiver ``value`` (step 4), or None.

        Per call: one ``type()`` and a dict hit; resolved once per (code, receiver type)."""
        t = type(value)
        if ci.recv == "cls" and issubclass(t, type):
            t = value
        key = (id(code), id(t))
        ent = self._recv.get(key)
        if ent is not None and ent[0] is code:
            ref = ent[1]                    # ``is_held``, inline (per call)
            if ref is t or (type(ref) is _WEAKREF and ref() is t):
                return ent[2]
        try:
            owner, func = class_member(t, code)
            rci = None
            if owner is not None:
                rci = ci.derived()
                rci.via_recv = False
                if ci.symq is not None or "<locals>" in ci.sym:
                    rci.sym, rci.symq = owner + "." + code.co_name, SYMQ_RECEIVER
                    rci.prefix = owner
                rci.defaults = _defaults_of(func, code)
        except Exception:
            rci = None
        ref = held(t)
        if ref is not None:
            if len(self._recv) >= _RECV_CACHE_MAX:
                self._recv.clear()
            self._recv[key] = (code, ref, rci)  # holds the code: its id cannot be recycled
        return rci

    def _lookup(self, code, module, g):
        ent = self._index.get(module)
        if ent is None or ent[3] is not g:
            ent = self._build(module, g, 0)
        hit = ent[1].get(id(code))
        if hit is not None and hit[0] is code:
            return hit, True
        # Miss: the namespace may have grown since the index was built (definitions added
        # later, a lazily populated module). Rebuild, boundedly, only if it actually grew.
        if ent[2] < _INDEX_MAX_REBUILDS and len(g) != ent[0]:
            ent = self._build(module, g, ent[2] + 1)
            hit = ent[1].get(id(code))
            if hit is not None and hit[0] is code:
                return hit, True
        return None, not _initializing(g)

    def _build(self, module, g, rebuilds):
        size = len(g)
        ent = [size, build_index(module, g), rebuilds, g]
        if len(self._index) >= _INDEX_MAX_MODULES:
            self._index.clear()
        self._index[module] = ent
        return ent

    # ── per call, generic wrappers only ───────────────────────────────────────
    def wrapped_inner(self, loc, cells):
        """InnerIdentity of the single function-valued closure cell, or None."""
        found = None
        for name in cells:
            value = loc.get(name)
            t = type(value)
            if t is _FUNCTION or t is _METHOD:
                if found is not None and found is not value:
                    return None                         # two candidates: not attributable
                found = value
        if found is None:
            return None
        # A bound method is decided by its function (its signature drops the receiver): the
        # entry never holds the receiver, nor a closure what it captured (``held``).
        cache = self._inner
        func = found
        if type(found) is _METHOD:
            cache = self._inner_m
            func = found.__func__
        ent = cache.get(id(func))
        if ent is not None and is_held(ent[0], func):
            return ent[1]
        try:
            info = self._inner_identity(found)
        except Exception:
            info = None
        ref = held(func)
        if ref is not None:
            if len(cache) >= _INNER_CACHE_MAX:
                cache.clear()
            cache[id(func)] = (ref, info)
        return info

    def _inner_identity(self, func):
        base = unwrap(func)
        if type(base) is _METHOD:
            base = base.__func__
        if type(base) is not _FUNCTION:
            return None
        module = base.__globals__.get("__name__")
        if not isinstance(module, str) or not module:
            return None
        if self._in_target is not None and not self._in_target(module):
            return None
        sym = _function_sym(base)
        if sym is None:
            return None
        info = InnerIdentity()
        info.sym = sym
        info.sig = inspect.signature(func)              # follows __wrapped__, honours __signature__
        params = list(info.sig.parameters.values())
        names = [p.name for p in params
                 if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
        # the parameter list a signature-preserving wrapper must repeat exactly (None: the
        # inner takes *args/**kwargs/keyword-only parameters, which no such wrapper passes)
        info.params = tuple(names) if len(names) == len(params) else None
        info.recv, info.prefix = _receiver_of(names[:1], sym)
        return info


_IDENTITY_FIELDS = ("sym", "symq", "own")


def sink_identity_fields(sink):
    """Which identity keywords (``sym``+``symq``, ``own``) the sink's ``on_return``/``on_raise``
    accept. Decided once per backend: a v1 sink keeps receiving exactly the v1 call — passing
    an unknown keyword would raise inside the hook and silently lose every event."""
    accepted = set(_IDENTITY_FIELDS)
    try:
        for name in ("on_return", "on_raise"):
            params = inspect.signature(getattr(sink, name)).parameters
            if any(p.kind == p.VAR_KEYWORD for p in params.values()):
                continue
            accepted &= set(params)
    except Exception:
        return frozenset()
    if not ("sym" in accepted and "symq" in accepted):
        accepted.discard("sym")
        accepted.discard("symq")
    return frozenset(accepted)


def fallback_identity(code, module):
    """The identity used when ``code_identity`` itself failed: v1 name, all args kept."""
    module = module or ""
    ci = CodeIdentity()
    ci.q = module + "." + code.co_name
    ci.argnames, ci.posnames, ci.varargs, ci.varkw = code_args(code)
    ci.sym, ci.symq = ci.q, SYMQ_FALLBACK
    ci.defaults = None
    ci.cells, ci.same_sig = (), False
    ci.recv, ci.prefix = _receiver_of(ci.posnames, ci.sym)
    ci.via_recv = False
    return ci
