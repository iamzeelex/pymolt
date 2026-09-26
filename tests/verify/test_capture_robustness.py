"""Failure-safety checks for named contract capture."""

from __future__ import annotations

import sys

import pytest

from pymolt.verify import service
from pymolt.verify.models import CaptureMode


def _capture(project_dir):
    script = project_dir / "capture.py"
    script.write_text("import json\njson.loads('{}')\n", encoding="utf-8")
    return service.capture_named_trace(
        project_dir,
        "baseline",
        CaptureMode.LIVE_COMMAND,
        command=[sys.executable, str(script)],
        target="json",
    )


def test_failed_recapture_keeps_the_active_trace_and_state(tmp_path):
    """A process-start failure must not archive the only usable baseline."""
    first = _capture(tmp_path)
    trace = tmp_path / ".pymolt" / "contract_traces" / "baseline.jsonl"
    original = trace.read_bytes()

    with pytest.raises(FileNotFoundError):
        service.capture_named_trace(
            tmp_path,
            "baseline",
            CaptureMode.LIVE_COMMAND,
            command=["definitely-not-a-pymolt-command"],
            target="json",
        )

    assert trace.read_bytes() == original
    state = service.load_contract_state(tmp_path)
    assert state.baseline is not None
    assert state.baseline.trace_path == first.trace_path
    assert not (tmp_path / ".pymolt" / "contract_traces" / "archive").exists()


def test_failed_recapture_removes_partial_staging_artifacts(tmp_path, monkeypatch):
    first = _capture(tmp_path)
    trace_dir = tmp_path / ".pymolt" / "contract_traces"
    original = (trace_dir / "baseline.jsonl").read_bytes()

    def fail_after_writing(*args, **kwargs):
        staged = args[2]
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_text("partial\n", encoding="utf-8")
        (staged.parent / f"{staged.stem}.command.log").write_text("partial log\n", encoding="utf-8")
        raise RuntimeError("capture failed after producing partial output")

    monkeypatch.setattr(service, "capture_trace", fail_after_writing)

    with pytest.raises(RuntimeError, match="partial output"):
        service.capture_named_trace(
            tmp_path,
            "baseline",
            CaptureMode.LIVE_COMMAND,
            command=[sys.executable, "-c", "pass"],
            target="json",
        )

    assert (trace_dir / "baseline.jsonl").read_bytes() == original
    assert service.load_contract_state(tmp_path).baseline == first
    assert list(trace_dir.glob("*.pending*")) == []
    assert not (trace_dir / "archive").exists()
