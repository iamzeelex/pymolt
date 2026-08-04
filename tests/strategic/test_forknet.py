"""Tests for the fork-network triage service, cache, and CLI command.

No network: a ``FakeAdapter`` stands in for :mod:`pymolt.adapters.github_forks`
(whose real bodies are implemented elsewhere and shouldn't be relied on here).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from typer.testing import CliRunner

import pymolt.strategic.forknet.service as forknet_service
from pymolt.adapters.github_forks import RawFork
from pymolt.interfaces.cli import commands
from pymolt.strategic.forknet.models import ForkNetworkReport, PortedSignal
from pymolt.strategic.forknet.service import run_forknet

runner = CliRunner()


def _fork(name, *, months_old=0, stars=10, branch="master"):
    pushed = datetime.now(UTC) - timedelta(days=months_old * 30)
    return RawFork(
        name_with_owner=name,
        url=f"https://github.com/{name}",
        pushed_at=pushed.isoformat().replace("+00:00", "Z"),
        stars=stars,
        default_branch=branch,
    )


class FakeAdapter:
    """A fake `pymolt.adapters.github_forks` module for tests."""

    def __init__(self, forks, compares, manifests, token=None):
        self._forks = forks
        self._compares = compares
        self._manifests = manifests
        self._token = token
        self.compare_calls = []
        self.manifest_calls = []

    def resolve_token(self):
        return self._token

    def auth_mode(self, token):
        return "token" if token else "anonymous"

    def fetch_recent_forks(self, base_repo, *, cutoff_iso, token, max_pages=50):
        return self._forks

    def fetch_default_branch(self, base_repo, *, token):
        return "master"

    def compare_fork(self, base_repo, base_branch, fork_name_with_owner, fork_branch, *, token):
        self.compare_calls.append(fork_name_with_owner)
        return self._compares.get(fork_name_with_owner)

    def fetch_manifest_deps(self, fork_name_with_owner, branch, *, token):
        self.manifest_calls.append(fork_name_with_owner)
        return self._manifests.get(fork_name_with_owner)


def test_online_ranks_recent_ported_fork_above_stale_one(tmp_path):
    recent = _fork("alice/proj", months_old=0, stars=5)
    stale = _fork("bob/proj", months_old=30, stars=50)  # more stars, but ancient
    adapter = FakeAdapter(
        forks=[recent, stale],
        compares={
            "alice/proj": {"ahead_by": 12, "behind_by": 1},
            "bob/proj": {"ahead_by": 3, "behind_by": 100},
        },
        manifests={
            "alice/proj": ["torch>=2.0", "numpy"],
            "bob/proj": ["tensorflow==1.15"],
        },
    )

    report = run_forknet(
        "base/proj", cache_root=tmp_path, online=True, cutoff_months=18, top_n=25, adapter=adapter
    )

    assert isinstance(report, ForkNetworkReport)
    assert report.forks_considered == 2
    assert report.candidates[0].name_with_owner == "alice/proj"
    assert report.candidates[0].ported_signal == PortedSignal.CONFIRMED
    assert report.candidates[1].name_with_owner == "bob/proj"
    assert report.candidates[1].ported_signal == PortedSignal.UNKNOWN


def test_online_compared_count_limited_to_top_n(tmp_path):
    a = _fork("a/proj", stars=100)
    b = _fork("b/proj", stars=1)
    adapter = FakeAdapter(
        forks=[a, b],
        compares={
            "a/proj": {"ahead_by": 1, "behind_by": 0},
            "b/proj": {"ahead_by": 2, "behind_by": 0},
        },
        manifests={},
    )

    report = run_forknet(
        "base/proj", cache_root=tmp_path, online=True, cutoff_months=18, top_n=1, adapter=adapter
    )

    assert report.forks_considered == 2
    assert report.compared == 1
    # Both still appear in the report, even the uncompared one.
    names = {c.name_with_owner for c in report.candidates}
    assert names == {"a/proj", "b/proj"}
    uncompared = next(c for c in report.candidates if c.name_with_owner == "b/proj")
    assert uncompared.ahead_by is None
    assert uncompared.ported_signal == PortedSignal.UNKNOWN


def test_offline_miss_returns_empty_report_with_note(tmp_path):
    adapter = FakeAdapter(forks=[], compares={}, manifests={})

    report = run_forknet(
        "base/proj", cache_root=tmp_path, online=False, cutoff_months=18, top_n=25, adapter=adapter
    )

    assert report.candidates == []
    assert report.forks_considered == 0
    assert any("offline" in n and "--online" in n for n in report.notes)


def test_offline_hit_returns_cached_report(tmp_path):
    fork = _fork("alice/proj", stars=5)
    adapter = FakeAdapter(
        forks=[fork],
        compares={"alice/proj": {"ahead_by": 4, "behind_by": 0}},
        manifests={"alice/proj": ["torch"]},
    )

    online_report = run_forknet(
        "base/proj", cache_root=tmp_path, online=True, cutoff_months=18, top_n=25, adapter=adapter
    )

    # A second, offline call with the same key should return the cached
    # report without touching the adapter's network-backed calls again.
    adapter.compare_calls.clear()
    adapter.manifest_calls.clear()
    cached_report = run_forknet(
        "base/proj", cache_root=tmp_path, online=False, cutoff_months=18, top_n=25, adapter=adapter
    )

    assert adapter.compare_calls == []
    assert adapter.manifest_calls == []
    assert cached_report.base_repo == online_report.base_repo
    assert cached_report.forks_considered == online_report.forks_considered
    assert [c.name_with_owner for c in cached_report.candidates] == [
        c.name_with_owner for c in online_report.candidates
    ]


def test_cli_forks_json_smoke(monkeypatch):
    fake_report = ForkNetworkReport(
        base_repo="base/proj",
        generated_at=datetime.now(UTC),
        cutoff_months=18,
        forks_considered=1,
        compared=1,
        auth_mode="anonymous",
        candidates=[],
        notes=["anonymous GitHub rate limit (60/hr); set GITHUB_TOKEN for full triage"],
    )
    monkeypatch.setattr(forknet_service, "run_forknet", lambda *a, **k: fake_report)

    result = runner.invoke(commands.app, ["forks", "base/proj", "--json"])

    assert result.exit_code == 0
    assert "base/proj" in result.output
    assert "anonymous GitHub rate limit" in result.output


def test_cli_forks_table_renders(monkeypatch):
    candidate = SimpleNamespace(
        name_with_owner="alice/proj",
        pushed_at=datetime.now(UTC),
        stars=5,
        ahead_by=3,
        ported_signal=PortedSignal.CONFIRMED,
        score=6.5,
    )
    fake_report = SimpleNamespace(
        base_repo="base/proj",
        auth_mode="token",
        candidates=[candidate],
        notes=[],
        model_dump_json=lambda indent=2: "{}",
    )
    monkeypatch.setattr(forknet_service, "run_forknet", lambda *a, **k: fake_report)

    result = runner.invoke(commands.app, ["forks", "base/proj"])

    assert result.exit_code == 0
    assert "alice/proj" in result.output
    assert "base/proj" in result.output
