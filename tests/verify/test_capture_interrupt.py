"""Ctrl+C during a capture keeps the capture; temp work dirs are cleaned up."""
import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from pymolt.verify import service

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")

_CHILD = textwrap.dedent("""
    import json, sys, time
    json.loads('{}')
    print('child-ready', flush=True)
    time.sleep(%s)
""")

_RUNNER = textwrap.dedent("""
    import sys
    from pathlib import Path
    from pymolt.verify import service
    project = Path(sys.argv[1])
    from pymolt.verify.models import CaptureMode
    slot = service.capture_named_trace(
        project, "baseline", CaptureMode.LIVE_COMMAND,
        command=[sys.executable, str(project / "child.py")], target="json", backend="auto",
    )
    print("SLOT", slot.validity.value, slot.events, flush=True)
""")


def _start(tmp_path, sleep, tmpdir):
    (tmp_path / "child.py").write_text(_CHILD % sleep)
    env = dict(os.environ, TMPDIR=str(tmpdir))
    return subprocess.Popen(
        [sys.executable, "-c", _RUNNER, str(tmp_path)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True,
    )


def _wait_for_child(tmp_path, proc):
    log = None
    deadline = time.time() + 20
    while time.time() < deadline:
        logs = list((tmp_path / ".pymolt" / "contract_traces").glob("*.command.log"))
        if logs and "child-ready" in logs[0].read_text():
            log = logs[0]
            break
        time.sleep(0.1)
    assert log is not None, "child never started"
    time.sleep(0.3)


@posix_only
def test_sigint_to_process_group_still_records_slot_and_cleans_tmp(tmp_path):
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    proc = _start(tmp_path, 60, tmpdir)
    _wait_for_child(tmp_path, proc)
    os.killpg(proc.pid, signal.SIGINT)  # what a terminal Ctrl+C does
    out, err = proc.communicate(timeout=30)

    assert proc.returncode == 0, err
    assert "SLOT" in out
    state = json.loads((tmp_path / ".pymolt" / "contract_state.json").read_text())
    assert state["baseline"] is not None
    assert not list(tmpdir.glob("pymolt-trace-*"))


@posix_only
def test_sigint_ignoring_child_is_terminated_after_second_sigint(tmp_path, monkeypatch):
    child = tmp_path / "stubborn.py"
    child.write_text(
        "import json, signal, time\n"
        "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
        "json.loads('{}')\n"
        "time.sleep(60)\n"
    )
    out = tmp_path / "t.jsonl"
    runner = textwrap.dedent("""
        import sys
        from pymolt.verify import service
        r = service.capture_trace("json", [sys.executable, sys.argv[1]], sys.argv[2], "auto")
        print("RC", r.returncode, r.cancelled, r.events, flush=True)
    """)
    env = dict(os.environ, TMPDIR=str(tmp_path))
    proc = subprocess.Popen(
        [sys.executable, "-c", runner, str(child), str(out)], env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
    )
    time.sleep(2.5)
    os.kill(proc.pid, signal.SIGINT)
    time.sleep(0.5)
    assert proc.poll() is None  # first Ctrl+C: still waiting out the grace period
    os.kill(proc.pid, signal.SIGINT)
    stdout, stderr = proc.communicate(timeout=30)
    assert "RC" in stdout, stderr
    assert "True" in stdout
    assert not list(tmp_path.glob("pymolt-trace-*"))


def test_sigint_shield_is_inert_off_main_thread():
    import threading

    seen = {}

    def run():
        with service._SigintShield() as shield:
            seen["active"] = shield.active

    t = threading.Thread(target=run)
    t.start()
    t.join()
    assert seen == {"active": False}


@posix_only
def test_sigint_shield_restores_previous_handler():
    before = signal.getsignal(signal.SIGINT)
    with service._SigintShield() as shield:
        assert shield.active
        assert signal.getsignal(signal.SIGINT) is not before
    assert signal.getsignal(signal.SIGINT) is before


def test_capture_trace_removes_work_dir(tmp_path, monkeypatch):
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    monkeypatch.setenv("TMPDIR", str(tmpdir))
    monkeypatch.setattr("tempfile.tempdir", None)
    script = tmp_path / "s.py"
    script.write_text("import json\njson.loads('{}')\n")
    out = tmp_path / "trace.jsonl"

    result = service.capture_trace("json", [sys.executable, str(script)], out, "auto")

    assert result.events >= 1 and out.is_file()
    assert Path(result.metadata_path).is_file()
    assert not list(tmpdir.glob("pymolt-trace-*"))


def test_capture_trace_failure_without_traces_removes_work_dir(tmp_path, monkeypatch):
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    monkeypatch.setenv("TMPDIR", str(tmpdir))
    monkeypatch.setattr("tempfile.tempdir", None)

    def boom(*a, **k):
        raise RuntimeError("no watcher")

    monkeypatch.setattr(service, "capture_trace_local", boom)

    with pytest.raises(RuntimeError):
        service.capture_trace("json", [sys.executable, "-c", "pass"], tmp_path / "t.jsonl", "auto")

    assert not list(tmpdir.glob("pymolt-trace-*"))


def test_capture_trace_timeout_keeps_uncopied_raw_traces(tmp_path, monkeypatch):
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    monkeypatch.setenv("TMPDIR", str(tmpdir))
    monkeypatch.setattr("tempfile.tempdir", None)
    monkeypatch.setattr(service, "_CAPTURE_TIMEOUT", 1.0)
    script = tmp_path / "s.py"
    script.write_text("import json, time\njson.loads('{}')\ntime.sleep(30)\n")

    with pytest.raises(ValueError):
        service.capture_trace("json", [sys.executable, str(script)], tmp_path / "t.jsonl", "auto")

    kept = list(tmpdir.glob("pymolt-trace-*"))
    # never delete traces that were not copied anywhere
    assert all(
        any(f.stat().st_size for f in d.rglob("trace-*.jsonl")) for d in kept
    )
