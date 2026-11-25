"""The injected runtime keeps working after the program monkeypatches threading (gevent
``patch_all``, eventlet): its writer / flusher / controller threads and every lock are built on
primitives captured at import, never on ``threading`` / ``queue`` / ``time.sleep`` as they are
by then (the locust pilot's pytest deadlocked: a writer parked in the gevent hub while the main
thread waited on the import lock).

Runtime-layer test: pure stdlib + pytest, 3.6-compatible syntax."""
import contextlib
import json
import subprocess
import sys
import textwrap
import threading
import time
import types

import pytest

from pymolt.verify import _rawthreads
from pymolt.verify._backend import HAS_MONITORING, PrefixMatcher, SetProfileBackend
from pymolt.verify._sinks import FactSink, JsonlSink

if HAS_MONITORING:
    from pymolt.verify._monitoring import MonitoringBackend

BACKENDS = [SetProfileBackend]
if HAS_MONITORING:
    BACKENDS.append(MonitoringBackend)
ROOT = str(__import__("pathlib").Path(__file__).resolve().parents[2])


def _poisoned(*a, **k):
    raise RuntimeError("a monkeypatched primitive was used by the runtime")


@contextlib.contextmanager
def monkeypatched_threading():
    """What a cooperative patch does to the primitives: every one of them is unusable (a real
    patch would park the caller in a hub). The runtime must not touch any of them."""
    import _thread
    import queue
    saved = [(threading, "Lock", threading.Lock), (threading, "Event", threading.Event),
             (threading, "Thread", threading.Thread), (threading, "Condition", threading.Condition),
             (threading, "get_ident", threading.get_ident),
             (_thread, "allocate_lock", _thread.allocate_lock),
             (_thread, "start_new_thread", _thread.start_new_thread),
             (_thread, "get_ident", _thread.get_ident), (time, "sleep", time.sleep),
             (queue, "Queue", queue.Queue)]
    for mod, name, _orig in saved:
        setattr(mod, name, _poisoned)
    try:
        yield
    finally:
        for mod, name, orig in reversed(saved):
            setattr(mod, name, orig)


def _dep():
    mod = types.ModuleType("vfakeraw")
    exec("def f(x):\n    return x + 1\n", mod.__dict__)
    sys.modules["vfakeraw"] = mod
    return mod


@pytest.mark.parametrize("cls", BACKENDS)
def test_jsonl_sink_and_backend_work_after_a_threading_monkeypatch(cls, tmp_path):
    dep = _dep()
    out = tmp_path / "t.jsonl"
    try:
        with monkeypatched_threading():
            sink = JsonlSink(str(out))
            be = cls(PrefixMatcher(["vfakeraw"]), sink, source=__name__)
            be.start()
            for i in range(20):
                dep.f(i)
            be.stop()
            sink.close()
    finally:
        sys.modules.pop("vfakeraw", None)
    events = [json.loads(line) for line in out.read_text().splitlines() if line.strip()]
    assert len([e for e in events if e.get("sym") == "vfakeraw.f"]) == 20
    meta = json.loads((tmp_path / "t.jsonl.meta.json").read_text())
    assert meta["counters"]["events_written"] >= 20
    assert meta["counters"]["dropped_events"] == 0


@pytest.mark.parametrize("cls", BACKENDS)
def test_fact_sink_flusher_and_backend_work_after_a_threading_monkeypatch(cls, tmp_path):
    dep = _dep()
    try:
        with monkeypatched_threading():
            sink = FactSink(str(tmp_path / "t-{pid}.jsonl"), flush_seconds=0.05)
            be = cls(PrefixMatcher(["vfakeraw"]), sink, source=__name__)
            be.start()
            for i in range(20):
                dep.f(i)
            _rawthreads.sleep(0.2)          # the flusher wrote at least one snapshot
            be.stop()
            sink.close()
    finally:
        sys.modules.pop("vfakeraw", None)
    snapshot = json.loads(open(sink.path).read())
    assert sum(f["count"] for f in snapshot["facts"]) == 20


def test_raw_primitives_behave_like_their_threading_counterparts():
    q = _rawthreads.RawQueue(2)
    q.put_nowait(1)
    q.put_nowait(2)
    with pytest.raises(_rawthreads.Full):
        q.put_nowait(3)
    assert (q.get(timeout=0.1), q.get_nowait()) == (1, 2)
    with pytest.raises(_rawthreads.Empty):
        q.get(timeout=0.02)
    seen = []
    done = _rawthreads.RawEvent()

    def consumer():
        seen.append(q.get(timeout=2))
        done.set()

    t = _rawthreads.spawn(consumer, "t")
    _rawthreads.sleep(0.05)
    q.put_nowait("x")
    assert done.wait(2) and seen == ["x"]
    t.join(1)
    assert not t.is_alive()


GEVENT_SCRIPT = textwrap.dedent('''
    import json, sys
    sys.path.insert(0, {root!r})
    from pymolt.verify._backend import PrefixMatcher
    from pymolt.verify.{module} import {backend}
    from pymolt.verify._sinks import {sink_cls}
    from gevent import monkey
    {early}
    monkey.patch_all()
    {late}
    import math, gevent
    sink = {sink_cls}({path!r})
    be = {backend}(PrefixMatcher(["math"]), sink, source="__main__")
    be.start()
    def work():
        for i in range(5):
            math.sqrt(i)
            gevent.sleep(0)
    gevent.joinall([gevent.spawn(work) for _ in range(4)])
    be.stop()
    sink.close()
    print("ok")
''')


@pytest.mark.parametrize("backend", ["SetProfileBackend", "MonitoringBackend"])
@pytest.mark.parametrize("when", ["sink-before-patch", "sink-after-patch"])
def test_real_gevent_patch_all_does_not_deadlock_a_capture(backend, when, tmp_path):
    pytest.importorskip("gevent")
    if backend == "MonitoringBackend" and not HAS_MONITORING:
        pytest.skip("sys.monitoring is 3.12+")
    path = tmp_path / "g.jsonl"
    module = "_monitoring" if backend == "MonitoringBackend" else "_backend"
    script = GEVENT_SCRIPT.format(root=ROOT, backend=backend, module=module, sink_cls="JsonlSink",
                                  path=str(path), early="", late="")
    if when == "sink-before-patch":
        # the sink (and its writer) exist before the program patches: move patch_all after
        script = script.replace("monkey.patch_all()\n", "", 1).replace(
            "import math, gevent", "import math, gevent\nmonkey.patch_all()", 1)
    r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                       timeout=90)
    assert r.returncode == 0 and "ok" in r.stdout, r.stderr
    events = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    assert len([e for e in events if e.get("sym") == "math.sqrt"]) == 20


@pytest.mark.parametrize("cls", BACKENDS)
def test_compiled_calls_in_flight_in_different_greenlets_pair_correctly(cls):
    greenlet = pytest.importorskip("greenlet")
    import functools

    from pymolt.verify._sinks import MemorySink

    sink = MemorySink()
    be = cls(PrefixMatcher(["_functools"]), sink, source=__name__)
    order = []
    main = greenlet.getcurrent()

    def a_work():
        def cb(x, y):
            order.append("a-cb")
            main.switch()                # a greenlet switch in the middle of reduce #1
            order.append("a-resumed")
            return x + y
        return functools.reduce(cb, [1, 2, 3])

    def b_work():
        def cb(x, y):
            order.append("b-cb")
            ga.switch()
            return x + y
        return functools.reduce(cb, [1, 2])

    ga = greenlet.greenlet(a_work)
    gb = greenlet.greenlet(b_work)
    be.start()
    try:
        ga.switch()                      # A: reduce in flight, switched out
        gb.switch()                      # B: reduce in flight, switches back to A
        ga.switch()                      # A resumes, finishes its reduce
        gb.switch()                      # B finishes its reduce
    finally:
        be.stop()
    # the two reduce calls really interleaved: A's first callback, all of B's, then A's rest
    assert order == ["a-cb", "b-cb", "a-resumed", "a-cb", "a-resumed"]
    recs = [r for r in sink.records.values() if r["qualname"].endswith("reduce")]
    assert sum(r["count"] for r in recs) == 2
    assert all(r["raised"] is None and r["result"] == {"__opaque__": "unobserved:c-return"}
               for r in recs)
    assert not be._cstack            # every stack drained: nothing mis-paired or left behind
    time.sleep(0)
