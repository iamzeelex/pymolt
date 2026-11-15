"""compute_deltas against real interpreters: a tiny fake package in two versions, each
installed (offline, from a locally built wheel) into its own uv venv. Skipped without uv."""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from pymolt.delta import TouchedSymbol, compute_deltas
from pymolt.delta.provider import LocalDeltaProvider
from pymolt.ingestion.config import EnvConfig
from pymolt.verify.models import CaptureMode, ContractSlot, ContractState

pytestmark = pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not on PATH")

V1 = {
    "fakepkg/__init__.py": '''\
import warnings

from fakepkg._impl import Widget, helper
from fakepkg.core import Engine

VERSION = "1.0"
alias = helper


def old_func(x):
    """Double x.

    .. deprecated:: 1.0
        Use :func:`new_func` instead.
    """
    warnings.warn("old_func is deprecated; use fakepkg.new_func instead", DeprecationWarning)
    return new_func(x)


def new_func(x):
    return x * 2


def compute(data, scale=1, mode="fast"):
    return data * scale
''',
    "fakepkg/_impl.py": '''\
def helper(a, b):
    return a + b


class Widget:
    def __init__(self, size, color="red"):
        self.size = size
''',
    "fakepkg/core.py": '''\
class Base:
    def run(self):
        return "ran"


class Engine(Base):
    pass
''',
}

V2 = {
    "fakepkg/__init__.py": '''\
import warnings

from fakepkg._impl import Widget, helper
from fakepkg.core import Engine


def __getattr__(name):
    if name == "VERSION":
        warnings.warn("VERSION is deprecated; use importlib.metadata instead",
                      DeprecationWarning, stacklevel=2)
        return "2.0"
    raise AttributeError(name)


def new_func(x):
    return x * 2


def compute(data, scale=2, *, mode="fast"):
    return data * scale
''',
    "fakepkg/_impl.py": '''\
def helper(a, b):
    return a + b


class Widget:
    def __init__(self, size, color="red", *, depth):
        self.size = size
''',
    "fakepkg/core.py": '''\
class Runner:
    def run(self):
        return "ran"


class Engine(Runner):
    pass
''',
}


def _record_line(name: str, data: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
    return f"{name},sha256={digest},{len(data)}"


def _wheel(dest: Path, version: str, files: dict[str, str]) -> Path:
    info = f"fakepkg-{version}.dist-info"
    payload = {name: text.encode() for name, text in files.items()}
    payload[f"{info}/METADATA"] = (f"Metadata-Version: 2.1\nName: fakepkg\nVersion: {version}\n"
                                   ).encode()
    payload[f"{info}/WHEEL"] = (b"Wheel-Version: 1.0\nGenerator: pymolt-tests\n"
                                b"Root-Is-Purelib: true\nTag: py3-none-any\n")
    payload[f"{info}/top_level.txt"] = b"fakepkg\n"
    record = [_record_line(n, d) for n, d in payload.items()] + [f"{info}/RECORD,,"]
    payload[f"{info}/RECORD"] = ("\n".join(record) + "\n").encode()
    path = dest / f"fakepkg-{version}-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in payload.items():
            zf.writestr(name, data)
    return path


def _venv(dest: Path, wheel: Path) -> Path:
    subprocess.run(["uv", "venv", "-q", "-p", sys.executable, str(dest)], check=True)
    python = dest / "bin" / "python"
    subprocess.run(["uv", "pip", "install", "-q", "--offline", "-p", str(python), str(wheel)],
                   check=True)
    return python


@pytest.fixture(scope="module")
def envs(tmp_path_factory):
    base = tmp_path_factory.mktemp("fakepkg")
    old = _venv(base / "old", _wheel(base, "1.0", V1))
    new = _venv(base / "new", _wheel(base, "2.0", V2))
    return {"old": old, "new": new, "base": base}


def _project(root: Path, envs: dict) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / ".pymolt").mkdir(exist_ok=True)
    EnvConfig(target_env_path=str(envs["new"].parent.parent)).save(
        root / ".pymolt" / "env_config.json")
    trace = root / ".pymolt" / "baseline.jsonl"
    trace.write_text("", encoding="utf-8")
    Path(str(trace) + ".meta.json").write_text(json.dumps(
        {"processes": [{"executable": str(envs["old"]), "counters": {"events_written": 3}}]}))
    ContractState(baseline=ContractSlot(
        trace_path=str(trace), captured_at="2026-11-15T00:00:00+00:00",
        mode=CaptureMode.TEST_SUITE,
    )).save(root / ".pymolt" / "contract_state.json")


SYMBOLS = ["fakepkg.old_func", "fakepkg.alias", "fakepkg.compute", "fakepkg.Widget",
           "fakepkg.core.Engine.run", "fakepkg.VERSION", "fakepkg.new_func",
           TouchedSymbol(path="fakepkg.core.Base.run", origin="trace")]


def _facts(report):
    return {(f.kind, f.symbol, f.param): f for f in report.facts}


def test_real_interpreters_every_fact_kind(tmp_path, envs):
    _project(tmp_path, envs)
    (report,) = compute_deltas(tmp_path, [("fakepkg", "1.0", "2.0")], symbols=SYMBOLS)

    assert report.quality == "complete", report.notes
    assert report.old_python and report.new_python
    facts = _facts(report)

    removed = facts[("REMOVED", "fakepkg.old_func", None)]
    assert removed.successor == "fakepkg.new_func"      # from the old docstring, qualified
    assert removed.deprecated_in_old is True
    assert removed.detail["successor_same_signature"] is True

    moved = facts[("MOVED", "fakepkg.alias", None)]
    assert moved.successor == "fakepkg._impl.helper"   # the same object, still importable

    # base-class re-home: the trace's defining name moved, the public spelling works
    base_move = facts[("MOVED", "fakepkg.core.Base.run", None)]
    assert base_move.successor == "fakepkg.core.Runner.run"
    assert not any(k == "REMOVED" and "Base" in s for k, s, _ in facts)
    bases = facts[("SIGNATURE", "fakepkg.core.Engine", None)]
    assert "bases" in bases.evidence

    assert facts[("DEFAULT", "fakepkg.compute", "scale")].new == "2"
    assert facts[("SIGNATURE", "fakepkg.compute", "mode")].new == "KEYWORD_ONLY"
    widget = facts[("SIGNATURE", "fakepkg._impl.Widget", "depth")]
    assert widget.aliases == ["fakepkg.Widget"]

    deprecated = facts[("DEPRECATED", "fakepkg.VERSION", None)]
    assert any(e.startswith("deprecated-in-new:getattr") for e in deprecated.evidence)
    assert ("UNCHANGED", "fakepkg.new_func", None) in facts


def test_project_symbols_and_the_provider_rewrite(tmp_path, envs):
    _project(tmp_path, envs)
    (tmp_path / "app.py").write_text(
        "from fakepkg import old_func, alias\n\nold_func(1)\nalias(1, 2)\n", encoding="utf-8")
    bundle = LocalDeltaProvider(tmp_path).fetch_bundle(
        [_mig("fakepkg", "1.0", "2.0")])["fakepkg"]
    patterns = {(p.kind, p.old_qualname, p.new_qualname) for p in bundle.patterns}
    assert ("rename-call", "fakepkg.old_func", "fakepkg.new_func") in patterns
    assert ("rename-call", "fakepkg.alias", "fakepkg._impl.helper") in patterns
    assert {i.kind for i in bundle.impacts} >= {"REMOVED", "MOVED"}


def test_throwaway_target_env_from_the_target_manifest(tmp_path, envs, monkeypatch):
    monkeypatch.setenv("UV_OFFLINE", "1")  # the wheel is local; never reach an index
    _project(tmp_path, envs)
    EnvConfig(target_python=f"{sys.version_info[0]}.{sys.version_info[1]}").save(
        tmp_path / ".pymolt" / "env_config.json")
    wheel = envs["base"] / "fakepkg-2.0-py3-none-any.whl"
    (tmp_path / "requirements-target.txt").write_text(f"fakepkg @ {wheel.as_uri()}\n")
    (report,) = compute_deltas(tmp_path, [("fakepkg", "1.0", "2.0")],
                               symbols=["fakepkg.old_func"])
    assert report.quality == "complete", report.notes
    assert any("throwaway environment from requirements-target.txt" in n for n in report.notes)
    assert list((tmp_path / ".pymolt" / "cache" / "envs").iterdir())
    assert [f.kind for f in report.facts] == ["REMOVED"]


def test_unbuildable_new_env_is_inconclusive(tmp_path, envs, monkeypatch):
    monkeypatch.setenv("UV_OFFLINE", "1")
    _project(tmp_path, envs)
    EnvConfig(target_python="3.12").save(tmp_path / ".pymolt" / "env_config.json")
    (tmp_path / "requirements-target.txt").write_text(
        "fakepkg @ file:///nonexistent/fakepkg-9.9-py3-none-any.whl\n")
    (report,) = compute_deltas(tmp_path, [("fakepkg", "1.0", "2.0")],
                               symbols=["fakepkg.old_func"])
    assert report.quality == "inconclusive"
    assert report.facts == []
    note = next(n for n in report.notes if n.startswith("no new interpreter"))
    assert "failed" in note  # the uv error is carried, not swallowed


def _mig(name, old, new):
    from pymolt.codemods.client import DependencyMigration

    return DependencyMigration(name, old, new)
