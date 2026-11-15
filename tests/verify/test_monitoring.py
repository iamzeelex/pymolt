"""The ``sys.monitoring`` backend (3.12+): parity with setprofile, DISABLE, watch mode,
typed raises, generators, tool-id hygiene and coexistence with another tool.

Runtime-layer test: pure stdlib + pytest (3.6-compatible syntax); skipped below 3.12.
"""
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
