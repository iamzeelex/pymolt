"""Sinks: JsonlSink streaming, MemorySink aggregation + nondeterministic collapse."""
import json

from pymolt.verify._sinks import JsonlSink, MemorySink


def test_memory_sink_aggregates_identical_calls():
    s = MemorySink()
    s.on_return("flask.f", {"bound": {"x": 1}}, 1)
    s.on_return("flask.f", {"bound": {"x": 1}}, 1)
    (rec,) = s.records.values()
    assert rec["count"] == 2
    assert rec["result"] == 1
    assert rec["raised"] is None


def test_memory_sink_collapses_nondeterministic_result():
    s = MemorySink()
    s.on_return("flask.f", {"bound": {}}, 1)
    s.on_return("flask.f", {"bound": {}}, 2)  # same inputs, different result
    (rec,) = s.records.values()
    assert rec["result"] == {"__nondeterministic__": True}


def test_memory_sink_records_raise():
    s = MemorySink()
    s.on_raise("flask.g", {"bound": {}}, "ValueError")
    (rec,) = s.records.values()
    assert rec["raised"] == "ValueError"
    assert rec["result"] is None


def test_memory_sink_dump_shape(tmp_path):
    s = MemorySink()
    s.on_return("flask.f", {"bound": {"x": 1}}, 1)
    p = tmp_path / "out.json"
    s.dump(str(p), target="flask")
    data = json.loads(p.read_text())
    assert data["target"] == "flask"
    assert data["records"][0]["qualname"] == "flask.f"


def test_jsonl_sink_streams_one_event_per_line(tmp_path):
    p = tmp_path / "out-{pid}.jsonl"
    sink = JsonlSink(str(p))
    sink.on_return("flask.f", {"bound": {"x": 1}}, 1)
    sink.on_raise("flask.g", {"bound": {}}, "ValueError")
    sink.close()
    lines = [json.loads(line) for line in open(sink.path) if line.strip()]
    assert {e["t"] for e in lines} == {"return", "raise"}
    assert lines[0]["q"] == "flask.f"


def test_jsonl_sink_expands_pid_template(tmp_path):
    sink = JsonlSink(str(tmp_path / "trace-{pid}.jsonl"))
    sink.close()
    assert "{pid}" not in sink.path
    assert sink.path.endswith(".jsonl")


def test_jsonl_sink_sampling_can_drop_events(tmp_path, monkeypatch):
    monkeypatch.setenv("PYMOLT_TRACE_SAMPLE_RATE", "0.25")
    monkeypatch.setattr("pymolt.verify._sinks.random.random", lambda: 0.9)
    sink = JsonlSink(str(tmp_path / "trace-{pid}.jsonl"))

    sink.on_return("flask.f", {"bound": {}}, 1)
    sink.close()

    assert open(sink.path).read() == ""


def test_jsonl_events_are_v2_and_carry_identity(tmp_path):
    sink = JsonlSink(str(tmp_path / "id-{pid}.jsonl"))
    sink.on_return("m.__init__", {"bound": {}}, None, sym="m.A.__init__")
    sink.on_return("m.wrapper", {"bound": {}}, 1, sym="m.f", symq="unwrapped")
    sink.on_raise("m.__init__", {"bound": {}}, "ValueError", sym="m.Base.__init__", own="m.Child")
    sink.close()
    events = [json.loads(line) for line in open(sink.path) if line.strip()]
    assert all(e["v"] == 2 for e in events)
    assert [(e["sym"], e.get("symq"), e.get("own")) for e in events] == [
        ("m.A.__init__", None, None), ("m.f", "unwrapped", None),
        ("m.Base.__init__", None, "m.Child")]


def test_memory_sink_keeps_two_inits_of_one_module_apart():
    s = MemorySink()
    s.on_return("m.__init__", {"bound": {}}, None, sym="m.A.__init__")
    s.on_return("m.__init__", {"bound": {}}, None, sym="m.B.__init__")
    s.on_return("m.__init__", {"bound": {}}, None, sym="m.A.__init__", own="m.Sub")
    assert sorted((r["sym"], r.get("own", "")) for r in s.records.values()) == [
        ("m.A.__init__", ""), ("m.A.__init__", "m.Sub"), ("m.B.__init__", "")]
