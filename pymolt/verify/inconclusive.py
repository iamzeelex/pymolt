"""Who settles an INCONCLUSIVE verdict: a person, or more evidence (design decision 7).

HOST side (3.12+). Every reason that keeps a verdict from PASS without failing it is one of two
kinds (``InconclusiveKind``):

- ``needs_review``: a person must look. A review item at a call: a value that differs on an
  edited line, a call that vanished from an unedited one, an accepted change under ``--strict``,
  or a difference a baseline replicate measured and still could not settle (a rare outcome, an
  input or order change). Only a person's ``verify --accept``, a fix or a person's judgement
  settles it.
- ``needs_evidence``: more or better captures settle it. A site no run reached (BLIND), a fact
  that could not be compared, a capture missing, invalid, stale or unlike the other, run-to-run
  noise a baseline replicate would measure, a difference of the two captures' environments.

Each reason carries its kind with it (``InconclusiveCause``), set where the reason is made and
never parsed back from its text. The verdict's kind is ``needs_review`` when any reason needs a
person, else ``needs_evidence``: the precedence of the step statuses (failed > needs_review >
needs_evidence). Exit code 4 stays for both.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from pymolt.verify.facts import (
    BACKEND_UNOBSERVABLE,
    C_SITE_CAPPED,
    CALL_SHAPE,
    ENVIRONMENT_DIFFERS,
    INPUT_CHANGED,
    INPUT_ORDER_CHANGED,
    ORDER_CHANGED,
    UNMEASURED_NOISE,
    UNPAIRED_SITE,
)
from pymolt.verify.models import InconclusiveCause, InconclusiveKind

NEEDS_REVIEW = InconclusiveKind.NEEDS_REVIEW
NEEDS_EVIDENCE = InconclusiveKind.NEEDS_EVIDENCE

#: Review items a baseline replicate settles while none was measured: run-to-run noise
#: (``facts``' noise classes) and hash-seeded dict orders. Once a replicate was measured, those
#: still standing are a person's (the replicate could not tell).
REPLICATE_REVIEWS = (UNMEASURED_NOISE, "masked input", INPUT_CHANGED, UNPAIRED_SITE,
                     ORDER_CHANGED, INPUT_ORDER_CHANGED)

SETTLE_REVIEW = ("a person reviews each finding (`pymolt verify .` names its id): accept an "
                 "intended change with `pymolt verify . --accept ID`, or fix the code")
SETTLE_NOISE = ("capture a baseline replicate (`pymolt capture baseline --replicate`), then "
                "verify again")
SETTLE_ENVIRONMENT = ("capture post-migration again in the baseline's environment (its "
                      "container or host, with the target Python)")
SETTLE_NOT_EXERCISED = "add a test that reaches those sites, then capture post-migration again"

#: Why a fact could not be compared (``uncomparable_mechanism``) -> what settles it. Only the
#: first three are settled by capturing again; the cap, the backends and a lost distribution are
#: properties of the watcher, so the same capture comes out the same.
SETTLE_UNCOMPARABLE = {
    "evicted": ("capture the side that evicted facts again with a higher "
                "`PYMOLT_TRACE_MAX_FACTS`"),
    "dropped": "capture the side that dropped events again",
    "unpaired": ("add a test that runs the edited statements, then capture post-migration "
                 "again"),
    "c-site-capped": ("capturing again does not settle it: the watcher records the first 2,000 "
                      "compiled calls per call site and counts the rest; a test that makes "
                      "fewer calls there does"),
    "call-shape": ("capturing again does not settle it: the two captures' backends record "
                   "these compiled calls differently, one without their inputs"),
    "backend-unobservable": ("capturing again does not settle it: one capture's backend cannot "
                             "record these compiled calls"),
    "distribution-lost": ("capturing again does not settle it: the capture kept only part of "
                          "these calls' outcomes (too many distinct ones for one input)"),
}


def _get(e: Any, key: str, default: Any = None) -> Any:
    if isinstance(e, Mapping):
        return e.get(key, default)
    return getattr(e, key, default)


def _value(v: Any) -> str | None:
    return None if v is None else str(getattr(v, "value", v))


def is_inconclusive(e: Any) -> bool:
    """``FactEntry.inconclusive`` for an entry or its JSON form."""
    if _get(e, "unverified") or _get(e, "noise") or _get(e, "accepted"):
        return False
    if _get(e, "review"):
        return True
    return (_value(_get(e, "comparison")) in ("uncomparable", "missing")
            and _value(_get(e, "category")) in ("uncomparable", "rewritten"))


def entry_cause(e: Any, *, noise_measured: bool) -> tuple[InconclusiveKind, str] | None:
    """``(kind, cause)`` of a fact entry (a ``FactEntry`` or its JSON form) that keeps the
    verdict from PASS without failing it; None for any other entry. ``noise_measured``: a
    baseline replicate judged the diff (``FactDiff.noise_measured``)."""
    if _get(e, "fails") or not is_inconclusive(e):
        return None
    review = _get(e, "review")
    if review:
        text = str(review)
        if text.startswith(ENVIRONMENT_DIFFERS):
            return NEEDS_EVIDENCE, "environment"
        if text.startswith(REPLICATE_REVIEWS) and not noise_measured:
            return NEEDS_EVIDENCE, "noise"
        return NEEDS_REVIEW, "review"
    return NEEDS_EVIDENCE, "uncomparable"


def uncomparable_mechanism(e: Any) -> str:
    """Why an uncomparable entry could not be compared (a key of ``SETTLE_UNCOMPARABLE``)."""
    notes = [str(n) for n in _get(e, "notes") or ()]

    def has(prefix: str) -> bool:
        return any(n.startswith(prefix) for n in notes)

    if has("possibly-evicted"):
        return "evicted"
    if has("possibly-dropped"):
        return "dropped"
    if BACKEND_UNOBSERVABLE in notes:
        return "backend-unobservable"
    if has(C_SITE_CAPPED):
        return "c-site-capped"
    if has(CALL_SHAPE):
        return "call-shape"
    if _value(_get(e, "category")) == "rewritten":
        return "unpaired"
    return "distribution-lost"


def settle_uncomparable(entries: Iterable[Any]) -> str:
    """What settles these uncomparable entries: the first mechanism (in
    ``SETTLE_UNCOMPARABLE``'s order) among them, so one a capture settles comes first."""
    found = {uncomparable_mechanism(e) for e in entries}
    for mechanism, text in SETTLE_UNCOMPARABLE.items():
        if mechanism in found:
            return text
    return SETTLE_UNCOMPARABLE["distribution-lost"]


def cause(reason: str, kind: InconclusiveKind, why: str, settle: str) -> InconclusiveCause:
    return InconclusiveCause(reason=reason, kind=kind, cause=why, settle=settle)


def kind_of(causes: Iterable[InconclusiveCause]) -> InconclusiveKind | None:
    """``needs_review`` when any cause needs a person, else ``needs_evidence``; None for no
    cause."""
    kinds = {c.kind for c in causes}
    if NEEDS_REVIEW in kinds:
        return NEEDS_REVIEW
    return NEEDS_EVIDENCE if NEEDS_EVIDENCE in kinds else None


def first_settle(causes: Iterable[InconclusiveCause], kind: InconclusiveKind | None
                 ) -> str | None:
    """What settles the first cause of ``kind``."""
    return next((c.settle for c in causes if c.kind == kind), None)


__all__ = [
    "NEEDS_EVIDENCE", "NEEDS_REVIEW", "REPLICATE_REVIEWS", "SETTLE_ENVIRONMENT",
    "SETTLE_NOISE", "SETTLE_NOT_EXERCISED", "SETTLE_REVIEW", "SETTLE_UNCOMPARABLE", "cause",
    "entry_cause", "first_settle", "is_inconclusive", "kind_of", "settle_uncomparable",
    "uncomparable_mechanism",
]
