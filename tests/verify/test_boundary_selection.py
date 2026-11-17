"""Boundary selection in the injected runtime: ``PYMOLT_TRACE_STDLIB`` makes named stdlib
modules dependency targets for one capture, and the test-runner packages are never targets
unless ``PYMOLT_TRACE_TARGET`` names them."""
import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

from pymolt.verify._backend import (
    HAS_MONITORING,
    TEST_RUNNER_PACKAGES,
    AnyMatcher,
    PrefixMatcher,
    SetProfileBackend,
    SitePackagesMatcher,
    build_matcher,
    stdlib_targets,
)
from pymolt.verify._sinks import MemorySink

# ── PYMOLT_TRACE_STDLIB ──────────────────────────────────────────────────────


def test_stdlib_targets_parse():
    assert stdlib_targets("") == frozenset()
    assert stdlib_targets(" asyncio, collections ,,datetime.sub ") == frozenset(
        ("asyncio", "collections", "datetime"))
    assert stdlib_targets(["os", "re"]) == frozenset(("os", "re"))


def test_stdlib_targets_read_the_environment(monkeypatch):
    monkeypatch.delenv("PYMOLT_TRACE_STDLIB", raising=False)
    assert stdlib_targets() == frozenset()
    monkeypatch.setenv("PYMOLT_TRACE_STDLIB", "shutil,json")
    assert stdlib_targets() == frozenset(("shutil", "json"))


def test_prefix_matcher_without_the_variable_never_matches_stdlib(monkeypatch):
    monkeypatch.delenv("PYMOLT_TRACE_STDLIB", raising=False)
    m = PrefixMatcher(["flask"])
    assert not m.matches("asyncio") and not m.matches("collections.abc")
    assert m.matches("flask.app")


def test_prefix_matcher_with_the_variable_matches_named_stdlib_only(monkeypatch):
    monkeypatch.setenv("PYMOLT_TRACE_STDLIB", "asyncio,collections,os")
    m = PrefixMatcher(["flask"])
    assert m.matches("asyncio") and m.matches("asyncio.tasks") and m.matches("collections.abc")
    assert m.matches("_asyncio") and m.matches("_collections")      # the C accelerator
    assert m.matches("posixpath") and m.matches("posix")             # os.path.* reports these
    assert not m.matches("json") and not m.matches("asyncioish")
    assert m.matches("flask.app")                                    # the dependency stays


def test_the_explicit_exclude_still_wins_over_a_stdlib_target():
    m = PrefixMatcher(["flask"], exclude=("collections",), stdlib="collections,asyncio")
    assert not m.matches("collections") and m.matches("asyncio")


def test_build_matcher_applies_the_variable_to_every_target_shape(monkeypatch):
    monkeypatch.setenv("PYMOLT_TRACE_STDLIB", "datetime")
    for target in ("flask", "flask,werkzeug", ["flask"]):
        assert build_matcher(target).matches("datetime")
    assert build_matcher("all").matches("datetime")


def test_site_packages_matcher_stdlib_hit_precedes_the_builtin_rule(monkeypatch):
    monkeypatch.setenv("PYMOLT_TRACE_STDLIB", "posix")
    assert SitePackagesMatcher().matches("posix")        # a C builtin, asked for by name
    monkeypatch.delenv("PYMOLT_TRACE_STDLIB")
    assert not SitePackagesMatcher().matches("posix")


def test_any_matcher_carries_the_stdlib_targets(monkeypatch):
    monkeypatch.setenv("PYMOLT_TRACE_STDLIB", "shutil")
    m = AnyMatcher(build_matcher("flask"), PrefixMatcher(["werkzeug"]))
    assert m.matches("shutil") and m.matches("werkzeug.x")


def _project_calls_stdlib():
    import collections

    chain = collections.ChainMap({"a": 1})
    return chain.new_child()


def _record(backend_name, monkeypatch, stdlib):
    if stdlib is None:
        monkeypatch.delenv("PYMOLT_TRACE_STDLIB", raising=False)
    else:
        monkeypatch.setenv("PYMOLT_TRACE_STDLIB", stdlib)
    sink = MemorySink()
    if backend_name == "setprofile":
        be = SetProfileBackend("nosuchdep", sink, source=__name__)
    else:
        from pymolt.verify._monitoring import MonitoringBackend
        be = MonitoringBackend("nosuchdep", sink, source=__name__, mode="discover")
    be.start()
    try:
        _project_calls_stdlib()
    finally:
        be.stop()
    return sorted(r.get("sym") or r["qualname"] for r in sink.records.values())


def _backends():
    return ["setprofile"] + (["monitoring"] if HAS_MONITORING else [])


@pytest.mark.parametrize("backend", _backends())
def test_stdlib_calls_are_recorded_only_with_the_variable(backend, monkeypatch):
    assert _record(backend, monkeypatch, None) == []
    syms = _record(backend, monkeypatch, "collections")
    assert any(s.startswith("collections.ChainMap") for s in syms), syms
    assert _record(backend, monkeypatch, "json") == []     # another module: still nothing


@pytest.mark.parametrize("backend", _backends())
def test_stdlib_internal_traffic_is_not_a_contact(backend, monkeypatch):
    """Only the project's own call into the module counts, not the module calling itself."""
    syms = _record(backend, monkeypatch, "collections")
    assert not any(s.startswith("collections.abc") for s in syms)


def test_activate_records_the_stdlib_targets_in_the_metadata(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYMOLT_TRACE")}
    env.update(PYMOLT_TRACE_TARGET="json", PYTHONPATH=str(repo),
               PYMOLT_TRACE_STDLIB="datetime, asyncio,,",
               PYMOLT_TRACE_OUT=str(tmp_path / "t-{pid}.jsonl"))
    code = ("from pymolt.verify import boundary_tracer\n"
            "boundary_tracer.activate()\n"
            "import json\njson.dumps(1)\n")
    subprocess.run([sys.executable, "-c", code], env=env, check=True, timeout=60)
    (meta,) = tmp_path.glob("*.meta.json")
    assert json.loads(meta.read_text())["stdlib_targets"] == ["asyncio", "datetime"]


def test_without_the_variable_the_metadata_names_no_stdlib_targets(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYMOLT_TRACE")}
    env.update(PYMOLT_TRACE_TARGET="json", PYTHONPATH=str(repo),
               PYMOLT_TRACE_OUT=str(tmp_path / "t-{pid}.jsonl"))
    code = ("from pymolt.verify import boundary_tracer\n"
            "boundary_tracer.activate()\n"
            "import json\njson.dumps(1)\n")
    subprocess.run([sys.executable, "-c", code], env=env, check=True, timeout=60)
    (meta,) = tmp_path.glob("*.meta.json")
    assert json.loads(meta.read_text())["stdlib_targets"] == []


# ── test-runner packages ─────────────────────────────────────────────────────


def _fake_installed(name, matcher_roots):
    """A module that lives under the first site-packages root."""
    root = matcher_roots[0]
    mod = types.ModuleType(name)
    mod.__file__ = root + name.replace(".", "/") + ".py"
    sys.modules[name] = mod
    return mod


def test_the_runner_packages_are_the_documented_set():
    assert TEST_RUNNER_PACKAGES == {"_pytest", "pytest", "pluggy", "py", "iniconfig",
                                    "_hypothesis_pytestplugin", "xdist"}


@pytest.mark.parametrize("name", sorted(TEST_RUNNER_PACKAGES))
def test_all_dependencies_leaves_the_test_runner_out(name):
    m = SitePackagesMatcher()
    if not m._roots:
        pytest.skip("no site-packages root on this interpreter")
    top = "vfakedep"
    try:
        _fake_installed(top, m._roots)
        _fake_installed(name + ".sub", m._roots)
        assert m.matches(top)                       # an ordinary installed module still counts
        assert not m.matches(name) and not m.matches(name + ".sub")
    finally:
        sys.modules.pop(top, None)
        sys.modules.pop(name + ".sub", None)


def test_naming_a_runner_package_in_the_target_brings_the_family_back():
    assert PrefixMatcher(["flask", "pytest"]).matches("pytest.mark")
    assert PrefixMatcher(["_pytest"]).matches("_pytest.monkeypatch")
    assert PrefixMatcher(["pluggy"]).matches("pluggy._hooks")
    # and only then: a target that names none of them excludes the whole family
    m = PrefixMatcher(["_pytest_like"])
    assert not m.matches("pytest") and not m.matches("xdist.plugin")


def test_a_target_prefix_that_is_itself_a_runner_module_is_explicit():
    assert PrefixMatcher(["xdist"]).matches("xdist.workermanage")
    assert PrefixMatcher(["xdist.workermanage"]).matches("xdist.workermanage")


@pytest.mark.parametrize("backend", _backends())
def test_a_runner_call_from_the_project_is_not_recorded(backend, monkeypatch, tmp_path):
    root = SitePackagesMatcher()._roots
    if not root:
        pytest.skip("no site-packages root on this interpreter")
    src = "def syspath_prepend(p):\n    return p\n"
    path = Path(root[0]) / "_pytest_vfake_monkeypatch.py"
    mod = types.ModuleType("_pytest.vfake")
    mod.__file__ = str(path)
    exec(compile(src, str(path), "exec"), mod.__dict__)
    sys.modules["_pytest.vfake"] = mod
    monkeypatch.delenv("PYMOLT_TRACE_STDLIB", raising=False)
    sink = MemorySink()
    if backend == "setprofile":
        be = SetProfileBackend("all", sink, source=__name__)
    else:
        from pymolt.verify._monitoring import MonitoringBackend
        be = MonitoringBackend("all", sink, source=__name__, mode="discover")
    be.start()
    try:
        mod.syspath_prepend(str(tmp_path))
    finally:
        be.stop()
        sys.modules.pop("_pytest.vfake", None)
    assert list(sink.records.values()) == []


def test_the_injected_runtime_is_never_the_near_side():
    """With stdlib targets its own ``os.environ.get`` would read as a project contact."""
    from pymolt.verify import _backend
    from pymolt.verify._backend import AppMatcher

    am = AppMatcher(())
    am._dep_roots = ("/x/site-packages/",)
    frame = types.SimpleNamespace(f_code=types.SimpleNamespace(co_filename="/tmp/b/_backend.py"))
    assert am.matches(_backend.__name__, frame) is False
    assert am.matches("proj.app", types.SimpleNamespace(
        f_code=types.SimpleNamespace(co_filename="/home/p/app.py"))) is True
