"""JsonlSink backpressure: a full queue makes the producer wait for the writer instead of
dropping the event.

The locust pilot's post-migration capture (3.12, monitoring backend, gevent) lost 1,015,860 of
2,363,581 events to ``backpressure_dropped``: the writer thread encodes ~300k events/s alone,
but a CPU-bound burst starved it of the GIL and the 65,536-slot queue overflowed. The diff then
had to mark thousands of facts "possibly-dropped". Waiting costs latency (counted as
``backpressure_wait_ms``), never evidence; giving up is reserved for a writer that cannot
make room (stalled, gone, or the caller itself), and is still counted.

Runtime-layer test: pure stdlib + pytest, 3.6-compatible syntax."""
import json
import os
import subprocess
import sys
import textwrap
import threading
import time

import pytest

from pymolt.verify._sinks import JsonlSink

ROOT = str(__import__("pathlib").Path(__file__).resolve().parents[2])


def _gate_writer(sink):
    """Hold the writer before each line while the returned event is clear (it starts clear)."""
    gate = threading.Event()
    real = sink._write_line

    def gated(fh, obj):
        gate.wait(30)
        real(fh, obj)

    sink._write_line = gated
    return gate


def _call(sink, i):
    sink.on_return("dep.f", {"bound": {"i": i}}, i)


def _lines(sink):
    with open(sink.path) as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _meta(sink):
    with open(sink.metadata_path) as fh:
        return json.load(fh)


def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached in {0}s".format(timeout))
        time.sleep(0.005)


def test_a_full_queue_waits_for_the_writer_and_loses_nothing(tmp_path):
    """The drop path that cost the locust pilot 43% of its events: with the writer held, a
    4-slot queue fills at once. Every one of 500 events is still written, in order."""
    sink = JsonlSink(str(tmp_path / "t-{pid}.jsonl"), queue_size=4, backpressure="block")
    gate = _gate_writer(sink)
    timer = threading.Timer(0.3, gate.set)
    timer.start()
    n = 500
    for i in range(n):
        _call(sink, i)
    sink.close()
    timer.join()

    assert [e["in"]["bound"]["i"] for e in _lines(sink)] == list(range(n))
    meta = _meta(sink)
    counters = meta["counters"]
    assert counters["events_seen"] == n and counters["events_written"] == n
    assert counters["backpressure_dropped"] == 0 and counters["dropped_events"] == 0
    assert counters["writer_stalls"] == 0
    # The price is stated: the producer waited while the writer was held.
    assert counters["backpressure_waits"] >= 1
    assert counters["backpressure_wait_ms"] >= 200
    assert meta["backpressure"] == {"policy": "block", "stall_seconds": 10.0, "queue_size": 4}


def test_block_is_the_default_policy(tmp_path, monkeypatch):
    monkeypatch.delenv("PYMOLT_TRACE_BACKPRESSURE", raising=False)
    sink = JsonlSink(str(tmp_path / "t-{pid}.jsonl"))
    sink.close()
    assert _meta(sink)["backpressure"]["policy"] == "block"
    monkeypatch.setenv("PYMOLT_TRACE_BACKPRESSURE", "drop")
    sink = JsonlSink(str(tmp_path / "d-{pid}.jsonl"))
    sink.close()
    assert _meta(sink)["backpressure"]["policy"] == "drop"
    monkeypatch.setenv("PYMOLT_TRACE_BACKPRESSURE", "bogus")
    sink = JsonlSink(str(tmp_path / "b-{pid}.jsonl"))
    sink.close()
    assert _meta(sink)["backpressure"]["policy"] == "block"


def test_the_drop_policy_still_counts_what_a_full_queue_refused(tmp_path):
    sink = JsonlSink(str(tmp_path / "t-{pid}.jsonl"), queue_size=4, backpressure="drop")
    gate = _gate_writer(sink)
    n = 100
    started = time.monotonic()
    for i in range(n):
        _call(sink, i)
    assert time.monotonic() - started < 1.0          # never waits
    gate.set()
    sink.close()

    counters = _meta(sink)["counters"]
    assert counters["events_seen"] == n
    assert counters["backpressure_dropped"] > 0
    assert counters["events_written"] + counters["backpressure_dropped"] == n
    assert counters["dropped_events"] == counters["backpressure_dropped"]
    assert counters["backpressure_waits"] == 0
    assert len(_lines(sink)) == counters["events_written"]


def test_a_stalled_writer_is_given_up_on_once_and_waited_for_again_when_it_moves(tmp_path):
    """A writer that makes no progress for ``stall_seconds`` (a wedged disk, a hung pipe) must
    not hold the program on every event: one producer waits that long, the losses after it
    are counted, and producers wait again only once the writer has moved."""
    sink = JsonlSink(str(tmp_path / "t-{pid}.jsonl"), queue_size=4, backpressure="block",
                     stall_seconds=0.2)
    gate = _gate_writer(sink)
    started = time.monotonic()
    for i in range(50):
        _call(sink, i)
    assert time.monotonic() - started < 2.0          # one stall wait, not one per event
    first = sink.metrics
    assert first["writer_stalls"] == 1
    assert first["backpressure_dropped"] > 0
    # Only the waits up to the stall: the events after it were dropped without waiting.
    assert 1 <= first["backpressure_waits"] <= 2
    assert first["backpressure_wait_ms"] < 1000

    gate.set()                                       # the writer moves again
    _wait_until(lambda: sink._queue.qsize() == 0)
    gate.clear()                                     # ... and is held once more
    for i in range(50, 100):
        _call(sink, i)
    second = sink.metrics
    assert second["writer_stalls"] == 2              # re-armed: it waited for the writer again
    assert second["backpressure_waits"] > first["backpressure_waits"]

    gate.set()
    sink.close()
    counters = _meta(sink)["counters"]
    assert counters["events_written"] + counters["backpressure_dropped"] == 100
    assert counters["dropped_events"] == counters["backpressure_dropped"]
    assert len(_lines(sink)) == counters["events_written"]


def test_the_writer_thread_never_waits_for_itself(tmp_path):
    sink = JsonlSink(str(tmp_path / "t-{pid}.jsonl"), queue_size=1, backpressure="block")
    gate = _gate_writer(sink)
    _call(sink, 0)
    _wait_until(lambda: sink._queue.qsize() == 0)    # the writer holds event 0
    _call(sink, 1)                                   # the queue is full now
    writer_ident = sink._writer.ident
    sink._writer.ident = threading.get_ident()       # as if this thread were the writer
    started = time.monotonic()
    _call(sink, 2)
    assert time.monotonic() - started < 0.1
    sink._writer.ident = writer_ident
    counters = sink.metrics
    assert counters["backpressure_dropped"] == 1 and counters["backpressure_waits"] == 0
    gate.set()
    sink.close()
    assert [e["in"]["bound"]["i"] for e in _lines(sink)] == [0, 1]


def test_close_waits_for_a_writer_that_is_still_draining(tmp_path):
    """At exit a deep queue is written, not abandoned after ``close_timeout``: the writer is
    waited for as long as it keeps processing events."""
    sink = JsonlSink(str(tmp_path / "t-{pid}.jsonl"), queue_size=10000, close_timeout=0.1)
    real = sink._write_line

    def slow(fh, obj):
        time.sleep(0.001)
        real(fh, obj)

    sink._write_line = slow
    n = 400                                          # ~0.4 s of writing > close_timeout
    for i in range(n):
        _call(sink, i)
    sink.close()
    counters = _meta(sink)["counters"]
    assert counters["events_written"] == n
    assert counters["shutdown_dropped"] == 0 and counters["sink_failures"] == 0
    assert len(_lines(sink)) == n


def test_close_gives_up_on_a_writer_that_stopped_and_counts_what_it_left(tmp_path):
    sink = JsonlSink(str(tmp_path / "t-{pid}.jsonl"), queue_size=100, close_timeout=0.1)
    gate = _gate_writer(sink)
    for i in range(10):
        _call(sink, i)
    started = time.monotonic()
    sink.close()
    assert time.monotonic() - started < 2.0
    counters = _meta(sink)["counters"]
    assert counters["sink_failures"] == 1
    # Nothing reached the disk: what is still queued and the batch in the writer's hand
    # are all counted as lost.
    assert counters["events_written"] == 0
    assert counters["shutdown_dropped"] == 10 and counters["dropped_events"] == 10
    gate.set()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="POSIX fork only")
@pytest.mark.skipif(sys.version_info < (3, 7), reason="os.register_at_fork is 3.7+")
def test_a_forked_child_writes_its_own_trace_with_its_own_writer(tmp_path):
    """Threads do not survive fork. The child used to inherit the parent's sink with no
    writer: its events filled a queue nobody drained (and, with backpressure, would have
    waited for it). Now it gets its own file, writer and counters; the parent's are intact."""
    script = textwrap.dedent('''
        import json, os, sys
        sys.path.insert(0, {root!r})
        from pymolt.verify._sinks import JsonlSink
        sink = JsonlSink({path!r}, queue_size=4)
        for i in range(10):
            sink.on_return("dep.parent", {{"bound": {{"i": i}}}}, i)
        pid = os.fork()
        if pid == 0:
            for i in range(300):
                sink.on_return("dep.child", {{"bound": {{"i": i}}}}, i)
            sink.close()
            os._exit(0)
        os.waitpid(pid, 0)
        for i in range(10, 20):
            sink.on_return("dep.parent", {{"bound": {{"i": i}}}}, i)
        sink.close()
        print(json.dumps({{"child": pid}}))
    ''').format(root=ROOT, path=str(tmp_path / "t-{pid}.jsonl"))
    r = subprocess.run([sys.executable, "-c", script], stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, universal_newlines=True, timeout=60)
    assert r.returncode == 0, r.stderr
    child = json.loads(r.stdout.strip().splitlines()[-1])["child"]
    traces = sorted(p.name for p in tmp_path.glob("t-*.jsonl"))
    assert len(traces) == 2
    child_path = tmp_path / "t-{0}.jsonl".format(child)
    parent_path = [tmp_path / name for name in traces if name != child_path.name][0]

    def read(path):
        with open(str(path)) as fh:
            return [json.loads(line) for line in fh if line.strip()]

    assert [e["in"]["bound"]["i"] for e in read(child_path)] == list(range(300))
    assert {e["q"] for e in read(child_path)} == {"dep.child"}
    assert [e["in"]["bound"]["i"] for e in read(parent_path)] == list(range(20))
    with open(str(child_path) + ".meta.json") as fh:
        child_meta = json.load(fh)
    with open(str(parent_path) + ".meta.json") as fh:
        parent_meta = json.load(fh)
    assert child_meta["pid"] == child and child_meta["counters"]["events_written"] == 300
    assert child_meta["counters"]["dropped_events"] == 0
    assert parent_meta["counters"]["events_written"] == 20
    assert parent_meta["counters"]["dropped_events"] == 0


GEVENT_BURST = textwrap.dedent('''
    import json, sys
    sys.path.insert(0, {root!r})
    sys.path.insert(0, {deps!r})
    from gevent import monkey
    monkey.patch_all()
    import gevent, burstdep
    from pymolt.verify._backend import PrefixMatcher
    from pymolt.verify.{module} import {backend}
    from pymolt.verify._sinks import JsonlSink
    sink = JsonlSink({path!r}, queue_size=8)
    be = {backend}(PrefixMatcher(["burstdep"]), sink, source="__main__")
    be.start()
    def work():
        for i in range(500):
            burstdep.f(i)
            if i % 50 == 0:
                gevent.sleep(0)
    gevent.joinall([gevent.spawn(work) for _ in range(8)])
    be.stop()
    sink.close()
    print("ok")
''')


@pytest.mark.parametrize("backend", ["SetProfileBackend", "MonitoringBackend"])
def test_gevent_burst_through_a_tiny_queue_waits_without_deadlock(backend, tmp_path):
    """The locust shape: greenlets hammer a dependency after ``patch_all``. A producer that
    waits for the writer parks its OS thread on the original ``time.sleep`` (never the hub),
    the raw writer drains, and every event is kept."""
    pytest.importorskip("gevent")
    if backend == "MonitoringBackend" and sys.version_info < (3, 12):
        pytest.skip("sys.monitoring is 3.12+")
    path = tmp_path / "g.jsonl"
    deps = tmp_path / "deps"
    deps.mkdir()
    (deps / "burstdep.py").write_text("def f(x):\n    return x + 1\n")
    module = "_monitoring" if backend == "MonitoringBackend" else "_backend"
    script = GEVENT_BURST.format(root=ROOT, deps=str(deps), backend=backend, module=module,
                                 path=str(path))
    r = subprocess.run([sys.executable, "-c", script], stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, universal_newlines=True, timeout=120)
    assert r.returncode == 0 and "ok" in r.stdout, r.stderr
    with open(str(path)) as fh:
        events = [json.loads(line) for line in fh if line.strip()]
    assert sorted(e["in"]["bound"]["x"] for e in events if e.get("sym") == "burstdep.f") == \
        sorted(list(range(500)) * 8)
    with open(str(path) + ".meta.json") as fh:
        counters = json.load(fh)["counters"]
    assert counters["dropped_events"] == 0 and counters["backpressure_dropped"] == 0
    assert counters["backpressure_waits"] >= 1
