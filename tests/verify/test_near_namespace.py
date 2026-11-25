"""Whether code is ours is decided for the namespace it runs in, never for a module name alone.

``python -m pytest`` runs pytest's own ``__main__`` (in site-packages). Streamlit's AppTest then
executes the app's script in a fresh module it also names ``__main__`` (``__file__`` = the
script). The near side (``AppMatcher``) cached its decision per module name, so whichever
``__main__`` was asked about first decided for both. Under sys.monitoring that was pytest's
(its CALL of ``console_main`` is a call site the backend looks at), and every call the
script made was "not our code": the 3.12 post-migration capture of the streamlit toy recorded
0 of the app's calls, against 233 on 3.11 (setprofile never asked about pytest's ``__main__``).
The dependency side (``SitePackagesMatcher``) cached the same way.

Runtime-layer test: pure stdlib + pytest, 3.6-compatible syntax.
"""
import json
import os
import subprocess
import sys
import types

import pytest

from pymolt.verify._backend import (
    HAS_MONITORING,
    AppMatcher,
    PrefixMatcher,
    SetProfileBackend,
    SitePackagesMatcher,
)
from pymolt.verify._sinks import MemorySink

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BACKENDS = ["setprofile"] + (["monitoring"] if HAS_MONITORING else [])


def _frame_in(namespace_file, code_file=None):
    """A frame running in a fresh namespace named ``__main__`` whose ``__file__`` is
    ``namespace_file`` (its code compiled from ``code_file``, by default the same path)."""
    ns = {"__name__": "__main__", "__file__": namespace_file}
    exec(compile("import sys\nframe = sys._getframe()\n", code_file or namespace_file, "exec"),
         ns)
    return ns["frame"]


def _installed_file(matcher_roots):
    return os.path.join(matcher_roots[0], "fakerunner", "__main__.py")


def test_the_near_side_decides_each_main_namespace_by_its_own_file(tmp_path):
    near = AppMatcher(())
    runner = _frame_in(_installed_file(near._dep_roots))
    script = _frame_in(str(tmp_path / "app" / "streamlit_app.py"))
    assert near.matches("__main__", runner) is False     # pytest's own __main__ first
    assert near.matches("__main__", script) is True      # then the app's script
    assert near.matches("__main__", runner) is False     # each namespace keeps its answer


def test_the_dependency_side_decides_each_main_namespace_by_its_own_file(tmp_path):
    deps = SitePackagesMatcher()
    if not deps._roots:
        pytest.skip("no site-packages root")
    runner = _frame_in(_installed_file(deps._roots))
    script = _frame_in(str(tmp_path / "app" / "streamlit_app.py"))
    assert deps.matches("__main__", runner) is True
    assert deps.matches("__main__", script) is False
    assert deps.matches("__main__", runner) is True


def test_a_namespace_without_a_file_is_placed_by_its_code(tmp_path):
    near = AppMatcher(())
    ns = {"__name__": "__main__"}                        # ``python -c``, ``exec`` with no file
    exec(compile("import sys\nframe = sys._getframe()\n", str(tmp_path / "x.py"), "exec"), ns)
    assert near.matches("__main__", ns["frame"]) is True


DEP_SRC = "def render(title):\n    return '<h1>' + title + '</h1>'\n"
SCRIPT_SRC = "import vfakestdep\n\nvfakestdep.render('Sales')\n"
RUNNER_SRC = "import vfakestdep\n\nvfakestdep.render('runner')\n"


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_script_run_as_main_after_an_installed_main_is_recorded(tmp_path, backend):
    """In one traced run: an installed tool's ``__main__`` makes calls first, then the app's
    script is executed as ``__main__`` (Streamlit's ScriptRunner) and calls a dependency."""
    if backend == "monitoring":
        from pymolt.verify._monitoring import MonitoringBackend as cls
    else:
        cls = SetProfileBackend
    dep = types.ModuleType("vfakestdep")
    exec(DEP_SRC, dep.__dict__)
    near = AppMatcher(())
    script_path = str(tmp_path / "app" / "streamlit_app.py")
    sink = MemorySink()
    be = cls(PrefixMatcher(["vfakestdep"]), sink)
    saved_main = sys.modules.get("__main__")
    sys.modules["vfakestdep"] = dep
    be.start()
    try:
        runner = types.ModuleType("__main__")
        runner.__file__ = _installed_file(near._dep_roots)
        sys.modules["__main__"] = runner
        exec(compile(RUNNER_SRC, runner.__file__, "exec"), runner.__dict__)
        script = types.ModuleType("__main__")
        script.__file__ = script_path
        sys.modules["__main__"] = script
        exec(compile(SCRIPT_SRC, script_path, "exec"), script.__dict__)
    finally:
        be.stop()
        sys.modules["__main__"] = saved_main
        sys.modules.pop("vfakestdep", None)
    sites = sorted((r["where"] or {}).get("file") for r in sink.records.values()
                   if (r.get("sym") or r["qualname"]) == "vfakestdep.render")
    assert sites == [script_path], sites


TEST_SRC = '''
import types
import sys


def test_runs_the_app_as_main():
    path = {script!r}
    with open(path) as fh:
        code = compile(fh.read(), path, "exec")
    module = types.ModuleType("__main__")
    module.__dict__["__file__"] = path
    saved = sys.modules["__main__"]
    sys.modules["__main__"] = module
    try:
        exec(code, module.__dict__)
    finally:
        sys.modules["__main__"] = saved
'''


@pytest.mark.parametrize("backend", BACKENDS)
def test_python_m_pytest_running_a_script_as_main_is_recorded(tmp_path, backend):
    """The QA case end to end: ``python -m pytest`` under capture (the watcher started by a
    ``sitecustomize``, as ``pymolt capture`` does), a test that executes the app's script as
    ``__main__`` (what ``streamlit.testing.v1.AppTest`` does)."""
    (tmp_path / "vfakestdep.py").write_text(DEP_SRC)
    (tmp_path / "app").mkdir()
    script = tmp_path / "app" / "streamlit_app.py"
    script.write_text(SCRIPT_SRC)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_app.py").write_text(TEST_SRC.format(script=str(script)))
    hook = tmp_path / "hook"
    hook.mkdir()
    (hook / "sitecustomize.py").write_text(
        "from pymolt.verify import boundary_tracer\nboundary_tracer.activate()\n")
    env = dict((k, v) for k, v in os.environ.items() if not k.startswith(("PYMOLT_", "PYTHON")))
    env.update(PYTHONPATH=os.pathsep.join([str(hook), ROOT, str(tmp_path)]),
               PYMOLT_TRACE_TARGET="vfakestdep", PYMOLT_TRACE_BACKEND=backend,
               PYMOLT_TRACE_MODE="discover", PYMOLT_TRACE_OUT=str(tmp_path / "t-{pid}.jsonl"))
    proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                           "tests"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          universal_newlines=True, timeout=300, cwd=str(tmp_path))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    (trace,) = tmp_path.glob("t-*.jsonl")
    events = [json.loads(line) for line in trace.read_text().splitlines() if line.strip()]
    sites = [(e.get("where") or {}).get("file") for e in events
             if e.get("sym") == "vfakestdep.render"]
    assert sites == [str(script)], events
