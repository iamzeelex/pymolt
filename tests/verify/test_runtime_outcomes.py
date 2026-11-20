"""Injected-runtime behaviour: raise visibility and the cost of the hot path.

Python-level raises used to be recorded as ``return None`` (setprofile has no exception
event), and unsampled calls used to pay for a full ``normalize`` of their arguments.
These pin the fixes.
"""
import json
import sys
import types

import pytest

import pymolt.verify._normalize as normalize_mod
from pymolt.verify._backend import (
    OUTCOME_AMBIGUOUS,
    OUTCOME_RAISE,
    OUTCOME_RETURN,
    UNTYPED_RAISE,
    SetProfileBackend,
    frame_outcome,
)
from pymolt.verify._normalize import AMBIGUOUS_OUTCOME, normalize
from pymolt.verify._sinks import JsonlSink, MemorySink
from pymolt.verify._wrap import WrapBackend

PREFIX = "vfakeoutcome"

TARGET_SRC = """
def ok(x):
    return x + 1

def returns_none():
    return None

def boom(x):
    raise ValueError("kaboom %s" % x)

def caught_then_none():
    try:
        raise KeyError("inner")
    except KeyError:
        pass
    return None

def finally_return():
    try:
        return None
    finally:
        pass

def gen(n):
    for i in range(n):
        yield i
"""


@pytest.fixture
def target():
    mod = types.ModuleType(PREFIX)
    exec(compile(TARGET_SRC, f"<{PREFIX}>", "exec"), mod.__dict__)
    sys.modules[PREFIX] = mod
    yield mod
    sys.modules.pop(PREFIX, None)


def _trace(target_mod, body, sink=None):
    sink = sink if sink is not None else MemorySink()
    be = SetProfileBackend(PREFIX, sink)
    be.start()
    try:
        body(target_mod)
    finally:
        be.stop()
    return sink, be


def _by_name(sink):
    return {r["qualname"].rsplit(".", 1)[1]: r for r in sink.records.values()}


def _swallow(fn, *args):
    try:
        fn(*args)
    except Exception:
        pass


# ── raise visibility ─────────────────────────────────────────────────────────


def test_python_level_raise_is_recorded_as_raise_not_return_none(target):
    sink, _ = _trace(target, lambda m: _swallow(m.boom, 1))
    rec = _by_name(sink)["boom"]
    # A raise, never a None return. Its type is read when the one-shot trace event reaches the
    # caller (it does on 3.6 and 3.7 here); otherwise the raise is recorded untyped.
    assert rec["raised"] in (UNTYPED_RAISE, "ValueError")
    assert rec["result"] is None


def test_genuine_none_returns_stay_returns(target):
    def body(m):
        m.returns_none()
        m.caught_then_none()  # an exception was raised AND handled inside: still a return
        m.ok(1)

    recs = _by_name(_trace(target, body)[0])
    assert recs["returns_none"]["raised"] is None
    assert recs["caught_then_none"]["raised"] is None
    assert recs["ok"]["result"] == 2


def test_return_none_through_finally_is_not_a_raise(target):
    rec = _by_name(_trace(target, lambda m: m.finally_return())[0])["finally_return"]
    if sys.version_info < (3, 8):
        # The documented 3.6/3.7 limit (docs/limits.md): the bytecode cannot tell this return
        # from a raise, so it is reported as ambiguous, never as a plain None return.
        assert rec["raised"] is None and rec["result"] == normalize(AMBIGUOUS_OUTCOME)
    else:
        assert rec["raised"] is None and rec["result"] is None


def test_generator_suspension_is_not_a_raise(target):
    sink, _ = _trace(target, lambda m: list(m.gen(3)))
    rec = _by_name(sink)["gen"]
    assert rec["raised"] is None


def test_frame_outcome_classifies_return_raise_and_value():
    seen = {}

    def profile(frame, event, arg):
        if event == "return" and frame.f_code.co_name in ("ret", "rai"):
            seen[frame.f_code.co_name] = frame_outcome(frame, arg)

    def ret():
        return None

    def rai():
        raise RuntimeError

    sys.setprofile(profile)
    try:
        ret()
        _swallow(rai)
    finally:
        sys.setprofile(None)
    assert seen == {"ret": OUTCOME_RETURN, "rai": OUTCOME_RAISE}
    assert frame_outcome(None, 5) == OUTCOME_RETURN  # a non-None value is always a return
    assert OUTCOME_AMBIGUOUS not in seen.values()


def test_ambiguous_marker_normalizes_to_an_undecidable_opaque():
    assert normalize(AMBIGUOUS_OUTCOME) == {"__opaque__": "ambiguous:return-or-raise"}
    assert normalize(AMBIGUOUS_OUTCOME, _privacy="shape") == {
        "__opaque__": "ambiguous:return-or-raise"
    }


def test_jsonl_trace_carries_the_raise_event(target, tmp_path):
    out = tmp_path / "t.jsonl"
    sink = JsonlSink(str(out))
    _trace(target, lambda m: (_swallow(m.boom, 2), m.ok(2)), sink=sink)
    sink.close()
    events = [json.loads(line) for line in out.read_text().splitlines()]
    kinds = {e["q"].rsplit(".", 1)[1]: e["t"] for e in events}
    assert kinds == {"boom": "raise", "ok": "return"}
    assert sink.metrics["events_seen"] == 2 and sink.metrics["events_written"] == 2


# ── sampling before normalization ────────────────────────────────────────────


def _count_normalize(monkeypatch):
    calls = []
    real = normalize_mod.normalize

    def counting(value, *a, **kw):
        calls.append(value)
        return real(value, *a, **kw)

    monkeypatch.setattr(normalize_mod, "normalize", counting)
    return calls


def test_unsampled_calls_skip_input_normalization(target, tmp_path, monkeypatch):
    monkeypatch.setenv("PYMOLT_TRACE_SAMPLE_RATE", "0")
    monkeypatch.setenv("PYMOLT_TRACE_SAMPLE_FIRST", "0")   # the rate alone, no novelty-first
    calls = _count_normalize(monkeypatch)
    sink = JsonlSink(str(tmp_path / "t.jsonl"))
    _trace(target, lambda m: [m.ok(i) for i in range(20)], sink=sink)
    sink.close()
    m = sink.metrics
    assert m["events_seen"] == 20 and m["sampling_dropped"] == 20
    assert m["events_written"] == 0
    assert calls == []  # no argument was normalized for a call that was never going to be kept


def test_sampled_calls_are_counted_once(target, tmp_path):
    sink = JsonlSink(str(tmp_path / "t.jsonl"))
    _trace(target, lambda m: [m.ok(i) for i in range(7)], sink=sink)
    sink.close()
    m = sink.metrics
    assert m["events_seen"] == 7 and m["events_written"] == 7 and m["sampling_dropped"] == 0


def test_wrap_backend_samples_before_normalizing_but_keeps_raises(target, tmp_path, monkeypatch):
    monkeypatch.setenv("PYMOLT_TRACE_SAMPLE_RATE", "0")
    monkeypatch.setenv("PYMOLT_TRACE_SAMPLE_FIRST", "0")
    out = tmp_path / "w.jsonl"
    sink = JsonlSink(str(out))
    be = WrapBackend([PREFIX + ".ok", PREFIX + ".boom"], sink, include_internal=True)
    be.start()
    try:
        for i in range(10):
            target.ok(i)
        _swallow(target.boom, 1)
    finally:
        be.stop()
    sink.close()
    events = [json.loads(line) for line in out.read_text().splitlines()]
    assert [(e["t"], e["raised"]) for e in events] == [("raise", "ValueError")]
    assert sink.metrics["events_seen"] == 11 and sink.metrics["sampling_dropped"] == 10


def test_novelty_first_admits_the_first_calls_of_each_symbol_and_site(target, tmp_path,
                                                                       monkeypatch):
    """The backend's admission key is (symbol, call site): each new one is recorded first."""
    monkeypatch.setenv("PYMOLT_TRACE_SAMPLE_RATE", "0")
    monkeypatch.setenv("PYMOLT_TRACE_SAMPLE_FIRST", "2")
    out = tmp_path / "n.jsonl"
    sink = JsonlSink(str(out))

    def body(m):
        for i in range(10):
            m.ok(i)                  # one site: the first two are kept
        m.ok(100)                    # a new site: kept

    _trace(target, body, sink=sink)
    sink.close()
    kept = [json.loads(line)["in"]["bound"]["x"] for line in out.read_text().splitlines()]
    assert kept == [0, 1, 100]
    assert sink.metrics["events_seen"] == 11


def test_memory_sink_admits_everything():
    assert MemorySink().admit() is True


def test_bytes_normalize_without_bytes_hex():
    assert normalize(b"\x00\xffA") == {"__bytes__": "00ff41"}


def test_default_queue_is_deep_enough_for_cpu_bound_runs(tmp_path, monkeypatch):
    monkeypatch.delenv("PYMOLT_TRACE_QUEUE_SIZE", raising=False)
    sink = JsonlSink(str(tmp_path / "q.jsonl"))
    sink.close()
    with open(sink.metadata_path) as fh:
        assert json.load(fh)["backpressure"]["queue_size"] == 65536


# ── runtime metadata ─────────────────────────────────────────────────────────


def test_activate_records_the_capturing_interpreter(tmp_path):
    """The host resolves static symbols in the interpreter that ran the capture."""
    import os
    import subprocess
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYMOLT_TRACE")}
    env.update(PYMOLT_TRACE_TARGET="json", PYTHONPATH=str(repo),
               PYMOLT_TRACE_OUT=str(tmp_path / "t-{pid}.jsonl"))
    code = ("from pymolt.verify import boundary_tracer\n"
            "boundary_tracer.activate()\n"
            "import json\n"
            "json.dumps(1)\n")
    subprocess.run([sys.executable, "-c", code], env=env, check=True, timeout=60)
    (meta,) = tmp_path.glob("*.meta.json")
    payload = json.loads(meta.read_text())
    assert payload["python"] == ".".join(str(n) for n in sys.version_info[:3])
    assert os.path.realpath(payload["executable"]) == os.path.realpath(sys.executable)
