"""Tests for pymolt.adapters.github_forks — GitHub fork-network adapter (mocked httpx)."""

from __future__ import annotations

import httpx

from pymolt.adapters import github_forks as forks_mod
from pymolt.adapters.github_forks import (
    RawFork,
    compare_fork,
    fetch_default_branch,
    fetch_manifest_deps,
    fetch_recent_forks,
)


class _FakeResp:
    def __init__(self, payload=None, status=200, text=""):
        self._payload = payload
        self.status_code = status
        self.text = text

    def json(self):
        return self._payload


def _node(name_with_owner, pushed_at, stars=1, branch="main"):
    return {
        "nameWithOwner": name_with_owner,
        "url": f"https://github.com/{name_with_owner}",
        "pushedAt": pushed_at,
        "stargazerCount": stars,
        "defaultBranchRef": {"name": branch},
    }


def _forks_page(nodes, has_next=False, end_cursor=None):
    return _FakeResp(
        {
            "data": {
                "repository": {
                    "forks": {
                        "pageInfo": {"hasNextPage": has_next, "endCursor": end_cursor},
                        "nodes": nodes,
                    }
                }
            }
        }
    )


def test_fetch_recent_forks_stops_paging_below_cutoff(monkeypatch):
    calls = []

    page1 = _forks_page(
        [
            _node("alice/repo", "2025-06-01T00:00:00Z"),
            _node("bob/repo", "2025-05-01T00:00:00Z"),
        ],
        has_next=True,
        end_cursor="CURSOR1",
    )
    # Second page contains an entry below cutoff — paging must stop there and
    # that entry (and anything after it) must not be included.
    page2 = _forks_page(
        [
            _node("carol/repo", "2025-04-01T00:00:00Z"),
            _node("dave/repo", "2025-01-01T00:00:00Z"),  # below cutoff
        ],
        has_next=True,
        end_cursor="CURSOR2",
    )

    def fake_post(url, json, headers, timeout):
        calls.append(json)
        assert url == forks_mod.GITHUB_GRAPHQL_URL
        if json["variables"]["cursor"] is None:
            return page1
        return page2

    monkeypatch.setattr(forks_mod.httpx, "post", fake_post)

    result = fetch_recent_forks(
        "base/repo", cutoff_iso="2025-03-01T00:00:00Z", token=None
    )

    names = [f.name_with_owner for f in result]
    assert names == ["alice/repo", "bob/repo", "carol/repo"]
    assert all(isinstance(f, RawFork) for f in result)
    # only two requests were made (paging stopped once cutoff was hit)
    assert len(calls) == 2


def test_fetch_recent_forks_sends_bearer_token_when_present(monkeypatch):
    captured = {}

    def fake_post(url, json, headers, timeout):
        captured["headers"] = headers
        return _forks_page([])

    monkeypatch.setattr(forks_mod.httpx, "post", fake_post)

    fetch_recent_forks(
        "base/repo", cutoff_iso="2025-01-01T00:00:00Z", token="secret-token"
    )

    assert captured["headers"]["Authorization"] == "Bearer secret-token"
    assert captured["headers"]["Accept"] == "application/vnd.github+json"


def test_fetch_recent_forks_degrades_to_empty_list_on_http_error(monkeypatch):
    def fake_post(url, json, headers, timeout):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(forks_mod.httpx, "post", fake_post)

    assert fetch_recent_forks("base/repo", cutoff_iso="2025-01-01T00:00:00Z", token=None) == []


def test_fetch_default_branch_reads_repo_json(monkeypatch):
    captured = {}

    def fake_get(url, headers, timeout):
        captured["url"] = url
        return _FakeResp({"default_branch": "trunk"})

    monkeypatch.setattr(forks_mod.httpx, "get", fake_get)

    assert fetch_default_branch("base/repo", token="tok") == "trunk"
    assert captured["url"] == f"{forks_mod.GITHUB_API_URL}/repos/base/repo"


def test_fetch_default_branch_degrades_to_none(monkeypatch):
    monkeypatch.setattr(
        forks_mod.httpx, "get", lambda url, headers, timeout: _FakeResp(status=404)
    )

    assert fetch_default_branch("base/repo", token=None) is None


def test_compare_fork_returns_ahead_behind(monkeypatch):
    captured = {}

    def fake_get(url, headers, timeout):
        captured["url"] = url
        captured["headers"] = headers
        return _FakeResp({"ahead_by": 3, "behind_by": 7})

    monkeypatch.setattr(forks_mod.httpx, "get", fake_get)

    result = compare_fork("base/repo", "main", "alice/repo", "feature", token="tok")

    assert result == {"ahead_by": 3, "behind_by": 7}
    assert captured["url"] == (
        f"{forks_mod.GITHUB_API_URL}/repos/base/repo/compare/main...alice:feature"
    )
    assert captured["headers"]["Authorization"] == "Bearer tok"


def test_compare_fork_degrades_to_none_on_http_error(monkeypatch):
    def fake_get(url, headers, timeout):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(forks_mod.httpx, "get", fake_get)

    assert compare_fork("base/repo", "main", "alice/repo", "feature", token=None) is None


def test_compare_fork_degrades_to_none_on_non_200(monkeypatch):
    monkeypatch.setattr(
        forks_mod.httpx, "get", lambda url, headers, timeout: _FakeResp(status=404)
    )

    assert compare_fork("base/repo", "main", "alice/repo", "feature", token=None) is None


def test_fetch_manifest_deps_falls_through_to_second_path(monkeypatch):
    requested = []

    def fake_get(url, headers, timeout):
        requested.append(url)
        if url.endswith("requirements.txt"):
            return _FakeResp(status=404)
        if url.endswith("setup.py"):
            return _FakeResp(status=200, text="flask==2.0.3\n\nrequests>=2.0\n")
        return _FakeResp(status=404)

    monkeypatch.setattr(forks_mod.httpx, "get", fake_get)

    result = fetch_manifest_deps("alice/repo", "main", token=None)

    assert result == ["flask==2.0.3", "requests>=2.0"]
    assert requested[0].endswith("requirements.txt")
    assert requested[1].endswith("setup.py")
    assert len(requested) == 2


def test_fetch_manifest_deps_returns_none_when_nothing_reachable(monkeypatch):
    def fake_get(url, headers, timeout):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(forks_mod.httpx, "get", fake_get)

    assert fetch_manifest_deps("alice/repo", "main", token=None) is None
