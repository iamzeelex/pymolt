"""HTTP client for the Axiom Graph ``/succession`` endpoint.

Sends the project's framework names (never code), receives curated ``SuccessionEdge``s.
Auth mirrors the codemod client: a Bearer token resolved explicit-arg → PYMOLT_API_TOKEN
env → saved config (``pymolt login``).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable

import httpx

from pymolt.strategic.succession.models import SuccessionEdge

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.pymolt.zeelex.me"


class SuccessionError(RuntimeError):
    """The Axiom Graph /succession endpoint was unreachable or returned an error."""


class SuccessionClient:
    """Thin HTTP client over the Axiom Graph /succession endpoint."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout: float = 120.0,
        token: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        from pymolt.config import load_token

        self.token = token or load_token()

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def fetch(
        self, frameworks: Iterable[str], *, progress: Callable[[str], None] | None = None
    ) -> list[SuccessionEdge]:
        """POST the framework names; return the matching succession edges (may be empty)."""
        names = list(frameworks)
        if not names:
            return []
        if progress:
            progress(f"Querying Axiom Graph for succession paths ({len(names)} frameworks)…")
        try:
            r = httpx.post(
                f"{self.base_url}/succession",
                json={"frameworks": names},
                headers=self._auth_headers(),
                timeout=self.timeout,
            )
            r.raise_for_status()
        except httpx.HTTPError as exc:
            raise SuccessionError(
                f"Axiom Graph /succession request to {self.base_url} failed: {exc}"
            ) from exc
        data = r.json()
        return [SuccessionEdge.model_validate(e) for e in data.get("edges", [])]
