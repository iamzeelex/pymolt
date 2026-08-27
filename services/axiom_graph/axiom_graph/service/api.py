"""
axiom_graph/api.py

The Axiom Graph HTTP API (FastAPI).

Contract — the hard product boundary:
  - INPUT  : a list of dependencies, each {name, from_version, to_version}
             plus optional flags. USER CODE IS NEVER SENT.
  - OUTPUT : a JSON document of codemod *patterns* (old_qualname → new_qualname,
             kind, confidence, evidence) plus codemod *rules* (match/rewrite
             templates, catalog hits and pattern projections) per dependency.
             Applying a pattern or rule to a user's source happens locally
             (pymolt), never on the server — server confidence is a claim.

The server resolves the release chain, diffs the public API (Griffe), and runs
the SSA value-flow + prose analysis over the changed symbols to derive the
patterns. It downloads package metadata/sdists from PyPI — so it needs network,
but only ever reads public package sources.

Run:
    uvicorn axiom_graph.service.api:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request, status
from pydantic import BaseModel, Field

from axiom_graph.core.models import CodemodPattern, CodemodRule
from axiom_graph.core.pipeline import compute_full_delta
from axiom_graph.core.succession import SuccessionEdge
from axiom_graph.core.succession_catalog import succession_for
from axiom_graph.service.auth import require_client
from axiom_graph.service.limits import limiter, max_dependencies_per_request

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Optional persistence — accumulate the evolution graph as the service runs.
#
# Opt-in: only when AXIOM_GRAPH_STORE is set (e.g. bolt://neo4j:7687 in compose).
# Persistence never affects the response — a store failure is logged and
# swallowed. The hard product boundary is unchanged: only patterns are stored,
# never user code.
# ─────────────────────────────────────────────────────────────────────────────


@asynccontextmanager
async def _lifespan(app: FastAPI):
    store = None
    if os.environ.get("AXIOM_GRAPH_STORE"):
        try:
            from axiom_graph.storage.graph_store import store_from_env

            store = store_from_env()
            log.info("axiom_graph: persisting patterns to %s",
                     os.environ["AXIOM_GRAPH_STORE"])
        except Exception as exc:  # a bad store config must not stop the API
            log.warning("axiom_graph: store unavailable, running stateless: %s", exc)
            store = None
    app.state.store = store
    try:
        yield
    finally:
        if store is not None:
            try:
                store.flush()
            except Exception as exc:
                log.warning("axiom_graph: store flush failed: %s", exc)


app = FastAPI(
    title="Axiom Graph",
    version="0.1.0",
    summary="Release-sequenced codemod-pattern service (no user code).",
    lifespan=_lifespan,
)


# ─────────────────────────────────────────────────────────────────────────────
# Request / response models
# ─────────────────────────────────────────────────────────────────────────────


class DependencySpec(BaseModel):
    """One dependency migration to analyze. No user code — versions only."""

    name: str = Field(examples=["flask"])
    from_version: str = Field(examples=["2.0.3"])
    to_version: str = Field(examples=["3.0.0"])
    use_git: bool = False
    include_prereleases: bool = False


class AnalyzeRequest(BaseModel):
    dependencies: list[DependencySpec]


class DependencyResult(BaseModel):
    name: str
    from_version: str
    to_version: str
    skipped: bool = False
    skip_reason: str | None = None
    release_chain: list[str] = Field(default_factory=list)
    breaking_changes: int = 0
    codemods: list[CodemodPattern] = Field(default_factory=list)
    rules: list[CodemodRule] = Field(default_factory=list)


class AnalyzeResponse(BaseModel):
    results: list[DependencyResult]


class SuccessionRequest(BaseModel):
    """Frameworks a project is migrating *from* (base package names, e.g. "keras").

    No user code — just which dead-framework era the project sits in.
    """

    frameworks: list[str] = Field(examples=[["keras", "tensorflow"]])


class SuccessionResponse(BaseModel):
    edges: list[SuccessionEdge]


# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def _analyze_one(dep: DependencySpec, store=None) -> DependencyResult:
    delta = compute_full_delta(
        dep.name,
        dep.from_version,
        dep.to_version,
        use_git=dep.use_git,
        include_prereleases=dep.include_prereleases,
    )
    if store is not None and not delta.skipped and (delta.codemods or delta.rules):
        try:
            from axiom_graph.storage.graph_store import persist_full_delta

            persist_full_delta(store, delta)
        except Exception as exc:  # persistence is best-effort, never blocks output
            log.warning("axiom_graph: persist failed for %s: %s", dep.name, exc)
    return DependencyResult(
        name=dep.name,
        from_version=dep.from_version,
        to_version=dep.to_version,
        skipped=delta.skipped,
        skip_reason=delta.skip_reason,
        release_chain=delta.release_chain,
        breaking_changes=delta.total_breaking,
        codemods=delta.codemods,
        rules=delta.rules,
    )


@app.post("/codemods", response_model=AnalyzeResponse)
def codemods(
    request: AnalyzeRequest,
    http_request: Request,
    _user_id: str = Depends(require_client),
) -> AnalyzeResponse:
    """
    Analyze a batch of dependency migrations and return codemod patterns.

    Requires a valid client token (verified against the billing backend) unless
    the service runs in explicit anonymous mode. Each dependency is processed
    independently; a failure on one degrades to a skipped result rather than
    failing the whole batch.

    DoS controls (P1): the batch is capped and each caller is rate-limited.
    """
    cap = max_dependencies_per_request()
    if len(request.dependencies) > cap:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            f"too many dependencies in one request ({len(request.dependencies)} > {cap})",
        )
    if not limiter.allow(_user_id):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "rate limit exceeded; slow down and retry shortly",
        )

    store = getattr(http_request.app.state, "store", None)
    results: list[DependencyResult] = []
    for dep in request.dependencies:
        try:
            results.append(_analyze_one(dep, store=store))
        except Exception as exc:  # never fail the batch on one bad dep
            log.warning("analysis failed for %s: %s", dep.name, exc)
            results.append(
                DependencyResult(
                    name=dep.name,
                    from_version=dep.from_version,
                    to_version=dep.to_version,
                    skipped=True,
                    skip_reason=f"analysis error: {exc}",
                )
            )
    return AnalyzeResponse(results=results)


@app.post("/succession", response_model=SuccessionResponse)
def succession(
    request: SuccessionRequest,
    _user_id: str = Depends(require_client),
) -> SuccessionResponse:
    """Curated framework-succession edges for the frameworks a project is migrating from.

    Answers "where should this dead-framework code go?" — in-place shim bundles and/or a
    transplant plan (see :mod:`axiom_graph.core.succession`). Curated knowledge, no user
    code. Gated + rate-limited like /codemods.
    """
    if not limiter.allow(_user_id):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "rate limit exceeded; slow down and retry shortly",
        )
    edges: list[SuccessionEdge] = succession_for(request.frameworks)
    return SuccessionResponse(edges=edges)
