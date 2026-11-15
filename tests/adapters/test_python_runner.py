"""pymolt.adapters.python_runner with a mocked docker (one function: ``_docker``)."""

from __future__ import annotations

import json
import subprocess

from pymolt.adapters import python_runner as pr
from pymolt.adapters.python_runner import Mount, RunnerSpec

SPEC = RunnerSpec(name="bench", container_id="abc123", image="python:3.6", image_id="sha256:1",
                  mounts=[Mount(source="/host/proj", destination="/work"),
                          Mount(source="/host/proj/data", destination="/data")])


def _cp(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def test_path_mapping_both_ways_and_longest_mount_wins():
    assert pr.to_host(SPEC, "/work/pkg/a.py") == "/host/proj/pkg/a.py"
    assert pr.to_host(SPEC, "/work") == "/host/proj"
    assert pr.to_host(SPEC, "/data/x.csv") == "/host/proj/data/x.csv"
    assert pr.to_host(SPEC, "/workshop/a.py") is None  # not a path prefix
    assert pr.to_host(SPEC, "/usr/lib/python3.6/os.py") is None
    assert pr.to_container(SPEC, "/host/proj/pkg/a.py") == "/work/pkg/a.py"
    assert pr.to_container(SPEC, "/host/proj/data/x.csv") == "/data/x.csv"
    assert pr.to_container(SPEC, "/host/proj") == "/work"
    assert pr.to_container(SPEC, "/elsewhere/a.py") is None


def test_inspect_container_builds_the_spec(monkeypatch):
    inspect = [{"Id": "f" * 64, "Name": "/bench", "Image": "sha256:img", "Platform": "linux",
                "Config": {"Image": "python:3.6", "WorkingDir": "/work"},
                "Mounts": [{"Type": "bind", "Source": "/host/proj", "Destination": "/work"},
                           {"Type": "volume", "Source": "/var/vol", "Destination": "/v"}]}]

    def fake(args, **kw):
        if args[0] == "inspect":
            return _cp(json.dumps(inspect))
        assert args[:2] == ["image", "inspect"]
        return _cp("linux/amd64\n")

    monkeypatch.setattr(pr, "_docker", fake)
    monkeypatch.setattr(pr, "_host_arch", lambda: "arm64")
    spec = pr.inspect_container("bench")
    assert spec.name == "bench" and spec.container_id == "f" * 64
    assert spec.image == "python:3.6" and spec.image_id == "sha256:img"
    assert spec.platform == "linux/amd64" and spec.emulated is True
    assert spec.workdir == "/work"
    assert [m.destination for m in spec.mounts] == ["/work"]  # volumes are not mapped
    monkeypatch.setattr(pr, "_host_arch", lambda: "amd64")
    assert pr.inspect_container("bench").emulated is False


def test_inspect_container_is_none_when_docker_cannot_say(monkeypatch):
    monkeypatch.setattr(pr, "_docker", lambda *a, **k: _cp("", 1, "No such object"))
    assert pr.inspect_container("nope") is None

    def boom(*a, **k):
        raise FileNotFoundError("docker is not on PATH")

    monkeypatch.setattr(pr, "_docker", boom)
    assert pr.inspect_container("nope") is None
    assert pr.is_running("nope") is False


def test_runner_available_names_the_container_and_docker_start(monkeypatch):
    monkeypatch.setattr(pr, "_docker", lambda *a, **k: _cp("true\n"))
    assert pr.runner_available(SPEC) == (True, None)
    monkeypatch.setattr(pr, "_docker", lambda *a, **k: _cp("false\n"))
    ok, why = pr.runner_available(SPEC)
    assert not ok and "bench" in why and "docker start bench" in why
    monkeypatch.setattr(pr, "_docker", lambda *a, **k: _cp("", 1))
    ok, why = pr.runner_available(SPEC)
    assert not ok and "no longer exists" in why


def test_run_in_is_docker_exec_with_stdin_cwd_and_emulated_timeout(monkeypatch):
    seen = {}

    def fake(args, *, stdin=None, timeout=None):
        seen.update(args=args, stdin=stdin, timeout=timeout)
        return _cp("ok")

    monkeypatch.setattr(pr, "_docker", fake)
    pr.run_in(SPEC, ["python", "-B", "-c", "x"], stdin="{}", cwd="/work", timeout=10)
    assert seen == {"args": ["exec", "-i", "-w", "/work", "abc123", "python", "-B", "-c", "x"],
                    "stdin": "{}", "timeout": 10}
    pr.run_in(SPEC.model_copy(update={"emulated": True}), ["python"], timeout=10)
    assert seen["timeout"] == 40 and seen["args"] == ["exec", "-i", "abc123", "python"]


def test_run_in_none_runs_locally():
    proc = pr.run_in(None, ["python3", "-c", "import sys; print(sys.stdin.read())"], stdin="hi")
    assert proc.stdout.strip() == "hi"


def test_container_is_running_delegates_to_the_runner_check(monkeypatch):
    from pymolt.verify import service

    monkeypatch.setattr(pr, "_docker", lambda *a, **k: _cp("true\n"))
    assert service.container_is_running("bench") is True
    monkeypatch.setattr(pr, "_docker", lambda *a, **k: _cp("false\n"))
    assert service.container_is_running("bench") is False


def test_inspect_container_reads_the_platform_from_the_manifest_descriptor(monkeypatch):
    inspect = [{"Id": "f" * 64, "Name": "/bench", "Image": "sha256:img",
                "ImageManifestDescriptor": {"platform": {"architecture": "amd64", "os": "linux"}},
                "Config": {"Image": "python:3.6"}, "Mounts": []}]

    def fake(args, **kw):
        assert args[0] == "inspect", "no image inspect needed when the descriptor says"
        return _cp(json.dumps(inspect))

    monkeypatch.setattr(pr, "_docker", fake)
    monkeypatch.setattr(pr, "_host_arch", lambda: "arm64")
    spec = pr.inspect_container("bench")
    assert spec.platform == "linux/amd64" and spec.emulated
