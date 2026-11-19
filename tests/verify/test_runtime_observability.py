"""P2 runtime targeting, propagation, and loss observability."""

from __future__ import annotations

import json
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
    # A long-run capture with no explicit rate samples values (every call is still
    # counted): the effective policy is recorded, and its source says it was the default.
    assert metadata["sample_rate"] == 0.1
    assert metadata["sampling"]["source"] == "default"
    assert metadata["counters"]["dropped_events"] == 0
    assert service.load_contract_state(tmp_path).baseline == slot


def test_sidecar_counts_sampling_drops(tmp_path, monkeypatch):
    monkeypatch.setenv("PYMOLT_TRACE_SAMPLE_RATE", "0.25")
    monkeypatch.setattr("pymolt.verify._sinks.random.random", lambda: 0.9)
    sampled = JsonlSink(str(tmp_path / "sampled-{pid}.jsonl"))
    sampled.on_return("dep.sampled", {"bound": {}}, 1)
    sampled.close()

    sampled_metadata = json.loads(service.Path(sampled.metadata_path).read_text())
    assert sampled_metadata["counters"]["events_seen"] == 1
    assert sampled_metadata["counters"]["sampling_dropped"] == 1
    assert sampled_metadata["counters"]["dropped_events"] == 1


def test_a_capture_through_a_one_slot_queue_keeps_every_event_and_states_the_wait(
        tmp_path, monkeypatch):
    """The locust drop path end to end: a JSONL capture whose writer cannot keep up (a
    one-slot queue) used to lose events to ``backpressure_dropped``; it now keeps all of
    them, and the result and sidecar say how long the program waited for the writer."""
    monkeypatch.setenv("PYMOLT_TRACE_QUEUE_SIZE", "1")
    monkeypatch.delenv("PYMOLT_TRACE_BACKPRESSURE", raising=False)
    script = tmp_path / "burst.py"
    script.write_text("import json\nfor i in range(3000):\n    json.loads('[%d]' % i)\n",
                      encoding="utf-8")
    out = tmp_path / "trace.jsonl"

    result = service.capture_trace("json", [sys.executable, str(script)], out, "auto")

    assert result.dropped_events == 0 and result.backpressure_dropped == 0
    records = [json.loads(line) for line in out.read_text().splitlines() if line.strip()]
    assert sorted(r["in"]["bound"]["s"] for r in records if r["q"] == "json.loads") == \
        sorted(f"[{i}]" for i in range(3000))
    metadata = json.loads(service.Path(result.metadata_path).read_text())
    assert metadata["backpressure"]["waits"] >= 1
    assert metadata["backpressure"]["writer_stalls"] == 0
    assert result.backpressure_wait_seconds == metadata["backpressure"]["wait_ms"] / 1000.0
    (process,) = metadata["processes"]
    assert process["backpressure"]["policy"] == "block"


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


def test_a_capture_counts_compiled_calls_past_the_site_cap_and_lists_the_site(tmp_path):
    """The locust ``msgpack.unpackb`` path end to end: a compiled call made 2,500 times from
    one line records its first 2,000 and counts the rest; the sidecar lists the capped site
    and the host reads it back onto the FactSet (no silent evidence drop)."""
    from pymolt.verify.facts import load_facts

    script = tmp_path / "hot.py"
    script.write_text(
        "import collections\n"
        "d = collections.deque(range(5))\n"
        "def spin():\n"
        "    for _ in range(2500):\n"
        "        d.rotate(1)\n"
        "spin()\n",
        encoding="utf-8",
    )
    out = tmp_path / "trace.jsonl"

    result = service.capture_trace("collections", [sys.executable, str(script)], out, "auto")

    records = [json.loads(line) for line in out.read_text().splitlines() if line.strip()]
    assert sum(1 for r in records if r["q"] == "collections.deque.rotate") == 2000
    metadata = json.loads(service.Path(result.metadata_path).read_text())
    (process,) = metadata["processes"]
    cap = process["c_site_cap"]
    assert cap["max"] == 2000 and cap["muted"] == 500 and cap["exact"] is True
    (site,) = cap["sites"]
    assert site["sym"] == "collections.deque.rotate" and site["func"] == "spin"
    assert site["line"] == 5 and site["muted"] == 500

    fs = load_facts(out, root=tmp_path)
    assert fs.c_cap_reported == 1 and fs.c_cap_unreported == 0 and fs.c_muted == 500
    (capped,) = fs.c_capped
    assert (capped.symbol, capped.file, capped.func, capped.line) == (
        "collections.deque.rotate", "hot.py", "spin", 5)
    (fact,) = [f for f in fs.facts if f.symbol == "collections.deque.rotate"]
    assert capped.text == fact.text and capped.recorded == 2000 and fact.count == 2000


def test_a_capture_with_no_capped_site_still_states_its_account(tmp_path):
    """An account is always written, so its absence means an older runtime (unknown)."""
    script = tmp_path / "cool.py"
    script.write_text("import collections\ncollections.deque(range(3)).rotate(1)\n",
                      encoding="utf-8")
    out = tmp_path / "trace.jsonl"
    result = service.capture_trace("collections", [sys.executable, str(script)], out, "auto")
    (process,) = json.loads(service.Path(result.metadata_path).read_text())["processes"]
    assert process["c_site_cap"] == {"max": 2000, "muted": 0, "exact": True, "sites": [],
                                     "sites_omitted": 0, "unsited": 0}
