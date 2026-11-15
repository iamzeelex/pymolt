"""The delta engine's old side through a container runner (docker mocked)."""

from __future__ import annotations

import json
import subprocess

import pytest

from pymolt.adapters import python_runner as pr
from pymolt.adapters.python_runner import Mount, RunnerSpec
from pymolt.delta import compute_deltas
from pymolt.delta import engine as engine_mod
from pymolt.delta import envs as envs_mod
from pymolt.delta.engine import ProbeRun
from pymolt.delta.models import Interpreter
from pymolt.delta.probe import MARK

from ._fakes import missing, res, shape, step

POK = "POSITIONAL_OR_KEYWORD"
SPEC = RunnerSpec(name="bench", container_id="abc123", image="python:3.6", image_id="sha256:1",
                  mounts=[Mount(source="/host/proj", destination="/work")])


def _cp(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


# ── resolve_old ─────────────────────────────────────────────────────────────────────────


def _config(monkeypatch, runner=SPEC):
    from pymolt.ingestion.config import EnvConfig, ToolChoice

    cfg = EnvConfig(selected_tool=ToolChoice.CONTAINER, container_id="bench", base_python="3.6",
                    runner=runner)
    monkeypatch.setattr(envs_mod, "_config", lambda root: cfg)
    monkeypatch.setattr(envs_mod, "_baseline_slot", lambda root: None)
    monkeypatch.setattr(envs_mod, "_baseline_trace", lambda root: None)
    return cfg


def test_resolve_old_prefers_the_running_container(monkeypatch, tmp_path):
    _config(monkeypatch)
    monkeypatch.setattr(envs_mod, "runner_available", lambda spec: (True, None))
    calls = []

    def fake_run_in(spec, argv, **kw):
        calls.append((spec, argv[:3]))
        return _cp(json.dumps({"python": "3.6.15", "versions": {"pandas": "0.25.3"}}))

    monkeypatch.setattr(envs_mod, "run_in", fake_run_in)
    monkeypatch.setattr(envs_mod, "throwaway_env",
                        lambda *a, **k: pytest.fail("fell back to the host"))
    old = envs_mod.resolve_old(tmp_path, [envs_mod.Want("pandas", "0.25.3")])
    assert old.available and old.source == "container" and old.runner == SPEC
    assert old.executable == "python" and old.python == "3.6.15"
    assert calls[0][0] == SPEC


def test_resolve_old_uses_the_baseline_slots_runner_first(monkeypatch, tmp_path):
    from types import SimpleNamespace

    _config(monkeypatch)
    slot_spec = SPEC.model_copy(update={"name": "slotbox", "container_id": "zzz"})
    monkeypatch.setattr(envs_mod, "_baseline_slot", lambda root: SimpleNamespace(runner=slot_spec))
    assert [r.name for r in envs_mod._runners(tmp_path, envs_mod._config(tmp_path))] == [
        "slotbox", "bench"]


def test_config_without_a_runner_is_rebuilt_from_the_container_name(monkeypatch, tmp_path):
    _config(monkeypatch, runner=None)
    monkeypatch.setattr(pr, "inspect_container", lambda name: SPEC if name == "bench" else None)
    assert envs_mod._runners(tmp_path, envs_mod._config(tmp_path)) == [SPEC]


def test_stopped_container_is_inconclusive_and_says_docker_start(monkeypatch, tmp_path):
    _config(monkeypatch)
    monkeypatch.setattr(pr, "_docker", lambda *a, **k: _cp("false\n"))
    monkeypatch.setattr(envs_mod, "throwaway_env", lambda *a, **k: (None, "no uv", None))
    monkeypatch.setattr("pymolt.ingestion.fallback_compiler.find_python_interpreter",
                        lambda *a, **k: None)
    old = envs_mod.resolve_old(tmp_path, [envs_mod.Want("pandas", "0.25.3")])
    assert not old.available
    assert "container bench is not running" in old.reason
    assert "docker start bench" in old.reason


def test_same_minor():
    assert envs_mod.same_minor("3.6.15", "3.6")
    assert not envs_mod.same_minor("3.6.15", "3.7.1")
    assert not envs_mod.same_minor(None, "3.6")


# ── run_probe through the runner ────────────────────────────────────────────────────────


def _probe_stdout(**meta):
    lines = [{"meta": {"python": "3.6.15", "site_dirs": ["/sp"], **meta}},
             {"path": "pkg.a", "result": {"exists": True, "steps": []}}, {"done": True}]
    return "".join(MARK + json.dumps(p) + "\n" for p in lines)


def test_run_probe_in_container_sends_request_on_stdin_with_project_cwd(monkeypatch, tmp_path):
    seen = {}

    def fake_run_in(spec, argv, *, stdin=None, cwd=None, timeout=60.0, env=None):
        if argv[:3] == ["python", "-B", "-c"] and "pymolt-delta" in argv[3]:
            seen.update(spec=spec, argv=argv, stdin=stdin, cwd=cwd)
            return _cp(_probe_stdout())
        return _cp(json.dumps({"interpreter:/usr/bin/python3.6": 1, "site:/sp": 2}))  # stamps

    monkeypatch.setattr(engine_mod, "run_in", fake_run_in)
    monkeypatch.setattr(engine_mod, "to_container", lambda spec, p: "/work")
    run = engine_mod.run_probe("python", tmp_path, {"paths": ["pkg.a"]}, runner=SPEC)
    assert run.ok and "pkg.a" in run.results
    assert seen["spec"] == SPEC and seen["cwd"] == "/work"
    assert json.loads(seen["stdin"]) == {"paths": ["pkg.a"]}
    # the second call is served from the cache, validated by stamps read in the container
    again = engine_mod.run_probe("python", tmp_path, {"paths": ["pkg.a"]}, runner=SPEC)
    assert again.from_cache


def test_probe_cache_key_includes_container_and_image(tmp_path):
    key = engine_mod._cache_file
    base = key(tmp_path, "python", {"a": 1}, SPEC)
    assert base != key(tmp_path, "python", {"a": 1}, SPEC.model_copy(update={"container_id": "x"}))
    assert base != key(tmp_path, "python", {"a": 1}, SPEC.model_copy(update={"image_id": "y"}))
    assert base != key(tmp_path, "python", {"a": 1}, None)


def test_cached_answer_is_dropped_when_the_stamps_move(monkeypatch, tmp_path):
    stamps = {"v": 1}

    def fake_run_in(spec, argv, *, stdin=None, **kw):
        if "pymolt-delta" in argv[3]:
            return _cp(_probe_stdout())
        return _cp(json.dumps({"s": stamps["v"]}))

    monkeypatch.setattr(engine_mod, "run_in", fake_run_in)
    assert not engine_mod.run_probe("python", tmp_path, {}, runner=SPEC).from_cache
    assert engine_mod.run_probe("python", tmp_path, {}, runner=SPEC).from_cache
    stamps["v"] = 2
    assert not engine_mod.run_probe("python", tmp_path, {}, runner=SPEC).from_cache


def test_probe_timeout_in_container_is_a_failure_not_a_raise(monkeypatch, tmp_path):
    def fake_run_in(spec, argv, **kw):
        raise subprocess.TimeoutExpired(argv, 1)

    monkeypatch.setattr(engine_mod, "run_in", fake_run_in)
    run = engine_mod.run_probe("python", tmp_path, {}, runner=SPEC, use_cache=False)
    assert not run.ok and "timed out" in run.failure and "bench" in run.failure


# ── the engine end to end ───────────────────────────────────────────────────────────────


def test_engine_probes_the_old_side_in_the_container(monkeypatch, tmp_path):
    old = Interpreter(executable="python", source="container", python="3.6.15", runner=SPEC)
    new = Interpreter(executable="/envs/new/bin/python", source="target-env")
    monkeypatch.setattr(engine_mod.envs_mod, "resolve_old", lambda *a, **k: old)
    monkeypatch.setattr(engine_mod.envs_mod, "resolve_new", lambda *a, **k: new)
    runners = {}

    def fake_probe(executable, root, request, *, use_cache=True, runner=None, timeout=0):
        side = "old" if runner is not None else "new"
        runners[side] = runner
        dist = {"version": "1.0" if side == "old" else "2.0", "roots": ["pkg"]}
        results = {"pkg.gone": (res("pkg", step("gone", "pkg.gone", shape=shape(("x", POK, None))))
                                if side == "old" else missing("pkg", "gone"))}
        return ProbeRun(meta={"python": "3.6.15" if side == "old" else "3.12.0",
                              "dists": {"pkg": dist}}, results=results)

    monkeypatch.setattr(engine_mod, "run_probe", fake_probe)
    (report,) = compute_deltas(tmp_path, [("pkg", "1.0", "2.0")], symbols=["pkg.gone"])
    assert runners == {"old": SPEC, "new": None}
    assert report.quality == "complete" and report.old_python == "3.6.15"
    assert [(f.kind, f.symbol) for f in report.facts] == [("REMOVED", "pkg.gone")]


def test_emulated_container_gets_a_longer_per_path_timeout(monkeypatch, tmp_path):
    spec = SPEC.model_copy(update={"emulated": True})
    old = Interpreter(executable="python", source="container", runner=spec)
    monkeypatch.setattr(engine_mod.envs_mod, "resolve_old", lambda *a, **k: old)
    monkeypatch.setattr(engine_mod.envs_mod, "resolve_new",
                        lambda *a, **k: Interpreter(executable="/n/python", source="target-env"))
    seen = {}

    def fake_probe(executable, root, request, *, use_cache=True, runner=None, timeout=0):
        seen["old" if runner else "new"] = request["per_path_timeout"]
        return ProbeRun(meta={"python": "3.6.1", "dists": {}}, results={})

    monkeypatch.setattr(engine_mod, "run_probe", fake_probe)
    compute_deltas(tmp_path, [("pkg", "1.0", "2.0")], symbols=["pkg.gone"])
    assert seen == {"old": engine_mod.PER_PATH_TIMEOUT * 4, "new": engine_mod.PER_PATH_TIMEOUT}


def test_import_errors_are_copied_and_never_reported_as_removed(monkeypatch, tmp_path):
    old = Interpreter(executable="/o/python", source="x")
    new = Interpreter(executable="/n/python", source="y")
    monkeypatch.setattr(engine_mod.envs_mod, "resolve_old", lambda *a, **k: old)
    monkeypatch.setattr(engine_mod.envs_mod, "resolve_new", lambda *a, **k: new)
    ie = {"module": "pkg.sub", "error": "ImportError: cannot import name 'x'",
          "origin": "site-packages:pkg", "cause": None}

    def fake_probe(executable, root, request, *, use_cache=True, runner=None, timeout=0):
        side = "old" if executable == "/o/python" else "new"
        dist = {"version": "1.0" if side == "old" else "2.0", "roots": ["pkg"]}
        if side == "old":
            old_step = step("f", "pkg.sub.f", shape=shape(("x", POK, None)))
            results = {"pkg.sub.f": res("pkg.sub", old_step)}
        else:
            results = {"pkg.sub.f": {"exists": False, "steps": [], "module": None,
                                     "error": "import raised: ImportError", "import_errors": [ie]}}
        return ProbeRun(meta={"python": "3.11.0", "dists": {"pkg": dist}}, results=results)

    monkeypatch.setattr(engine_mod, "run_probe", fake_probe)
    (report,) = compute_deltas(tmp_path, [("pkg", "1.0", "2.0")], symbols=["pkg.sub.f"])
    assert not any(f.kind == "REMOVED" for f in report.facts), report.facts
    assert report.import_errors == [{**ie, "side": "new"}]
    assert report.model_dump()["import_errors"][0]["side"] == "new"


# ── the probe itself (runs on the host here) ────────────────────────────────────────────


def test_probe_records_import_errors_not_missing_names(tmp_path):
    pkg = tmp_path / "boompkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "bad.py").write_text("import not_a_real_module_xyz\n")
    (pkg / "worse.py").write_text("raise RuntimeError('boom')\n")
    (pkg / "good.py").write_text("def f():\n    pass\n")
    import sys

    from pymolt.delta import probe

    sys.path.insert(0, str(tmp_path))
    try:
        bad = probe.resolve("boompkg.bad.f")
        worse = probe.resolve("boompkg.worse.f")
        gone = probe.resolve("boompkg.nothere.f")
    finally:
        sys.path.remove(str(tmp_path))
    # a dependency of the module is missing: an import error, with the cause named
    assert bad["import_errors"][0]["module"] == "boompkg.bad"
    assert bad["import_errors"][0]["cause"] == "not_a_real_module_xyz"
    assert worse["import_errors"][0]["error"].startswith("RuntimeError")
    # the module itself is absent: simply missing, not an import error
    assert "import_errors" not in gone
