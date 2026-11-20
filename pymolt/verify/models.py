"""Typed output contracts for verify/ (host side, Pydantic, Python >=3.12).

These are *data*, not presentation. Any human-readable rendering of a diff or report belongs
in ``interfaces/`` (CLI/MCP), never here — the core stays non-interactive and equally usable
from CLI, MCP, or as a library (``.agent.md``). Like the IngestionReport, every artifact
carries its own trust level (``evidence_level``) and where it is unsure (``honesty``), so a
downstream consumer never has to ask "how good is this verdict?".
"""
import logging
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, computed_field

from pymolt.adapters.python_runner import RunnerSpec
from pymolt.state_io import atomic_write_json, load_state

logger = logging.getLogger(__name__)

# 2: every slot path is stored relative to the project root (a version-1 file, with absolute
# paths, is read as it is: ``load_contract_state`` anchors both, and the next save writes 2).
CONTRACT_STATE_SCHEMA_VERSION = 2
CONTRACT_STATE_KIND = "capture state"
CONTRACT_STATE_REMEDY = (
    "It points at the captured recordings. Restore it from version control or a backup if you "
    "have one; otherwise move it into .pymolt/archive/ and capture again (the baseline before "
    "any code change). The recordings themselves stay in .pymolt/contract_traces/.")


class VerificationVerdict(StrEnum):
    """Machine-facing result of a verification oracle.

    ``INCONCLUSIVE`` is deliberately distinct from ``PASS``: missing, stale,
    opaque, or otherwise insufficient evidence must never become a green CI
    result merely because no concrete incompatibility was observed.
    """

    PASS = "pass"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"


class InconclusiveKind(StrEnum):
    """Who settles an INCONCLUSIVE reason (design decision 7): a person must look
    (``needs_review``), or more or better captures settle it (``needs_evidence``). When both are
    present ``needs_review`` wins, as in the step statuses."""

    NEEDS_REVIEW = "needs_review"
    NEEDS_EVIDENCE = "needs_evidence"


class InconclusiveCause(BaseModel):
    """One reason a verdict is INCONCLUSIVE, with its kind, set where the reason is made.

    ``cause``: ``review`` (a person must look), ``noise`` (a baseline replicate settles it),
    ``environment`` (the captures ran in different environments), ``not-exercised`` (a site no
    run reached), ``uncomparable`` (a fact or value that could not be compared), ``capture``
    (a capture missing, invalid, stale or unlike the other), ``static-map`` (no static
    contact to judge), ``metric`` (a canary metric missing). ``settle``: what settles it."""

    reason: str
    kind: InconclusiveKind
    cause: str
    settle: str


class CaptureValidity(StrEnum):
    """Whether a captured trace is eligible to act as named evidence."""

    VALID = "valid"
    UNKNOWN = "unknown"  # legacy state written before validity was recorded
    COMMAND_FAILED = "command-failed"
    EMPTY = "empty"
    CORRUPT = "corrupt"
    MISSING = "missing"


class TraceArtifactQuality(BaseModel):
    """Cheap structural validation of a JSON/JSONL trace artifact."""

    path: str
    validity: CaptureValidity
    events: int = 0
    comparable_events: int = 0
    invalid_lines: int = 0
    reason: str | None = None


class GoldenDiff(BaseModel):
    """L2 result: value-level diff of golden-master snapshots across the version pair."""

    changed: list[dict[str, Any]] = Field(default_factory=list)  # point whose value moved
    missing: list[str] = Field(default_factory=list)             # point absent on one side
    skipped: list[str] = Field(default_factory=list)             # opaque/nondet -> declined

    def is_clean(self) -> bool:
        return not (self.changed or self.missing)


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
    # The container this capture ran in (None = it ran locally): lets later phases reach the
    # same interpreter and map its paths back to the project.
    runner: RunnerSpec | None = None
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
    # The same without the manifest's contents: which manifest file, base Python,
    # toolset and container. A baseline whose setup is unchanged while only the
    # manifest's contents moved is still the starting point of a migration that
    # bumped pins (see ``service.environment_setup_fingerprint``). None = unknown.
    env_setup_fingerprint: str | None = None
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
    # Stdlib top-level modules traced as dependencies (PYMOLT_TRACE_STDLIB; Python moves).
    traced_stdlib: list[str] = Field(default_factory=list)
    metadata_path: str | None = None
    # ``<trace>.sources.json``: text + sha256 of every project file with a fact (None: an older
    # capture, or none could be written).
    sources_path: str | None = None
    events_seen: int | None = None
    dropped_events: int | None = None
    sampling_dropped: int | None = None
    backpressure_dropped: int | None = None
    write_failures: int | None = None
    sink_failures: int | None = None
    # Seconds the traced program waited for a JSONL trace writer that fell behind (summed over
    # threads and processes): every event was kept, at that cost in timing. None = unknown.
    backpressure_wait_seconds: float | None = None
    instrumentation_skipped: list[dict[str, str]] = Field(default_factory=list)
    # Processes where tracing was requested but the watcher could not load (untraced).
    activation_failures: list[dict] = Field(default_factory=list)
    # A "red" baseline: the command exited non-zero (failing tests) but recorded events,
    # and the capture was accepted on purpose (``--accept-failing``). Holds
    # ``{"returncode": int, "failed": int | None, "summary": str}``; None = not red.
    # The slot is still ``valid``; the stamp only says the run was not green.
    red: dict | None = None
    # A baseline replicate: the ``captured_at`` of the baseline whose command it re-ran on the
    # same code in the same environment (None for every other slot). A replicate whose
    # baseline was replaced is stale and ignored.
    replicate_of: str | None = None
    # A post-migration capture set aside by ``pymolt rollback``: the run id of the migration
    # whose rollback restored the code it recorded (None for every other slot). Kept among the
    # diagnostic captures, its recording moved to the archive so no later capture overwrites it.
    set_aside_by: str | None = None


class ContractState(BaseModel):
    """Schema for ``.pymolt/contract_state.json``."""

    schema_version: int = CONTRACT_STATE_SCHEMA_VERSION
    baseline: ContractSlot | None = None
    post_migration: ContractSlot | None = None
    # The baseline's command run a second time (``pymolt capture baseline --replicate``):
    # what differs between the two runs is run-to-run noise (``verify.replicate``).
    baseline_replicate: ContractSlot | None = None
    # Failed/empty/corrupt attempts are durable diagnostics, but never replace
    # either active evidence slot above.
    diagnostic_captures: list[ContractSlot] = Field(default_factory=list)

    def _anchored(self, root: Path) -> dict:
        """The state as JSON with every slot path stored relative to the project root
        (``root``), so the file stays valid whatever directory pymolt runs from."""
        payload = self.model_dump(mode="json")
        payload["schema_version"] = CONTRACT_STATE_SCHEMA_VERSION
        base = Path(root).resolve()

        def relative(value):
            if not value:
                return value
            candidate = Path(value)
            if not candidate.is_absolute():
                return value
            try:
                return candidate.resolve().relative_to(base).as_posix()
            except (ValueError, OSError):
                return value

        slots = [payload.get("baseline"), payload.get("post_migration"),
                 payload.get("baseline_replicate"),
                 *(payload.get("diagnostic_captures") or [])]
        for slot in slots:
            if isinstance(slot, dict):
                for key in ("trace_path", "metadata_path", "command_log", "sources_path"):
                    slot[key] = relative(slot.get(key))
        return payload

    @classmethod
    def load(cls, path: Path) -> "ContractState | None":
        """Read and validate the state file: None when it is missing. A damaged file, or
        one written by a newer pymolt, raises ``StateFileError`` (``pymolt.state_io``): read
        as empty, the next capture would overwrite every slot's pointer."""
        return load_state(path, cls, kind=CONTRACT_STATE_KIND,
                          newest=CONTRACT_STATE_SCHEMA_VERSION, remedy=CONTRACT_STATE_REMEDY)

    def save(self, path: Path) -> None:
        """Atomically write state so interruption cannot destroy active evidence. A caller
        that read the state to change it holds ``state_lock`` around both."""
        atomic_write_json(path, self._anchored(path.parent.parent))
