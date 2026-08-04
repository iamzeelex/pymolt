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
