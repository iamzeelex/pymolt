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


# ── code a program made with ``exec`` ───────────────────────────────────────────────────
# SQLAlchemy 2.0 builds a mapped class's ``__init__`` and its ``util.decorator`` wrappers with
# ``exec`` in a fresh dict that carries ``__name__`` (its module) and no ``__file__``; the
# frames run as ``"<string>"``. The near side placed such a namespace by the frame's file,
# which no root rejects, so the generated code was "ours" and every call it made inside
# SQLAlchemy became a contact: 71 APPEARED entries in an equivalent 1.3 -> 2.0 migration
# (1.3's dicts carry no ``__name__`` and were never asked about).

GEN_SRC = "import sys\nframe = sys._getframe()\n"


def _frame_exec(namespace, code_file="<string>"):
    exec(compile(GEN_SRC, code_file, "exec"), namespace)
    return namespace.pop("frame")


def _module(monkeypatch, name, filename):
    mod = types.ModuleType(name)
    mod.__file__ = filename
    monkeypatch.setitem(sys.modules, name, mod)
    return mod


def test_a_dependency_s_generated_code_is_not_ours(monkeypatch):
    near = AppMatcher(())
    _module(monkeypatch, "vfakesa.instrumentation",
            os.path.join(near._dep_roots[0], "vfakesa", "instrumentation.py"))
    frame = _frame_exec({"__name__": "vfakesa.instrumentation"})
    assert near.matches("vfakesa.instrumentation", frame) is False


def test_generated_code_named_for_the_project_s_module_is_ours(monkeypatch, tmp_path):
    """A wrapper a dependency made around the project's function names that function's
    module (SQLAlchemy's ``decorator``: ``"__name__": fn.__module__``)."""
    near = AppMatcher(())
    _module(monkeypatch, "vfakeproj.core", str(tmp_path / "vfakeproj" / "core.py"))
    frame = _frame_exec({"__name__": "vfakeproj.core"})
    assert near.matches("vfakeproj.core", frame) is True


def test_generated_code_named_for_no_module_is_not_ours():
    """``collections.namedtuple`` runs its ``__new__`` in ``{"__name__": "namedtuple_P"}``."""
    near = AppMatcher(())
    frame = _frame_exec({"__name__": "namedtuple_vfakePoint"})
    assert near.matches("namedtuple_vfakePoint", frame) is False


def test_generated_code_named_for_a_module_without_a_file_is_ours_by_elimination(monkeypatch):
    """A module the program built from a string (no ``__file__``) is no installed
    dependency's: its code, and code that names it, stay ours, as before."""
    near = AppMatcher(())
    mod = types.ModuleType("vfakebuilt")
    monkeypatch.setitem(sys.modules, "vfakebuilt", mod)
    assert near.matches("vfakebuilt", _frame_exec(mod.__dict__, "<vfakebuilt>")) is True
    assert near.matches("vfakebuilt", _frame_exec({"__name__": "vfakebuilt"})) is True


@pytest.mark.parametrize("code_file", ["<string>", "<stdin>", "<python-input-0>",
                                       "<ipython-input-1-abc>", "<doctest README.rst[0]>"])
def test_main_without_a_file_is_ours(code_file):
    """``python -c``, ``<stdin>``, the REPL, a notebook cell, a doctest text file: the user's
    code in ``__main__``, which has no ``__file__``, even when ``sys.modules["__main__"]``
    is an installed tool's (``python -m pytest``)."""
    near = AppMatcher(())
    frame = _frame_exec({"__name__": "__main__"}, code_file)
    assert near.matches("__main__", frame) is True


def test_code_interact_is_ours():
    near = AppMatcher(())
    frame = _frame_exec({"__name__": "__console__", "__doc__": None}, "<console>")
    assert near.matches("__console__", frame) is True


def test_generated_code_in_a_module_s_own_namespace_follows_the_module(monkeypatch, tmp_path):
    """dataclass / namedtuple methods exec'd in the module's own globals (``__file__`` there)
    are the module's: the project's for a project class, a dependency's for one of its."""
    near = AppMatcher(())
    proj = _module(monkeypatch, "vfakeproj.models", str(tmp_path / "vfakeproj" / "models.py"))
    dep = _module(monkeypatch, "vfakedep.models",
                  os.path.join(near._dep_roots[0], "vfakedep", "models.py"))
    assert near.matches("vfakeproj.models", _frame_exec(proj.__dict__)) is True
    assert near.matches("vfakedep.models", _frame_exec(dep.__dict__)) is False


GEN_DEP_SRC = '''
def _new_state(obj):
    return obj


def _render(title):
    return "<h1>" + title + "</h1>"


def instrument(cls):
    env = {"_new_state": _new_state, "__name__": __name__}
    exec("def __init__(self, title):\\n    _new_state(self)\\n    self.title = title\\n", env)
    cls.__init__ = env["__init__"]
    return cls


def render(title):
    return _render(title)
'''
GEN_APP_SRC = '''
import vfakegendep


@vfakegendep.instrument
class Book(object):
    pass


book = Book("Notes")
vfakegendep.render(book.title)
'''


@pytest.mark.parametrize("backend", BACKENDS)
def test_calls_a_dependency_s_generated_code_makes_are_not_recorded(tmp_path, backend):
    """In one traced run of every installed dependency (``capture``'s default target): the
    project's class gets an ``__init__`` its dependency generated with ``exec``; constructing
    it calls into the dependency from ``"<string>"``. Only the project's own calls
    (``instrument``, ``render``) are contacts."""
    if backend == "monitoring":
        from pymolt.verify._monitoring import MonitoringBackend as cls
    else:
        cls = SetProfileBackend
    sink = MemorySink()
    be = cls("all", sink)
    dep = types.ModuleType("vfakegendep")
    dep.__file__ = os.path.join(be.near._dep_roots[0], "vfakegendep.py")
    exec(compile(GEN_DEP_SRC, dep.__file__, "exec"), dep.__dict__)
    app_path = str(tmp_path / "app" / "core.py")
    app = types.ModuleType("vfakegenapp")
    app.__file__ = app_path
    sys.modules["vfakegendep"] = dep
    be.start()
    try:
        exec(compile(GEN_APP_SRC, app_path, "exec"), app.__dict__)
    finally:
        be.stop()
        sys.modules.pop("vfakegendep", None)
    calls = sorted(((r.get("sym") or r["qualname"]), (r["where"] or {}).get("file"))
                   for r in sink.records.values())
    assert calls == [("vfakegendep.instrument", app_path), ("vfakegendep.render", app_path)]


@pytest.mark.parametrize("backend", BACKENDS)
def test_python_c_calls_are_recorded(tmp_path, backend):
    """``capture -- python -c "..."`` (docs/TESTING.md): the ``-c`` program runs in
    ``__main__`` with no ``__file__``, as ``"<string>"``; its calls are the user's."""
    (tmp_path / "vfakestdep.py").write_text(DEP_SRC)
    hook = tmp_path / "hook"
    hook.mkdir()
    (hook / "sitecustomize.py").write_text(
        "from pymolt.verify import boundary_tracer\nboundary_tracer.activate()\n")
    env = dict((k, v) for k, v in os.environ.items() if not k.startswith(("PYMOLT_", "PYTHON")))
    env.update(PYTHONPATH=os.pathsep.join([str(hook), ROOT, str(tmp_path)]),
               PYMOLT_TRACE_TARGET="vfakestdep", PYMOLT_TRACE_BACKEND=backend,
               PYMOLT_TRACE_MODE="discover", PYMOLT_TRACE_OUT=str(tmp_path / "t-{pid}.jsonl"))
    proc = subprocess.run([sys.executable, "-c", "import vfakestdep\nvfakestdep.render('x')\n"],
                          env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          universal_newlines=True, timeout=300, cwd=str(tmp_path))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    (trace,) = tmp_path.glob("t-*.jsonl")
    events = [json.loads(line) for line in trace.read_text().splitlines() if line.strip()]
    sites = [(e.get("where") or {}).get("file") for e in events
             if e.get("sym") == "vfakestdep.render"]
    assert sites == ["<string>"], events
