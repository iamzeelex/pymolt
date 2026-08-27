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
from pymolt.config import (
    DEFAULT_ENDPOINT,
    get_cached_delta,
    load_endpoint,
    load_token,
    save_cached_delta,
)

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = DEFAULT_ENDPOINT


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
    """Thin HTTP client over the Axiom Graph API with local offline cache."""

    #: Dependencies per request. The server's cost is per release chain, so a
    #: large batch is a single long request that either lands or times out
    #: whole; small ones fail narrowly and show progress.
    CHUNK_SIZE = 3

    def __init__(
        self,
        base_url: str | None = None,
        *,
        timeout: float = 120.0,
        token: str | None = None,
        use_cache: bool = True,
    ) -> None:
        import os
        self.base_url = (base_url or load_endpoint()).rstrip("/")
        self.timeout = timeout
        self.token = token or load_token()
        self.use_cache = use_cache and not os.environ.get("PYMOLT_NO_CACHE")

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
        use_cache: bool | None = None,
    ) -> dict[str, CodemodBundle]:
        """
        Fetch codemods for `migrations`; return a CodemodBundle (patterns AND
        rules) keyed by package.

        Checks local offline cache first. Uncached migrations are sent to the
        Axiom Cloud Hub in chunks, and results are cached locally.
        """
        if not migrations:
            return {}

        should_cache = self.use_cache if use_cache is None else use_cache
        out: dict[str, CodemodBundle] = {}
        uncached: list[DependencyMigration] = []

        # 1. Check local cache
        if should_cache:
            for m in migrations:
                cached_raw = get_cached_delta(m.name, m.from_version, m.to_version)
                if cached_raw:
                    try:
                        bundle = CodemodBundle.model_validate(cached_raw)
                        out[m.name] = bundle
                        log.debug("Local delta cache hit for %s %s->%s", m.name, m.from_version, m.to_version)
                    except Exception as c_exc:
                        log.debug("Cache validation failed for %s: %s", m.name, c_exc)
                        uncached.append(m)
                else:
                    uncached.append(m)
        else:
            uncached = list(migrations)

        if not uncached:
            if progress:
                progress(f"Loaded {len(out)} package delta(s) from local cache.")
            return out

        # 2. Fetch uncached chunks from remote Hub
        chunks = [
            uncached[i : i + self.CHUNK_SIZE]
            for i in range(0, len(uncached), self.CHUNK_SIZE)
        ]
        for index, chunk in enumerate(chunks, start=1):
            if progress:
                names = ", ".join(m.name for m in chunk)
                progress(
                    f"Fetching batch {index}/{len(chunks)} from Axiom Cloud Hub ({self.base_url}) — {names}…"
                )
            fetched = self._fetch_chunk(chunk, progress=progress)
            for m in chunk:
                if m.name in fetched:
                    bundle = fetched[m.name]
                    out[m.name] = bundle
                    if should_cache:
                        save_cached_delta(m.name, m.from_version, m.to_version, bundle.model_dump())

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
