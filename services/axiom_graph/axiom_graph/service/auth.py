"""Client authentication for the Axiom Graph API.

The service owns no users. A caller (the pymolt CLI) presents an opaque API token
issued by the PyMolt billing backend (the website); we verify it by calling that
backend's internal endpoint — the website stays the single source of truth for
identity and entitlements, and Axiom Graph never touches its database.

Secure-by-default: enforcement is ON unless ``AXIOM_ALLOW_ANONYMOUS`` is explicitly
set (local dev / the OSS self-host story). When enforcement is on but the backend
is unset or unreachable, the gate fails **closed** (503), never open.

Config:
  PYMOLT_BILLING_URL     e.g. https://pymolt.zeelex.me  (verify endpoint base)
  PYMOLT_BILLING_SECRET  shared secret sent as x-internal-secret
  AXIOM_ALLOW_ANONYMOUS  "1"/"true"/"yes" → disable the gate (dev only)
"""

from __future__ import annotations

import logging
import os

import httpx
from fastapi import Header, HTTPException, status

log = logging.getLogger(__name__)

_VERIFY_PATH = "/api/internal/verify-token"
_UNAUTH_HEADERS = {"WWW-Authenticate": "Bearer"}


def _anonymous_allowed() -> bool:
    return os.environ.get("AXIOM_ALLOW_ANONYMOUS", "").strip().lower() in ("1", "true", "yes")


def require_client(authorization: str | None = Header(default=None)) -> str:
    """FastAPI dependency: return the verified caller's user id, or raise 401/503.

    In the explicit dev-open mode (``AXIOM_ALLOW_ANONYMOUS``) returns
    ``"anonymous"`` without a network call.
    """
    if _anonymous_allowed():
        return "anonymous"

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token", _UNAUTH_HEADERS)
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "empty bearer token", _UNAUTH_HEADERS)

    billing_url = os.environ.get("PYMOLT_BILLING_URL")
    secret = os.environ.get("PYMOLT_BILLING_SECRET")
    if not billing_url or not secret:
        # Enforcement is on but misconfigured — fail closed, do not silently allow.
        log.error("auth required but PYMOLT_BILLING_URL/PYMOLT_BILLING_SECRET unset; refusing")
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "auth backend not configured")

    try:
        resp = httpx.post(
            billing_url.rstrip("/") + _VERIFY_PATH,
            json={"token": token},
            headers={"x-internal-secret": secret},
            timeout=10.0,
        )
    except httpx.HTTPError as exc:
        log.warning("token verification unreachable: %s", exc)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "auth backend unreachable") from exc

    if resp.status_code != 200:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token", _UNAUTH_HEADERS)
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if not data.get("valid"):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token", _UNAUTH_HEADERS)
    return str(data.get("userId") or "unknown")
