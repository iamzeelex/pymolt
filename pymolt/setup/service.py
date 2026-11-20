"""UI-agnostic core for the setup phase.

``gather_setup_options`` performs all the detection (manifests, tools, containers,
local envs, candidate target Pythons) and returns it as a model. ``apply_setup``
takes the user's selections and writes the persisted ``EnvConfig``. Neither
prompts nor prints — callers drive these and own their own I/O.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from pydantic import BaseModel, Field

from pymolt.core.enums import Mode
from pymolt.ingestion.config import EnvConfig, ToolChoice
from pymolt.ingestion.detect import (
    detect_local_environments,
    detect_manifest,
    detect_project_python_version,
    detect_sources,
    detect_system_tools,
)
from pymolt.ingestion.fallback_compiler import list_project_containers
from pymolt.setup.pythons import (
    classify_eol,
    get_available_python_versions,
    get_eol_versions,
    version_floor,
)

# The resolution toolsets offered, in display order.
_TOOL_ORDER = ["uv", "conda", "poetry", "container", "system"]


class ManifestOption(BaseModel):
    name: str            # the manifest's path relative to the project (its identity)
    mode: str            # Mode value (pip / conda / ...)
    fixation: str        # SourceFixation value
    is_lock: bool
    is_default: bool = False


class ToolOption(BaseModel):
    name: str            # uv | conda | poetry | container | system
    available: bool
    detail: str          # short status, e.g. "found (/usr/bin/uv)"
    is_default: bool = False


class ContainerOption(BaseModel):
    id: str
    name: str = ""
    python: str | None = None


class PythonTargetOption(BaseModel):
    version: str         # major.minor
    eol_date: str | None = None
    eol_status: str = "unknown"   # supported | soon | eol | unknown
    is_default: bool = False


class SetupOptions(BaseModel):
    """Everything the user picks from, detected offline + via uv/EOL lookups."""

    project_dir: str
    manifests: list[ManifestOption] = Field(default_factory=list)
    tools: list[ToolOption] = Field(default_factory=list)
    containers: list[ContainerOption] = Field(default_factory=list)
    interpreters: list[str] = Field(default_factory=list)  # for the `system` toolset
    local_envs: list[str] = Field(default_factory=list)
    system_tools: dict[str, str | None] = Field(default_factory=dict)
    detected_python: str | None = None
    base_python_default: str = ""
    #: "declared" when the project states its Python version somewhere;
    #: "assumed" when nothing does and the default above is merely the
    #: interpreter pymolt is running on. Interfaces must show the difference.
    base_python_source: str = "declared"
    target_versions: list[PythonTargetOption] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @property
    def default_manifest(self) -> str | None:
        for m in self.manifests:
            if m.is_default:
                return m.name
        return self.manifests[0].name if self.manifests else None

    @property
    def default_tool(self) -> str:
        for t in self.tools:
            if t.is_default:
                return t.name
        return ToolChoice.UV.value

    @property
    def default_target(self) -> str | None:
        for v in self.target_versions:
            if v.is_default:
                return v.version
        return self.target_versions[0].version if self.target_versions else None


class SetupChoices(BaseModel):
    """The user's selections, fed back into :func:`apply_setup`."""

    selected_manifest: str
    selected_tool: ToolChoice
    base_python: str
    #: "declared" (the project states this version somewhere) or "assumed"
    #: (nothing did, so this is pymolt's own interpreter). Carried to disk so
    #: later phases can tell a fact from a fallback.
    base_python_source: str | None = None
    target_python: str
    container_id: str | None = None
    system_python: str | None = None
    target_env_created: bool = False
    target_env_path: str | None = None


#: Manifest stems that describe a *variant* of the project rather than the
#: project: a docs build, a test extra, one row of a CI matrix. Picking one of
#: these as the source of truth answers the wrong question — which is what
#: taking whatever sorted first used to do (six manifests, `requirements-310.txt`
#: wins on the alphabet, and the whole assess runs on the Python-3.10 row).
_VARIANT_MARKERS = ("dev", "test", "tests", "doc", "docs", "rtd", "ci", "lint",
                    "typing", "build", "bench", "example", "examples", "optional")


#: Names that state the project's dependencies outright.
_CANONICAL_MANIFESTS = {"requirements.txt", "requirements.in", "pyproject.toml",
                        "pipfile", "environment.yml", "environment.yaml"}
#: Packaging metadata: its dependencies are only partly readable (computed
#: values are not executed) — a last resort, never the assumed source of truth.
_UNCLASSIFIED_MANIFESTS = {"setup.py", "setup.cfg"}


def _manifest_rank(source) -> tuple:
    """Sort key for "which of these is the project's real dependency set?".

    Lower sorts first, in tiers: a lock (the exact world that was installed),
    then a canonical manifest, then a decorated one, then a variant — a docs
    build, a test extra, one row of a Python matrix — and finally setup.py,
    whose edges pymolt does not parse yet. Taking whatever sorted first
    alphabetically is how a six-manifest project got assessed on the
    Python-3.10 row of its CI matrix.
    """
    name = source.path.name
    lowered = name.lower()
    stem = lowered.rsplit(".", 1)[0]
    tail = stem.replace("requirements", "").strip("-_.")
    words = [w for w in tail.replace("_", "-").split("-") if w]
    variant = any(w in _VARIANT_MARKERS for w in words)
    # "310" / "39" / "py38" — a per-interpreter row, not the project.
    version_specific = any(w.replace("py", "").replace(".", "").isdigit() for w in words)

    if source.subroot:
        # A part of the repo, not the project at its top: never assumed.
        tier = 5
    elif source.is_lock:
        tier = 0
    elif lowered in _UNCLASSIFIED_MANIFESTS:
        tier = 4
    elif variant or version_specific:
        tier = 3
    elif lowered in _CANONICAL_MANIFESTS:
        tier = 1
    else:
        tier = 2
    return (tier, len(name), lowered)


def _pick_default_manifest(sources):
    """The manifest to assume when the engineer does not choose one."""
    return min(sources, key=_manifest_rank) if sources else None


def gather_setup_options(project_dir: str | Path) -> SetupOptions:
    """Detect manifests, tools, containers and candidate Pythons for setup."""
    project_path = Path(project_dir)
    notes: list[str] = []

    sources = detect_sources(project_path, include_subroots=True)
    default_source = _pick_default_manifest(sources)
    manifests = [
        ManifestOption(
            name=src.ref(project_path),
            mode=src.mode.value,
            fixation=src.fixation.value,
            is_lock=src.is_lock,
            is_default=(src is default_source),
        )
        for src in sources
    ]
    if not sources:
        notes.append(f"No Python dependency sources detected in {project_path}.")
    elif len(sources) > 1 and default_source is not None:
        others = [s.ref(project_path) for s in sources if s is not default_source]
        notes.append(
            f"{len(sources)} manifests here; assuming {default_source.ref(project_path)} is the "
            f"source of truth. Others: {', '.join(others)}."
        )

    local_envs = detect_local_environments(project_path)
    system_tools = detect_system_tools()

    detected_python = detect_project_python_version(project_path)
    current_python = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    base_python_default = detected_python or current_python
    base_python_source = "declared" if detected_python else "assumed"
    if not detected_python:
        notes.append(
            f"No Python version is declared anywhere in this project — assuming "
            f"{current_python}, the interpreter pymolt is running on. If the project "
            "actually targets an older Python, set it explicitly: the base dependencies "
            "are resolved against this number."
        )

    # Call the lister unconditionally — it self-guards on docker availability, so
    # we don't depend on the (sometimes flaky) daemon-liveness probe in system_tools.
    # Only a container that mounts this project is offered.
    containers: list[ContainerOption] = []
    try:
        for c in list_project_containers(project_path):
            containers.append(
                ContainerOption(id=c["id"], name=c.get("name", ""),
                                python=c.get("python") or None)
            )
    except Exception as e:  # docker probing is best-effort
        notes.append(f"Container detection skipped: {e}")
    if not containers:
        notes.append("No running container mounts this project.")

    tools = _build_tool_options(sources, system_tools, containers)
    target_versions = build_target_options(base_python_default)

    return SetupOptions(
        project_dir=str(project_path.resolve()),
        manifests=manifests,
        tools=tools,
        containers=containers,
        interpreters=list_interpreters(project_path),
        local_envs=local_envs,
        system_tools=system_tools,
        detected_python=detected_python,
        base_python_default=base_python_default,
        base_python_source=base_python_source,
        target_versions=target_versions,
        notes=notes,
    )


def list_interpreters(project_dir: str | Path) -> list[str]:
    """Concrete Python interpreters found on the machine (paths) for the `system` toolset.

    Local-venv interpreters first (most likely the engineer's target), then the
    system `python3`/`python` on PATH. Versions are appended via uv as a fallback
    so there is always something to pick.
    """
    project_path = Path(project_dir)
    out: list[str] = []
    for env in detect_local_environments(project_path):
        for sub in ("bin/python", "Scripts/python.exe"):
            p = project_path / env / sub
            if p.exists():
                out.append(str(p))
    for name in ("python3", "python"):
        found = shutil.which(name)
        if found:
            out.append(found)
    return list(dict.fromkeys(out))


def _build_tool_options(sources, system_tools, containers) -> list[ToolOption]:
    if sources and sources[0].mode == Mode.CONDA:
        default = "conda"
    elif system_tools.get("uv"):
        default = "uv"
    elif system_tools.get("poetry"):
        default = "poetry"
    else:
        default = "system"

    options: list[ToolOption] = []
    for name in _TOOL_ORDER:
        if name == "container":
            available = bool(containers)
            detail = (
                f"{len(containers)} running container(s) mounting the project" if containers
                else "no running container mounts the project"
            )
        elif name == "system":
            available = True
            detail = "system python compiler"
        else:
            path = system_tools.get(name)
            available = bool(path)
            detail = f"found ({path})" if path else "not found"
        options.append(
            ToolOption(name=name, available=available, detail=detail, is_default=(name == default))
        )
    return options


def build_target_options(base_python: str) -> list[PythonTargetOption]:
    """Candidate target Pythons (>= the base's resolver floor), with EOL + default.

    Exposed so caller interfaces can recompute the list when the user changes the base
    Python, matching what the CLI's interactive picker does.
    """
    floor = version_floor(base_python)
    eol_map = get_eol_versions()
    available = [
        v for v in get_available_python_versions()
        if [int(p) for p in v.split(".")] >= floor
    ]
    classified = [(v, *classify_eol(v, eol_map)) for v in available]

    # Default target: the lowest version that is not (about to be) EOL — a
    # migration tool must not suggest landing on an already-dead Python. The
    # smaller jumps stay in the list for engineers who migrate incrementally.
    def _first_with(status: str) -> str | None:
        return next((v for v, _date, s in classified if s == status), None)

    default_version = _first_with("supported") or _first_with("soon") or _first_with("unknown")
    if default_version is None:
        # Every candidate is EOL — fall back to the version just above the base.
        base_mm = ".".join(base_python.split(".")[:2]) if base_python else None
        default_version = available[0] if available else None
        if base_mm and base_mm in available:
            idx = available.index(base_mm)
            default_version = available[idx + 1] if idx + 1 < len(available) else available[idx]

    return [
        PythonTargetOption(
            version=v,
            eol_date=eol_date,
            eol_status=status,
            is_default=(v == default_version),
        )
        for v, eol_date, status in classified
    ]



class SetupError(ValueError):
    """A setup value pymolt cannot act on. ``hint`` names the valid ones."""

    def __init__(self, message: str, hint: str) -> None:
        super().__init__(message)
        self.hint = hint


def is_python_version(value: str) -> bool:
    """`3`, `3.12`, `3.12.1` — and nothing else."""
    parts = value.split(".")
    return 1 <= len(parts) <= 3 and all(p.isdigit() for p in parts)


def check_setup_values(
    options: SetupOptions,
    *,
    manifest: str | None = None,
    tool: str | None = None,
    base_python: str | None = None,
    target_python: str | None = None,
    container_id: str | None = None,
) -> None:
    """Reject a given value that later phases could not act on. ``None`` = not given.

    Whether a well-formed target Python must also be on the offered list is the
    caller's policy: uv may provision interpreters the list does not show.
    """
    manifests = [m.name for m in options.manifests]
    if manifest is not None and manifest not in manifests:
        # A path the scan did not list is still fine if it is a supported manifest
        # inside the project (e.g. a lock under a directory scan does not treat as a root).
        project = options.project_dir
        if Path(manifest).is_absolute() or ".." in Path(manifest).parts:
            example = manifests[0] if manifests else "requirements.txt"
            raise SetupError(f"manifest {manifest!r} must be a path inside the project",
                             f"Give it relative to {project}, e.g. {example}.")
        if not (Path(project) / manifest).is_file():
            raise SetupError(f"unknown manifest {manifest!r}: no such file in the project",
                             f"Detected: {', '.join(manifests)}.")
        if detect_manifest(project, manifest) is None:
            raise SetupError(f"manifest {manifest!r} is not a supported format",
                             "Supported: requirements*.txt/.in, pyproject.toml, Pipfile, "
                             "setup.py/setup.cfg, environment.yml, and the lock files.")
    available = [t.name for t in options.tools if t.available]
    if tool is not None and tool not in available:
        raise SetupError(
            f"tool {tool!r} is not available here", f"Available: {', '.join(available)}.",
        )
    for label, value in (("base Python", base_python), ("target Python", target_python)):
        if value is not None and not is_python_version(value):
            raise SetupError(
                f"{label} {value!r} is not a Python version", "Expected e.g. 3.11 or 3.6.1.",
            )
    if tool == "container" and not container_id:
        running = ", ".join(c.id for c in options.containers) or "none running"
        raise SetupError("a container is required with the container tool", f"Running: {running}.")


def apply_setup(
    project_dir: str | Path, choices: SetupChoices, *, notes: list[str] | None = None,
) -> EnvConfig:
    """Persist the chosen config to ``.pymolt/env_config.json`` and return it.

    Also creates the ``.pymolt`` folder and adds the config to ``.gitignore``
    (best-effort). Every side effect outside ``.pymolt/`` is appended to ``notes``
    when given, so the caller can say what it changed in the user's project. Refuses
    (``StateFileError``) to replace a damaged config or one a newer pymolt wrote.
    """
    project_path = Path(project_dir)
    pymolt_dir = project_path / ".pymolt"
    # A damaged config, or one a newer pymolt wrote, is not replaced (``StateFileError``).
    EnvConfig.load(pymolt_dir / "env_config.json")
    pymolt_dir.mkdir(parents=True, exist_ok=True)
    (pymolt_dir / "cache").mkdir(exist_ok=True)
    added = _ensure_gitignore(project_path)
    if added and notes is not None:
        notes.append(f"Added {_GITIGNORE_ENTRY} to {project_path / '.gitignore'}.")

    config = EnvConfig(
        selected_manifest=choices.selected_manifest,
        selected_tool=choices.selected_tool,
        base_python=choices.base_python,
        base_python_source=choices.base_python_source,
        container_id=choices.container_id,
        runner=_container_runner(choices),
        system_python=choices.system_python,
        target_python=choices.target_python,
        target_env_created=choices.target_env_created,
        target_env_path=choices.target_env_path,
    )
    config.save(pymolt_dir / "env_config.json")
    return config


def _container_runner(choices: SetupChoices):
    """How to reach the chosen container again (name, mounts, platform); None if unreadable."""
    from pymolt.adapters.python_runner import inspect_container

    if choices.selected_tool != ToolChoice.CONTAINER or not choices.container_id:
        return None
    return inspect_container(choices.container_id)


_GITIGNORE_ENTRY = ".pymolt/env_config.json"


def _ensure_gitignore(project_path: Path) -> bool:
    """Append the config to an existing ``.gitignore``. True only when it wrote."""
    gitignore_path = project_path / ".gitignore"
    try:
        if not gitignore_path.exists():
            return False
        content = gitignore_path.read_text(encoding="utf-8")
        if _GITIGNORE_ENTRY in content:
            return False
        separator = "\n" if content and not content.endswith("\n") else ""
        with open(gitignore_path, "a", encoding="utf-8") as f:
            f.write(f"{separator}{_GITIGNORE_ENTRY}\n")
        return True
    except OSError:
        return False
