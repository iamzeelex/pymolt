"""
pymolt/codemods/client.py

Client for the Axiom Graph codemod service.

Sends dependency names + versions, receives Tier-1 codemod patterns AND Tier-2
codemod rules. USER CODE IS NEVER SENT — only the migration spec crosses the
wire. Applying the returned patterns/rules to the repo is done locally by
pymolt.codemods.apply.

pymolt is the SOLE verification authority: every rule the server returns is
independently re-verified here (`rules.verify_rule`) against its own golden
pair before its confidence is trusted. A server-claimed `verified` rule that
fails local verification is downgraded to `heuristic` — never auto-applied.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

import httpx

from pymolt.codemods.models import CodemodBundle, CodemodPattern
from pymolt.codemods.rules import CodemodRule, verify_rule

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://localhost:8000"


class AxiomGraphError(RuntimeError):
    """The Axiom Graph service was unreachable or returned an error."""


class DependencyMigration:
    """One dependency to analyze (versions only — never code)."""

    def __init__(
        self,
        name: str,
        from_version: str,
        to_version: str,
        *,
        use_git: bool = False,
    ) -> None:
        self.name = name
        self.from_version = from_version
        self.to_version = to_version
        self.use_git = use_git

    def to_payload(self) -> dict:
        return {
            "name": self.name,
            "from_version": self.from_version,
            "to_version": self.to_version,
            "use_git": self.use_git,
        }


def _rule_label(rule: CodemodRule) -> str:
    """A short human-readable identity for a rule (for downgrade summaries)."""
    if rule.old_qualname and rule.new_qualname:
        return f"{rule.old_qualname} → {rule.new_qualname}"
    return rule.match


class AxiomGraphClient:
    """Thin HTTP client over the Axiom Graph API."""

    #: Dependencies per request. The server's cost is per release chain, so a
    #: large batch is a single long request that either lands or times out
    #: whole; small ones fail narrowly and show progress.
    CHUNK_SIZE = 3

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout: float = 120.0,
        token: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        # The service authenticates the caller against the billing backend; send
        # the user's API token when present. Resolution: explicit arg, else the
        # PYMOLT_API_TOKEN env var, else the saved config (`pymolt login`).
        from pymolt.config import load_token

        self.token = token or load_token()

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def health(self) -> bool:
        """True if the service answers /health."""
        try:
            r = httpx.get(f"{self.base_url}/health", timeout=5.0)
            return r.status_code == 200
        except httpx.HTTPError as exc:
            log.warning("Axiom Graph health check failed: %s", exc)
            return False

    def fetch_bundle(
        self,
        migrations: list[DependencyMigration],
        *,
        progress: Callable[[str], None] | None = None,
    ) -> dict[str, CodemodBundle]:
        """
        Fetch codemods for `migrations`; return a CodemodBundle (patterns AND
        rules) keyed by package.

        Sent in chunks of ``CHUNK_SIZE`` rather than one request. The server
        diffs whole release chains per dependency, so cost grows with the batch
        — an eleven-package upgrade set timed out as a single POST while the
        same work split into chunks completes and, unlike the all-or-nothing
        request, reports progress as it goes.

        LOCAL RE-VERIFY: every rule is independently re-run against its own
        golden pair (`verify_rule`) here, at the trust boundary — pymolt is the
        sole verification authority. A rule that verifies locally is trusted as
        `verified` regardless of what the server claimed; a rule the server
        claimed `verified` that fails locally is downgraded to `heuristic` (a
        warning is logged and its identity recorded in `bundle.downgraded`).

        Raises AxiomGraphError on connection/HTTP failure.
        """
        if not migrations:
            return {}
        out: dict[str, CodemodBundle] = {}
        chunks = [
            migrations[i : i + self.CHUNK_SIZE]
            for i in range(0, len(migrations), self.CHUNK_SIZE)
        ]
        for index, chunk in enumerate(chunks, start=1):
            if progress:
                names = ", ".join(m.name for m in chunk)
                progress(
                    f"Analyzing batch {index}/{len(chunks)} on Axiom Graph — {names}. "
                    "Diffing release chains server-side can take a while…"
                )
            out.update(self._fetch_chunk(chunk, progress=progress))
        return out

    def _fetch_chunk(
        self,
        migrations: list[DependencyMigration],
        *,
        progress: Callable[[str], None] | None = None,
    ) -> dict[str, CodemodBundle]:
        """One request's worth of migrations. See :meth:`fetch_bundle`."""
        payload = {"dependencies": [m.to_payload() for m in migrations]}
        try:
            r = httpx.post(
                f"{self.base_url}/codemods",
                json=payload,
                headers=self._auth_headers(),
                timeout=self.timeout,
            )
            r.raise_for_status()
        except httpx.HTTPError as exc:
            names = ", ".join(m.name for m in migrations)
            raise AxiomGraphError(
                f"Axiom Graph request to {self.base_url} failed for {names}: {exc}"
            ) from exc

        if progress:
            progress("Re-verifying returned rules locally against their golden pairs…")
        data = r.json()
        out: dict[str, CodemodBundle] = {}
        for result in data.get("results", []):
            name = result.get("name", "")
            patterns = [CodemodPattern.model_validate(c) for c in result.get("codemods", [])]

            rules: list[CodemodRule] = []
            downgraded: list[str] = []
            for raw_rule in result.get("rules", []):
                rule = CodemodRule.model_validate(raw_rule)
                if verify_rule(rule):
                    if rule.confidence != "verified":
                        rule = rule.model_copy(update={"confidence": "verified"})
                elif rule.confidence == "verified":
                    log.warning(
                        "codemods: downgrading rule to heuristic (server claimed "
                        "verified, local verification failed): %s",
                        _rule_label(rule),
                    )
                    downgraded.append(_rule_label(rule))
                    rule = rule.model_copy(update={"confidence": "heuristic"})
                rules.append(rule)

            out[name] = CodemodBundle(patterns=patterns, rules=rules, downgraded=downgraded)
        return out

    def fetch_codemods(
        self, migrations: list[DependencyMigration]
    ) -> dict[str, list[CodemodPattern]]:
        """
        POST a batch of dependency migrations; return patterns keyed by package.
        A thin wrapper over `fetch_bundle` (patterns only) for callers that only
        need the Tier-1 shape.

        Raises AxiomGraphError on connection/HTTP failure.
        """
        bundles = self.fetch_bundle(migrations)
        return {name: bundle.patterns for name, bundle in bundles.items()}

    def fetch_flat(self, migrations: list[DependencyMigration]) -> list[CodemodPattern]:
        """All patterns across the batch as one flat list (apply order)."""
        by_pkg = self.fetch_codemods(migrations)
        flat: list[CodemodPattern] = []
        for patterns in by_pkg.values():
            flat.extend(patterns)
        return flat
