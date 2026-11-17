"""The discover -> watch controller (``boundary_tracer.Controller``) and Mode B wiring.

Runtime-layer test: pure stdlib + pytest, 3.6-compatible syntax. The monitoring family runs on
3.12+; the setprofile family (discover = setprofile, watch = wrap) runs everywhere.
"""
import json
import os
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest

from pymolt.verify import _sinks, boundary_tracer
from pymolt.verify._backend import HAS_MONITORING
from pymolt.verify._normalize import normalize
from pymolt.verify.boundary_tracer import Controller

MOD = "vfakectl"
USER = "vfakectl_user"

SRC = '''
def plain(a, b=2):
    return a

def other(x):
    return x

def boom():
    raise KeyError("k")

def calls_back(fn):
    fn()
    return "done"

def _deco(f):
    import functools

    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        return f(*args, **kwargs)
    return wrapper

@_deco
def decorated(n=1):
    return n
'''

USER_SRC = '''
from vfakectl import plain as aliased

def go(i):
    return aliased(i)
'''

FAMILIES = ["monitoring", "setprofile"] if HAS_MONITORING else ["setprofile"]


class RecSink:
    def __init__(self):
        self.events = []
        self.tallies = []
        self.metadata = {}
        self._lock = threading.Lock()

    def admit(self, force=False):
        return True

    def on_call(self, qualname, inputs, site=None):
        pass

    def on_return(self, qualname, inputs, result_obj, site=None, admitted=False, sym=None,
                  symq=None, own=None, dur=None, tid=None):
        with self._lock:
            self.events.append({"t": "return", "sym": sym, "in": inputs,
                                "result": normalize(result_obj), "dur": dur, "tid": tid})

    def on_raise(self, qualname, inputs, exc_typename, site=None, admitted=False, sym=None,
                 symq=None, own=None, dur=None, tid=None):
        with self._lock:
            self.events.append({"t": "raise", "sym": sym, "in": inputs, "raised": exc_typename,
                                "dur": dur, "tid": tid})

    def tally(self, qualname, sym, site, kind, raised=None, dur=None):
        self.tallies.append((sym, kind))

    def set_metadata(self, **values):
        self.metadata.update({k: v for k, v in values.items() if v is not None})

    def note_failure(self, kind="sink_failures", count=1):
        pass

    def close(self):
        pass

    def of(self, sym):
        return [e for e in self.events if e["sym"] == sym]


def _module(name, src):
    mod = types.ModuleType(name)
    sys.modules[name] = mod
    exec(compile(src, "<" + name + ">", "exec"), mod.__dict__)
    return mod


@pytest.fixture
def dep():
    mod = _module(MOD, SRC)
    user = _module(USER, USER_SRC)
    yield mod, user
    for name in (MOD, USER):
        sys.modules.pop(name, None)


def _controller(sink, family, **kw):
    kw.setdefault("quiet", 1000.0)
    kw.setdefault("max_seconds", 1000.0)
    kw.setdefault("incremental", False)     # these exercise discover -> watch
    return Controller(MOD, sink, prefer=family, source=(__name__, USER), **kw)


def _wait(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


# ── the switch ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("family", FAMILIES)
def test_auto_switches_after_a_quiet_window_and_records_it(dep, family):
    mod, _ = dep
    sink = RecSink()
    ctl = _controller(sink, family, mode="auto", quiet=0.05)
    ctl.start()
    try:
        mod.plain(1)
        mod.boom.__name__                          # no call: nothing new
        assert _wait(lambda: ctl.switched_at is not None)
        mod.plain(2)                               # observed: watched
        mod.other(3)                               # first called after the switch: missed
    finally:
        ctl.stop()
    assert [e["in"]["bound"]["a"] for e in sink.of(MOD + ".plain")] == [1, 2]
    assert sink.of(MOD + ".other") == []
    meta = sink.metadata
    assert meta["mode"] == "auto" and meta["switch_reason"] == "quiet"
    assert meta["discovered"] == 1 and meta["switched_at"] == ctl.switched_at
    assert meta["watch_backend"] == ("monitoring" if family == "monitoring" else "wrap")
    assert meta["backend"] == family


def test_quiet_clock_starts_at_the_first_symbol_and_max_always_applies(dep):
    mod, _ = dep
    ctl = _controller(RecSink(), FAMILIES[0], mode="auto", quiet=0.01, max_seconds=50.0)
    ctl.start()
    try:
        time.sleep(0.05)
        assert ctl.saturated() is None              # idle start: nothing observed yet
        mod.plain(1)
        assert ctl.saturated(time.monotonic() + 1.0) == "quiet"
        assert ctl.saturated(time.monotonic() + 100.0) == "max"
    finally:
        ctl.stop()


@pytest.mark.parametrize("family", FAMILIES)
def test_a_call_in_flight_across_the_switch_is_recorded_once(dep, family):
    mod, user = dep
    sink = RecSink()
    ctl = _controller(sink, family, mode="auto")
    ctl.start()
    try:
        mod.plain(0)
        user.go(10)                                 # through a module-global alias
        assert mod.calls_back(lambda: ctl.switch("manual")) == "done"
        mod.plain(1)
        user.go(11)                                 # the alias was bound before the switch
        mod.calls_back(lambda: None)
    finally:
        ctl.stop()
    assert ctl.switch_reason == "manual"
    assert [e["in"]["bound"]["a"] for e in sink.of(MOD + ".plain")] == [0, 10, 1, 11]
    assert [e["result"] for e in sink.of(MOD + ".calls_back")] == ["done", "done"]
    assert all(e["tid"] == threading.get_ident() for e in sink.events)


@pytest.mark.parametrize("family", FAMILIES)
def test_no_call_is_lost_or_doubled_while_another_thread_runs_through_the_switch(dep, family):
    mod, _ = dep
    sink = RecSink()
    ctl = _controller(sink, family, mode="auto")
    n = 3000
    halfway = threading.Event()

    def worker():
        for i in range(n):
            mod.plain(i)
            if i == n // 3:
                halfway.set()

    ctl.start()
    try:
        mod.plain(-1)
        t = threading.Thread(target=worker)
        t.start()
        assert halfway.wait(10)
        assert ctl.switch("manual")
        t.join(30)
    finally:
        ctl.stop()
    got = sorted(e["in"]["bound"]["a"] for e in sink.of(MOD + ".plain"))
    assert got == list(range(-1, n))


@pytest.mark.parametrize("family", FAMILIES)
def test_discover_mode_never_switches(dep, family):
    mod, _ = dep
    sink = RecSink()
    ctl = _controller(sink, family, mode="discover", quiet=0.0, max_seconds=0.0)
    ctl.start()
    try:
        mod.plain(1)
        time.sleep(0.05)
        assert ctl.switch("manual") is False
        mod.other(2)
    finally:
        ctl.stop()
    assert {e["sym"] for e in sink.events} == {MOD + ".plain", MOD + ".other"}
    assert sink.metadata["discovered"] == 2 and "switched_at" not in sink.metadata


@pytest.mark.parametrize("family", FAMILIES)
def test_watch_mode_observes_only_the_impact_targets(dep, family):
    mod, _ = dep
    sink = RecSink()
    ctl = _controller(sink, family, mode="watch", impact_targets=[MOD + ".other"])
    ctl.start()
    try:
        mod.plain(1)
        mod.other(2)
    finally:
        ctl.stop()
    assert [e["sym"] for e in sink.events] == [MOD + ".other"]
    assert ctl.mode == "watch" and ctl.discover is None


@pytest.mark.parametrize("family", FAMILIES)
def test_impact_targets_outside_the_target_are_observed_during_discover(dep, family):
    mod, _ = dep
    other = _module("vfakectl_other", "def h(x=0):\n    return x\n")
    try:
        sink = RecSink()
        ctl = _controller(sink, family, mode="auto", impact_targets=["vfakectl_other.h"])
        ctl.start()
        try:
            other.h(1)
            mod.plain(1)
            ctl.switch("manual")
            other.h(2)
        finally:
            ctl.stop()
    finally:
        sys.modules.pop("vfakectl_other", None)
    assert [e["in"]["bound"].get("x") for e in sink.of("vfakectl_other.h")] == [1, 2]


def test_watch_without_impact_targets_falls_back_to_discover_with_a_note(dep):
    mod, _ = dep
    sink = RecSink()
    ctl = _controller(sink, FAMILIES[0], mode="watch")
    ctl.start()
    try:
        mod.plain(1)
    finally:
        ctl.stop()
    assert ctl.mode == "discover" and len(sink.events) == 1
    assert "watch-without-impact-targets: discover" in sink.metadata["backend_notes"]


def test_monitoring_requested_below_312_falls_back_to_setprofile(dep, monkeypatch):
    mod, _ = dep
    monkeypatch.setattr(boundary_tracer, "HAS_MONITORING", False)
    sink = RecSink()
    ctl = _controller(sink, "monitoring", mode="discover")
    ctl.start()
    try:
        mod.plain(1)
    finally:
        ctl.stop()
    assert ctl.discover.name == "setprofile" and len(sink.events) == 1
    assert "monitoring-unavailable: setprofile" in sink.metadata["backend_notes"]


# ── sink selection ───────────────────────────────────────────────────────────


def test_facts_path_replaces_the_jsonl_suffix():
    assert boundary_tracer._facts_path("/t/trace-{pid}.jsonl") == "/t/trace-{pid}.facts.json"
    assert boundary_tracer._facts_path("/t/trace.json") == "/t/trace.json"


def test_facts_sink_is_constructed_when_the_runtime_has_it(tmp_path, monkeypatch):
    made = {}

    class FakeFactSink(RecSink):
        def __init__(self, path_template, metadata=None):
            RecSink.__init__(self)
            made["path"] = path_template
            made["metadata"] = metadata

    monkeypatch.setattr(_sinks, "FactSink", FakeFactSink, raising=False)
    sink, kind = boundary_tracer._make_sink("facts", str(tmp_path / "t-{pid}.jsonl"), {"a": 1})
    assert kind == "facts" and isinstance(sink, FakeFactSink)
    assert made["path"] == str(tmp_path / "t-{pid}.facts.json")
    assert made["metadata"]["a"] == 1


def test_facts_sink_unavailable_falls_back_to_jsonl(tmp_path, monkeypatch):
    monkeypatch.delattr(_sinks, "FactSink", raising=False)
    sink, kind = boundary_tracer._make_sink("facts", str(tmp_path / "t.jsonl"), {})
    try:
        assert kind == "jsonl" and isinstance(sink, _sinks.JsonlSink)
        assert sink._metadata["sink_note"] == "facts-sink-unavailable"
    finally:
        sink.close()


# ── Mode B end to end ────────────────────────────────────────────────────────


def test_activate_records_mode_switch_and_discovered_count(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    pkg = tmp_path / "vfakeact"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("def f(x):\n    return x\n\ndef g(x):\n    return x\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYMOLT_TRACE")}
    env.update(PYMOLT_TRACE_TARGET="vfakeact", PYTHONPATH=os.pathsep.join([str(repo),
                                                                          str(tmp_path)]),
               PYMOLT_TRACE_OUT=str(tmp_path / "t-{pid}.jsonl"), PYMOLT_TRACE_MODE="auto",
               PYMOLT_TRACE_DISCOVER_QUIET="0.05", PYMOLT_TRACE_SINK="jsonl",
               PYMOLT_TRACE_INCREMENTAL="0")
    code = ("from pymolt.verify import boundary_tracer\n"
            "boundary_tracer.activate()\n"
            "import time, vfakeact\n"
            "vfakeact.f(1)\n"
            "ctl = boundary_tracer._active[0]\n"
            "deadline = time.monotonic() + 10\n"
            "while ctl.switched_at is None and time.monotonic() < deadline:\n"
            "    time.sleep(0.01)\n"
            "vfakeact.f(2)\n"
            "vfakeact.g(3)\n")
    subprocess.run([sys.executable, "-c", code], env=env, check=True, timeout=60,
                   cwd=str(tmp_path))
    (meta_path,) = tmp_path.glob("*.meta.json")
    meta = json.loads(meta_path.read_text())
    assert meta["mode"] == "auto" and meta["switch_reason"] == "quiet"
    assert meta["discovered"] == 1 and meta["switched_at"] >= meta["started_at"]
    assert meta["trace_sink"] == "jsonl"
    (trace,) = [p for p in tmp_path.glob("t-*.jsonl")]
    events = [json.loads(line) for line in trace.read_text().splitlines()]
    assert [(e["sym"], e["result"]) for e in events] == [("vfakeact.f", 1), ("vfakeact.f", 2)]


# ── the runtime stays 3.6-parseable and ships whole ──────────────────────────

RUNTIME_FILES = ("_normalize.py", "_identity.py", "_backend.py", "_monitoring.py", "_sinks.py",
                 "_wrap.py", "boundary_tracer.py", "sitecustomize.py", "_install_hook.py")


@pytest.mark.skipif(sys.version_info < (3, 8), reason="ast feature_version is 3.8+")
@pytest.mark.parametrize("name", RUNTIME_FILES)
def test_runtime_file_parses_as_python_36(name):
    import ast

    path = Path(boundary_tracer.__file__).with_name(name)
    ast.parse(path.read_text(encoding="utf-8"), filename=str(path), feature_version=(3, 6))


@pytest.mark.skipif(not HAS_MONITORING, reason="host-only helper (3.12+)")
def test_exported_bundle_ships_the_monitoring_backend(tmp_path):
    from pymolt.verify.export_watcher import export_watcher

    root = export_watcher(tmp_path / "bundle")
    shipped = {p.name for p in (root / "pymolt_trace").glob("*.py")}
    assert {"_monitoring.py", "_backend.py", "_wrap.py", "boundary_tracer.py"} <= shipped


# ── novelty-first sampling: the admission key ────────────────────────────────


class KeySink(RecSink):
    """Admits the first call per key only (novelty-first with K=1 and a zero rate)."""

    def __init__(self):
        RecSink.__init__(self)
        self.admits = []
        self._seen = set()

    def admit(self, key=None, force=False):
        self.admits.append((key, force))
        novel = key not in self._seen
        self._seen.add(key)
        return novel or force


def _keyed_backend(kind, sink):
    from pymolt.verify._backend import SetProfileBackend
    from pymolt.verify._wrap import WrapBackend
    if kind == "wrap":
        return WrapBackend([MOD + ".plain", MOD + ".decorated", MOD + ".boom"], sink,
                           source=__name__)
    if kind == "monitoring":
        from pymolt.verify._monitoring import MonitoringBackend
        return MonitoringBackend(MOD, sink, source=__name__)
    return SetProfileBackend(MOD, sink, source=__name__)


@pytest.mark.parametrize("kind", FAMILIES + ["wrap"])
def test_admit_gets_the_symbol_and_call_site_as_key(dep, kind):
    mod, _ = dep
    sink = KeySink()
    lines = {}

    def body():
        line = sys._getframe().f_lineno
        lines["plain1"], lines["plain2"] = line + 3, line + 5
        lines["decorated"], lines["boom"] = line + 6, line + 8
        mod.plain(1)
        for _ in range(2):
            mod.plain(2)
        mod.decorated(3)
        with pytest.raises(KeyError):
            mod.boom()

    be = _keyed_backend(kind, sink)
    be.start()
    try:
        body()
    finally:
        be.stop()
    f = body.__code__.co_filename
    keys = [k for k, _force in sink.admits]
    assert keys == [
        (MOD + ".plain", f, lines["plain1"]),
        (MOD + ".plain", f, lines["plain2"]),
        (MOD + ".plain", f, lines["plain2"]),
        (MOD + ".decorated", f, lines["decorated"]),      # the unwrapped symbol, per call
        (MOD + ".boom", f, lines["boom"]),
    ]
    # raises are forced where the backend knows the outcome before admitting (wrap)
    assert [force for _k, force in sink.admits] == [False] * 4 + [kind == "wrap"]
    # the repeat at a known (symbol, site) was declined, and still counted
    assert [e["in"]["bound"]["a"] for e in sink.of(MOD + ".plain")] == [1, 2]
    assert sink.tallies == [(MOD + ".plain", "return")]


@pytest.mark.parametrize("kind", FAMILIES + ["wrap"])
def test_a_sink_without_key_keeps_todays_admit_call(dep, kind):
    mod, _ = dep
    calls = []

    class OldSink(RecSink):
        def admit(self, force=False):
            calls.append(force)
            return True

    be = _keyed_backend(kind, OldSink())
    be.start()
    try:
        mod.plain(1)
    finally:
        be.stop()
    assert calls == [False]


# ── incremental (3.12+ auto) ─────────────────────────────────────────────────


@pytest.mark.skipif(not HAS_MONITORING, reason="the CALL event is 3.12+")
def test_auto_is_incremental_on_312_and_records_it(dep):
    mod, _ = dep
    sink = RecSink()
    before = time.time()
    ctl = Controller(MOD, sink, mode="auto", prefer="auto", source=(__name__, USER))
    ctl.start()
    try:
        mod.plain(1)
        mod.other(2)                    # no discover window: a symbol seen late is still seen
        assert ctl.switch("manual") is False
    finally:
        ctl.stop()
    assert {e["sym"] for e in sink.events} == {MOD + ".plain", MOD + ".other"}
    meta = sink.metadata
    assert meta["mode"] == "incremental" and meta["requested_mode"] == "auto"
    assert before <= meta["switched_at"] <= time.time()
    assert meta["discovered"] == 2 and meta["armed"] == 2


@pytest.mark.skipif(not HAS_MONITORING, reason="the CALL event is 3.12+")
def test_incremental_falls_back_to_discover_then_watch_with_one_tool_id(dep):
    from pymolt.verify import _monitoring
    mon = sys.monitoring
    taken = []
    try:
        for tool in _monitoring.TOOL_IDS[1:]:
            if mon.get_tool(tool) is None:
                mon.use_tool_id(tool, "squatter")
                taken.append(tool)
        mod, _ = dep
        sink = RecSink()
        ctl = Controller(MOD, sink, mode="auto", source=(__name__, USER), quiet=1000.0)
        ctl.start()
        try:
            mod.plain(1)
        finally:
            ctl.stop()
    finally:
        for tool in taken:
            mon.free_tool_id(tool)
    assert ctl.mode == "auto" and ctl.discover.name == "monitoring"
    assert any(n.startswith("incremental-unavailable") for n in sink.metadata["backend_notes"])
    assert len(sink.events) == 1


def test_incremental_below_312_is_discover_then_watch_with_a_note(dep, monkeypatch):
    mod, _ = dep
    monkeypatch.setattr(boundary_tracer, "HAS_MONITORING", False)
    sink = RecSink()
    ctl = Controller(MOD, sink, mode="incremental", source=(__name__, USER), quiet=1000.0)
    ctl.start()
    try:
        mod.plain(1)
    finally:
        ctl.stop()
    assert ctl.mode == "auto" and ctl.discover.name == "setprofile"
    assert "incremental-unavailable: discover -> watch" in sink.metadata["backend_notes"]
    assert (ctl.quiet, ctl.max_seconds) == (1000.0, boundary_tracer.DEFAULT_MAX)
    assert (boundary_tracer.DEFAULT_QUIET, boundary_tracer.DEFAULT_MAX) == (5.0, 60.0)


@pytest.mark.skipif(not HAS_MONITORING, reason="the CALL event is 3.12+")
def test_activate_auto_is_incremental_end_to_end(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    pkg = tmp_path / "vfakeinc"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("def f(x):\n    return x\n\ndef g(x):\n    return x\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYMOLT_TRACE")}
    env.update(PYMOLT_TRACE_TARGET="vfakeinc", PYTHONPATH=os.pathsep.join([str(repo),
                                                                          str(tmp_path)]),
               PYMOLT_TRACE_OUT=str(tmp_path / "t-{pid}.jsonl"), PYMOLT_TRACE_MODE="auto",
               PYMOLT_TRACE_SINK="jsonl", PYMOLT_TRACE_INCREMENTAL_SETTLE="0.05")
    code = ("from pymolt.verify import boundary_tracer\n"
            "boundary_tracer.activate()\n"
            "import time, vfakeinc\n"
            "vfakeinc.f(1)\n"
            "vfakeinc.f(2)\n"
            "be = boundary_tracer._active[0].discover\n"
            "deadline = time.monotonic() + 10\n"
            "while be.settled_at is None and time.monotonic() < deadline:\n"
            "    time.sleep(0.01)\n"
            "vfakeinc.g(3)\n")
    subprocess.run([sys.executable, "-c", code], env=env, check=True, timeout=60,
                   cwd=str(tmp_path))
    (meta_path,) = tmp_path.glob("*.meta.json")
    meta = json.loads(meta_path.read_text())
    assert meta["mode"] == "incremental" and meta["discovered"] == 2
    assert meta["started_at"] <= meta["switched_at"] <= meta["ended_at"]
    assert meta["settle_reason"] == "quiet"
    assert meta["switched_at"] < meta["settled_at"] <= meta["ended_at"]
    (trace,) = list(tmp_path.glob("t-*.jsonl"))
    events = [json.loads(line) for line in trace.read_text().splitlines()]
    assert [(e["sym"], e["result"]) for e in events] == [
        ("vfakeinc.f", 1), ("vfakeinc.f", 2), ("vfakeinc.g", 3)]


@pytest.mark.skipif(not HAS_MONITORING, reason="the CALL event is 3.12+")
def test_activate_records_implicit_contacts_first_made_after_the_settle(tmp_path):
    """A long run: the settle frees the startup instrumentation, then a function of ours that
    only a dependency calls runs for the first time and touches a dunder and a property."""
    repo = Path(__file__).resolve().parents[2]
    pkg = tmp_path / "vfakelate"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(
        "class Box:\n"
        "    def __init__(self, v):\n        self.v = v\n"
        "    def __len__(self):\n        return self.v\n"
        "    @property\n    def size(self):\n        return self.v * 10\n"
        "def make(v):\n    return Box(v)\n"
        "def calls_back(fn, arg):\n    return fn(arg)\n")
    (tmp_path / "lateuser.py").write_text(
        "def rare(box):\n    return len(box), box.size\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYMOLT_TRACE")}
    env.update(PYMOLT_TRACE_TARGET="vfakelate", PYMOLT_TRACE_SOURCE="lateuser,__main__",
               PYTHONPATH=os.pathsep.join([str(repo), str(tmp_path)]),
               PYMOLT_TRACE_OUT=str(tmp_path / "t-{pid}.jsonl"), PYMOLT_TRACE_MODE="auto",
               PYMOLT_TRACE_SINK="jsonl", PYMOLT_TRACE_INCREMENTAL_SETTLE="0.05")
    code = ("from pymolt.verify import boundary_tracer\n"
            "boundary_tracer.activate()\n"
            "import time, vfakelate, lateuser\n"
            "box = vfakelate.make(3)\n"
            "be = boundary_tracer._active[0].discover\n"
            "deadline = time.monotonic() + 10\n"
            "while be.settled_at is None and time.monotonic() < deadline:\n"
            "    time.sleep(0.01)\n"
            "assert be.settled_at is not None\n"
            "assert vfakelate.calls_back(lateuser.rare, box) == (3, 30)\n")
    subprocess.run([sys.executable, "-c", code], env=env, check=True, timeout=60,
                   cwd=str(tmp_path))
    (trace,) = list(tmp_path.glob("t-*.jsonl"))
    events = [json.loads(line) for line in trace.read_text().splitlines()]
    assert sorted(e["sym"] for e in events) == [
        "vfakelate.Box.__len__", "vfakelate.Box.size", "vfakelate.calls_back", "vfakelate.make"]
    results = {e["sym"]: e["result"] for e in events}
    assert (results["vfakelate.Box.__len__"], results["vfakelate.Box.size"]) == (3, 30)
    (meta_path,) = tmp_path.glob("*.meta.json")
    meta = json.loads(meta_path.read_text())
    assert meta["settled_at"] is not None and meta["reopened"] >= 1


@pytest.mark.skipif(not HAS_MONITORING, reason="the CALL event is 3.12+")
def test_incremental_settles_after_a_quiet_window_and_records_it(dep):
    mod, _ = dep
    sink = RecSink()
    ctl = Controller(MOD, sink, mode="auto", source=(__name__, USER), settle=0.05)
    ctl.start()
    try:
        be = ctl.discover
        tool2 = be._tool2
        mod.plain(1)
        assert _wait(lambda: be.settled_at is not None)
        # kept: this new line of ours (the test is our code) has turned it on again
        assert sys.monitoring.get_tool(tool2) == "pymolt" and be.reopened == 1
        mod.plain(2)
        mod.other(3)                     # a plain call first made after the settle: found
    finally:
        ctl.stop()
    assert [e["sym"] for e in sink.events] == [MOD + ".plain"] * 2 + [MOD + ".other"]
    meta = sink.metadata
    assert meta["mode"] == "incremental" and meta["settle_reason"] == "quiet"
    assert meta["settled_at"] == be.settled_at and meta["incremental_settle"] == 0.05
    assert meta["incremental_reopen"] == boundary_tracer.DEFAULT_REOPEN == 5.0
    assert meta["reopened"] == 1 and meta["reopen_seconds"] > 0
    assert meta["discovered"] == 2


@pytest.mark.skipif(not HAS_MONITORING, reason="the CALL event is 3.12+")
def test_incremental_never_settles_with_zero_or_when_idle(dep):
    mod, _ = dep
    for settle, call in ((0.0, True), (0.01, False)):
        sink = RecSink()
        ctl = Controller(MOD, sink, mode="auto", source=(__name__, USER), settle=settle)
        ctl.start()
        try:
            if call:
                mod.plain(1)
            time.sleep(0.1)
            assert ctl.discover.settled_at is None and ctl.discover._tool2 is not None
            assert ctl.settled(time.monotonic() + 1000.0) is None
        finally:
            ctl.stop()
        assert "settled_at" not in sink.metadata
