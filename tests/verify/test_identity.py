"""Fact model v2, injected side: canonical ``sym``, caller-passed ``bound``/``defaulted``.

Runtime-layer test: pure stdlib + pytest, 3.6-compatible syntax (it runs in the tox matrix).
The pre-3.11 per-module index is exercised on any interpreter by forcing
``_identity.HAS_CO_QUALNAME`` off (the ``identity_path`` fixture runs each test both ways).
"""
import functools
import sys
import types

import pytest

from pymolt.verify import _identity
from pymolt.verify._backend import SetProfileBackend
from pymolt.verify._identity import (
    IdentityResolver,
    bind_inputs,
    callable_identity,
    sink_accepts_identity,
)
from pymolt.verify._normalize import normalize
from pymolt.verify._sinks import MemorySink
from pymolt.verify._wrap import WrapBackend

MOD = "vfakeident"

SRC = '''
import functools

def deco(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        return f(*args, **kwargs)
    return wrapper

def two_cells(f):
    def helper():
        return None

    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        helper()
        return f(*args, **kwargs)
    return wrapper

def plain(a, b=2, *rest, c=None, **extra):
    return a

@deco
def decorated(a, b=2, *rest, c=None, **extra):
    return a

@two_cells
def doubly(a, b=2):
    return a

class A:
    def __init__(self, x, y=None):
        self.x = x

    def m(self, k=1):
        return k

    @staticmethod
    def s(v=3):
        return v

    @classmethod
    def c(cls, v=4):
        return v

    @property
    def p(self):
        return 5

    class Inner:
        def __init__(self, z=0):
            self.z = z

        def m(self):
            return 1

class B:
    def __init__(self, x):
        self.x = x

def make_closure():
    def nested(q=1):
        return q
    return nested

def boom(x):
    raise ValueError(x)

class Base:
    def __init__(self, x=0):
        self.x = x

    def hello(self):
        return 1

    @classmethod
    def make(cls):
        return 2

    @deco
    def decorated_method(self, n=1):
        return n

class Child(Base):
    pass
'''

DC_SRC = '''
import dataclasses

@dataclasses.dataclass
class Point:
    x: int
    y: int = 0

@dataclasses.dataclass(frozen=True, order=True)
class Frozen:
    a: int
'''


class RecSink:
    """A v2 sink: records every event with its identity."""

    def __init__(self):
        self.events = []

    def admit(self, force=False):
        return True

    def on_call(self, qualname, inputs, site=None):
        pass

    def on_return(self, qualname, inputs, result_obj, site=None, admitted=False, sym=None,
                  symq=None, own=None):
        self.events.append({"t": "return", "q": qualname, "in": inputs, "sym": sym,
                            "symq": symq, "own": own, "result": normalize(result_obj)})

    def on_raise(self, qualname, inputs, exc_typename, site=None, admitted=False, sym=None,
                 symq=None, own=None):
        self.events.append({"t": "raise", "q": qualname, "in": inputs, "sym": sym,
                            "symq": symq, "own": own, "raised": exc_typename})


@pytest.fixture(params=["co_qualname", "index"])
def identity_path(request, monkeypatch):
    if request.param == "co_qualname" and not _identity.HAS_CO_QUALNAME:
        pytest.skip("co_qualname is 3.11+")
    monkeypatch.setattr(_identity, "HAS_CO_QUALNAME", request.param == "co_qualname")
    return request.param


def _module(name, src):
    mod = types.ModuleType(name)
    sys.modules[name] = mod          # as the import system does: in place before the body runs
    exec(compile(src, "<" + name + ">", "exec"), mod.__dict__)
    return mod


@pytest.fixture
def dep():
    yield _module(MOD, SRC)
    sys.modules.pop(MOD, None)


def _trace(body, sink=None):
    sink = sink if sink is not None else RecSink()
    be = SetProfileBackend(MOD, sink, source=__name__)
    be.start()
    try:
        body()
    finally:
        be.stop()
    return sink, be


def _one(sink, sym):
    hits = [e for e in sink.events if e["sym"] == sym]
    assert len(hits) == 1, (sym, [e["sym"] for e in sink.events])
    return hits[0]


# ── canonical identity ───────────────────────────────────────────────────────


def test_two_classes_init_get_distinct_sym(dep, identity_path):
    sink, _ = _trace(lambda: (dep.A(1), dep.B(2)))
    a = _one(sink, MOD + ".A.__init__")
    b = _one(sink, MOD + ".B.__init__")
    assert a["q"] == b["q"] == MOD + ".__init__"     # the v1 name still collides
    assert a["symq"] is None and b["symq"] is None   # exact on both paths


def test_methods_static_class_property_and_nested_classes(dep, identity_path):
    def body():
        a = dep.A(1)
        a.m()
        dep.A.s()
        dep.A.c()
        a.p
        dep.A.Inner().m()

    sink, _ = _trace(body)
    for name in ("A.m", "A.s", "A.c", "A.p", "A.Inner.__init__", "A.Inner.m"):
        assert _one(sink, MOD + "." + name)["symq"] is None


def test_raise_events_carry_the_sym(dep, identity_path):
    def body():
        try:
            dep.boom(1)
        except ValueError:
            pass

    ev = _one(_trace(body)[0], MOD + ".boom")
    assert ev["t"] == "raise"


def test_unreachable_nested_function_falls_back_and_keeps_all_args(dep, identity_path):
    nested = dep.make_closure()
    ev = _trace(lambda: nested())[0].events[0]
    if identity_path == "co_qualname":
        assert (ev["sym"], ev["symq"]) == (MOD + ".make_closure.<locals>.nested", None)
    else:
        assert (ev["sym"], ev["symq"]) == (MOD + ".nested", "fallback")
    # no function object -> defaults unknown -> every argument kept, `defaulted` omitted
    assert ev["in"] == {"bound": {"q": 1}}


# ── caller-passed inputs ─────────────────────────────────────────────────────


def test_argument_left_at_default_is_listed_not_bound(dep, identity_path):
    sink, _ = _trace(lambda: (dep.plain(1), dep.A(7).m(k=9)))
    ev = _one(sink, MOD + ".plain")
    assert ev["in"]["bound"] == {"a": 1}
    assert ev["in"]["defaulted"] == ["b", "c"]
    assert ev["in"]["varargs"] == {"__tuple__": []}
    assert ev["in"]["kwargs"] == {"__dict__": []}
    m = _one(sink, MOD + ".A.m")
    assert m["in"]["bound"]["k"] == 9 and m["in"]["defaulted"] == []


def test_explicit_value_identical_to_default_counts_as_defaulted(dep, identity_path):
    sink, _ = _trace(lambda: dep.plain(1, 2, c=None))
    ev = _one(sink, MOD + ".plain")
    assert ev["in"]["bound"] == {"a": 1} and ev["in"]["defaulted"] == ["b", "c"]


def test_decorated_dependency_is_attributed_to_the_inner_function(dep, identity_path):
    calls = [((1,), {}), ((1, 5, 6), {"c": 7, "z": 8}), ((), {"a": 3, "b": 2})]

    def body():
        for args, kwargs in calls:
            dep.decorated(*args, **kwargs)
            dep.plain(*args, **kwargs)

    sink, _ = _trace(body)
    dec = [e for e in sink.events if e["q"] == MOD + ".wrapper"]
    plain = [e for e in sink.events if e["q"] == MOD + ".plain"]
    assert len(dec) == len(plain) == len(calls)       # the inner call is not double-recorded
    for d, p in zip(dec, plain):
        assert (d["sym"], d["symq"]) == (MOD + ".decorated", "unwrapped")
        assert d["in"] == p["in"]                     # same `bound` form with or without it
    assert dec[1]["in"]["varargs"] == {"__tuple__": [6]}
    assert dec[1]["in"]["kwargs"] == {"__dict__": [["z", 8]]}


def test_wrapper_with_two_function_cells_is_not_unwrapped(dep, identity_path):
    ev = _trace(lambda: dep.doubly(1))[0].events[0]
    assert ev["symq"] != "unwrapped"
    if identity_path == "co_qualname":
        assert ev["sym"] == MOD + ".two_cells.<locals>.wrapper"
    else:
        assert (ev["sym"], ev["symq"]) == (MOD + ".wrapper", "fallback")


def _our_function(x, y=1):
    return x


def test_dependency_decorator_around_our_function_keeps_the_wrapper_name(dep):
    ours = dep.deco(_our_function)          # our function: not a dependency contact
    ev = _trace(lambda: ours(4))[0].events[0]
    assert ev["symq"] != "unwrapped"
    assert ev["sym"] != __name__ + "._our_function"


def test_identity_is_computed_once_per_code_object(dep, monkeypatch):
    calls = []
    real = IdentityResolver.code_identity

    def counting(self, code, module, g):
        calls.append(code)
        return real(self, code, module, g)

    monkeypatch.setattr(IdentityResolver, "code_identity", counting)
    _, be = _trace(lambda: [dep.plain(i) for i in range(5)])
    assert calls == [dep.plain.__code__]
    (ent,) = [e for e in be._callee.values() if e[0] is dep.plain.__code__]
    assert ent[4] is not None and ent[4].sym == MOD + ".plain"


def test_wrapped_inner_is_cached_per_inner_function(dep):
    _, be = _trace(lambda: [dep.decorated(i) for i in range(5)])
    inners = [v for v in be._identity._inner.values() if v[1] is not None]
    assert len(inners) == 1 and inners[0][1].sym == MOD + ".decorated"


# ── the pre-3.11 index ───────────────────────────────────────────────────────


def _codes(mod):
    """Every function reachable from the module: (function, its __qualname__)."""
    a = mod.A
    funcs = [mod.plain, mod.deco, mod.two_cells, mod.make_closure, mod.boom,
             a.__init__, a.m, a.__dict__["s"].__func__, a.__dict__["c"].__func__,
             a.__dict__["p"].fget, a.Inner.__init__, a.Inner.m, mod.B.__init__,
             mod.decorated.__wrapped__]
    return funcs


def test_index_names_every_reachable_code_by_its_qualname(dep):
    index = _identity.build_index(MOD, dep.__dict__)
    for fn in _codes(dep):
        hit = index.get(id(fn.__code__))
        assert hit is not None and hit[0] is fn.__code__, fn.__qualname__
        assert hit[1] == fn.__qualname__ and hit[2] is fn
        if _identity.HAS_CO_QUALNAME:
            assert hit[1] == fn.__code__.co_qualname   # both paths agree


def test_index_skips_wraps_copied_names_and_foreign_functions(dep):
    index = _identity.build_index(MOD, dep.__dict__)
    wrapper_code = dep.decorated.__code__              # `wrapper`, named "decorated" by wraps
    assert id(wrapper_code) not in index
    assert id(functools.wraps.__code__) not in index   # imported, defined elsewhere


def test_index_rebuilds_when_the_module_grows(dep, monkeypatch):
    monkeypatch.setattr(_identity, "HAS_CO_QUALNAME", False)
    resolver = IdentityResolver()
    ci, _ = resolver.code_identity(dep.plain.__code__, MOD, dep.__dict__)
    assert ci.sym == MOD + ".plain"
    exec(compile("def late(x=1):\n    return x\n", "<late>", "exec"), dep.__dict__)
    ci, cacheable = resolver.code_identity(dep.late.__code__, MOD, dep.__dict__)
    assert (ci.sym, ci.symq, cacheable) == (MOD + ".late", None, True)
    assert ci.defaults == {"x": 1}


def test_index_rebuilds_are_bounded(dep, monkeypatch):
    monkeypatch.setattr(_identity, "HAS_CO_QUALNAME", False)
    resolver = IdentityResolver()
    nested = dep.make_closure()
    for i in range(_identity._INDEX_MAX_REBUILDS + 3):
        dep.__dict__["filler" + str(i)] = i           # namespace keeps growing
        ci, _ = resolver.code_identity(nested.__code__, MOD, dep.__dict__)
        assert ci.symq == "fallback"
    assert resolver._index[MOD][2] == _identity._INDEX_MAX_REBUILDS


def test_one_code_under_two_names_is_ambiguous(dep, monkeypatch):
    monkeypatch.setattr(_identity, "HAS_CO_QUALNAME", False)
    clone = types.FunctionType(dep.A.m.__code__, dep.__dict__, "m")
    clone.__qualname__ = "B.m"
    dep.B.m = clone
    ci, _ = IdentityResolver().code_identity(dep.A.m.__code__, MOD, dep.__dict__)
    assert (ci.sym, ci.symq) == (MOD + ".m", "fallback")
    assert ci.defaults is None


def test_index_never_touches_proxy_objects_in_a_namespace(dep):
    class Proxy:
        """A werkzeug LocalProxy-like object: any attribute access is an error."""

        def __getattribute__(self, name):
            raise RuntimeError("touched " + name)

    dep.request = Proxy()
    index = _identity.build_index(MOD, dep.__dict__)
    assert id(dep.plain.__code__) in index


# ── helpers shared with the wrap backend ─────────────────────────────────────


def test_bind_inputs_matches_the_frame_form():
    def f(a, b=2, *rest, c=None, **extra):
        return a

    import inspect

    sig = inspect.signature(f)
    assert bind_inputs(sig, (1,), {}, normalize) == {
        "bound": {"a": 1}, "defaulted": ["b", "c"],
        "varargs": {"__tuple__": []}, "kwargs": {"__dict__": []},
    }
    out = bind_inputs(sig, (1, 3, 4), {"c": 5, "k": 6}, normalize)
    assert out["bound"] == {"a": 1, "b": 3, "c": 5} and out["defaulted"] == []
    assert out["varargs"] == {"__tuple__": [4]} and out["kwargs"] == {"__dict__": [["k", 6]]}


def test_callable_identity_unwraps(dep):
    assert callable_identity(dep.plain) == (MOD + ".plain", None)
    assert callable_identity(dep.decorated) == (MOD + ".decorated", "unwrapped")
    assert callable_identity(dep.A.__init__) == (MOD + ".A.__init__", None)
    assert callable_identity(len) == ("builtins.len", None)


def test_sink_capability_detection():
    assert sink_accepts_identity(RecSink()) is True

    class V1Sink:
        def on_return(self, qualname, inputs, result_obj, site=None):
            pass

        def on_raise(self, qualname, inputs, exc_typename, site=None):
            pass

    class KwSink:
        def on_return(self, *a, **kw):
            pass

        def on_raise(self, *a, **kw):
            pass

    class NoOwnSink:
        def on_return(self, qualname, inputs, result_obj, site=None, sym=None, symq=None):
            pass

        def on_raise(self, qualname, inputs, exc_typename, site=None, sym=None, symq=None):
            pass

    assert sink_accepts_identity(V1Sink()) is False
    assert sink_accepts_identity(KwSink()) is True
    assert sink_accepts_identity(object()) is False
    assert _identity.sink_identity_fields(RecSink()) == {"sym", "symq", "own"}
    assert _identity.sink_identity_fields(NoOwnSink()) == {"sym", "symq"}
    assert _identity.sink_identity_fields(V1Sink()) == frozenset()


def test_v1_sink_still_receives_every_event(dep):
    """A sink without sym/symq gets the v1 call: no event is lost to a TypeError."""
    sink = MemorySink()
    _, be = _trace(lambda: (dep.plain(1), dep.decorated(2)), sink=sink)
    assert be._sym_kw is sink_accepts_identity(sink)
    quals = sorted(r["qualname"] for r in sink.records.values())
    assert quals == [MOD + ".plain", MOD + ".wrapper"]


# ── wrap backend: same identity and the same `bound` form ───────────────────


def _wrap(symbols, body):
    sink = RecSink()
    be = WrapBackend(symbols, sink, source=__name__)
    be.start()
    try:
        body()
    finally:
        be.stop()
    return sink


def test_wrap_backend_emits_the_same_sym_and_bound_as_setprofile(dep):
    def body():
        dep.decorated(1, c=3)
        dep.plain(1, c=3)
        dep.A(5)

    wrapped = _wrap([MOD + ".decorated", MOD + ".plain", MOD + ".A"], body)
    profiled, _ = _trace(body)
    for sym in (MOD + ".decorated", MOD + ".plain", MOD + ".A.__init__"):
        w = _one(wrapped, sym)
        p = _one(profiled, sym)
        assert w["symq"] == p["symq"]
        assert w["in"] == p["in"]
    assert _one(wrapped, MOD + ".decorated")["in"]["defaulted"] == ["b"]
    assert _one(wrapped, MOD + ".A.__init__")["q"] == MOD + ".A"   # v1 name: the symbol


def test_wrap_backend_names_a_c_level_init_after_the_class():
    mod = types.ModuleType("vfakecinit")
    exec("class NoInit:\n    pass\n", mod.__dict__)
    sys.modules["vfakecinit"] = mod
    try:
        sink = _wrap(["vfakecinit.NoInit"], lambda: mod.NoInit())
    finally:
        sys.modules.pop("vfakecinit", None)
    (ev,) = sink.events
    assert (ev["sym"], ev["symq"]) == ("vfakecinit.NoInit.__init__", "fallback")


# ── receiver (`own`) ─────────────────────────────────────────────────────────


def test_inherited_constructor_and_methods_report_the_receiver_class(dep, identity_path):
    def body():
        dep.Base(1)
        child = dep.Child(1)
        child.hello()
        dep.Child.make()
        child.decorated_method(5)
        dep.boom  # module function: no receiver

    sink, _ = _trace(body)
    inits = [e for e in sink.events if e["sym"] == MOD + ".Base.__init__"]
    assert [e["own"] for e in inits] == [None, MOD + ".Child"]   # same class -> no `own`
    assert _one(sink, MOD + ".Base.hello")["own"] == MOD + ".Child"
    assert _one(sink, MOD + ".Base.make")["own"] == MOD + ".Child"       # cls: the class
    unwrapped = _one(sink, MOD + ".Base.decorated_method")
    assert unwrapped["symq"] == "unwrapped" and unwrapped["own"] == MOD + ".Child"


def test_own_is_absent_for_functions_and_same_class_calls(dep):
    sink, _ = _trace(lambda: (dep.plain(1), dep.A(1).m(), dep.A.s()))
    assert all(e["own"] is None for e in sink.events)


def test_wrap_backend_reports_the_receiver_class_too(dep):
    def body():
        dep.Child(3).hello()

    sink = _wrap([MOD + ".Child", MOD + ".Child.hello"], body)
    init = _one(sink, MOD + ".Base.__init__")
    assert (init["q"], init["own"]) == (MOD + ".Child", MOD + ".Child")
    assert _one(sink, MOD + ".Base.hello")["own"] == MOD + ".Child"


def test_receiver_owner_helper():
    class K:
        pass

    label = _identity.type_label(K)
    assert label == __name__ + "." + K.__qualname__
    assert _identity.receiver_owner(K(), "self", label) is None
    assert _identity.receiver_owner(K(), "self", "other.Base") == label
    assert _identity.receiver_owner(K, "cls", "other.Base") == label


# ── generated methods (dataclasses) ──────────────────────────────────────────


def test_dataclass_generated_methods_are_named_by_their_class(identity_path):
    pytest.importorskip("dataclasses")
    name = "vfakedc"
    mod = _module(name, DC_SRC)
    try:
        def body():
            mod.Point(1)
            mod.Point(1) == mod.Point(2)
            mod.Frozen(1) < mod.Frozen(2)

        sink = RecSink()
        be = SetProfileBackend(name, sink, source=__name__)
        be.start()
        try:
            body()
        finally:
            be.stop()
    finally:
        sys.modules.pop(name, None)
    syms = {}
    for e in sink.events:
        syms.setdefault(e["sym"], e)          # first event per symbol: Point(1)
    for sym in (name + ".Point.__init__", name + ".Point.__eq__", name + ".Frozen.__init__",
                name + ".Frozen.__lt__"):
        assert sym in syms and syms[sym]["symq"] is None, sorted(syms)
    init = syms[name + ".Point.__init__"]
    assert init["in"]["bound"]["x"] == 1 and init["in"]["defaulted"] == ["y"]


# ── factory-made methods (pandas 1.5 ``_add_numeric_operations``) ─────────────

OPS_SRC = '''
def _add_ops(cls):
    def mean(self, axis=None, skipna=True):
        return axis

    def make(cls, n=1):
        return n

    cls.mean = mean
    cls.make = classmethod(make)
'''

FRAMES_SRC = '''
from vfakeops_gen import _add_ops

class Frame:
    pass

class Series:
    pass

_add_ops(Frame)
_add_ops(Series)

class SubFrame(Frame):
    pass
'''


@pytest.fixture
def ops():
    gen = _module("vfakeops_gen", OPS_SRC)
    frames = _module("vfakeops_cls", FRAMES_SRC)
    yield gen, frames
    for name in ("vfakeops_gen", "vfakeops_cls"):
        sys.modules.pop(name, None)


def _trace_ops(body, sink=None):
    sink = sink if sink is not None else RecSink()
    be = SetProfileBackend("vfakeops_gen,vfakeops_cls", sink, source=__name__)
    be.start()
    try:
        body()
    finally:
        be.stop()
    return sink, be


def test_factory_methods_are_named_by_the_class_the_receiver_reaches(ops, identity_path):
    _, m = ops
    assert m.Frame.mean.__code__ is m.Series.mean.__code__      # one shared code object

    def body():
        m.Frame().mean()
        m.Series().mean(axis=1)
        m.SubFrame().mean(skipna=False)
        m.Frame.make()
        m.Series.make(n=3)

    sink, be = _trace_ops(body)
    by = [(e["sym"], e["symq"], e["own"]) for e in sink.events]
    assert by == [
        ("vfakeops_cls.Frame.mean", "receiver", None),
        ("vfakeops_cls.Series.mean", "receiver", None),
        ("vfakeops_cls.Frame.mean", "receiver", "vfakeops_cls.SubFrame"),
        ("vfakeops_cls.Frame.make", "receiver", None),
        ("vfakeops_cls.Series.make", "receiver", None),
    ]
    assert all(e["q"].startswith("vfakeops_gen.") for e in sink.events)   # v1 name unchanged
    ins = [e["in"] for e in sink.events]
    # the found function's defaults: `bound` keeps only what the caller passed
    assert ins[0]["bound"] == {"self": ins[0]["bound"]["self"]}
    assert ins[0]["defaulted"] == ["axis", "skipna"]
    assert ins[1]["bound"]["axis"] == 1 and ins[1]["defaulted"] == ["skipna"]
    assert ins[2]["bound"]["skipna"] is False and ins[2]["defaulted"] == ["axis"]
    assert ins[4]["bound"]["n"] == 3 and ins[4]["defaulted"] == []


def test_receiver_resolution_is_cached_per_code_and_receiver_type(ops, monkeypatch):
    _, m = ops
    calls = []
    real = _identity.class_member

    def counting(cls, code):
        calls.append(cls)
        return real(cls, code)

    monkeypatch.setattr(_identity, "class_member", counting)
    _trace_ops(lambda: [(m.Frame().mean(), m.Series().mean()) for _ in range(5)])
    assert calls == [m.Frame, m.Series]          # once per (code, type), not per call


def test_wrap_backend_names_factory_methods_by_their_class(ops):
    _, m = ops
    sink = RecSink()
    be = WrapBackend(["vfakeops_cls.Frame.mean"], sink, source=__name__)
    be.start()
    try:
        m.Frame().mean(axis=2)
        m.Series().mean()                        # not wrapped
    finally:
        be.stop()
    (ev,) = sink.events
    assert (ev["sym"], ev["symq"]) == ("vfakeops_cls.Frame.mean", "receiver")
    assert ev["in"]["bound"]["axis"] == 2 and ev["in"]["defaulted"] == ["skipna"]
