import logging
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

from pymolt.adapters.subprocess_runner import run_command
from pymolt.core.enums import Mode, Provenance, ResolutionQuality, SourceFixation
from pymolt.core.graph import DependencyGraph, Edge, Node
from pymolt.ingestion.detect import (
    DiscoveredSource,
    extract_declared_requirements,
    normalize_pkg_name,
)

logger = logging.getLogger(__name__)

# Upper bound (seconds) for a single `uv pip compile` invocation.
COMPILE_TIMEOUT = 600


def _resolve_with_poetry(manifest_path: Path) -> DependencyGraph | None:
    """Resolve a Poetry project with Poetry's own solver, non-invasively.

    Runs ``poetry lock`` against a temp copy of the project's pyproject (so the
    user's tree is never touched) and parses the resulting lock. Returns None —
    so the caller falls back to uv — when this is not a Poetry project or poetry
    is unavailable/fails.
    """
    if manifest_path.name.lower() != "pyproject.toml":
        return None
    if not shutil.which("poetry"):
        return None
    try:
        with open(manifest_path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    if "poetry" not in data.get("tool", {}):
        return None

    import tempfile

    from pymolt.ingestion import lock_parsers

    with tempfile.TemporaryDirectory(prefix="pymolt_poetry_") as td:
        tdp = Path(td)
        shutil.copy(manifest_path, tdp / "pyproject.toml")
        existing_lock = manifest_path.parent / "poetry.lock"
        if existing_lock.is_file():
            shutil.copy(existing_lock, tdp / "poetry.lock")
        try:
            run_command(["poetry", "lock"], check=True, cwd=str(tdp), timeout=COMPILE_TIMEOUT)
        except (subprocess.SubprocessError, OSError) as e:
            logger.info("poetry lock failed for %s: %s", manifest_path.name, e)
            return None
        lock_file = tdp / "poetry.lock"
        if not lock_file.is_file():
            return None
        try:
            return lock_parsers.parse_poetry_lock(lock_file)
        except (OSError, ValueError) as e:
            logger.info("could not parse poetry lock: %s", e)
            return None

def parse_uv_compile_output(
    output_text: str,
    manifest_name: str,
    declared_reqs: dict[str, str] | None = None
) -> DependencyGraph:
    """Parse the commented stdout of `uv pip compile` to build a DependencyGraph.
    
    Line format:
    anyio==4.3.0
        # via httpx
        # via -r requirements.in
    """
    if declared_reqs is None:
        declared_reqs = {}
    nodes = {}
    edges = []
    
    current_node = None
    
    # Regex to match package name and version (e.g., anyio==4.3.0 or anyio[all]==4.3.0)
    package_pattern = re.compile(r"^([\w\-\.]+)(?:\[[\w\-\.,]+\])?==([\w\-\.\+]+)")
    # Regex to match the # via lines
    via_pattern = re.compile(r"^\s*#\s+via\s+(.+)$")
    
    for line in output_text.splitlines():
        # Check if it is a package line
        package_match = package_pattern.match(line)
        if package_match:
            name = package_match.group(1).lower()
            version = package_match.group(2)
            
            current_node = Node(
                name=name,
                version=version,
                mode=Mode.PYPI,
                provenance=Provenance.PYPI,
                direct=False,
                manual_bridge=False,
                raw={"line": line.strip()}
            )
            
            norm_key = normalize_pkg_name(name)
            if norm_key in declared_reqs:
                current_node.declared_requirement = declared_reqs[norm_key]
                
            nodes[name] = current_node
            continue
            
        # Check if it is a via line
        via_match = via_pattern.match(line)
        if via_match and current_node is not None:
            via_target = via_match.group(1).strip()
            
            # Check if direct manifest reference
            # Typically starts with "-r" or matches the manifest file name
            if via_target.startswith("-r") or manifest_name in via_target or via_target.startswith("pyproject.toml") or via_target.startswith("setup.py"):
                current_node.direct = True
                norm_key = normalize_pkg_name(current_node.name)
                if norm_key in declared_reqs:
                    current_node.declared_requirement = declared_reqs[norm_key]
            else:
                # Transitive dependency edge
                parent_name = via_target.lower()
                # Remove any extras (e.g., "httpx[cli]" -> "httpx")
                if "[" in parent_name:
                    parent_name = parent_name.split("[")[0].strip()
                    
                edges.append(Edge(
                    source=parent_name,
                    target=current_node.name,
                    via="transitive"
                ))

    # Roots are direct dependencies (nodes that have direct=True)
    roots = [name for name, node in nodes.items() if node.direct]
    
    return DependencyGraph(
        nodes=nodes,
        edges=edges,
        roots=roots,
        resolution_quality=ResolutionQuality.RESOLVED,
        source_fixation=SourceFixation.PINNED
    )


def ingest(source: DiscoveredSource, current_python: str | None = None, container_id: str | None = None, constraint_file: Path | None = None, force_recompile: bool = False, tool: str | None = None) -> DependencyGraph:
    """Resolve a PyPI source into a DependencyGraph.

    A lock file is read back verbatim (PINNED fixation, LOCK_PARSED quality);
    a manifest is resolved with a strategy chosen from ``tool`` (the engineer's
    configured toolset): ``poetry`` uses Poetry's solver, ``system`` (or a missing
    uv) uses pip-tools, anything else uses the fast ``uv pip compile`` default.
    Each non-uv strategy falls back to uv on failure. If current_python is < 3.7
    or container_id is provided, falls back to fallback_compiler.compile_legacy.
    When ``force_recompile`` is set, a lock is deliberately ignored and its
    sibling manifest is recompiled instead (prospective target-Python resolution).
    """
    manifest_path = source.path.absolute()
    manifest_name = source.path.name
    manifest_dir = manifest_path.parent

    if source.is_lock:
        # A lock file is an authoritative resolved snapshot. Parse it directly so
        # we surface the exact pinned versions rather than recompiling the
        # manifest (a reconstruction that can drift to different versions).
        # ``force_recompile`` deliberately skips this to re-resolve a manifest
        # for a prospective target Python.
        if not force_recompile:
            from pymolt.ingestion import lock_parsers
            try:
                graph = lock_parsers.parse_pypi_lock(manifest_path)
                if graph is not None:
                    return graph
                logger.info(
                    "Lock file %s is not a directly parseable PyPI lock; "
                    "recompiling sibling manifest instead.", manifest_name
                )
            except (OSError, ValueError) as e:
                logger.warning(
                    "Failed to parse lock file %s directly (%s); "
                    "falling back to recompiling the sibling manifest.", manifest_name, e
                )

        # About to recompile: a lock cannot itself be fed to `uv pip compile`,
        # so substitute its sibling manifest (pyproject/requirements).
        from pymolt.ingestion.detect import detect_sources
        try:
            sibling_sources = detect_sources(manifest_dir)
            non_lock_sources = [s for s in sibling_sources if not s.is_lock]
            if non_lock_sources:
                compile_target = non_lock_sources[0]
                manifest_path = compile_target.path.absolute()
                manifest_name = compile_target.path.name
        except OSError as e:
            logger.warning("Could not scan for a sibling manifest next to %s: %s", manifest_name, e)

    python_ver = current_python
    is_legacy = False
    if python_ver:
        match = re.match(r"^(\d+)\.(\d+)", python_ver)
        if match:
            major = int(match.group(1))
            minor = int(match.group(2))
            if major < 3 or (major == 3 and minor < 7):
                is_legacy = True
                 
    if is_legacy or container_id:
        from pymolt.ingestion import fallback_compiler
        compiled_output = fallback_compiler.compile_legacy(manifest_path, python_ver or "3.6", container_id=container_id, constraint_file=constraint_file)
        declared_reqs = extract_declared_requirements(manifest_path)
        return parse_uv_compile_output(compiled_output, manifest_name, declared_reqs)

    declared_reqs = extract_declared_requirements(manifest_path)

    # Honour the engineer's configured toolset for a fresh manifest resolve.
    if tool == "poetry":
        graph = _resolve_with_poetry(manifest_path)
        if graph is not None:
            return graph
        logger.info("Poetry resolution unavailable for %s; falling back to uv.", manifest_name)

    if tool == "system":
        from pymolt.ingestion import fallback_compiler
        resolve_py = python_ver or f"{sys.version_info.major}.{sys.version_info.minor}"
        compiled_output = fallback_compiler.compile_legacy(
            manifest_path, resolve_py, constraint_file=constraint_file,
            pip_tools_spec="pip-tools", env_prefix="tools_env",
        )
        return parse_uv_compile_output(compiled_output, manifest_name, declared_reqs)

    # Default: compile with uv (fast), relative to the manifest dir so editable
    # installs like '-e .' resolve correctly.
    cmd = ["uv", "pip", "compile", manifest_name]
    if python_ver:
        cmd.extend(["--python-version", python_ver])
    if constraint_file:
        cmd.extend(["--constraint", str(constraint_file.absolute())])

    result = run_command(cmd, check=True, cwd=str(manifest_dir), timeout=COMPILE_TIMEOUT)
    return parse_uv_compile_output(result.stdout, manifest_name, declared_reqs)
