"""Typed, validated model for the persisted environment configuration.

``pymolt start`` writes ``.pymolt/env_config.json`` and ``pymolt audit`` reads it
back. Modelling it with pydantic gives a single schema definition, defaults for
missing keys, silent dropping of unknown keys (forward/backward migration), and
a ``schema_version`` so future changes can be migrated deliberately.
"""

import json
import logging
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError, field_validator

logger = logging.getLogger(__name__)

CONFIG_SCHEMA_VERSION = 1


class ToolChoice(str, Enum):
    """Resolution toolset the engineer selected.

    ``uv`` is the fast default. The others record the engineer's existing
    toolset so resolution can honour it rather than imposing uv (the actual
    per-tool resolver branching is a follow-up; today only ``container`` changes
    the resolution path, via ``container_id``).
    """

    UV = "uv"
    CONDA = "conda"
    POETRY = "poetry"
    CONTAINER = "container"
    SYSTEM = "system"


class EnvConfig(BaseModel):
    """Schema for ``.pymolt/env_config.json``."""

    schema_version: int = CONFIG_SCHEMA_VERSION
    selected_manifest: str | None = None
    selected_tool: ToolChoice = ToolChoice.UV
    base_python: str | None = None
    # How base_python was arrived at. "declared" = read from the project (a
    # .python-version, requires-python, Dockerfile, …); "assumed" = nothing
    # declared one, so pymolt fell back to the interpreter it happened to run
    # on. The distinction has to survive to disk: everything downstream resolves
    # the legacy baseline against this number, and a guess presented as a fact
    # is exactly what the honesty markers exist to prevent. Defaults to None
    # ("unknown") so configs written before this field still load.
    base_python_source: str | None = None
    container_id: str | None = None
    # Chosen interpreter for the `system` toolset (path or version); None = auto-detect.
    system_python: str | None = None
    target_python: str | None = None
    # Informational only: env creation/installation is the engineer's toolset's
    # responsibility; audit uses these for interpreter display, not resolution.
    target_env_created: bool = False
    target_env_path: str | None = None
    target_overrides: dict[str, str] = Field(default_factory=dict)

    @field_validator("base_python", "target_python")
    @classmethod
    def _must_look_like_a_version(cls, value: str | None) -> str | None:
        """Reject a Python version that isn't one.

        Every later phase resolves against these two fields, so a stray answer
        at a prompt (the literal ``q`` has happened) would otherwise be written
        to disk and silently steer the whole migration. Deliberately permissive
        about the tail — ``3``, ``3.12``, ``3.6.1``, ``3.13.0rc1`` are all real —
        and strict about the one thing that identifies a version: it starts with
        a digit.
        """
        if value is None or value == "":
            return value
        if not value[0].isdigit():
            raise ValueError(
                f"{value!r} is not a Python version (expected e.g. 3.11 or 3.6.1)"
            )
        return value

    @classmethod
    def load(cls, path: Path) -> "EnvConfig | None":
        """Read and validate the config file.

        Returns ``None`` when the file is missing or its contents are unreadable/
        invalid (the caller decides how to degrade); never raises.
        """
        if not path.is_file():
            return None
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("Could not read env config %s: %s", path, e)
            return None
        try:
            return cls.model_validate(raw)
        except ValidationError as e:
            logger.warning("env config %s failed validation, ignoring: %s", path, e)
            return None

    def save(self, path: Path) -> None:
        """Write the config as JSON (creating the parent directory if needed)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.model_dump(mode="json"), f, indent=2)
