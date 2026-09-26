"""P2 runtime targeting, propagation, and loss observability."""

from __future__ import annotations

import json
import queue
import sys

from pymolt.verify import service
from pymolt.verify._sinks import JsonlSink, MemorySink
from pymolt.verify._wrap import TargetedBackend
from pymolt.verify.models import CaptureMode, ContractState


def test_exact_impact_set_wraps_only_named_symbols_without_profiling(monkeypatch):
    import types

    dependency = types.ModuleType("impactdep")
    exec(
        compile("def affected(): return 1\ndef unaffected(): return 2\n", "<impactdep>", "exec"),
        dependency.__dict__,
    )
    monkeypatch.setitem(sys.modules, "impactdep", dependency)
    sink = MemorySink()
    backend = TargetedBackend(["impactdep.affected"], sink, source=__name__)

    assert backend.profile is None
    assert backend.uses_profiling is False
    backend.start()
    try:
        dependency.affected()
        dependency.unaffected()
    finally:
        backend.stop()

    assert {record["qualname"] for record in sink.records.values()} == {
        "impactdep.affected"
    }


def test_wildcard_impact_prefix_profiles_only_that_prefix(monkeypatch):
    import types

    affected = types.ModuleType("impactprefix")
    other = types.ModuleType("outsideprefix")
    exec(compile("def call(): return 1\n", "<impactprefix>", "exec"), affected.__dict__)
    exec(compile("def call(): return 2\n", "<outsideprefix>", "exec"), other.__dict__)
    monkeypatch.setitem(sys.modules, "impactprefix", affected)
    monkeypatch.setitem(sys.modules, "outsideprefix", other)
    sink = MemorySink()
    backend = TargetedBackend(["impactprefix.*"], sink, source=__name__)

    assert backend.profile is not None
    assert backend.uses_profiling is True
    backend.start()
    try:
        affected.call()
        other.call()
    finally:
        backend.stop()

    assert {record["qualname"] for record in sink.records.values()} == {
        "impactprefix.call"
    }


def test_runtime_metadata_propagates_to_result_slot_and_sidecar(tmp_path):
    script = tmp_path / "capture.py"
    script.write_text(
        "import json, sys\n"
        "assert sys.getprofile() is None\n"
        "json.loads('{}')\n"
        "json.dumps({'not': 'captured'})\n",
        encoding="utf-8",
    )

    slot = service.capture_named_trace(
        tmp_path,
        "baseline",
        CaptureMode.LIVE_COMMAND,
        command=[sys.executable, str(script)],
        target="all",
        impact_targets=["json.loads"],
        deployment_id="release-42",
        request_id="request-7",
        correlation_id="correlation-9",
    )

    assert slot.backend == "targeted"
    assert slot.impact_targets == ["json.loads"]
    assert slot.deployment_id == "release-42"
    assert slot.request_id == "request-7"
    assert slot.correlation_id == "correlation-9"
    assert slot.duration_seconds is not None and slot.duration_seconds > 0
    assert slot.events_seen == slot.events
    assert slot.dropped_events == 0
    assert slot.metadata_path is not None
    records = [json.loads(line) for line in service.Path(slot.trace_path).read_text().splitlines()]
    assert {record["q"] for record in records} == {"json.loads"}

    metadata = json.loads(service.Path(slot.metadata_path).read_text())
    assert metadata["deployment_id"] == "release-42"
    assert metadata["request_id"] == "request-7"
    assert metadata["correlation_id"] == "correlation-9"
    assert metadata["sample_rate"] == 1.0
    assert metadata["counters"]["dropped_events"] == 0
    assert service.load_contract_state(tmp_path).baseline == slot


def test_sink_counts_sampling_and_backpressure_drops(tmp_path, monkeypatch):
    monkeypatch.setenv("PYMOLT_TRACE_SAMPLE_RATE", "0.25")
    monkeypatch.setattr("pymolt.verify._sinks.random.random", lambda: 0.9)
    sampled = JsonlSink(str(tmp_path / "sampled-{pid}.jsonl"))
    sampled.on_return("dep.sampled", {"bound": {}}, 1)
    sampled.close()

    sampled_metadata = json.loads(service.Path(sampled.metadata_path).read_text())
    assert sampled_metadata["counters"]["events_seen"] == 1
    assert sampled_metadata["counters"]["sampling_dropped"] == 1
    assert sampled_metadata["counters"]["dropped_events"] == 1

    monkeypatch.setenv("PYMOLT_TRACE_SAMPLE_RATE", "1")
    pressured = JsonlSink(str(tmp_path / "pressured-{pid}.jsonl"))

    def full(_event):
        raise queue.Full

    monkeypatch.setattr(pressured._queue, "put_nowait", full)
    pressured.on_return("dep.pressured", {"bound": {}}, 1)
    pressured.close()

    pressured_metadata = json.loads(service.Path(pressured.metadata_path).read_text())
    assert pressured_metadata["counters"]["events_seen"] == 1
    assert pressured_metadata["counters"]["backpressure_dropped"] == 1
    assert pressured_metadata["counters"]["dropped_events"] == 1


def test_sink_counts_observable_write_failures(tmp_path, monkeypatch):
    sink = JsonlSink(str(tmp_path / "failed-{pid}.jsonl"))

    def fail_write(_fh, _event):
        raise OSError("disk unavailable")

    monkeypatch.setattr(sink, "_write_line", fail_write)
    sink.on_return("dep.failed", {"bound": {}}, 1)
    sink.close()

    metadata = json.loads(service.Path(sink.metadata_path).read_text())
    assert metadata["counters"]["write_failures"] == 1
    assert metadata["counters"]["dropped_events"] == 1


def test_trace_env_uses_json_target_set_and_inherited_identity(monkeypatch):
    monkeypatch.setenv("PYMOLT_TRACE_DEPLOYMENT_ID", "stage-blue")
    monkeypatch.setenv("PYMOLT_TRACE_CORRELATION_ID", "corr-env")

    env = service.trace_env(
        "all", "auto", "", "", False,
        impact_targets=["dep.api.call", "dep.api.*"],
        request_id="request-explicit",
    )

    assert json.loads(env["PYMOLT_TRACE_IMPACT_TARGETS"]) == [
        "dep.api.call", "dep.api.*"
    ]
    assert env["PYMOLT_TRACE_DEPLOYMENT_ID"] == "stage-blue"
    assert env["PYMOLT_TRACE_REQUEST_ID"] == "request-explicit"
    assert env["PYMOLT_TRACE_CORRELATION_ID"] == "corr-env"


def test_old_contract_state_loads_with_unknown_runtime_metrics(tmp_path):
    path = tmp_path / "contract_state.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "baseline": {
            "trace_path": "/tmp/old.jsonl",
            "captured_at": "2025-01-01T00:00:00+00:00",
            "mode": "test_suite",
            "events": 3,
            "processes": 1,
        },
        "post_migration": None,
    }), encoding="utf-8")

    state = ContractState.load(path)

    assert state is not None and state.baseline is not None
    assert state.baseline.events == 3
    assert state.baseline.duration_seconds is None
    assert state.baseline.dropped_events is None
    assert state.baseline.metadata_path is None
    assert state.baseline.impact_targets == []


def test_attached_capture_without_runtime_sidecar_keeps_loss_unknown(tmp_path):
    pending = tmp_path / "attached.jsonl"
    (tmp_path / ".pymolt" / "contract_traces").mkdir(parents=True)
    pending.write_text('{"q": "requests.get"}\n', encoding="utf-8")

    slot = service.finalize_attached_capture(
        tmp_path,
        "baseline",
        pending,
        target="requests",
    )

    assert slot.events == 1
    assert slot.events_seen is None
    assert slot.dropped_events is None
    assert slot.sampling_dropped is None
    assert slot.backpressure_dropped is None
    assert slot.write_failures is None
    assert slot.sink_failures is None
