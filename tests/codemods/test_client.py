"""Tests for pymolt.codemods.client — Axiom Graph HTTP client (mocked httpx)."""

from __future__ import annotations

import httpx
import pytest

from pymolt.codemods import client as client_mod
from pymolt.codemods.client import (
    AxiomGraphClient,
    AxiomGraphError,
    DependencyMigration,
)


class _FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=None, response=None)


_PAYLOAD = {
    "results": [
        {
            "name": "flask",
            "from_version": "2.0.3",
            "to_version": "3.0.0",
            "codemods": [
                {
                    "old_qualname": "flask.helpers.safe_join",
                    "new_qualname": "werkzeug.utils.safe_join",
                    "kind": "rewrite-import",
                    "confidence": "high",
                    "evidence": ["data-flow + prose"],
                }
            ],
        }
    ]
}


def test_fetch_codemods_parses_patterns(monkeypatch):
    captured = {}

    def fake_post(url, json, timeout, headers=None):
        captured["url"] = url
        captured["json"] = json
        return _FakeResp(_PAYLOAD)

    monkeypatch.setattr(client_mod.httpx, "post", fake_post)

    client = AxiomGraphClient("http://svc:8000")
    out = client.fetch_codemods([DependencyMigration("flask", "2.0.3", "3.0.0")])

    assert captured["url"] == "http://svc:8000/codemods"
    assert "flask" in out
    assert out["flask"][0].new_qualname == "werkzeug.utils.safe_join"


def test_sends_bearer_token_from_env(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(
        client_mod.httpx, "post",
        lambda url, json, timeout, headers=None: captured.update(headers=headers)
        or _FakeResp(_PAYLOAD),
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))  # isolate saved-token file
    monkeypatch.setenv("PYMOLT_API_TOKEN", "tok-abc")
    AxiomGraphClient().fetch_codemods([DependencyMigration("flask", "2.0.3", "3.0.0")])
    assert captured["headers"]["Authorization"] == "Bearer tok-abc"


def test_no_auth_header_without_token(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(
        client_mod.httpx, "post",
        lambda url, json, timeout, headers=None: captured.update(headers=headers)
        or _FakeResp(_PAYLOAD),
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))  # no saved token here
    monkeypatch.delenv("PYMOLT_API_TOKEN", raising=False)
    AxiomGraphClient().fetch_codemods([DependencyMigration("flask", "2.0.3", "3.0.0")])
    assert "Authorization" not in captured["headers"]


def test_request_sends_versions_only_no_code(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        client_mod.httpx, "post",
        lambda url, json, timeout, headers=None: captured.update(json=json) or _FakeResp(_PAYLOAD),
    )
    AxiomGraphClient().fetch_codemods([DependencyMigration("flask", "2.0.3", "3.0.0")])
    dep = captured["json"]["dependencies"][0]
    # the contract: only versions, never source/code
    assert set(dep) == {"name", "from_version", "to_version", "use_git"}
    assert "code" not in dep and "source" not in dep


def test_empty_migrations_no_request(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("should not call the network for an empty batch")

    monkeypatch.setattr(client_mod.httpx, "post", boom)
    assert AxiomGraphClient().fetch_codemods([]) == {}


def test_connection_error_raises_axiom_error(monkeypatch):
    def fail(*a, **k):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(client_mod.httpx, "post", fail)
    with pytest.raises(AxiomGraphError):
        AxiomGraphClient().fetch_codemods([DependencyMigration("flask", "2.0.3", "3.0.0")])


def test_fetch_flat(monkeypatch):
    monkeypatch.setattr(
        client_mod.httpx, "post",
        lambda url, json, timeout, headers=None: _FakeResp(_PAYLOAD),
    )
    flat = AxiomGraphClient().fetch_flat([DependencyMigration("flask", "2.0.3", "3.0.0")])
    assert len(flat) == 1
    assert flat[0].kind == "rewrite-import"


def test_health(monkeypatch):
    monkeypatch.setattr(client_mod.httpx, "get", lambda url, timeout: _FakeResp({}, 200))
    assert AxiomGraphClient().health() is True
    monkeypatch.setattr(
        client_mod.httpx, "get",
        lambda url, timeout: (_ for _ in ()).throw(httpx.ConnectError("x")),
    )
    assert AxiomGraphClient().health() is False


# ─────────────────────────────────────────────────────────────────────────────
# fetch_bundle — the trust boundary: pymolt re-verifies every rule locally and
# is the sole authority on its confidence, regardless of what the server claims.
# ─────────────────────────────────────────────────────────────────────────────

_LOOKUP_RULE = {
    "library": "pandas",
    "from_version": "1.0.0",
    "to_version": "1.2.0",
    "match": "$DF.lookup($ROWS, $COLS)",
    "rewrite": [
        "_ridx = $DF.index.get_indexer($ROWS)",
        "_cidx = $DF.columns.get_indexer($COLS)",
        "$RESULT = $DF.to_numpy()[_ridx, _cidx]",
    ],
    "condition": "simple_name_args",
    "runtime_precondition": "unique_index_and_columns",
    "test_before": "vals = df.lookup(rows, cols)",
    "test_after": (
        "_ridx = df.index.get_indexer(rows)\n"
        "_cidx = df.columns.get_indexer(cols)\n"
        "vals = df.to_numpy()[_ridx, _cidx]"
    ),
}


def _bundle_payload(rule: dict) -> dict:
    return {"results": [{"name": "pandas", "codemods": [], "rules": [rule]}]}


def test_fetch_bundle_trusts_a_rule_that_verifies_locally(monkeypatch):
    rule = {**_LOOKUP_RULE, "confidence": "verified"}
    monkeypatch.setattr(
        client_mod.httpx, "post",
        lambda url, json, timeout, headers=None: _FakeResp(_bundle_payload(rule)),
    )
    out = AxiomGraphClient().fetch_bundle([DependencyMigration("pandas", "1.0.0", "1.2.0")])
    assert out["pandas"].rules[0].confidence == "verified"
    assert out["pandas"].downgraded == []


def test_fetch_bundle_upgrades_confidence_when_local_verify_passes(monkeypatch):
    # Server under-claims (heuristic); local re-verification is the sole
    # authority, so a rule that actually passes its golden pair is trusted.
    rule = {**_LOOKUP_RULE, "confidence": "heuristic"}
    monkeypatch.setattr(
        client_mod.httpx, "post",
        lambda url, json, timeout, headers=None: _FakeResp(_bundle_payload(rule)),
    )
    out = AxiomGraphClient().fetch_bundle([DependencyMigration("pandas", "1.0.0", "1.2.0")])
    assert out["pandas"].rules[0].confidence == "verified"


def test_fetch_bundle_downgrades_rule_that_fails_local_verification(monkeypatch):
    # Server over-claims verified, but the golden pair doesn't actually match
    # the rewrite output — pymolt must not trust the server's word.
    bad_rule = {**_LOOKUP_RULE, "confidence": "verified", "test_after": "vals = 1\n"}
    monkeypatch.setattr(
        client_mod.httpx, "post",
        lambda url, json, timeout, headers=None: _FakeResp(_bundle_payload(bad_rule)),
    )
    out = AxiomGraphClient().fetch_bundle([DependencyMigration("pandas", "1.0.0", "1.2.0")])
    bundle = out["pandas"]
    assert bundle.rules[0].confidence == "heuristic"
    assert len(bundle.downgraded) == 1
    assert "pandas" in bundle.downgraded[0] or "lookup" in bundle.downgraded[0]


def test_fetch_bundle_falls_back_to_patterns_when_rules_absent(monkeypatch):
    payload = {
        "results": [{
            "name": "flask",
            "codemods": [
                {
                    "old_qualname": "flask.helpers.safe_join",
                    "new_qualname": "werkzeug.utils.safe_join",
                    "kind": "rewrite-import",
                    "confidence": "high",
                }
            ],
            # no "rules" key — old server
        }]
    }
    monkeypatch.setattr(
        client_mod.httpx, "post",
        lambda url, json, timeout, headers=None: _FakeResp(payload),
    )
    out = AxiomGraphClient().fetch_bundle([DependencyMigration("flask", "2.0.3", "3.0.0")])
    assert out["flask"].rules == []
    assert out["flask"].patterns[0].new_qualname == "werkzeug.utils.safe_join"
