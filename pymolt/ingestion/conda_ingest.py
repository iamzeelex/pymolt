import json
import logging
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import yaml

from pymolt.adapters.subprocess_runner import run_command
from pymolt.core.enums import Mode, Provenance, ResolutionQuality, SourceFixation
from pymolt.core.graph import DependencyGraph, Edge, Node
from pymolt.ingestion.detect import detect_project_python_version

logger = logging.getLogger(__name__)

# Upper bound (seconds) for a single dependency-solver invocation so a hung
# conda/mamba solve can never block the whole ingestion indefinitely.
SOLVE_TIMEOUT = 600


def parse_environment_yaml(content: str) -> dict[str, list]:
    """Parse an environment.yml file into its conda and pip dependency lists.

    Uses a real YAML parser (``yaml.safe_load``) so flow-style lists, quoting,
    anchors and nested ``pip:`` blocks are handled correctly instead of the
    previous indentation-sensitive line scanner.

    Returns:
        {
            "dependencies": [...],  # conda dependency specs as strings
            "pip": [...]            # pip dependency specs as strings
        }
    """
    dependencies: list = []
    pip_dependencies: list = []

    try:
        data = yaml.safe_load(content)
    except yaml.YAMLError as e:
        logger.warning("Failed to parse environment YAML: %s", e)
        return {"dependencies": dependencies, "pip": pip_dependencies}

    if not isinstance(data, dict):
        return {"dependencies": dependencies, "pip": pip_dependencies}

    raw_deps = data.get("dependencies") or []
    if not isinstance(raw_deps, list):
        return {"dependencies": dependencies, "pip": pip_dependencies}

    for entry in raw_deps:
        # The pip sub-block is encoded as a mapping: {"pip": [...]}.
        if isinstance(entry, dict):
            pip_block = entry.get("pip")
            if isinstance(pip_block, list):
                for pip_dep in pip_block:
                    if pip_dep is not None:
                        pip_dependencies.append(str(pip_dep).strip())
            continue
        if entry is None:
            continue
        spec = str(entry).strip()
        if spec and spec != "pip":
            dependencies.append(spec)

    return {"dependencies": dependencies, "pip": pip_dependencies}


def parse_conda_lock_yaml(content: str) -> list:
    """Parse a conda-lock.yml file into a list of package records.

    Each record always carries ``name``/``version``/``manager`` and, when the
    lock provides them, the transitive ``dependencies`` (a mapping of dependency
    name -> version constraint) and the ``category`` (``main``/``dev``). The
    extra keys are only attached when present so simpler locks keep producing
    minimal records.
    """
    try:
        data = yaml.safe_load(content)
    except yaml.YAMLError as e:
        logger.warning("Failed to parse conda-lock YAML: %s", e)
        return []

    if not isinstance(data, dict):
        return []

    raw_packages = data.get("package") or []
    if not isinstance(raw_packages, list):
        return []

    packages = []
    for raw in raw_packages:
        if not isinstance(raw, dict):
            continue
        name = raw.get("name")
        if not name:
            continue
        record: dict[str, Any] = {"name": str(name)}
        if raw.get("version") is not None:
            record["version"] = str(raw["version"])
        if raw.get("manager") is not None:
            record["manager"] = str(raw["manager"])
        deps = raw.get("dependencies")
        if isinstance(deps, dict) and deps:
            record["dependencies"] = {str(k): str(v) for k, v in deps.items()}
        category = raw.get("category")
        if category is not None:
            record["category"] = str(category)
        packages.append(record)

    return packages


def find_conda_executable_on_host() -> str | None:
    """Search system PATH and common miniconda/anaconda install locations for conda executable."""
    # 1. Check standard PATH executables
    for exe in ["mamba", "conda", "micromamba"]:
        path = shutil.which(exe)
        if path:
            return path

    # 2. Check common installations
    home = Path.home()
    candidates = [
        home / "miniconda3" / "condabin" / "conda",
        home / "miniconda3" / "bin" / "conda",
        home / "anaconda3" / "condabin" / "conda",
        home / "anaconda3" / "bin" / "conda",
        Path("/opt/miniconda3/condabin/conda"),
        Path("/opt/miniconda3/bin/conda"),
        Path("/opt/anaconda3/condabin/conda"),
        Path("/opt/anaconda3/bin/conda"),
        Path("/usr/local/miniconda3/condabin/conda"),
        Path("/usr/local/miniconda3/bin/conda"),
        Path("/usr/local/anaconda3/condabin/conda"),
        Path("/usr/local/anaconda3/bin/conda"),
    ]
    for cand in candidates:
        if cand.is_file():
            return str(cand)
    return None


def find_conda_executable_in_container(container_id: str) -> str | None:
    """Search for mamba/conda/micromamba inside the running docker container."""
    for exe in ["mamba", "conda", "micromamba"]:
        try:
            res = subprocess.run(
                ["docker", "exec", container_id, "which", exe],
                capture_output=True, text=True, timeout=3
            )
            if res.returncode == 0 and res.stdout.strip():
                return exe
        except Exception:
            pass
    return None


def parse_conda_solve_json(json_text: str) -> list[dict[str, str]]:
    """Parse output JSON from conda/mamba env solve robustly."""
    text = json_text.strip()
    try:
        data = json.loads(text)
        return _extract_packages_from_inspect(data)
    except Exception:
        # Find the outer JSON boundaries to isolate it from logs/warnings
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                data = json.loads(text[start:end+1])
                return _extract_packages_from_inspect(data)
            except Exception:
                pass
    return []


def _extract_packages_from_inspect(data: dict) -> list[dict[str, str]]:
    packages = []
    # Direct list output
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and "name" in item:
                packages.append(item)
        return packages
        
    actions = data.get("actions", {})
    link_list = actions.get("LINK", [])
    for item in link_list:
        if isinstance(item, dict):
            name = item.get("name")
            version = item.get("version")
            channel = item.get("channel", "")
            if name and version:
                packages.append({"name": name, "version": version, "channel": channel})
        elif isinstance(item, str):
            # Parse dist string (e.g., conda-forge::numpy-1.22.0-py39_0)
            channel = ""
            val = item
            if "::" in val:
                channel, val = val.split("::", 1)
            parts = val.rsplit("-", 2)
            if len(parts) >= 2:
                packages.append({"name": parts[0], "version": parts[1], "channel": channel})
    return packages


def ingest(env_file: str | Path, lock_file: str | Path | None = None, container_id: str | None = None, force_recompile: bool = False, base_python: str | None = None) -> DependencyGraph:
    """Ingest conda package structure:
    LEVEL 1: conda/mamba on PATH -> real solve (RESOLVED)
    LEVEL 2: no conda, lock present -> parse lock (LOCK_PARSED)
    LEVEL 3: only environment.yml -> static parse (DECLARED_ONLY)

    ``force_recompile`` skips the verbatim lock level so a prospective target
    resolution is re-derived from the environment rather than echoing the
    baseline lock. ``base_python`` is the configured project Python used to match
    the right interpreter when solving inside a container.
    """
    env_path = Path(env_file)
    lock_path = Path(lock_file) if lock_file else None

    nodes = {}
    edges = []
    roots = []

    # LEVEL 1: Solver integration (if local solver or container solver is available)
    if env_path.is_file():
        solver_exe = None
        is_container_solve = False
        
        if container_id:
            solver_exe = find_conda_executable_in_container(container_id)
            is_container_solve = solver_exe is not None
            
        if not solver_exe:
            solver_exe = find_conda_executable_on_host()
            
        if solver_exe:
            try:
                solved_packages = []
                if is_container_solve:
                    from pymolt.ingestion.fallback_compiler import (
                        find_running_containers_for_project,
                    )
                    proj_py_ver = base_python or detect_project_python_version(env_path.parent) or "3.12"
                    containers = find_running_containers_for_project(env_path.parent, proj_py_ver)
                    container_info = None
                    for c in containers:
                        if c["id"] == container_id or c["name"] == container_id:
                            container_info = c
                            break
                    if container_info:
                        container_cwd = container_info["cwd"]
                        env_rel_name = env_path.name
                        
                        cmd = [
                            "docker", "exec", "-w", container_cwd, container_id,
                            solver_exe, "env", "create", "-f", env_rel_name, "--dry-run", "--json"
                        ]
                        result = run_command(cmd, check=True, timeout=SOLVE_TIMEOUT)
                        solved_packages = parse_conda_solve_json(result.stdout)
                else:
                    cmd = [solver_exe, "env", "create", "-f", str(env_path), "--dry-run", "--json"]
                    result = run_command(cmd, check=True, cwd=str(env_path.parent), timeout=SOLVE_TIMEOUT)
                    solved_packages = parse_conda_solve_json(result.stdout)

                if solved_packages:
                    content = env_path.read_text(encoding="utf-8")
                    parsed_declared = parse_environment_yaml(content)
                    
                    direct_conda_names = set()
                    for dep in parsed_declared["dependencies"]:
                        parts = re.split(r"[>=<!]", dep)
                        direct_conda_names.add(parts[0].strip().lower())
                        
                    direct_pip_names = set()
                    declared_pip_requirements = {}
                    for dep in parsed_declared["pip"]:
                        parts = re.split(r"[>=<!]", dep)
                        name_norm = parts[0].strip().lower()
                        direct_pip_names.add(name_norm)
                        declared_pip_requirements[name_norm] = dep

                    for pkg in solved_packages:
                        name = pkg["name"].lower()
                        version = pkg["version"]
                        channel = pkg.get("channel", "")
                        
                        prov = Provenance.CONDA_FORGE
                        if "pypi" in channel.lower():
                            prov = Provenance.PIP_IN_CONDA
                            
                        is_direct = name in direct_conda_names
                        
                        nodes[name] = Node(
                            name=name,
                            version=version,
                            mode=Mode.CONDA,
                            provenance=prov,
                            direct=is_direct,
                            manual_bridge=False,
                            raw=pkg
                        )
                        if is_direct:
                            for dep in parsed_declared["dependencies"]:
                                if dep.startswith(name):
                                    nodes[name].declared_requirement = dep
                                    break
                            roots.append(name)
                            
                    for pip_name in direct_pip_names:
                        if pip_name not in nodes:
                            nodes[pip_name] = Node(
                                name=pip_name,
                                version=None,
                                mode=Mode.CONDA,
                                provenance=Provenance.PIP_IN_CONDA,
                                direct=True,
                                declared_requirement=declared_pip_requirements[pip_name],
                                manual_bridge=False,
                                raw={"declared_dep": declared_pip_requirements[pip_name]}
                            )
                            roots.append(pip_name)
                        else:
                            nodes[pip_name].direct = True
                            nodes[pip_name].provenance = Provenance.PIP_IN_CONDA
                            nodes[pip_name].declared_requirement = declared_pip_requirements[pip_name]
                            if pip_name not in roots:
                                roots.append(pip_name)

                    return DependencyGraph(
                        nodes=nodes,
                        edges=edges,
                        roots=roots,
                        resolution_quality=ResolutionQuality.RESOLVED,
                        source_fixation=SourceFixation.PINNED
                    )
            except (subprocess.SubprocessError, OSError, ValueError) as e:
                logger.warning(
                    "Live conda solve failed (%s); falling back to lock/static parsing.", e
                )

    # LEVEL 2: Parse conda-lock if present
    if lock_path and lock_path.is_file() and not force_recompile:
        try:
            content = lock_path.read_text(encoding="utf-8")
            packages = parse_conda_lock_yaml(content)

            # A conda-lock holds the full transitive closure but does not flag
            # which packages are top-level. Recover that from the declared
            # environment file when it is available; otherwise fall back to the
            # lock's own ``category`` (main == declared) so roles are not lost.
            declared_direct: set = set()
            env_available = env_path.is_file()
            if env_available:
                try:
                    parsed_env = parse_environment_yaml(env_path.read_text(encoding="utf-8"))
                    for dep in parsed_env["dependencies"] + parsed_env["pip"]:
                        declared_direct.add(re.split(r"[>=<!~ ]", dep)[0].strip().lower())
                except OSError as e:
                    logger.warning("Could not read environment file %s: %s", env_path, e)
                    env_available = False

            any_category = any("category" in pkg for pkg in packages)

            for pkg in packages:
                name = pkg["name"].lower()
                version = pkg.get("version")
                manager = pkg.get("manager", "conda")

                prov = Provenance.CONDA_FORGE if manager == "conda" else Provenance.PIP_IN_CONDA

                if declared_direct:
                    is_direct = name in declared_direct
                elif any_category:
                    is_direct = pkg.get("category", "main") == "main"
                else:
                    # No signal to distinguish roles: keep the closure but treat
                    # every entry as direct rather than silently dropping roots.
                    is_direct = True

                nodes[name] = Node(
                    name=name,
                    version=version,
                    mode=Mode.CONDA,
                    provenance=prov,
                    direct=is_direct,
                    manual_bridge=False,
                    raw=pkg
                )
                if is_direct:
                    roots.append(name)

            # Rebuild the transitive edges the lock encodes per package.
            # Convention (matching uv_runner): source requires target.
            for pkg in packages:
                source = pkg["name"].lower()
                for dep_name in pkg.get("dependencies", {}):
                    target = dep_name.lower()
                    if target in nodes and source != target:
                        edges.append(Edge(source=source, target=target, via="transitive"))

            if not declared_direct and not any_category:
                logger.warning(
                    "conda-lock %s carries no environment file or category metadata; "
                    "all packages were marked direct.",
                    lock_path,
                )

            return DependencyGraph(
                nodes=nodes,
                edges=edges,
                roots=roots,
                resolution_quality=ResolutionQuality.LOCK_PARSED,
                source_fixation=SourceFixation.PINNED
            )
        except (OSError, yaml.YAMLError) as e:
            logger.warning("Failed to ingest conda-lock %s: %s", lock_path, e)

    # LEVEL 3: Parse environment.yml
    if env_path.is_file():
        content = env_path.read_text(encoding="utf-8")
        parsed = parse_environment_yaml(content)
        
        for dep in parsed["dependencies"]:
            parts = re.split(r"[>=<!]", dep)
            conda_name = parts[0].strip().lower()
            version_str = dep[len(parts[0]):].strip()
            
            nodes[conda_name] = Node(
                name=conda_name,
                version=version_str if version_str else None,
                mode=Mode.CONDA,
                provenance=Provenance.CONDA_FORGE,
                direct=True,
                manual_bridge=False,
                raw={"declared_dep": dep}
            )
            roots.append(conda_name)
            
        for dep in parsed["pip"]:
            parts = re.split(r"[>=<!]", dep)
            pip_name = parts[0].strip().lower()
            version_str = dep[len(parts[0]):].strip()
            
            nodes[pip_name] = Node(
                name=pip_name,
                version=version_str if version_str else None,
                mode=Mode.CONDA,
                provenance=Provenance.PIP_IN_CONDA,
                direct=True,
                manual_bridge=False,
                raw={"declared_dep": dep}
            )
            roots.append(pip_name)
            
        return DependencyGraph(
            nodes=nodes,
            edges=edges,
            roots=roots,
            resolution_quality=ResolutionQuality.DECLARED_ONLY,
            source_fixation=SourceFixation.INTENT
        )

    raise ValueError(f"No valid environment file found at {env_file}")
