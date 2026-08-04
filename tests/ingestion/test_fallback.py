"""pymolt.ingestion.fallback_compiler — tested at the adapter boundary.

Only the process boundary is faked (raw ``subprocess.run`` and the
``run_command`` adapter, plus ``shutil.which`` PATH probes); every fake
performs the real side effect the command would have — a venv appearing on
disk, pip-compile writing its ``--output-file``. The module's own logic
(cache-hit checks, temp-file lifecycle, container path mapping, error
messages) runs for real. No internal function of the module is mocked.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import pymolt.ingestion.fallback_compiler as fc
from pymolt.ingestion.fallback_compiler import (
    compile_legacy,
    find_python_interpreter,
    find_running_containers_for_project,
    get_or_create_legacy_env,
    list_running_containers,
)


def _proc(rc: int = 0, out: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], rc, out, "")


def _is_version_probe(cmd: list) -> bool:
    return len(cmd) == 3 and cmd[1] == "-c" and "sys.version_info" in cmd[2]


class _Boundary:
    """A fake process boundary: routes commands, performs their side effects."""

    def __init__(self, interpreter_versions: dict[str, str] | None = None):
        self.interpreter_versions = interpreter_versions or {}
        self.subprocess_calls: list[list] = []
        self.run_command_calls: list[dict] = []

    # -- raw subprocess.run ------------------------------------------------
    def subprocess_run(self, cmd, **kwargs):
        self.subprocess_calls.append(list(cmd))
        if _is_version_probe(cmd):
            version = self.interpreter_versions.get(str(cmd[0]))
            return _proc(0, f"{version}\n") if version else _proc(1)
        if len(cmd) >= 3 and cmd[1] == "-m" and cmd[2] == "venv":
            env_dir = Path(cmd[3])
            (env_dir / "bin").mkdir(parents=True)
            (env_dir / "bin" / "pip").touch()          # venv ships pip
            return _proc(0)
        if "install" in cmd:
            pip_exe = Path(cmd[0])
            if any("pip-tools" in str(a) for a in cmd):
                (pip_exe.parent / "pip-compile").touch()
            return _proc(0)
        raise AssertionError(f"unrouted subprocess call: {cmd}")

    # -- the run_command adapter -------------------------------------------
    def run_command(self, cmd, **kwargs):
        self.run_command_calls.append({"cmd": list(cmd), **kwargs})
        if "--output-file" in cmd:
            out_path = Path(cmd[cmd.index("--output-file") + 1])
            out_path.write_text("flask==2.2.5\n    # via -r requirements.txt\n",
                                encoding="utf-8")
            return _proc(0)
        raise AssertionError(f"unrouted run_command call: {cmd}")

    def install(self, monkeypatch):
        monkeypatch.setattr(fc.subprocess, "run", self.subprocess_run)
        monkeypatch.setattr(fc, "run_command", self.run_command)
        return self


# ── find_python_interpreter (PATH / local venv / pyenv probes) ────────────────

def test_find_interpreter_on_path(monkeypatch, tmp_path):
    b = _Boundary({"/usr/bin/python3.6": "3.6"}).install(monkeypatch)
    monkeypatch.setattr(fc.shutil, "which",
                        lambda c: "/usr/bin/python3.6" if "3.6" in c else None)
    monkeypatch.setattr(fc.Path, "home", classmethod(lambda cls: tmp_path))

    assert find_python_interpreter("3.6.1") == Path("/usr/bin/python3.6")
    assert any(_is_version_probe(c) for c in b.subprocess_calls)


def test_find_interpreter_version_mismatch_returns_none(monkeypatch, tmp_path):
    # Every candidate resolves, but each probe answers 3.12 — never a false match.
    _Boundary({}).install(monkeypatch)  # unknown paths probe as failures
    monkeypatch.setattr(fc.shutil, "which", lambda c: "/usr/bin/python3")
    monkeypatch.setattr(fc.Path, "home", classmethod(lambda cls: tmp_path))

    assert find_python_interpreter("3.6.1") is None


def test_find_interpreter_prefers_project_local_venv(monkeypatch, tmp_path):
    venv_python = tmp_path / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.touch()
    _Boundary({str(venv_python): "3.6"}).install(monkeypatch)
    monkeypatch.setattr(fc.Path, "home", classmethod(lambda cls: tmp_path))

    assert find_python_interpreter("3.6.1", project_dir=tmp_path) == venv_python


def test_find_interpreter_via_pyenv(monkeypatch, tmp_path):
    pyenv_python = tmp_path / ".pyenv" / "versions" / "3.6.15" / "bin" / "python"
    pyenv_python.parent.mkdir(parents=True)
    pyenv_python.touch()
    _Boundary({str(pyenv_python): "3.6"}).install(monkeypatch)
    monkeypatch.setattr(fc.Path, "home", classmethod(lambda cls: tmp_path))

    assert find_python_interpreter("3.6.1") == pyenv_python


# ── get_or_create_legacy_env (real cache-hit logic) ───────────────────────────

def test_legacy_env_created_once_then_reused(monkeypatch, tmp_path):
    b = _Boundary().install(monkeypatch)

    env_dir = get_or_create_legacy_env(Path("/fake/python3.6"), "3.6.1",
                                       cache_root=tmp_path)
    assert env_dir == (tmp_path / ".pymolt" / "cache" / "legacy_env_3_6").resolve()
    assert (env_dir / "bin" / "pip-compile").is_file()
    # venv creation + pip upgrade + pip-tools install crossed the boundary
    assert len(b.subprocess_calls) == 3

    again = get_or_create_legacy_env(Path("/fake/python3.6"), "3.6.1",
                                     cache_root=tmp_path)
    assert again == env_dir
    assert len(b.subprocess_calls) == 3      # cache hit: no new process calls


# ── compile_legacy, local path (end to end through the real function) ─────────

def test_compile_legacy_local_end_to_end(monkeypatch, tmp_path):
    manifest = tmp_path / "requirements.txt"
    manifest.write_text("flask\n", encoding="utf-8")
    b = _Boundary({"/usr/bin/python3.6": "3.6"}).install(monkeypatch)
    monkeypatch.setattr(fc.shutil, "which",
                        lambda c: "/usr/bin/python3.6" if "3.6" in c else None)
    monkeypatch.setattr(fc.Path, "home", classmethod(lambda cls: tmp_path))

    output = compile_legacy(manifest, "3.6.1")

    assert output.startswith("flask==2.2.5")
    (call,) = b.run_command_calls
    pip_compile = Path(call["cmd"][0])
    assert pip_compile.name == "pip-compile"
    assert (tmp_path / ".pymolt" / "cache" / "legacy_env_3_6") in pip_compile.parents
    assert call["cwd"] == str(tmp_path)      # runs relative to the manifest dir
    # the temp --output-file is cleaned up after being read
    out_arg = call["cmd"][call["cmd"].index("--output-file") + 1]
    assert not Path(out_arg).exists()


def test_compile_legacy_missing_interpreter_message_contract(monkeypatch, tmp_path):
    """assess degrades on this exact wording (_is_missing_interpreter_error)."""
    manifest = tmp_path / "requirements.txt"
    manifest.write_text("flask\n", encoding="utf-8")
    _Boundary({}).install(monkeypatch)       # no interpreter probes succeed
    monkeypatch.setattr(fc.shutil, "which", lambda c: None)
    monkeypatch.setattr(fc.Path, "home", classmethod(lambda cls: tmp_path))

    with pytest.raises(ValueError, match="interpreter was not found in your PATH"):
        compile_legacy(manifest, "3.6.1")


# ── containers ────────────────────────────────────────────────────────────────

def _docker_boundary(monkeypatch, tmp_path, container_id="container_id_1234567890"):
    """Route docker ps/inspect/probe/test/exec with real side effects."""
    calls = {"subprocess": [], "run_command": []}

    def fake_run(cmd, **kwargs):
        calls["subprocess"].append(list(cmd))
        if cmd[:2] == ["docker", "ps"]:
            return _proc(0, container_id[:12] + "\n")
        if cmd[:2] == ["docker", "inspect"]:
            payload = [{
                "Id": container_id,
                "Name": "/test_container",
                "Mounts": [{"Type": "bind", "Source": str(tmp_path),
                            "Destination": "/workspace"}],
                "Config": {"WorkingDir": "/workspace"},
            }]
            return _proc(0, json.dumps(payload))
        if _is_version_probe(cmd[-3:]) or (cmd[0] == "docker" and "-c" in cmd):
            return _proc(0, "3.6\n")
        if cmd[0] == "docker" and "[" in cmd:      # test -f pip-compile
            return _proc(0)
        raise AssertionError(f"unrouted docker call: {cmd}")

    def fake_run_command(cmd, **kwargs):
        calls["run_command"].append(list(cmd))
        if "--output-file" in cmd:
            container_out = cmd[cmd.index("--output-file") + 1]
            rel = container_out.removeprefix("/workspace/")
            host_out = tmp_path / rel
            host_out.write_text("flask==2.2.5\n", encoding="utf-8")
            return _proc(0)
        raise AssertionError(f"unrouted run_command call: {cmd}")

    monkeypatch.setattr(fc.subprocess, "run", fake_run)
    monkeypatch.setattr(fc, "run_command", fake_run_command)
    monkeypatch.setattr(fc.shutil, "which", lambda c: "/usr/local/bin/docker")
    return calls


def test_find_running_containers_for_project(monkeypatch, tmp_path):
    _docker_boundary(monkeypatch, tmp_path)

    containers = find_running_containers_for_project(tmp_path, "3.6.1")

    assert len(containers) == 1
    assert containers[0]["id"] == "container_id_1234567890"[:12]
    assert containers[0]["name"] == "test_container"
    assert containers[0]["cwd"] == "/workspace"


def test_compile_legacy_in_container_end_to_end(monkeypatch, tmp_path):
    manifest = tmp_path / "requirements.txt"
    manifest.write_text("flask\n", encoding="utf-8")
    calls = _docker_boundary(monkeypatch, tmp_path)

    # matched by container *name* — the id-or-name contract of compile_legacy
    output = compile_legacy(manifest, "3.6.1", container_id="test_container")

    assert output == "flask==2.2.5\n"
    (compile_cmd,) = calls["run_command"]
    assert compile_cmd[:4] == ["docker", "exec", "-w", "/workspace"]
    assert any(a.endswith(".pymolt/cache/legacy_env_container_3_6/bin/pip-compile")
               for a in compile_cmd)


def test_compile_legacy_dead_container_message_contract(monkeypatch, tmp_path):
    """codemods/assess degrade on this exact wording (_is_missing_container_error)."""
    manifest = tmp_path / "requirements.txt"
    manifest.write_text("flask\n", encoding="utf-8")

    def no_containers(cmd, **kwargs):
        if cmd[:2] == ["docker", "ps"]:
            return _proc(0, "")              # nothing running
        raise AssertionError(f"unrouted docker call: {cmd}")

    monkeypatch.setattr(fc.subprocess, "run", no_containers)
    monkeypatch.setattr(fc.shutil, "which", lambda c: "/usr/local/bin/docker")

    with pytest.raises(ValueError, match="was not found running"):
        compile_legacy(manifest, "3.6.1", container_id="deadbeef0000")


def test_list_running_containers_without_docker(monkeypatch):
    monkeypatch.setattr(fc.shutil, "which", lambda c: None)
    assert list_running_containers() == []
