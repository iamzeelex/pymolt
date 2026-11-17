"""Persisted receipt for the mutating part of a migration.

The contract captures describe the worlds before and after a migration.  This
receipt is the missing link between them: it proves that the mutating command
actually ran, records which baseline authorised it, and gives ``status`` a
durable phase to project instead of guessing from the presence of two traces.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, PrivateAttr
from pydantic_core import ValidationError

MIGRATION_STATE_SCHEMA_VERSION = 1


class MigrationReceipt(BaseModel):
    """A successful ``pymolt apply`` run."""

    schema_version: int = MIGRATION_STATE_SCHEMA_VERSION
    run_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    # The plan the run applied. A plan applied again after a rollback is a new run with its
    # own id; None in receipts from before this was recorded, when the two ids were equal.
    plan_id: str | None = None
    status: Literal["applied", "rolled_back"] = "applied"
    # Capture timestamps are second-granularity; use the same clock precision so
    # a post capture taken immediately after apply is not misclassified as older.
    applied_at: str = Field(
        default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds")
    )
    target_python: str
    source_manifest: str
    target_manifest_path: str | None = None
    baseline_trace_path: str | None = None
    baseline_captured_at: str | None = None
    # The environment fingerprint the migration started from, stamped before any
    # source write. A baseline is judged against this, not against the project
    # as it is now: a migration that upgrades the manifest moves "now" away from
    # the baseline by design.
    environment_fingerprint: str | None = None
    # Its setup part (manifest file, base Python, toolset, container). A baseline with the
    # same setup is the migration's starting point even when the manifest's pins moved
    # between the capture and the apply. None in receipts from before it was recorded.
    environment_setup_fingerprint: str | None = None
    allow_no_baseline: bool = False
    files_changed: list[str] = Field(default_factory=list)
    patterns_applied: int = 0
    no_codemods_required: bool = False
    plan_path: str | None = None
    rolled_back_at: str | None = None
    # The project files the rollback rewrote ([] = it restored no file: the run had changed
    # none). None = not rolled back, or rolled back before this was recorded.
    files_restored: list[str] | None = None
    # What the apply call that returned this receipt did; not saved. ``already_applied``: the
    # plan was applied before and its files still hold its changes, so nothing was written.
    _outcome: Literal["applied", "already_applied"] = PrivateAttr(default="applied")

    @property
    def outcome(self) -> str:
        return self._outcome

    def mark_outcome(self, outcome: Literal["applied", "already_applied"]) -> None:
        self._outcome = outcome

    @property
    def in_effect(self) -> bool:
        """Whether the code on disk is the code this run left: it is applied, or its rollback
        restored no file. Verify reads such a run's plan; a rollback that restored files
        took the migration's code away, and with it the plan's say over verify."""
        return self.status == "applied" or self.files_restored == []

    def starting_fingerprint(self, project_dir: str | Path) -> str | None:
        """The environment fingerprint this migration started from; None if unrecorded.

        Receipts written before the field existed fall back to the frozen plan:
        apply then refused a plan whose fingerprint differed from the project's at
        apply time, so for such a run the two are the same value.
        """
        if self.environment_fingerprint is not None:
            return self.environment_fingerprint
        if not self.plan_path:
            return None
        from pymolt.migration_plan import MigrationPlan

        plan = MigrationPlan.load(project_dir, self.plan_path)
        return plan.environment_fingerprint if plan is not None else None

    @classmethod
    def load(cls, project_dir: str | Path) -> MigrationReceipt | None:
        path = receipt_path(project_dir)
        if not path.is_file():
            return None
        try:
            with path.open(encoding="utf-8") as stream:
                return cls.model_validate(json.load(stream))
        except (OSError, json.JSONDecodeError, ValidationError):
            return None

    def save(self, project_dir: str | Path) -> Path:
        """Atomically persist the receipt after all code writes have succeeded."""
        path = receipt_path(project_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, staged_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(self.model_dump(mode="json"), stream, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(staged_name, path)
        except BaseException:
            try:
                os.unlink(staged_name)
            except OSError:
                pass
            raise
        return path


def receipt_path(project_dir: str | Path) -> Path:
    return Path(project_dir) / ".pymolt" / "migration_state.json"
