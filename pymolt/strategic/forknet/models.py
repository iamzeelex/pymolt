"""Fork-network domain models + pure scoring.

The strategic denominator for a dead repo: which of its (often thousands of)
forks is the *live successor*. We rank **evidence** — recent pushes, star
gravity, divergence from base (real work done), and a declared-dependency
"already ported?" signal — never a claim that a fork *works*. Honesty markers
live on every candidate (``ported_signal`` is only as strong as a manifest we
read, not a run we observed) and on the report (``auth_mode``, ``notes``).

Everything here is pure: no I/O, no network. The adapter
(:mod:`pymolt.adapters.github_forks`) fetches; the service orchestrates; this
module classifies and scores.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field

# Dependency tokens that, if a fork declares them, signal it has moved onto the
# modern framework stack. Matched case-insensitively against manifest lines.
_PORT_TOKENS_STRONG = ("tensorflow>=2", "tensorflow==2", "tensorflow~=2", "torch", "torchvision")
_PORT_TOKENS_WEAK = ("tf.keras", "tensorflow-gpu>=2", "keras>=2.4", "tf-nightly")


class PortedSignal(StrEnum):
    """How strongly a fork looks already migrated onto a modern stack."""

    CONFIRMED = "confirmed"  # manifest declares a modern framework pin (tensorflow>=2 / torch)
    LIKELY = "likely"  # weaker textual signal (tf.keras, loose modern keras)
    UNKNOWN = "unknown"  # no manifest fetched, or no signal found


def classify_ported_signal(deps: list[str] | None) -> tuple[PortedSignal, list[str]]:
    """Classify a fork's port status from its declared dependency lines.

    Returns the signal plus the concrete matched tokens (the *evidence* we show
    the user). ``None`` (manifest not fetched) → UNKNOWN with no evidence.
    """
    if not deps:
        return PortedSignal.UNKNOWN, []
    blob = "\n".join(deps).lower()
    strong = [t for t in _PORT_TOKENS_STRONG if t in blob]
    if strong:
        return PortedSignal.CONFIRMED, strong
    weak = [t for t in _PORT_TOKENS_WEAK if t in blob]
    if weak:
        return PortedSignal.LIKELY, weak
    return PortedSignal.UNKNOWN, []


class ForkCandidate(BaseModel):
    """One fork, with the evidence we gathered and the score we derived from it."""

    name_with_owner: str
    url: str
    pushed_at: datetime
    stars: int = 0
    default_branch: str = "master"
    # Filled by the expensive compare pass; None means "not compared".
    ahead_by: int | None = None
    behind_by: int | None = None
    # Filled by the optional enrichment pass.
    ported_signal: PortedSignal = PortedSignal.UNKNOWN
    matched_deps: list[str] = Field(default_factory=list)
    score: float = 0.0


class ForkNetworkReport(BaseModel):
    """Ranked successor-fork candidates for a base repo, with honesty markers."""

    base_repo: str  # "owner/name"
    generated_at: datetime
    cutoff_months: int
    forks_considered: int  # forks inspected before the recency cutoff halted paging
    compared: int = 0  # how many survivors got the expensive ahead_by pass
    auth_mode: str = "anonymous"  # "token" | "anonymous"
    candidates: list[ForkCandidate] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)  # degradations / caveats surfaced to the user


_PORTED_BONUS = {PortedSignal.CONFIRMED: 2.0, PortedSignal.LIKELY: 1.0, PortedSignal.UNKNOWN: 0.0}


def score_candidate(c: ForkCandidate, *, now: datetime | None = None) -> float:
    """Weighted evidence score (higher = more likely the live successor).

    Recency dominates (a fork not touched in years is dead regardless of stars);
    star gravity and divergence (``ahead_by`` = real work done past the base) are
    log-damped so one giant fork doesn't swamp the ranking; a ported-signal adds a
    flat bonus. Pure and deterministic given ``now``.
    """
    now = now or datetime.now(UTC)
    pushed = c.pushed_at if c.pushed_at.tzinfo else c.pushed_at.replace(tzinfo=UTC)
    months = max(0.0, (now - pushed).days / 30.0)
    recency = 1.0 / (1.0 + months / 6.0)  # 1.0 now, 0.5 at 6mo, decaying
    stars = math.log10(c.stars + 1)  # ~0..4
    ahead = math.log10((c.ahead_by or 0) + 1)  # divergence work, 0 if uncompared
    return round(3.0 * recency + 1.5 * stars + 1.0 * ahead + _PORTED_BONUS[c.ported_signal], 4)


def rank_candidates(
    candidates: list[ForkCandidate], *, now: datetime | None = None
) -> list[ForkCandidate]:
    """Score each candidate in place and return them sorted best-first."""
    for c in candidates:
        c.score = score_candidate(c, now=now)
    return sorted(candidates, key=lambda c: c.score, reverse=True)
