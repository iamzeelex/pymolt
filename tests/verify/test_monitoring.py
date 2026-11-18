"""The ``sys.monitoring`` backend (3.12+): parity with setprofile, DISABLE, watch mode,
typed raises, generators, tool-id hygiene and coexistence with another tool.

Runtime-layer test: pure stdlib + pytest (3.6-compatible syntax); skipped below 3.12.
"""
import functools
import importlib
import sys
import types

import pytest

from pymolt.verify._backend import HAS_MONITORING, UNTYPED_RAISE, SetProfileBackend
from pymolt.verify._normalize import normalize

pytestmark = pytest.mark.skipif(not HAS_MONITORING, reason="sys.monitoring is 3.12+")

if HAS_MONITORING:
    from pymolt.verify import _monitoring
    from pymolt.verify._monitoring import MonitoringBackend

MOD = "vfakemon"

SRC = '''
import functools

class MyErr(Exception):
    pass

def deco(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        return f(*args, **kwargs)
    return wrapper

def plain(a, b=2, *rest, c=None, **extra):
    return a

@deco
def decorated(a, b=2):
    return a + b

def helper(x):
    return x * 2

def uses_helper(x):
    return helper(x) + helper(x)

def boom(x):
    raise ValueError(x)

def boom_custom():
    raise MyErr("no")

def gen(n):
    for i in range(n):
        yield i

async def coro(x):
    return x

def calls_back(fn):
    fn()
    return "done"

class Base:
    def __init__(self, x=0):
        self.x = x

    def hello(self, k=1):
        return k

    @classmethod
    def make(cls, n=1):
        return n

    @staticmethod
    def stat(v=3):
        return v

    @property
    def prop(self):
        return 5

class Child(Base):
    pass
'''


class RecSink:
    """A sink with today's v2 hooks plus the phase-2 ``dur``/``tid`` and ``tally``."""

    def __init__(self, sample=True):
        self.events = []
        self.tallies = []
        self.sample = sample

    def admit(self, force=False):
        return self.sample

    def on_call(self, qualname, inputs, site=None):
        pass

    def on_return(self, qualname, inputs, result_obj, site=None, admitted=False, sym=None,
                  symq=None, own=None, dur=None, tid=None):
        self.events.append({"t": "return", "q": qualname, "in": inputs, "sym": sym,
                            "symq": symq, "own": own, "result": normalize(result_obj),
                            "where": site, "dur": dur, "tid": tid})

    def on_raise(self, qualname, inputs, exc_typename, site=None, admitted=False, sym=None,
                 symq=None, own=None, dur=None, tid=None):
        self.events.append({"t": "raise", "q": qualname, "in": inputs, "sym": sym,
                            "symq": symq, "own": own, "raised": exc_typename,
                            "where": site, "dur": dur, "tid": tid})

    def tally(self, qualname, sym, site, kind, raised=None, dur=None):
        self.tallies.append({"q": qualname, "sym": sym, "where": site, "kind": kind,
                             "raised": raised, "dur": dur})


def _module(name, src):
    mod = types.ModuleType(name)
    sys.modules[name] = mod
    exec(compile(src, "<" + name + ">", "exec"), mod.__dict__)
    return mod


@pytest.fixture
def dep():
    yield _module(MOD, SRC)
    sys.modules.pop(MOD, None)


def _run(backend, body):
    backend.start()
    try:
        body()
    finally:
        backend.stop()
    return backend


def _swallow(fn, *args):
    try:
        fn(*args)
    except Exception:
        pass


def _workload(dep):
    def body():
        dep.plain(1)
        dep.plain(1, 5, 6, c=2, z=3)
        dep.decorated(1)
        dep.decorated(1, b=7)
        dep.uses_helper(3)
        dep.Base(1)
        dep.Child().hello()
        dep.Child.make(n=2)
        dep.Base.stat()
        dep.Base().prop
        list(dep.gen(3))
        _swallow(dep.boom, 4)
        _swallow(dep.boom_custom)
    return body


def _comparable(events):
    out = []
    for e in events:
        e = dict(e)
        e.pop("dur")
        e.pop("tid")
        if e["t"] == "raise":
            e["raised"] = "*"            # setprofile cannot see the type
        e["where"] = (e["where"]["func"], e["where"]["line"])
        out.append(e)
    return out


# ── parity ───────────────────────────────────────────────────────────────────


def test_events_are_identical_to_setprofile(dep):
    sp, mon = RecSink(), RecSink()
    _run(SetProfileBackend(MOD, sp, source=__name__), _workload(dep))
    _run(MonitoringBackend(MOD, mon, source=__name__), _workload(dep))
    assert _comparable(mon.events) == _comparable(sp.events)
    syms = [e["sym"] for e in mon.events]
    assert MOD + ".decorated" in syms and MOD + ".Base.hello" in syms
    hello = [e for e in mon.events if e["sym"] == MOD + ".Base.hello"][0]
    assert hello["own"] == MOD + ".Child" and hello["in"]["defaulted"] == ["k"]
    assert MOD + ".helper" not in syms           # dep -> dep traffic is not our boundary


def test_raises_carry_their_type(dep):
    sink = RecSink()
    _run(MonitoringBackend(MOD, sink, source=__name__),
         lambda: (_swallow(dep.boom, 1), _swallow(dep.boom_custom)))
    assert [e["raised"] for e in sink.events] == ["ValueError", MOD + ".MyErr"]
    sp = RecSink()
    _run(SetProfileBackend(MOD, sp, source=__name__), lambda: _swallow(dep.boom, 1))
    assert sp.events[0]["raised"] == UNTYPED_RAISE


def test_generator_call_is_recorded_once_as_a_generator(dep):
    for cls in (MonitoringBackend, SetProfileBackend):
        sink = RecSink()

        def body():
            assert list(dep.gen(3)) == [0, 1, 2]
            g = dep.gen(5)
            next(g)
            next(g)
            g.close()
            c = dep.coro(1)
            _swallow(c.send, None)

        _run(cls(MOD, sink, source=__name__), body)
        gens = [e for e in sink.events if e["sym"] == MOD + ".gen"]
        assert [g["result"] for g in gens] == [{"__opaque__": "generator"}] * 2, cls
        assert [g["in"]["bound"]["n"] for g in gens] == [3, 5]
        assert all(g["dur"] is None for g in gens)
        (co,) = [e for e in sink.events if e["sym"] == MOD + ".coro"]
        assert co["result"] == {"__opaque__": "coroutine"}


def test_every_recorded_call_has_dur_and_tid(dep):
    import threading
    sink = RecSink()
    _run(MonitoringBackend(MOD, sink, source=__name__), _workload(dep))
    for e in sink.events:
        assert e["tid"] == threading.get_ident()
        if e["sym"] not in (MOD + ".gen",):
            assert isinstance(e["dur"], float) and e["dur"] >= 0.0


def test_unsampled_calls_are_tallied_not_dropped(dep):
    for cls in (MonitoringBackend, SetProfileBackend):
        sink = RecSink(sample=False)
        _run(cls(MOD, sink, source=__name__),
             lambda: ([dep.decorated(i) for i in range(4)], _swallow(dep.boom, 1)))
        assert sink.events == []
        kinds = [(t["sym"], t["kind"]) for t in sink.tallies]
        assert kinds == [(MOD + ".decorated", "return")] * 4 + [(MOD + ".boom", "raise")], cls
        raised = sink.tallies[-1]["raised"]
        assert raised == ("ValueError" if cls is MonitoringBackend else UNTYPED_RAISE)
        assert all(t["dur"] is not None for t in sink.tallies)
        assert [t["where"]["func"] for t in sink.tallies][-1] == "_swallow"


def test_todays_sinks_keep_todays_call(dep, tmp_path):
    """MemorySink / JsonlSink accept neither dur/tid nor tally: nothing is lost."""
    import json

    from pymolt.verify._sinks import JsonlSink, MemorySink
    mem = MemorySink()
    _run(MonitoringBackend(MOD, mem, source=__name__), _workload(dep))
    assert len(mem.records) >= 10
    out = tmp_path / "t.jsonl"
    js = JsonlSink(str(out))
    _run(MonitoringBackend(MOD, js, source=__name__), lambda: _swallow(dep.boom, 1))
    js.close()
    (ev,) = [json.loads(line) for line in out.read_text().splitlines()]
    assert ev["t"] == "raise" and ev["raised"] == "ValueError"


# ── DISABLE and lazy returns ─────────────────────────────────────────────────


def _counting(backend, name):
    counts = {}
    real = getattr(backend, name)

    def counted(code, *rest):
        counts[code.co_name] = counts.get(code.co_name, 0) + 1
        return real(code, *rest)

    setattr(backend, name, counted)
    return counts


def test_disable_silences_code_outside_the_target(dep):
    def ours(x):
        return x

    sink = RecSink()
    be = MonitoringBackend(MOD, sink, source=__name__)
    starts = _counting(be, "_on_start")
    returns = _counting(be, "_on_return")
    _run(be, lambda: [(ours(i), dep.uses_helper(i)) for i in range(50)])
    assert starts["ours"] == 1                   # DISABLEd after the first call
    assert starts["uses_helper"] == 50           # a dependency: the caller check is per call
    assert starts["helper"] == 100               # (called from the dependency: not recorded)
    assert returns.get("helper", 0) == 0         # PY_RETURN armed only on recorded code
    assert returns["uses_helper"] == 50
    assert [e["sym"] for e in sink.events] == [MOD + ".uses_helper"] * 50


def test_callback_errors_never_reach_the_program(dep):
    class Broken(RecSink):
        def on_return(self, *a, **kw):
            raise RuntimeError("sink broke")

    _run(MonitoringBackend(MOD, Broken(), source=__name__),
         lambda: [dep.plain(i) for i in range(3)])
    assert dep.plain(9) == 9


# ── watch mode ───────────────────────────────────────────────────────────────


def test_watch_mode_fires_only_for_watch_set_code(dep):
    sink = RecSink()
    be = MonitoringBackend(MOD, sink, source=__name__, mode="watch",
                           watch_targets=[MOD + ".plain", MOD + ".Base", MOD + ".decorated"])
    starts = _counting(be, "_on_start")
    _run(be, lambda: (dep.plain(1), dep.uses_helper(2), dep.Base(3).hello(),
                      dep.decorated(4), dep.Child.make()))
    assert set(starts) == {"plain", "__init__", "hello", "wrapper", "decorated", "make"}
    syms = [e["sym"] for e in sink.events]
    assert syms == [MOD + ".plain", MOD + ".Base.__init__", MOD + ".Base.hello",
                    MOD + ".decorated", MOD + ".Base.make"]
    assert not be.uses_profiling


def test_watch_mode_resolves_targets_imported_later(tmp_path, monkeypatch):
    pkg = tmp_path / "vfakelate"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("def f(a, b=1):\n    return a\n\n"
                                     "def g():\n    return 2\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop("vfakelate", None)
    sink = RecSink()
    be = MonitoringBackend("vfakelate", sink, source=__name__, mode="watch",
                           watch_targets=["vfakelate.f", "vfakelate.missing"])
    try:
        def body():
            mod = importlib.import_module("vfakelate")
            mod.f(1)
            mod.g()
        _run(be, body)
    finally:
        sys.modules.pop("vfakelate", None)
    assert [e["sym"] for e in sink.events] == ["vfakelate.f"]
    assert be.skipped == [("vfakelate.missing", "attr-missing")]


def test_watch_prefix_targets_use_a_prefix_restricted_global_start(dep):
    sink = RecSink()
    be = MonitoringBackend(MOD, sink, source=__name__, mode="watch",
                           watch_targets=[MOD + ".*"])
    _run(be, lambda: (dep.plain(1), dep.uses_helper(1)))
    assert [e["sym"] for e in sink.events] == [MOD + ".plain", MOD + ".uses_helper"]
    assert be.uses_profiling


def test_switch_keeps_the_call_in_flight_and_narrows_to_observed(dep):
    sink = RecSink()
    be = MonitoringBackend(MOD, sink, source=__name__)

    def body():
        dep.plain(1)                                       # observed in discover
        # in flight across the switch: starts in discover, returns in watch
        assert dep.calls_back(lambda: be.enter_watch()) == "done"
        dep.plain(2)                                       # watched (observed)
        dep.decorated(3)                                   # never observed: not watched
        _swallow(dep.boom, 1)                              # never observed
    _run(be, body)
    assert be.mode == "watch" and be.switched
    got = [(e["sym"], e.get("result")) for e in sink.events]
    assert got == [(MOD + ".plain", 1), (MOD + ".calls_back", "done"), (MOD + ".plain", 2)]


def test_switch_adds_impact_targets_outside_the_discover_target(dep):
    other = _module("vfakeother", "def h(x=0):\n    return x\n")
    try:
        sink = RecSink()
        be = MonitoringBackend(MOD, sink, source=__name__)

        def body():
            other.h()                                       # DISABLEd in discover
            be.enter_watch(["vfakeother.h"])
            other.h(5)                                      # re-armed for watch
        _run(be, body)
    finally:
        sys.modules.pop("vfakeother", None)
    assert [(e["sym"], e["result"]) for e in sink.events] == [("vfakeother.h", 5)]


# ── tool ids ─────────────────────────────────────────────────────────────────


def test_tool_id_is_released_on_stop(dep):
    mon = sys.monitoring
    be = MonitoringBackend(MOD, RecSink(), source=__name__)
    be.start()
    tool = be._tool
    try:
        assert mon.get_tool(tool) == _monitoring.TOOL_NAME
    finally:
        be.stop()
    assert mon.get_tool(tool) is None
    assert mon.get_events(tool) == 0


def test_busy_tool_ids_are_skipped_and_exhaustion_raises(dep):
    mon = sys.monitoring
    taken = []
    try:
        for tool in _monitoring.TOOL_IDS[:2]:
            if mon.get_tool(tool) is None:
                mon.use_tool_id(tool, "squatter")
                taken.append(tool)
        sink = RecSink()
        be = MonitoringBackend(MOD, sink, source=__name__)
        _run(be, lambda: dep.plain(1))
        assert len(sink.events) == 1
        for tool in _monitoring.TOOL_IDS[2:]:
            if mon.get_tool(tool) is None:
                mon.use_tool_id(tool, "squatter")
                taken.append(tool)
        with pytest.raises(RuntimeError):
            MonitoringBackend(MOD, RecSink(), source=__name__).start()
    finally:
        for tool in taken:
            mon.free_tool_id(tool)


def test_coexists_with_another_monitoring_tool(dep):
    """Another tool (coverage.py style: its own id, DISABLE after the first hit) keeps its
    events, and ours are complete."""
    mon = sys.monitoring
    other = mon.COVERAGE_ID
    if mon.get_tool(other) is not None:
        pytest.skip("coverage tool id in use")
    seen = []

    def theirs(code, offset):
        if code.co_filename == "<" + MOD + ">":
            seen.append(code.co_name)
        return mon.DISABLE

    mon.use_tool_id(other, "fake-coverage")
    mon.register_callback(other, mon.events.PY_START, theirs)
    mon.set_events(other, mon.events.PY_START)
    try:
        sink = RecSink()
        _run(MonitoringBackend(MOD, sink, source=__name__),
             lambda: [dep.plain(i) for i in range(5)])
        dep.uses_helper(1)
    finally:
        mon.set_events(other, 0)
        mon.register_callback(other, mon.events.PY_START, None)
        mon.free_tool_id(other)
    assert len(sink.events) == 5
    assert seen.count("plain") == 1 and "uses_helper" in seen and "helper" in seen


# ── incremental (CALL-driven watch set) ──────────────────────────────────────

RECALL_MOD = "vfakerecall"

RECALL_SRC = '''
import functools

def _helper(x):
    return x + 1

def f(x):
    return _helper(x) + _helper(x)

def g(x, y=2):
    return x * y

def deco(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return fn(*args, **kwargs)
    return wrapper

@deco
def decorated(n=1):
    return _helper(n)

def _gen(n):
    for i in range(n):
        yield i

def gen(n):
    for i in range(n):
        yield _helper(i)

def rows(n):
    return _gen(n)               # a generator created by the dependency, iterated by us

class Base:
    def __init__(self, x=0):
        self.x = _helper(x)

    def method(self, k=1):
        return self.x + k

    @classmethod
    def make(cls, x):
        return cls(x)

    @staticmethod
    def stat(v=3):
        return v

    @property
    def prop(self):
        return self.x

    def __getitem__(self, k):
        return k

    def __iter__(self):
        return iter([self.x])

    def __len__(self):
        return 1

    def __add__(self, other):
        return self.x + 1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __call__(self, v):
        return v

class Result:
    def __init__(self, v):
        self.v = v

    def value(self):
        return self.v

    def double(self):
        return self.v * 2

def query(v):
    return Result(_helper(v))     # methods on a returned object

def boom():
    raise ValueError("no")
'''


def _local(x):
    return x


def _recall_workload(dep):
    """Every way our code reaches dependency code, run several times."""
    class Mine(dep.Base):
        def __init__(self):
            super().__init__(1)

    class Inherit(dep.Base):
        pass

    part = functools.partial(dep.g, 3)
    for _round in range(4):
        dep.f(1)
        dep.decorated(2)
        list(dep.gen(2))
        for _row in dep.rows(2):
            pass
        b = dep.Base(1)
        b.method()
        dep.Base.make(2)
        dep.Base.stat()
        b.prop
        b[3]
        for _item in b:
            pass
        len(b)
        b + b
        with b:
            pass
        b(5)
        r = dep.query(1)
        r.value()
        r.double()
        part()
        for fn in (dep.f, dep.g, _local, dep.decorated):   # a polymorphic call site
            fn(1)
        list(map(dep.g, [1, 2]))
        Mine()
        Inherit(4)
        try:
            dep.boom()
        except ValueError:
            pass


def _site_counts(sink):
    import collections
    return collections.Counter((e["sym"], e["t"], e["where"]["func"], e["where"]["line"])
                               for e in sink.events)


@pytest.fixture
def recall_dep():
    yield _module(RECALL_MOD, RECALL_SRC)
    sys.modules.pop(RECALL_MOD, None)


def _run_threads(body, n=3):
    import threading
    threads = [threading.Thread(target=body) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)


def test_incremental_observes_the_same_calls_as_discover(recall_dep):
    sinks = {}
    for mode in ("discover", "incremental"):
        sink = RecSink()
        be = MonitoringBackend(RECALL_MOD, sink, source=__name__, mode=mode)
        _run(be, lambda: (_recall_workload(recall_dep),
                          _run_threads(lambda: _recall_workload(recall_dep))))
        sinks[mode] = sink
    discover, incremental = _site_counts(sinks["discover"]), _site_counts(sinks["incremental"])
    assert incremental == discover          # same (sym, site) set AND the same call counts
    syms = {k[0] for k in discover}
    for expected in ("f", "decorated", "gen", "_gen", "Base.__init__", "Base.method",
                     "Base.make", "Base.stat", "Base.prop", "Base.__getitem__",
                     "Base.__iter__", "Base.__len__", "Base.__add__", "Base.__enter__",
                     "Base.__exit__", "Base.__call__", "query", "Result.value",
                     "Result.double", "g", "boom"):
        assert RECALL_MOD + "." + expected in syms, expected
    assert RECALL_MOD + "._helper" not in syms
    raised = [e for e in sinks["incremental"].events if e["t"] == "raise"]
    assert raised and all(e["raised"] == "ValueError" for e in raised)


def test_incremental_disables_internal_and_settled_call_sites(recall_dep):
    sink = RecSink()
    be = MonitoringBackend(RECALL_MOD, sink, source=__name__, mode="incremental")
    calls = _counting(be, "_on_call")
    starts = _counting(be, "_on_start")

    def body():
        for _ in range(50):
            recall_dep.f(1)                         # one monomorphic site
        for _ in range(10):
            for fn in (recall_dep.f, recall_dep.g):
                fn(1)                               # a polymorphic site: never disabled

    _run(be, body)
    # the f site settles after 2 calls; the polymorphic site fires all 20; range() x2 once each
    assert calls["body"] == 2 + 20 + 2
    assert calls.get("f", 0) <= 2                   # dep-internal sites: one callback each, ever
    assert starts.get("_helper", 0) == 0            # unwatched dependency internals are free
    assert starts["f"] == 60
    assert len([e for e in sink.events if e["sym"] == RECALL_MOD + ".f"]) == 60
    assert be.armed == 2


def test_incremental_needs_two_tool_ids_and_releases_both(recall_dep):
    mon = sys.monitoring
    be = MonitoringBackend(RECALL_MOD, RecSink(), source=__name__, mode="incremental")
    be.start()
    tools = (be._tool, be._tool2)
    try:
        assert None not in tools and tools[0] != tools[1]
        assert all(mon.get_tool(t) == _monitoring.TOOL_NAME for t in tools)
    finally:
        be.stop()
    assert all(mon.get_tool(t) is None and mon.get_events(t) == 0 for t in tools)
    taken = []
    try:
        for tool in _monitoring.TOOL_IDS[1:]:
            if mon.get_tool(tool) is None:
                mon.use_tool_id(tool, "squatter")
                taken.append(tool)
        with pytest.raises(RuntimeError, match="two free"):
            MonitoringBackend(RECALL_MOD, RecSink(), source=__name__, mode="incremental").start()
        assert mon.get_tool(_monitoring.TOOL_IDS[0]) is None     # nothing leaked
    finally:
        for tool in taken:
            mon.free_tool_id(tool)


def test_incremental_prewatches_impact_targets_outside_the_target(recall_dep):
    other = _module("vfakerecall_other", "def h(x=0):\n    return x\n")
    try:
        sink = RecSink()
        be = MonitoringBackend(RECALL_MOD, sink, source=__name__, mode="incremental",
                               watch_targets=["vfakerecall_other.h"])
        _run(be, lambda: (other.h(1), recall_dep.f(1)))
    finally:
        sys.modules.pop("vfakerecall_other", None)
    assert [e["sym"] for e in sink.events] == ["vfakerecall_other.h", RECALL_MOD + ".f"]


# ── incremental settle (tool B freed) ────────────────────────────────────────


def _syms(sink, name):
    return [e for e in sink.events if e["sym"] == RECALL_MOD + "." + name]


def test_settle_frees_tool_b_hands_over_its_codes_and_keeps_arming_calls(recall_dep):
    """``reopen=0``: the settle frees tool B for good (no LINE events on our code), so the
    hand-over is seen alone, and the recall caveat it leaves."""
    dep = recall_dep
    mon = sys.monitoring
    sink = RecSink()
    be = MonitoringBackend(RECALL_MOD, sink, source=__name__, mode="incremental", reopen=0)
    be.start()
    tool, tool2 = be._tool, be._tool2
    try:
        b = dep.Base(1)
        b[1]                                  # dunder: tool B
        b.prop                                # property: tool B
        list(dep.gen(1))                      # generator: tool B
        dep.f(1)                              # plain call: tool A
        assert be.settle("test") is True
        assert be.settle("again") is False    # once
        assert mon.get_tool(tool2) is None and mon.get_events(tool2) == 0
        assert mon.get_tool(tool) == _monitoring.TOOL_NAME
        b[2]                                  # handed over: still recorded
        b.prop
        list(dep.gen(2))
        dep.f(2)
        dep.g(7)                              # a plain call first made after the settle
        dep.query(3).double()                 # ... and methods on its returned object
        len(b)                                # recall caveat: a dunder first used after
        for _row in dep.rows(2):              # ... and a dependency-made generator
            pass
    finally:
        be.stop()
    assert mon.get_tool(tool) is None
    assert [e["in"]["bound"]["k"] for e in _syms(sink, "Base.__getitem__")] == [1, 2]
    assert len(_syms(sink, "Base.prop")) == 2
    assert [e["in"]["bound"]["n"] for e in _syms(sink, "gen")] == [1, 2]
    assert [e["in"]["bound"]["x"] for e in _syms(sink, "f")] == [1, 2]
    assert len(_syms(sink, "g")) == 1 and len(_syms(sink, "Result.double")) == 1
    assert _syms(sink, "Base.__len__") == [] and _syms(sink, "_gen") == []
    assert be.settle_reason == "test" and be.handed_over >= 3 and be.settled_at is not None
    assert be.reopened == 0 and not be._lined


def test_settle_neither_loses_nor_doubles_calls_from_threads(recall_dep):
    import collections
    import threading
    dep = recall_dep
    sink = RecSink()
    be = MonitoringBackend(RECALL_MOD, sink, source=__name__, mode="incremental")
    n, nthreads = 1500, 4
    halfway = threading.Barrier(nthreads + 1)
    b = dep.Base(0)

    def worker(base):
        for i in range(n):
            v = base + i
            b[v]                              # B -> A hand-over
            dep.f(v)                          # A
            list(dep.gen(v % 3))              # B generator -> A
            if i == n // 3:
                halfway.wait(10)

    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    be.start()
    try:
        b[-1], dep.f(-1), list(dep.gen(0))    # recorded by both tools' owners first
        threads = [threading.Thread(target=worker, args=(k * 100000,))
                   for k in range(nthreads)]
        for t in threads:
            t.start()
        halfway.wait(10)
        assert be.settle("test")
        for t in threads:
            t.join(60)
    finally:
        be.stop()
        sys.setswitchinterval(old)
    values = [k * 100000 + i for k in range(nthreads) for i in range(n)]
    assert sorted(e["in"]["bound"]["k"] for e in _syms(sink, "Base.__getitem__")) == \
        sorted([-1] + values)
    assert sorted(e["in"]["bound"]["x"] for e in _syms(sink, "f")) == sorted([-1] + values)
    gens = collections.Counter(e["in"]["bound"]["n"] for e in _syms(sink, "gen"))
    expected = collections.Counter([0] + [v % 3 for v in values])
    assert gens == expected


# ── late contacts: a line of ours that first runs after the settle ──────────

_LATE = ("Base.__len__", "Base.prop", "_gen", "Base.__enter__", "Base.__exit__",
         "Base.__add__", "Base.__getitem__")


def _late_path(dep, b):
    """A path of ours first run after the settle (an hour in, in a long-running service).
    Its dependency contacts pass no CALL instruction of ours, except ``__exit__``."""
    n = len(b)                          # Base.__len__, run by the builtin
    v = b.prop                          # a property accessor
    for _row in dep.rows(2):            # _gen: a generator the dependency made, started by us
        pass
    with b:                             # Base.__enter__ (no CALL), Base.__exit__
        pass
    return n, v, b + b, b[7]            # Base.__add__, Base.__getitem__


def test_a_dunder_property_or_generator_first_used_after_the_settle_is_recorded(recall_dep):
    dep = recall_dep
    sink = RecSink()
    be = MonitoringBackend(RECALL_MOD, sink, source=__name__, mode="incremental")
    be.start()
    try:
        b = dep.Base(1)
        dep.f(1)
        assert be.settle("test")
        assert _late_path(dep, b) == (1, 2, 3, 7)
    finally:
        be.stop()
    for name in _LATE:
        assert len(_syms(sink, name)) == 1, name


LATE_USER = "vfakelateuser"

LATE_USER_SRC = '''
def warm(dep):
    b = dep.Base(1)
    dep.f(1)
    return b

def late(dep, b):
    n = len(b)
    v = b.prop
    for _row in dep.rows(2):
        pass
    with b:
        pass
    return n, v, b + b, b[7]
'''


def test_a_new_line_reopens_tool_b_until_quiet_and_an_old_line_does_not(recall_dep):
    """Our code is a module imported under tool B (its functions get LINE events from its
    module body); the test driving it is not our code, so only its lines count."""
    dep = recall_dep
    mon = sys.monitoring
    sink = RecSink()
    be = MonitoringBackend(RECALL_MOD, sink, source=LATE_USER, mode="incremental")
    be.start()
    try:
        user = _module(LATE_USER, LATE_USER_SRC)
        b = user.warm(dep)
        assert be.settle("test")
        tool2 = be._tool2
        assert tool2 is not None and mon.get_events(tool2) == 0 and be.reopened == 0
        assert user.late(dep, b) == (1, 2, 3, 7)        # its new lines re-open tool B
        assert be.reopened == 1 and mon.get_events(tool2) == mon.events.PY_START
        assert be.settle("quiet again")                 # closed: B's new contacts handed over
        assert mon.get_events(tool2) == 0 and be.settle_reason == "test"
        assert user.late(dep, b) == (1, 2, 3, 7)        # lines that ran before: B stays off
        assert be.reopened == 1 and mon.get_events(tool2) == 0
        mon.restart_events()                            # re-arms every DISABLEd line ...
        assert user.late(dep, b) == (1, 2, 3, 7)        # ... that still ran before
        assert be.reopened == 1 and mon.get_events(tool2) == 0
    finally:
        be.stop()
        sys.modules.pop(LATE_USER, None)
    for name in _LATE:
        assert len(_syms(sink, name)) == 3, name        # once per run: neither lost nor doubled
    assert [e["in"]["bound"]["k"] for e in _syms(sink, "Base.__getitem__")] == [7, 7, 7]
    assert be.reopen_seconds > 0 and mon.get_tool(tool2) is None


def test_reopen_and_close_neither_lose_nor_double_calls_from_threads(recall_dep):
    """While tool B is on again, a code handed over to tool A is seen by both tools: each
    start is recorded once, across repeated re-open / close cycles under thread switches."""
    import collections
    import threading
    dep = recall_dep
    sink = RecSink()
    be = MonitoringBackend(RECALL_MOD, sink, source=__name__, mode="incremental")
    n, nthreads = 1500, 4
    b = dep.Base(0)

    def worker(base):
        for i in range(n):
            v = base + i
            b[v]                              # handed over to A at the first settle
            dep.f(v)                          # A
            list(dep.gen(v % 3))              # generator: handed over to A
            len(b)                            # a contact first made while B is on again

    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    be.start()
    try:
        b[-1], dep.f(-1), list(dep.gen(0))
        assert be.settle("test")
        threads = [threading.Thread(target=worker, args=(k * 100000,))
                   for k in range(nthreads)]
        for t in threads:
            t.start()
        cycles = 0
        while any(t.is_alive() for t in threads) and cycles < 200:
            be._reopen()
            be.settle("again")
            cycles += 1
        for t in threads:
            t.join(60)
    finally:
        be.stop()
        sys.setswitchinterval(old)
    assert be.reopened >= 2
    values = [k * 100000 + i for k in range(nthreads) for i in range(n)]
    assert sorted(e["in"]["bound"]["k"] for e in _syms(sink, "Base.__getitem__")) == \
        sorted([-1] + values)
    assert sorted(e["in"]["bound"]["x"] for e in _syms(sink, "f")) == sorted([-1] + values)
    gens = collections.Counter(e["in"]["bound"]["n"] for e in _syms(sink, "gen"))
    assert gens == collections.Counter([0] + [v % 3 for v in values])
    assert len(_syms(sink, "Base.__len__")) <= nthreads * n     # never doubled


# ── microbenchmark (opt-in: PYMOLT_BENCH=1) ──────────────────────────────────

BENCH_SRC = '''
class _Node:
    def __init__(self, v):
        self.v = v

    def __len__(self):
        return 1

def _helper(x):
    return x + 1

def api(x, scale=2):
    acc = 0
    for i in range(20):              # library-shaped: many internal calls per boundary call
        acc += _helper(i)
    node = _Node(x)                  # an internal construction
    return acc + len(node) + scale   # an internal dunder (tool B's worst case)
'''


class _TallySink(RecSink):
    """Steady state of a novelty-first sink: values unsampled, every call tallied."""

    def __init__(self):
        RecSink.__init__(self, sample=False)
        self.n = 0

    def tally(self, qualname, sym, site, kind, raised=None, dur=None):
        self.n += 1


# Our code, in a module of its own: the test driving it is not our code, so its lines never
# turn tool B on again after the settle (a timed line of ours that is new would).
BENCH_USER_SRC = '''
def _bench_local(x):
    return x * 2

def _bench_workload(api, n):
    acc = 0
    for i in range(n):
        acc += api(i)
        for _ in range(20):
            acc += _bench_local(i)
    return acc
'''


@pytest.mark.skipif(not __import__("os").environ.get("PYMOLT_BENCH"),
                    reason="microbenchmark: set PYMOLT_BENCH=1")
def test_microbenchmark_discover_vs_incremental_vs_watch(capsys):
    import statistics
    import time
    dep = _module("vfakebench", BENCH_SRC)
    user = _module("vfakebench_user", BENCH_USER_SRC)
    n = 60000
    try:
        def make(mode):
            if mode == "none":
                return None
            be = MonitoringBackend("vfakebench", _TallySink(), source="vfakebench_user",
                                   mode="discover" if mode in ("discover", "watch")
                                   else "incremental")
            if mode in ("incremental-settled", "incremental-reopened"):
                orig_s = be.start

                def start_settled():
                    orig_s()
                    user._bench_workload(dep.api, 1)     # arm, then tool B off
                    be.settle("bench")
                    # Steady state: past the de-duplication window after the settle. Busy,
                    # not asleep: an idle second lets the OS park the thread on a slow core.
                    until = time.monotonic() + _monitoring._SETTLE_GRACE + 0.05
                    while time.monotonic() < until:
                        user._bench_workload(dep.api, 1)
                    if mode == "incremental-reopened":
                        be._reopen()                     # what a new line of ours does
                    be.sink.n = 0
                be.start = start_settled
            if mode == "watch":
                orig = be.start

                def start():
                    orig()
                    user._bench_workload(dep.api, 1)     # observe, then watch the observed set
                    be.enter_watch()
                    be.sink.n = 0
                be.start = start
            return be

        results = {}
        for mode in ("none", "discover", "incremental", "incremental-settled",
                     "incremental-reopened", "watch"):
            times = []
            for _ in range(3):
                be = make(mode)
                if be is not None:
                    be.start()
                t0 = time.perf_counter()
                user._bench_workload(dep.api, n)
                times.append(time.perf_counter() - t0)
                if be is not None:
                    be.stop()
                    assert be.sink.n == n, (mode, be.sink.n)   # every boundary call counted
            results[mode] = statistics.median(times)
    finally:
        sys.modules.pop("vfakebench", None)
        sys.modules.pop("vfakebench_user", None)
    base = results["none"]
    with capsys.disabled():
        print(f"\n{n} boundary calls, 22 dependency-internal + 20 local calls each")
        for mode, t in results.items():
            print(f"  {mode:<20} {t:.3f}s  {t / base:.2f}x")
    assert results["incremental"] < results["discover"]
    assert results["incremental-settled"] < results["incremental"]
    assert results["incremental-settled"] < results["incremental-reopened"]
