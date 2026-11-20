"""Honest watcher activation: a requested-but-failed watcher must never look like a clean run.

``sitecustomize`` swallowed every activation error, so an interpreter the runtime could not
load in (Python 2.7 today) ran untraced while the capture looked valid. The failure is now
reported on stderr, written as ``<trace>.activation-error.json``, read by the host before
the work dir is removed, and blocks a PASS.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pymolt.verify._normalize as normalize_mod

SITECUSTOMIZE = Path(normalize_mod.__file__).with_name("sitecustomize.py")


def _run_with_broken_runtime(tmp_path, env_extra):
    hook_dir = tmp_path / "hook"
    (hook_dir / "pymolt").mkdir(parents=True)
    shutil.copy(SITECUSTOMIZE, hook_dir / "sitecustomize.py")
    (hook_dir / "pymolt" / "__init__.py").write_text("raise ImportError('runtime unavailable')\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYMOLT_TRACE")}
    env["PYTHONPATH"] = str(hook_dir)
    env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-c", "print('app ran')"],
        env=env, capture_output=True, text=True, timeout=60, cwd=str(tmp_path),
    )


def test_requested_but_failed_activation_is_reported(tmp_path):
    out_tpl = str(tmp_path / "trace-{pid}.jsonl")
    proc = _run_with_broken_runtime(
        tmp_path, {"PYMOLT_TRACE_TARGET": "json", "PYMOLT_TRACE_OUT": out_tpl}
    )
    assert proc.returncode == 0 and proc.stdout.strip() == "app ran"  # the app is never broken
    assert "UNTRACED" in proc.stderr and "runtime unavailable" in proc.stderr
    (marker,) = list(tmp_path.glob("trace-*.jsonl.activation-error.json"))
    payload = json.loads(marker.read_text())
    assert payload["error_type"] == "ImportError"
    assert payload["python"].startswith("{}.{}.".format(*sys.version_info[:2]))


def test_unrequested_tracing_stays_silent(tmp_path):
    proc = _run_with_broken_runtime(tmp_path, {})
    assert proc.returncode == 0 and proc.stdout.strip() == "app ran"
    assert proc.stderr == ""
    assert not list(tmp_path.glob("*.activation-error.json"))


def test_sitecustomize_is_ascii_so_it_parses_on_python2():
    SITECUSTOMIZE.read_bytes().decode("ascii")


def test_exported_bundle_reports_activation_failure_too(tmp_path):
    from pymolt.verify.export_watcher import export_watcher

    bundle = export_watcher(tmp_path / "bundle")
    text = (bundle / "sitecustomize.py").read_text()
    assert "from pymolt_trace.boundary_tracer import activate" in text
    assert "_report_activation_failure" in text
    text.encode("ascii")
    # Break the bundled runtime and check the app still runs but the failure is surfaced.
    (bundle / "pymolt_trace" / "boundary_tracer.py").write_text("raise ImportError('broken')\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYMOLT_TRACE")}
    env.update(PYTHONPATH=str(bundle), PYMOLT_TRACE_TARGET="json",
               PYMOLT_TRACE_OUT=str(tmp_path / "trace-{pid}.jsonl"))
    proc = subprocess.run([sys.executable, "-c", "print('app ran')"], env=env,
                          capture_output=True, text=True, timeout=60)
    assert proc.stdout.strip() == "app ran" and "UNTRACED" in proc.stderr
    assert list(tmp_path.glob("trace-*.jsonl.activation-error.json"))


def test_capture_surfaces_child_activation_failures(tmp_path):
    """A child interpreter that could not load the watcher (e.g. 2.7) leaves a marker; the
    host must report it and refuse to claim a loss-free capture."""
    from pymolt.verify import service

    simulate_failed_child = (
        "import json, os\n"
        "p = os.environ['PYMOLT_TRACE_OUT'].replace('{pid}', '4242') + '.activation-error.json'\n"
        "json.dump({'pid': 4242, 'python': '2.7.18', 'executable': '/usr/bin/python2',\n"
        "           'error': 'SyntaxError: Non-ASCII character'}, open(p, 'w'))\n"
    )
    result = service.capture_trace(
        "json", [sys.executable, "-c", simulate_failed_child], tmp_path / "t.jsonl", "auto",
        cwd=tmp_path,
    )
    assert result.activation_failures == [{
        "pid": 4242, "python": "2.7.18", "executable": "/usr/bin/python2",
        "error": "SyntaxError: Non-ASCII character",
    }]
    assert result.dropped_events is None  # loss can no longer be claimed to be zero
    meta = json.loads(Path(result.metadata_path).read_text())
    assert meta["activation_failures"][0]["pid"] == 4242
