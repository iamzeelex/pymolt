"""Phase 2 — setup: pick manifest, toolset and base -> target Python.

The core here is UI-agnostic: :func:`gather_setup_options` runs all detection and
returns a model of the choices available; :func:`apply_setup` takes the user's
:class:`SetupChoices` and writes ``.pymolt/env_config.json``. No prompts, no
printing — callers drive this same core and own their own I/O.
"""

from pymolt.setup.service import (
    ContainerOption,
    ManifestOption,
    PythonTargetOption,
    SetupChoices,
    SetupError,
    SetupOptions,
    ToolOption,
    apply_setup,
    build_target_options,
    check_setup_values,
    gather_setup_options,
    is_python_version,
)

__all__ = [
    "ContainerOption",
    "ManifestOption",
    "PythonTargetOption",
    "SetupChoices",
    "SetupError",
    "SetupOptions",
    "ToolOption",
    "apply_setup",
    "build_target_options",
    "check_setup_values",
    "gather_setup_options",
    "is_python_version",
]
