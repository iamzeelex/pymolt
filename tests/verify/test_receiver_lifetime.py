"""The tracer never changes how long the traced program's objects live.

The ``sys.monitoring`` backend decides once what a call from our code reaches and caches the
decision. A cache entry that held the callable kept everything the callable holds alive: a
builtin method bound to a dict, a list or a dependency's C object (its receiver), a callable
instance, a closure and what it captured, a ``functools.partial`` and its arguments. So did
the name cache of decorator wrappers (both backends) and, during the incremental settle, the
frame kept to drop a duplicate start. Their finalizers ran late or never and weak references
stayed alive. Each case here drops the object while the tracer runs and checks that the
program sees it die, as it would untraced; the facts recorded for the same calls are unchanged.

Runtime-layer test: pure stdlib + pytest, 3.6-compatible syntax; skipped below 3.12.
"""
import collections
import functools
import gc
import itertools
import sys
import types
import weakref

import pytest

from pymolt.verify._backend import HAS_MONITORING, PrefixMatcher
from pymolt.verify._sinks import MemorySink

pytestmark = pytest.mark.skipif(not HAS_MONITORING, reason="sys.monitoring is 3.12+")

if HAS_MONITORING:
    from pymolt.verify._monitoring import MonitoringBackend

MODES = ["discover", "incremental", "watch"]
MOD = "vfakelife"
SRC = '''
import functools

class Handler(object):
    def __init__(self, payload):
        self.payload = payload
    def __call__(self):
        return 1
    def method(self):
        return 2

def make_getter(obj):
    def getter():
        return 3
    return getter

def take(obj, n=1):
    return n

def deco(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        return f(*args, **kwargs)
    return wrapper

@deco
def wrapped(x):
    return x

class MyDict(dict):
    pass
'''


class Tracked:
    """Its finalizer leaves a mark: the program sees when it dies."""

    died = []

    def __init__(self, name):
        self.name = name

    def __del__(self):
        Tracked.died.append(self.name)


class Dct(dict):
    """A dict that can be weakly referenced."""


class Callback:
    def __init__(self, payload):
        self.payload = payload

    def __call__(self):
        return 4


@pytest.fixture
def dep():
    mod = types.ModuleType(MOD)
    exec(compile(SRC, "<" + MOD + ">", "exec"), mod.__dict__)
    sys.modules[MOD] = mod
    yield mod
    sys.modules.pop(MOD, None)


def _run(mode, body, targets=(MOD,)):
    sink = MemorySink()
    be = MonitoringBackend(PrefixMatcher(list(targets)), sink, source=__name__, mode=mode)
    be.start()
    try:
        body()
    finally:
        be.stop()
    return be, sink


_SEQ = itertools.count()


def _unique(name):
    """A name no earlier run used: what an earlier test's tracer held dies whenever that
    tracer is collected, possibly during a later test."""
    return name + "#" + str(next(_SEQ))


def _dropped(make_and_call, name, mode, targets=(MOD,)):
    """Run ``make_and_call(name)`` (returns a weak reference, or None) under the tracer, then
    drop everything it made: the names of the ``Tracked`` objects that died, and whether the
    weak reference is dead, both seen by the program while the tracer still runs."""
    seen = {}

    def body():
        ref = make_and_call(name)
        gc.collect()
        seen["died"] = list(Tracked.died)
        seen["ref_dead"] = ref is None or ref() is None

    _run(mode, body, targets)
    return seen


def _bound_dict_method(name):
    d = Dct(a=Tracked(name))
    items = d.items             # a builtin method bound to the dict
    items()
    items()
    return weakref.ref(d)


def _bound_plain_dict_method(name):
    d = {"a": Tracked(name)}
    get = d.get
    get("a")
    return None


def _bound_list_method(name):
    lst = [Tracked(name)]
    append = lst.append
    append(1)
    append(2)
    return None


def _fresh_bound_method_per_call(name):
    lst = [Tracked(name)]
    for i in range(3):
        getattr(lst, "append")(i)   # a new bound method each time
    return None


def _bound_deque_method(name):
    dq = collections.deque([Tracked(name)])
    push = dq.append            # a dependency's C class (target ``collections``)
    push(1)
    push(2)
    dq.append(3)                # the method descriptor path: never held the deque
    return weakref.ref(dq)


def _method_wrapper(name):
    obj = Tracked(name)
    eq = obj.__eq__             # ``object.__eq__`` bound: a method-wrapper
    eq(obj)
    eq(obj)
    return weakref.ref(obj)


def _callable_instance(name):
    cb = Callback(Tracked(name))
    cb()
    cb()
    return weakref.ref(cb)


def _closure(name):
    def make(obj):
        def inner():
            return obj.name
        return inner
    f = make(Tracked(name))
    f()
    f()
    return weakref.ref(f)


def _partial(name):
    def take(obj, n=1):
        return n
    p = functools.partial(take, Tracked(name))
    p()
    p()
    return weakref.ref(p)


def _bound_python_method(name):
    cb = Callback(Tracked(name))
    m = cb.__call__
    m()
    m()
    return weakref.ref(cb)


CASES = [
    ("dict", _bound_dict_method, ("collections",)),
    ("plain-dict", _bound_plain_dict_method, ("collections",)),
    ("list", _bound_list_method, ("collections",)),
    ("fresh", _fresh_bound_method_per_call, ("collections",)),
    ("deque", _bound_deque_method, ("collections",)),
    ("method-wrapper", _method_wrapper, ("collections",)),
    ("callable", _callable_instance, ("collections",)),
    ("closure", _closure, ("collections",)),
    ("partial", _partial, ("collections",)),
    ("py-method", _bound_python_method, ("collections",)),
]


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("case, make_and_call, targets", CASES, ids=[c[0] for c in CASES])
def test_an_object_the_program_drops_dies_under_the_tracer(case, make_and_call, targets, mode):
    name = _unique(case)
    seen = _dropped(make_and_call, name, mode, targets)
    assert name in seen["died"], seen
    assert seen["ref_dead"], seen


def _dep_calls(dep):
    def make(name):
        h = dep.Handler(Tracked(name + "-callable"))
        h()                     # a dependency's callable instance
        h()
        g = dep.make_getter(Tracked(name + "-closure"))
        g()                     # a dependency's closure (its last traced Python callee)
        g()
        p = functools.partial(dep.take, Tracked(name + "-partial"))
        p()                     # a partial of a dependency function
        p()
        m = h.method
        m()
        return weakref.ref(h)
    return make


@pytest.mark.parametrize("mode", MODES)
def test_dependency_callables_and_what_they_hold_die_under_the_tracer(dep, mode):
    name = _unique("dep")
    seen = _dropped(_dep_calls(dep), name, mode)
    want = {name + "-callable", name + "-closure", name + "-partial"}
    assert want <= set(seen["died"]), seen
    assert seen["ref_dead"], seen


@pytest.mark.parametrize("mode", ["discover", "incremental"])
def test_a_dependency_closure_that_failed_to_bind_dies_under_the_tracer(dep, mode):
    name = _unique("bind-fail")
    seen = {}

    def body():
        g = dep.make_getter(Tracked(name))
        with pytest.raises(TypeError):
            g(1)                # the binding failure is recorded
        ref = weakref.ref(g)
        del g
        gc.collect()
        seen["died"] = list(Tracked.died)
        seen["ref_dead"] = ref() is None

    _be, sink = _run(mode, body)
    assert name in seen["died"], seen
    assert seen["ref_dead"], seen
    assert any(r.get("binding") for r in sink.records.values()), sink.records


@pytest.mark.parametrize("mode", ["discover", "incremental"])
def test_a_fresh_bound_builtin_per_call_is_decided_once(mode):
    """A bound builtin method is a new object on every attribute access: it is decided once
    per receiver class and name, not once per object (each new object was a cache miss)."""
    lst = []
    decided = []

    def body():
        for i in range(300):
            getattr(lst, "append")(i)

    sink = MemorySink()
    be = MonitoringBackend(PrefixMatcher(["collections"]), sink, source=__name__, mode=mode)
    c_sym = be._c_sym

    def counting(obj):
        if getattr(obj, "__self__", None) is lst:
            decided.append(obj)
        return c_sym(obj)

    be._c_sym = counting
    be.start()
    try:
        body()
    finally:
        be.stop()
    assert len(decided) == 1, len(decided)


def _captures(obj):
    return lambda: obj.name


@pytest.mark.parametrize("backend", ["setprofile", "monitoring"])
def test_what_a_decorator_wrapper_closes_over_dies_under_the_tracer(dep, backend):
    """A generic wrapper is named after the function its closure holds (``wrapped_inner``),
    cached per function: never by the bound method or closure it found, which hold a
    receiver or captured objects."""
    from pymolt.verify._backend import SetProfileBackend

    name = _unique("wrapped")
    seen = {}

    def body():
        h = dep.Handler(Tracked(name + "-receiver"))
        w = dep.deco(h.method)          # the wrapper's cell holds a bound method
        w()
        w()
        v = dep.deco(_captures(Tracked(name + "-captured")))    # ... or a closure of ours
        v()
        ref = weakref.ref(h)
        del h, w, v
        gc.collect()
        seen["died"] = list(Tracked.died)
        seen["ref_dead"] = ref() is None

    cls = SetProfileBackend if backend == "setprofile" else MonitoringBackend
    sink = MemorySink()
    be = cls(PrefixMatcher([MOD]), sink, source=__name__)
    be.start()
    try:
        body()
    finally:
        be.stop()
    assert {name + "-receiver", name + "-captured"} <= set(seen["died"]), seen
    assert seen["ref_dead"], seen
    assert any((r.get("sym") or "") == "vfakelife.Handler.method"
               for r in sink.records.values()), [r.get("sym") for r in sink.records.values()]


def test_a_frame_kept_to_drop_a_duplicate_start_is_released_when_its_call_ends(dep):
    """While both incremental tools may deliver one start (the settle), the last recorded
    frame is kept to recognise the duplicate. A frame holds its locals: it is let go when
    its call ends, not at the thread's next recorded call."""
    name = _unique("settle-frame")
    seen = {}
    sink = MemorySink()
    be = MonitoringBackend(PrefixMatcher([MOD]), sink, source=__name__, mode="incremental")
    be.start()
    try:
        dep.take(0)
        assert be.settle()
        assert be._dedupe_until > 0
        dep.take(Tracked(name))         # recorded in the settle grace: its frame is kept
        gc.collect()
        seen["died"] = list(Tracked.died)
        seen["kept"] = len(be._last_start)
    finally:
        be.stop()
    assert name in seen["died"], seen
    assert seen["kept"] == 0, seen


# ── the facts are the same ────────────────────────────────────────────────────

def _mixed_calls(dep):
    def body():
        dq = collections.deque()
        push = dq.append
        for i in range(3):
            push(i)                         # compiled, bound to the deque
            dq.append(i)                    # compiled, through the method descriptor
            getattr(dq, "appendleft")(i)    # compiled, a fresh bound method
        h = dep.Handler(1)
        for _ in range(3):
            h()                             # callable instance of a dependency class
            h.method()                      # its Python method
            dep.take(h)                     # a dependency function
            functools.partial(dep.take, h, n=2)()   # a fresh partial each time
            dep.make_getter(h)()            # a fresh dependency closure each time
            dep.wrapped(5)                  # a functools.wraps wrapper
        md = dep.MyDict(a=1)
        for _ in range(3):
            md.items()                      # inherited builtin method of a dependency class
            md.get("a")
        g = dep.make_getter(0)
        for _ in range(2):
            try:
                g(1)                        # binding failure
            except TypeError:
                pass
    return body


def _facts(sink):
    out = []
    for rec in sink.records.values():
        out.append((rec.get("sym") or rec["qualname"], repr(sorted(rec["inputs"].items())),
                    repr(rec["result"]), rec["raised"], bool(rec.get("binding")),
                    rec["count"]))
    return sorted(out)


_UNOBSERVED = "{'__opaque__': 'unobserved:c-return'}"
_HANDLER = "{'__opaque__': 'vfakelife.Handler'}"


def _c(sym, arg0, count=1):
    return (sym, "[('__c_call__', True), ('arg0', " + arg0 + ")]", _UNOBSERVED, None, False,
            count)


def _py(sym, bound, defaulted, result, count=3):
    return (sym, "[('bound', {" + bound + "}), ('defaulted', [" + defaulted + "])]", result,
            None, False, count)


# Recorded before the fix (discover and incremental alike), and unchanged since.
EXPECTED_FACTS = [
    _c("collections.deque.append", "0"),
    _c("collections.deque.append", "1"),
    _c("collections.deque.append", "2"),
    _c("collections.deque.append", "{'__opaque__': 'collections.deque'}", 3),
    _c("collections.deque.appendleft", "0"),
    _c("collections.deque.appendleft", "1"),
    _c("collections.deque.appendleft", "2"),
    _py("vfakelife.Handler.__call__", "'self': " + _HANDLER, "", "1"),
    _py("vfakelife.Handler.__init__", "'self': " + _HANDLER + ", 'payload': 1", "", "None", 1),
    _py("vfakelife.Handler.method", "'self': " + _HANDLER, "", "2"),
    _c("vfakelife.MyDict.get", "{'__dict__': [['a', 1]]}", 3),
    _c("vfakelife.MyDict.items", "{'__dict__': [['a', 1]]}", 3),
    _py("vfakelife.make_getter", "'obj': 0", "",
        "{'__callable__': 'vfakelife.make_getter.<locals>.getter'}", 1),
    _py("vfakelife.make_getter", "'obj': " + _HANDLER, "",
        "{'__callable__': 'vfakelife.make_getter.<locals>.getter'}"),
    ("vfakelife.make_getter.<locals>.getter", "[('__c_call__', True)]", "None", "TypeError",
     True, 2),
    ("vfakelife.make_getter.<locals>.getter", "[('bound', {})]", "3", None, False, 3),
    _py("vfakelife.take", "'obj': " + _HANDLER + ", 'n': 2", "", "2"),
    _py("vfakelife.take", "'obj': " + _HANDLER, "'n'", "1"),
    _py("vfakelife.wrapped", "'x': 5", "", "5"),
]


@pytest.mark.parametrize("mode", ["discover", "incremental"])
def test_the_same_facts_are_recorded_for_a_mix_of_calls(dep, mode):
    _be, sink = _run(mode, _mixed_calls(dep), (MOD, "collections"))
    assert _facts(sink) == EXPECTED_FACTS
