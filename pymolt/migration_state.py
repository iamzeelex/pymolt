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

from pydantic import BaseModel, Field
from pydantic_core import ValidationError

MIGRATION_STATE_SCHEMA_VERSION = 1


class MigrationReceipt(BaseModel):
    """A successful ``pymolt migrate --write`` run."""

    schema_version: int = MIGRATION_STATE_SCHEMA_VERSION
    run_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
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
    allow_no_baseline: bool = False
    files_changed: list[str] = Field(default_factory=list)
    patterns_applied: int = 0
    no_codemods_required: bool = False
    plan_path: str | None = None
    rolled_back_at: str | None = None

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
