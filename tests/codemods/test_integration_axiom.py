"""
Cross-service contract test: the pymolt client talks to the REAL Axiom Graph
FastAPI app (routed in-process via TestClient, compute_full_delta mocked so
there's no network). Proves the request/response shapes AND the auth contract
match end to end, and that fetched patterns actually rewrite a repo.

Needs the axiom_graph checkout in `.research/` and `fastapi` importable; each
missing piece skips with its own explicit reason (visible under `-rs`) instead
of one silent blanket skip — this file rotted unnoticed once when the service
module moved (`axiom_graph.api` → `axiom_graph.service.api`).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_RESEARCH = Path(__file__).resolve().parents[2] / ".research"
if str(_RESEARCH) not in sys.path:
    sys.path.insert(0, str(_RESEARCH))

pytest.importorskip(
    "fastapi",
    reason="cross-service contract test needs fastapi (the axiom_graph web framework)",
)
axiom_api = pytest.importorskip(
    "axiom_graph.service.api",
    reason="axiom_graph checkout not importable from .research/",
)

from axiom_graph.core.models import CodemodPattern as AxiomPattern  # noqa: E402
from axiom_graph.core.models import FullDelta  # noqa: E402

from pymolt.codemods import client as client_mod  # noqa: E402
from pymolt.codemods.client import (  # noqa: E402
    AxiomGraphClient,
    AxiomGraphError,
    DependencyMigration,
)
from pymolt.codemods.service import run_codemods  # noqa: E402


def _fake_delta(package, from_v, to_v, **kwargs) -> FullDelta:
    return FullDelta(
        package=package, from_version=from_v, to_version=to_v,
        release_chain=[to_v],
        codemods=[
            AxiomPattern(
                old_qualname="flask.helpers.safe_join",
                new_qualname="werkzeug.utils.safe_join",
                kind="rewrite-import",
                confidence="high",
                evidence=["data-flow + prose agree"],
            )
        ],
    )


def _route_through(monkeypatch, tc):
    """Route the pymolt client's httpx.post through the in-process app.

    Signature mirrors the client's real call — including `headers`, which the
    client always passes (the old routed fake predated the auth header and
    would TypeError today).
    """

    def routed_post(url, json, timeout, headers=None):
        path = "/" + url.rstrip("/").split("/", 3)[-1]
        return tc.post(path, json=json, headers=headers or {})

    monkeypatch.setattr(client_mod.httpx, "post", routed_post)


@pytest.fixture
def routed_client(monkeypatch):
    """Client wired to the real app in the documented dev-open (anonymous) mode."""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("AXIOM_ALLOW_ANONYMOUS", "1")
    monkeypatch.setattr(axiom_api, "compute_full_delta", _fake_delta)
    tc = TestClient(axiom_api.app)
    _route_through(monkeypatch, tc)
    yield AxiomGraphClient("http://axiom.test")
    tc.close()


def test_client_to_api_contract(routed_client):
    by_pkg = routed_client.fetch_codemods(
        [DependencyMigration("flask", "2.0.3", "3.0.0")]
    )
    assert "flask" in by_pkg
    pattern = by_pkg["flask"][0]
    # the API's CodemodPattern shape parses cleanly into pymolt's model
    assert pattern.old_qualname == "flask.helpers.safe_join"
    assert pattern.new_qualname == "werkzeug.utils.safe_join"
    assert pattern.kind == "rewrite-import"


def test_full_loop_fetch_and_apply(routed_client, tmp_path):
    (tmp_path / "app.py").write_text(
        "from flask.helpers import safe_join\n\n"
        "def serve(b, n):\n    return safe_join(b, n)\n"
    )
    by_pkg, result = run_codemods(
        tmp_path,
        [DependencyMigration("flask", "2.0.3", "3.0.0")],
        base_url="http://axiom.test",
        write=True,
        client=routed_client,
    )
    assert result.files_changed == 1
    assert "from werkzeug.utils import safe_join" in (tmp_path / "app.py").read_text()


def test_multiple_libraries_returned_per_package(monkeypatch):
    """Client sends a batch of libraries; the real API returns each one's own
    codemods, keyed by package."""
    from fastapi.testclient import TestClient

    per_pkg = {
        "flask": [("flask.helpers.safe_join", "werkzeug.utils.safe_join", "rewrite-import")],
        "click": [("click.Option.full_process_value", "process_value", "rename-call")],
    }

    def fake(package, from_v, to_v, **kwargs):
        return FullDelta(
            package=package, from_version=from_v, to_version=to_v, release_chain=[to_v],
            codemods=[
                AxiomPattern(old_qualname=o, new_qualname=n, kind=k,
                             confidence="high", evidence=["e"])
                for (o, n, k) in per_pkg.get(package, [])
            ],
        )

    monkeypatch.setenv("AXIOM_ALLOW_ANONYMOUS", "1")
    monkeypatch.setattr(axiom_api, "compute_full_delta", fake)
    tc = TestClient(axiom_api.app)
    _route_through(monkeypatch, tc)

    by_pkg = AxiomGraphClient("http://axiom.test").fetch_codemods([
        DependencyMigration("flask", "2.0.3", "3.0.0"),
        DependencyMigration("click", "7.1.2", "8.1.0"),
    ])
    tc.close()

    assert set(by_pkg) == {"flask", "click"}
    assert by_pkg["flask"][0].new_qualname == "werkzeug.utils.safe_join"
    assert by_pkg["click"][0].new_qualname == "process_value"
    assert by_pkg["click"][0].kind == "rename-call"


# ── the auth contract (P0 security: gated unless explicitly anonymous) ────────

@pytest.fixture
def _no_auth_env(monkeypatch, tmp_path):
    """Auth enforced (no anonymous mode), and no token leaking in from the
    developer's real environment or saved config file."""
    monkeypatch.delenv("AXIOM_ALLOW_ANONYMOUS", raising=False)
    monkeypatch.delenv("PYMOLT_API_TOKEN", raising=False)
    monkeypatch.delenv("PYMOLT_BILLING_URL", raising=False)
    monkeypatch.delenv("PYMOLT_BILLING_SECRET", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))  # no saved token file


def test_missing_token_is_rejected_with_401(_no_auth_env, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setattr(axiom_api, "compute_full_delta", _fake_delta)
    tc = TestClient(axiom_api.app)
    _route_through(monkeypatch, tc)

    with pytest.raises(AxiomGraphError):
        AxiomGraphClient("http://axiom.test").fetch_codemods(
            [DependencyMigration("flask", "2.0.3", "3.0.0")]
        )
    tc.close()


def test_token_without_billing_backend_fails_closed(_no_auth_env, monkeypatch):
    """Enforcement on but billing backend unconfigured → 503, never a silent allow."""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("PYMOLT_API_TOKEN", "pmk_sometoken")
    monkeypatch.setattr(axiom_api, "compute_full_delta", _fake_delta)
    tc = TestClient(axiom_api.app)
    _route_through(monkeypatch, tc)

    with pytest.raises(AxiomGraphError):
        AxiomGraphClient("http://axiom.test").fetch_codemods(
            [DependencyMigration("flask", "2.0.3", "3.0.0")]
        )
    tc.close()
