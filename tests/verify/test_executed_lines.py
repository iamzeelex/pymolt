"""The executed-code record: which lines (3.12+) or functions (setprofile) of the project's
own code ran, written to the sidecar as ``executed`` for the host's coverage.

Runtime-layer test: pure stdlib + pytest, 3.6-compatible syntax. The scenarios run in process
on the host and, through ``boundary_tracer.activate`` in a subprocess, on the host and on real
3.8 and 3.11 interpreters when uv has them.
"""
import importlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from pymolt.verify import _backend
from pymolt.verify._backend import (
    HAS_MONITORING,
    SetProfileBackend,
    code_lines,
    decode_lines,
    encode_lines,
    executed_metadata,
)
from pymolt.verify._sinks import MemorySink

REPO = Path(__file__).resolve().parents[2]
DEP = "vfakeexecdep"
USER = "vfakeexecuser"

DEP_SRC = '''
def f(x):
    return x + 1
'''

# Each line a test asserts on carries a marker comment.
USER_SRC = '''
import vfakeexecdep

TOP = vfakeexecdep.f(0)  # @top


def used(x):
    a = vfakeexecdep.f(x)  # @used-call
    if x > 100:
        b = vfakeexecdep.f(-1)  # @branch-not-taken
    return (a +  # @multi-start
            1)  # @multi-end


def unused(x):
    return vfakeexecdep.f(x)  # @unused-body


def deco(fn):
    return fn


@deco  # @decorator
def decorated():
    return 2  # @decorated-body
'''

FAMILIES = ["monitoring", "setprofile"] if HAS_MONITORING else ["setprofile"]


def _line(marker):
    for number, text in enumerate(USER_SRC.splitlines(), 1):
        if text.endswith("# @" + marker):
            return number
    raise AssertionError(marker)


@pytest.fixture
def project(tmp_path):
    (tmp_path / (DEP + ".py")).write_text(DEP_SRC)
    (tmp_path / (USER + ".py")).write_text(USER_SRC)
    sys.path.insert(0, str(tmp_path))
    yield tmp_path
    sys.path.remove(str(tmp_path))
    for name in (DEP, USER):
        sys.modules.pop(name, None)


def _backend_for(family, **kw):
    sink = MemorySink()
    if family == "monitoring":
        from pymolt.verify._monitoring import MonitoringBackend
        return MonitoringBackend(DEP, sink, source=USER, **kw)
    return SetProfileBackend(DEP, sink, source=USER)


def _run(be, body):
    be.start()
    try:
        body()
    finally:
        be.stop()


def _drive():
    user = importlib.import_module(USER)
    user.used(1)
    user.decorated()


def _user_file(record, project):
    files = record["files"]
    (name,) = [n for n in files if n.endswith(USER + ".py")]
    assert os.path.realpath(name) == os.path.realpath(str(project / (USER + ".py")))
    return {key: decode_lines(value) for key, value in files[name].items()}


# ── encoding ─────────────────────────────────────────────────────────────────


def test_lines_encode_as_sorted_ranges_and_decode_back():
    lines = {1, 2, 3, 5, 9, 10, 40}
    assert encode_lines(lines) == "1-3,5,9-10,40"
    assert decode_lines(encode_lines(lines)) == lines
    assert encode_lines(()) == "" and decode_lines("") == set()
    assert decode_lines("3,x,5-6,") == {3, 5, 6}     # a malformed part is skipped


def test_a_code_objects_lines_are_its_own_the_decorator_runs_in_the_enclosing_code():
    import types
    namespace = {}
    stub = types.SimpleNamespace(f=lambda x: x)
    exec(compile(USER_SRC.replace("import vfakeexecdep", "vfakeexecdep = stub"),
                 "u.py", "exec"), dict(namespace, stub=stub), namespace)
    assert code_lines(namespace["used"].__code__) >= {_line("used-call"), _line("multi-start")}
    assert _line("unused-body") not in code_lines(namespace["used"].__code__)
    module = compile(USER_SRC, "u.py", "exec")
    assert _line("decorator") in code_lines(module)
    assert _line("decorated-body") not in code_lines(module)


# ── in process ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("family", FAMILIES)
def test_discover_records_what_of_ours_ran(project, family):
    be = _backend_for(family)
    _run(be, _drive)
    record = executed_metadata([be])
    assert record["v"] == 1 and record["complete"] is True and record["until"] is None
    got = _user_file(record, project)
    ran = {_line(m) for m in ("top", "used-call", "multi-start", "decorator", "decorated-body")}
    if family == "monitoring":
        # line by line: the branch not taken and the function never called did not run
        assert record["granularity"] == "line"
        assert ran <= got["lines"] and "functions" not in got
        assert _line("branch-not-taken") not in got["lines"]
    else:
        # function granularity: a line of a function that started ran or not
        assert record["granularity"] == "function"
        assert ran <= got["functions"] and "lines" not in got
        assert _line("branch-not-taken") in got["functions"]
    assert _line("unused-body") not in got.get("lines", set()) | got.get("functions", set())
    # the dependency's own code is not ours
    assert not [n for n in record["files"] if n.endswith(DEP + ".py")]


@pytest.mark.skipif(not HAS_MONITORING, reason="sys.monitoring is 3.12+")
def test_incremental_records_lines_from_the_module_body_on(project):
    be = _backend_for("monitoring", mode="incremental")
    _run(be, _drive)
    record = executed_metadata([be])
    got = _user_file(record, project)
    assert record["granularity"] == "line" and record["complete"] is True
    assert {_line("top"), _line("used-call"), _line("decorator")} <= got["lines"]
    assert not {_line("branch-not-taken"), _line("unused-body")} & got["lines"]


@pytest.mark.skipif(not HAS_MONITORING, reason="sys.monitoring is 3.12+")
def test_a_code_object_too_large_for_line_events_counts_at_function_granularity(
        project, monkeypatch):
    from pymolt.verify import _monitoring
    monkeypatch.setattr(_monitoring, "_LINE_SLOW", True)
    monkeypatch.setattr(_monitoring, "_LINE_BIG", 0)     # every code object is "too large"
    be = _backend_for("monitoring")
    _run(be, _drive)
    record = executed_metadata([be])
    got = _user_file(record, project)
    # its lines are unknown one by one, not "not run": the code object started
    assert "lines" not in got
    assert {_line("used-call"), _line("branch-not-taken")} <= got["functions"]
    assert _line("unused-body") not in got["functions"]
    assert "late-lines-skipped" in (be.note or "")


@pytest.mark.skipif(not HAS_MONITORING, reason="sys.monitoring is 3.12+")
def test_watch_mode_sees_no_code_of_ours_start_and_records_nothing(project):
    be = _backend_for("monitoring", mode="watch", watch_targets=[DEP + ".f"])
    _run(be, _drive)
    assert executed_metadata([be]) is None


@pytest.mark.parametrize("family", FAMILIES)
def test_the_discover_to_watch_switch_ends_the_record(project, family):
    be = _backend_for(family)
    be.start()
    try:
        user = importlib.import_module(USER)
        user.used(1)
        if family == "monitoring":
            be.enter_watch()
        else:
            be.retire()
        user.unused(1)                  # first run after the switch: not seen
    finally:
        be.stop()
    record = executed_metadata([be])
    got = _user_file(record, project)
    assert record["complete"] is False and record["until"] is not None
    assert _line("used-call") in got.get("lines", set()) | got.get("functions", set())
    assert _line("unused-body") not in got.get("lines", set()) | got.get("functions", set())


@pytest.mark.parametrize("family", FAMILIES)
def test_no_record_when_turned_off_or_without_a_near_side(project, family, monkeypatch):
    monkeypatch.setenv("PYMOLT_TRACE_EXECUTED", "0")
    be = _backend_for(family)
    _run(be, _drive)
    assert executed_metadata([be]) is None
    monkeypatch.delenv("PYMOLT_TRACE_EXECUTED")
    sink = MemorySink()
    if family == "monitoring":
        from pymolt.verify._monitoring import MonitoringBackend
        be = MonitoringBackend(DEP, sink, include_internal=True)
    else:
        be = SetProfileBackend(DEP, sink, include_internal=True)
    _run(be, _drive)
    assert executed_metadata([be]) is None


def test_records_from_several_backends_merge():
    class Fake:
        def __init__(self, granularity, files, complete=True, until=None):
            self.executed_granularity = granularity
            self._files = files
            self._complete = complete
            self._exec_until = until

        def executed_observed(self):
            return True

        def executed_files(self):
            return self._files

        def executed_complete(self):
            return self._complete

    record = executed_metadata([
        Fake("function", {"/p/a.py": (set(), {1, 2, 3})}, complete=False, until=5.0),
        Fake("line", {"/p/a.py": ({2, 7}, set()), "/p/b.py": ({1}, set())}),
    ])
    assert record["granularity"] == "line" and record["complete"] is False
    assert record["until"] == 5.0
    assert record["files"] == {"/p/a.py": {"lines": "2,7", "functions": "1,3"},
                               "/p/b.py": {"lines": "1"}}


# ── end to end: activate() writes it to the sidecar ──────────────────────────

SCRIPT = (
    "import sys\n"
    "from pymolt.verify import boundary_tracer\n"
    "boundary_tracer.activate()\n"
    "import vfakeexecuser\n"
    "vfakeexecuser.used(1)\n"
    "vfakeexecuser.decorated()\n"
)


def _interpreter(minor):
    uv = shutil.which("uv")
    if uv is None:
        return None
    proc = subprocess.run([uv, "python", "find", "--no-project", minor], capture_output=True,
                          text=True, timeout=30, cwd="/")
    found = proc.stdout.strip()
    return found if proc.returncode == 0 and found and Path(found).is_file() else None


def _activate(python, project, mode, sink="jsonl", **extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYMOLT_", "PYTHON"))}
    env.update(PYTHONPATH=os.pathsep.join([str(REPO), str(project)]),
               PYMOLT_TRACE_TARGET=DEP, PYMOLT_TRACE_SOURCE=USER, PYMOLT_TRACE_MODE=mode,
               PYMOLT_TRACE_SINK=sink, PYMOLT_TRACE_OUT=str(project / "t-{pid}.jsonl"), **extra)
    proc = subprocess.run([python, "-c", SCRIPT], env=env, capture_output=True, text=True,
                          timeout=120, cwd=str(project))
    assert proc.returncode == 0, proc.stderr
    (meta_path,) = project.glob("*.meta.json")
    return json.loads(meta_path.read_text())


@pytest.mark.parametrize("minor", ["3.8", "3.11"])
def test_old_interpreters_record_the_functions_of_ours_that_started(minor, project):
    python = _interpreter(minor)
    if python is None:
        pytest.skip("needs a uv-managed Python " + minor)
    meta = _activate(python, project, "discover")
    assert meta["python"].startswith(minor + ".") and meta["backend"] == "setprofile"
    record = meta["executed"]
    assert record["granularity"] == "function" and record["complete"] is True
    got = _user_file(record, project)
    assert {_line("top"), _line("used-call"), _line("branch-not-taken"),
            _line("decorator"), _line("decorated-body")} <= got["functions"]
    assert _line("unused-body") not in got["functions"]


@pytest.mark.skipif(not HAS_MONITORING, reason="sys.monitoring is 3.12+")
@pytest.mark.parametrize("mode", ["discover", "incremental"])
def test_312_records_the_lines_of_ours_that_ran(mode, project):
    meta = _activate(sys.executable, project, mode)
    assert meta["mode"] == mode
    record = meta["executed"]
    assert record["granularity"] == "line" and record["complete"] is True
    got = _user_file(record, project)
    assert {_line("top"), _line("used-call"), _line("multi-start"), _line("decorator"),
            _line("decorated-body")} <= got["lines"]
    assert not {_line("branch-not-taken"), _line("unused-body")} & got["lines"]


def test_the_record_is_in_the_sidecar_not_in_the_facts_snapshot(project):
    meta = _activate(sys.executable, project, "discover", sink="facts")
    assert meta["sink"] == "facts" and "executed" in meta
    snapshot = Path(meta["facts_path"])
    assert snapshot.is_file() and '"executed"' not in snapshot.read_text()


def test_turned_off_the_sidecar_has_no_record(project):
    meta = _activate(sys.executable, project, "discover", PYMOLT_TRACE_EXECUTED="0")
    assert "executed" not in meta


def test_the_env_switch_is_read_by_the_backends(monkeypatch):
    monkeypatch.setenv(_backend.ENV_EXECUTED, "off")
    assert _backend.executed_enabled() is False
    monkeypatch.setenv(_backend.ENV_EXECUTED, "1")
    assert _backend.executed_enabled() is True
