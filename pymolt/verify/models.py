"""Typed output contracts for verify/ (host side, Pydantic, Python >=3.12).

These are *data*, not presentation. Any human-readable rendering of a diff or report belongs
in ``interfaces/`` (CLI/MCP), never here — the core stays non-interactive and equally usable
from CLI, MCP, or as a library (``.agent.md``). Like the IngestionReport, every artifact
carries its own trust level (``evidence_level``) and where it is unsure (``honesty``), so a
downstream consumer never has to ask "how good is this verdict?".
"""
import json
import logging
import os
import tempfile
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError, computed_field

from pymolt.core.enums import EvidenceLevel, TestStatus, TraceScope, Verdict

logger = logging.getLogger(__name__)

CONTRACT_STATE_SCHEMA_VERSION = 1


class VerificationVerdict(StrEnum):
    """Machine-facing result of a verification oracle.

    ``INCONCLUSIVE`` is deliberately distinct from ``PASS``: missing, stale,
    opaque, or otherwise insufficient evidence must never become a green CI
    result merely because no concrete incompatibility was observed.
    """

    PASS = "pass"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"


class CaptureValidity(StrEnum):
    """Whether a captured trace is eligible to act as named evidence."""

    VALID = "valid"
    UNKNOWN = "unknown"  # legacy state written before validity was recorded
    COMMAND_FAILED = "command-failed"
    EMPTY = "empty"
    CORRUPT = "corrupt"
    MISSING = "missing"


class VerificationOutcome(BaseModel):
    """A verdict plus the concrete facts that prevented a stronger result."""

    verdict: VerificationVerdict
    reasons: list[str] = Field(default_factory=list)


class TraceArtifactQuality(BaseModel):
    """Cheap structural validation of a JSON/JSONL trace artifact."""

    path: str
    validity: CaptureValidity
    events: int = 0
    comparable_events: int = 0
    invalid_lines: int = 0
    reason: str | None = None


class TestOutcome(BaseModel):
    """L1 input: how the suite touching a node behaved across the version pair."""

    __test__ = False  # not a pytest test class

    status: TestStatus
    detail: dict[str, Any] = Field(default_factory=dict)


class GoldenDiff(BaseModel):
    """L2 result: value-level diff of golden-master snapshots across the version pair."""

    changed: list[dict[str, Any]] = Field(default_factory=list)  # point whose value moved
    missing: list[str] = Field(default_factory=list)             # point absent on one side
    skipped: list[str] = Field(default_factory=list)             # opaque/nondet -> declined

    def is_clean(self) -> bool:
        return not (self.changed or self.missing)


class NodeRef(BaseModel):
    """Identity of a node under verification: a dependency and the version pair to cross."""

    name: str
    old_version: str
    new_version: str
    trace_prefix: str  # dependency import prefix to trace, e.g. "flask"


class BoundaryDiff(BaseModel):
    """What L3 yields: two boundary recordings folded into categories.

    ``is_clean()`` ⇔ none of {disappeared, result_changed, raise_changed}. ``skipped_opaque``
    is NOT clean-blocking but IS a typed honesty marker the report must carry — a confident-
    but-wrong diff is worse than an honest "I can't compare this".
    """

    # All three "changed" categories -> BEHAVIOR_CHANGED; appeared is INFO;
    # skipped_opaque is a NEEDS_ACTION honesty marker (not clean-blocking).
    disappeared: list[dict[str, Any]] = Field(default_factory=list)     # in old, absent in new
    result_changed: list[dict[str, Any]] = Field(default_factory=list)  # same inputs, diff return
    raise_changed: list[dict[str, Any]] = Field(default_factory=list)   # value <-> exception flip
    appeared: list[dict[str, Any]] = Field(default_factory=list)        # new boundary contact
    skipped_opaque: list[dict[str, Any]] = Field(default_factory=list)  # comparison declined

    def is_clean(self) -> bool:
        return not (self.disappeared or self.result_changed or self.raise_changed)

    @computed_field
    @property
    def verdict(self) -> VerificationVerdict:
        """A clean-but-uncomparable diff is inconclusive, never a pass."""
        if not self.is_clean():
            return VerificationVerdict.FAIL
        if self.skipped_opaque:
            return VerificationVerdict.INCONCLUSIVE
        return VerificationVerdict.PASS

    def counts(self) -> dict[str, int]:
        return {
            "disappeared": len(self.disappeared),
            "result_changed": len(self.result_changed),
            "raise_changed": len(self.raise_changed),
            "appeared": len(self.appeared),
            "skipped_opaque": len(self.skipped_opaque),
        }


class NodeVerifyResult(BaseModel):
    """Per-node behavioral fact + how it was reached + where it is unsure."""

    node: NodeRef
    verdict: Verdict
    evidence_level: EvidenceLevel          # which cascade level produced the verdict
    detail: dict[str, Any] = Field(default_factory=dict)  # the L1/L2/L3 payload behind it
    # opaque comparisons, coverage gaps, nondeterminism
    honesty: list[str] = Field(default_factory=list)


class VerifyReport(BaseModel):
    """Per-run aggregate. Both scopes produce this same shape; only breadth differs."""

    results: list[NodeVerifyResult] = Field(default_factory=list)
    scope: TraceScope
    coverage_summary: dict[str, Any] = Field(default_factory=dict)  # exercised vs blind counts
    warnings: list[str] = Field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# Persisted contract capture state — ``.pymolt/contract_state.json``. Names a
# captured dynamic trace as "baseline" (pre-migration) or "post_migration"
# (post-codemods), so the guided capture flow and the report builder never
# need the engineer to remember/retype raw JSONL paths. Mirrors EnvConfig's
# load/save pattern (pymolt/ingestion/config.py) exactly.
# ─────────────────────────────────────────────────────────────────────────────


class CaptureMode(StrEnum):
    """How a dynamic trace was captured — a label, not a different mechanism:
    all three ultimately produce a JSONL recording via the same injected
    boundary tracer; they differ in who drives the process."""

    TEST_SUITE = "test_suite"      # pymolt runs your test command, waits for exit
    LIVE_COMMAND = "live_command"  # pymolt runs your app command; Stop & Collect ends it early
    LIVE_ATTACH = "live_attach"    # you run your own process; pymolt only watches + finalizes


class ContractSlot(BaseModel):
    """One captured dynamic trace, named by when it was taken relative to migration."""

    trace_path: str
    captured_at: str  # ISO 8601 timestamp
    mode: CaptureMode
    command: list[str] = Field(default_factory=list)
    target: str = "all"
    events: int = 0
    processes: int = 0
    # Path to the merged stdout+stderr of the captured command, and its exit
    # code. Both default so OLD .pymolt/contract_state.json files (written before
    # these existed) still load without a schema bump.
    command_log: str | None = None
    returncode: int | None = None
    # Human-readable note on which env this capture actually ran in (e.g.
    # "baseline → container abc123" or "running locally"). Defaults so OLD
    # .pymolt/contract_state.json files (written before this existed) still load.
    env_note: str | None = None
    # Coverage-based gap summary (TEST_SUITE captures only) — what fraction of the
    # project's static dependency-usage sites the test suite actually exercised. None/0
    # when unavailable (coverage not installed, non-TEST_SUITE capture, or the second
    # coverage pass failed) so OLD contract_state.json files still load unchanged.
    coverage_pct: float | None = None
    covered_sites: int = 0
    blind_sites: int = 0
    # Fingerprint of the world this capture was taken in (manifest content, base
    # Python, toolset, container). A baseline is a recording of an environment
    # that stops existing the moment you migrate, so the only way to know the
    # recording still describes *this* project is to stamp its inputs and
    # re-check them at report time. None on old state files, and on projects
    # with no config to fingerprint — absence is "unknown", never "fresh".
    env_fingerprint: str | None = None
    # The previous recording this one displaced, if any: overwriting a capture
    # archives it instead of deleting it, and this is where it went.
    archived_previous: str | None = None
    # Added without a schema bump: old state files load as UNKNOWN and remain
    # readable, but are not silently vouched for by the newer verifier.
    validity: CaptureValidity = CaptureValidity.UNKNOWN
    validity_reason: str | None = None
    privacy_profile: str = "values"
    sample_rate: float = 1.0
    # Runtime-capture observability.  All fields default to ``None``/empty so
    # state written before runtime metrics existed remains loadable and is not
    # misrepresented as a measured zero-loss capture.
    duration_seconds: float | None = None
    deployment_id: str | None = None
    request_id: str | None = None
    correlation_id: str | None = None
    backend: str | None = None
    impact_targets: list[str] = Field(default_factory=list)
    metadata_path: str | None = None
    events_seen: int | None = None
    dropped_events: int | None = None
    sampling_dropped: int | None = None
    backpressure_dropped: int | None = None
    write_failures: int | None = None
    sink_failures: int | None = None
    instrumentation_skipped: list[dict[str, str]] = Field(default_factory=list)


class ContractState(BaseModel):
    """Schema for ``.pymolt/contract_state.json``."""

    schema_version: int = CONTRACT_STATE_SCHEMA_VERSION
    baseline: ContractSlot | None = None
    post_migration: ContractSlot | None = None
    # Failed/empty/corrupt attempts are durable diagnostics, but never replace
    # either active evidence slot above.
    diagnostic_captures: list[ContractSlot] = Field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> "ContractState | None":
        """Read and validate the state file.

        Returns ``None`` when the file is missing or its contents are
        unreadable/invalid (the caller decides how to degrade); never raises.
        """
        if not path.is_file():
            return None
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("Could not read contract state %s: %s", path, e)
            return None
        try:
            return cls.model_validate(raw)
        except ValidationError as e:
            logger.warning("contract state %s failed validation, ignoring: %s", path, e)
            return None

    def save(self, path: Path) -> None:
        """Atomically write state so interruption cannot destroy active evidence."""
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, staged = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(self.model_dump(mode="json"), stream, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(staged, path)
        except BaseException:
            try:
                os.unlink(staged)
            except OSError:
                pass
            raise
