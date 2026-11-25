"""A capture that ran in a container records its RunnerSpec on the slot and hands it to
the call-site text recorder (so ``/work/...`` sites resolve to host files)."""

from __future__ import annotations

from pymolt.adapters.python_runner import Mount, RunnerSpec
from pymolt.verify import service
from pymolt.verify.models import CaptureMode
from pymolt.verify.service import TraceCaptureResult

SPEC = RunnerSpec(name="bench", container_id="abc",
                  mounts=[Mount(source="/h", destination="/work")])


def _fake_capture(target, command, out_path, backend, **kw):
    from pathlib import Path

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text("", encoding="utf-8")
    return TraceCaptureResult(out_path=str(out_path), events=0, processes=1, returncode=0,
                              where=f"container:{kw.get('container')}")


def test_container_capture_records_runner_and_passes_it_to_site_text(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(service, "capture_trace", _fake_capture)
    monkeypatch.setattr(service, "_container_runner_spec", lambda c: SPEC if c else None)
    monkeypatch.setattr(service, "record_site_text",
                        lambda trace, root, runner=None: seen.update(runner=runner) or 0)
    slot = service.capture_named_trace(
        tmp_path, "baseline", CaptureMode.LIVE_COMMAND, command=["python", "x.py"],
        target="json", container="bench", accept_failing=True)
    assert slot.runner == SPEC
    assert seen["runner"] == SPEC


def test_local_capture_has_no_runner(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(service, "capture_trace", _fake_capture)
    monkeypatch.setattr(service, "record_site_text",
                        lambda trace, root, runner=None: seen.update(runner=runner) or 0)
    slot = service.capture_named_trace(
        tmp_path, "baseline", CaptureMode.LIVE_COMMAND, command=["python", "x.py"],
        target="json", accept_failing=True)
    assert slot.runner is None and seen["runner"] is None
