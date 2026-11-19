"""Fresh-install smoke test: the built wheel, installed with ``[mcp]``, serves MCP.

Builds the wheel, installs it into a throwaway venv with uv (which fetches the
dependencies, ``mcp<2`` among them), starts ``pymolt mcp`` on pipes and speaks
JSON-RPC to it: ``initialize`` must return a valid result, and ``tools/list`` the
tools the person granted (``--allow-run`` adds ``capture``). Slow and network-bound, so it
runs only with PYMOLT_SMOKE_INSTALL=1 (CI's package job sets it).
"""
from __future__ import annotations

import json
import os
import select
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("PYMOLT_SMOKE_INSTALL") != "1",
    reason="builds a wheel and a venv; set PYMOLT_SMOKE_INSTALL=1 to run",
)

REPO = Path(__file__).resolve().parents[2]
_TIMEOUT = 60


def _run(*argv: str, timeout: int = 600) -> None:
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    assert proc.returncode == 0, f"{' '.join(argv)}\n{proc.stdout}\n{proc.stderr}"


def _send(proc: subprocess.Popen, message: dict) -> None:
    proc.stdin.write((json.dumps(message) + "\n").encode())
    proc.stdin.flush()


def _receive(proc: subprocess.Popen) -> dict:
    ready, _, _ = select.select([proc.stdout], [], [], _TIMEOUT)
    assert ready, f"no answer within {_TIMEOUT}s; stderr: {proc.stderr.read1().decode()}"
    line = proc.stdout.readline()
    assert line, f"server closed stdout; stderr: {proc.stderr.read1().decode()}"
    return json.loads(line)


@pytest.mark.parametrize("allow_run", [False, True], ids=["default", "allow-run"])
def test_installed_wheel_answers_mcp_initialize(tmp_path, allow_run):
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not on PATH")
    dist, venv = tmp_path / "dist", tmp_path / "venv"
    _run(uv, "build", "--wheel", "--out-dir", str(dist), str(REPO))
    wheel = next(dist.glob("pymolt-*.whl"))
    _run(uv, "venv", str(venv), "--python", sys.executable)
    python = venv / ("Scripts" if os.name == "nt" else "bin") / "python"
    _run(uv, "pip", "install", "--python", str(python), f"pymolt[mcp] @ {wheel.as_uri()}")

    exe = python.parent / "pymolt"
    proc = subprocess.Popen(
        [str(exe), "mcp", "--root", str(tmp_path), *(["--allow-run"] if allow_run else [])],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=tmp_path,
    )
    try:
        _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "pymolt-smoke", "version": "0"},
        }})
        init = _receive(proc)
        assert init["id"] == 1 and "error" not in init, init
        result = init["result"]
        assert result["serverInfo"]["name"] == "pymolt"
        assert result["protocolVersion"]
        assert "tools" in result["capabilities"]

        _send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = {tool["name"] for tool in _receive(proc)["result"]["tools"]}
        granted = {"status", "scan", "setup", "assess", "plan", "verify", "apply", "rollback"}
        assert tools == (granted | {"capture"} if allow_run else granted)
    finally:
        proc.kill()
        proc.wait(timeout=10)
