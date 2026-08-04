import logging
import re
import tomllib
from collections.abc import Iterable
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement
from pydantic import BaseModel

from pymolt.core.enums import Mode, SourceFixation
from pymolt.core.generated import is_pymolt_generated

logger = logging.getLogger(__name__)


class DiscoveredSource(BaseModel):
    path: Path
    mode: Mode
    is_lock: bool
    fixation: SourceFixation


def _spec_is_pinned(spec: str) -> bool | None:
    """Classify a single requirement spec using the ``packaging`` grammar.

    Returns ``True`` if it is exactly pinned (``==``/``===`` only, or a direct
    URL/VCS reference), ``False`` if it is a loose/open constraint, or ``None``
    if the spec is not a parseable requirement (so the caller can skip it).

    Using a real parser avoids the substring traps of the old heuristic — e.g.
    a ``<`` inside an environment marker (``foo==1; python_version<"3.8"``) no
    longer makes a pinned requirement look unpinned.
    """
    try:
        req = Requirement(spec)
    except InvalidRequirement:
        return None
    # A direct URL/VCS reference points at a specific artifact or source.
    if req.url:
        return True
    operators = {s.operator for s in req.specifier}
    if not operators:
        return False  # no version constraint at all -> intent
    return operators <= {"==", "==="}


def _classify_specs(specs: Iterable[str]) -> SourceFixation:
    """Aggregate per-spec pin classification into a source-level fixation tier."""
    total = 0
    pinned = 0
    for spec in specs:
        result = _spec_is_pinned(spec)
        if result is None:
            continue
        total += 1
        if result:
            pinned += 1

    if total == 0:
        return SourceFixation.INTENT
    if pinned == total:
        return SourceFixation.PINNED
    if pinned == 0:
        return SourceFixation.INTENT
    return SourceFixation.MIXED


def _classify_requirements_fixation(path: Path) -> SourceFixation:
    """Classify the fixation tier of a requirements.txt/.in file."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as e:
        logger.warning("Could not read requirements file %s: %s", path, e)
        return SourceFixation.INTENT

    specs = []
    for line in lines:
        # Drop inline comments (must be preceded by whitespace per pip's grammar,
        # so a URL fragment like '#egg=' is left intact).
        line = line.split(" #", 1)[0].strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        specs.append(line)

    return _classify_specs(specs)


def _classify_pyproject_fixation(path: Path) -> SourceFixation:
    """Classify the fixation tier of a pyproject.toml's [project.dependencies]."""
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        logger.warning("Could not read pyproject %s: %s", path, e)
        return SourceFixation.INTENT

    deps = data.get("project", {}).get("dependencies", [])
    return _classify_specs(deps)


# Manifests pymolt itself writes must never be re-detected as INPUT sources:
# `requirements-target.txt` starts with "requirements" and ends ".txt", so the
# requirements glob below would otherwise pick pymolt's own resolved output back
# up and feed it into resolution (crashing pip-compile on the pinned --hash
# file). The predicate lives in pymolt.core.generated, shared with discovery.


def detect_sources(
    project_dir: str | Path, *, include_generated: bool = False
) -> list[DiscoveredSource]:
    """Scan top level for known manifests:
    requirements*.txt/.in, pyproject.toml, poetry.lock, Pipfile(.lock),
    setup.py/.cfg, environment.yml/.yaml, conda-lock.yml.

    Classify mode, is_lock, and fixation. pymolt-generated target manifests
    (``requirements-target.txt`` etc.) are skipped by default so assess auto-discovery
    never re-ingests its own output as input; pass ``include_generated=True`` when a
    caller explicitly consumes such a file (e.g. codemods reading the target manifest).
    """
    path = Path(project_dir)
    if not path.is_dir():
        return []

    sources = []
    
    # We scan the top level directory
    for item in path.iterdir():
        if not item.is_file():
            continue

        # Never re-ingest pymolt's own generated target manifest as an input,
        # unless a caller explicitly asked for it.
        if not include_generated and is_pymolt_generated(item):
            continue

        name = item.name.lower()

        # 1. Conda sources
        if name == "conda-lock.yml":
            sources.append(DiscoveredSource(
                path=item,
                mode=Mode.CONDA,
                is_lock=True,
                fixation=SourceFixation.PINNED
            ))
        elif name in ("environment.yml", "environment.yaml"):
            sources.append(DiscoveredSource(
                path=item,
                mode=Mode.CONDA,
                is_lock=False,
                fixation=SourceFixation.INTENT
            ))
            
        # 2. PyPI Lock files
        elif name == "poetry.lock":
            sources.append(DiscoveredSource(
                path=item,
                mode=Mode.PYPI,
                is_lock=True,
                fixation=SourceFixation.PINNED
            ))
        elif name == "pipfile.lock":
            sources.append(DiscoveredSource(
                path=item,
                mode=Mode.PYPI,
                is_lock=True,
                fixation=SourceFixation.PINNED
            ))
        elif name == "uv.lock":
            sources.append(DiscoveredSource(
                path=item,
                mode=Mode.PYPI,
                is_lock=True,
                fixation=SourceFixation.PINNED
            ))
            
        # 3. PyPI Manifests
        elif name == "pyproject.toml":
            sources.append(DiscoveredSource(
                path=item,
                mode=Mode.PYPI,
                is_lock=False,
                fixation=_classify_pyproject_fixation(item)
            ))
        elif name == "pipfile":
            sources.append(DiscoveredSource(
                path=item,
                mode=Mode.PYPI,
                is_lock=False,
                fixation=SourceFixation.INTENT
            ))
        elif name in ("setup.py", "setup.cfg"):
            sources.append(DiscoveredSource(
                path=item,
                mode=Mode.PYPI,
                is_lock=False,
                fixation=SourceFixation.INTENT
            ))
        elif (name.startswith("requirements") and (name.endswith(".txt") or name.endswith(".in"))) or name == "pip-packages.txt":
            fixation = _classify_requirements_fixation(item)
            sources.append(DiscoveredSource(
                path=item,
                mode=Mode.PYPI,
                is_lock=False,
                fixation=fixation
            ))

    # Sort sources: lock files first (is_lock=True), then by path name for deterministic output
    sources.sort(key=lambda s: (not s.is_lock, s.path.name))
    return sources


def detect_project_python_version(project_dir: str | Path) -> str | None:
    """Search for the project's own Python version configuration using standard project files:
    .python-version, pyproject.toml requires-python, or setup.py/setup.cfg classifiers/python_requires.
    """
    project_path = Path(project_dir)
    
    # 1. Check .python-version
    python_version_file = project_path / ".python-version"
    if python_version_file.is_file():
        try:
            val = python_version_file.read_text(encoding="utf-8").strip()
            if val and re.match(r"^\d+\.\d+", val):
                return val
        except Exception:
            pass

    # 2. Check pyproject.toml
    pyproject_file = project_path / "pyproject.toml"
    if pyproject_file.is_file():
        try:
            with open(pyproject_file, "rb") as f:
                data = tomllib.load(f)
            requires_python = data.get("project", {}).get("requires-python")
            if requires_python:
                match = re.search(r"(\d+\.\d+(?:\.\d+)?)", requires_python)
                if match:
                    return match.group(1)
        except Exception:
            pass

    # 3. Check Pipfile
    pipfile_file = project_path / "Pipfile"
    if pipfile_file.is_file():
        try:
            with open(pipfile_file, "rb") as f:
                data = tomllib.load(f)
            requires = data.get("requires", {})
            py_ver = requires.get("python_full_version") or requires.get("python_version")
            if py_ver:
                match = re.search(r"(\d+\.\d+(?:\.\d+)?)", py_ver)
                if match:
                    return match.group(1)
        except Exception:
            pass

    # 4. Check setup.py
    setup_py_file = project_path / "setup.py"
    if setup_py_file.is_file():
        try:
            content = setup_py_file.read_text(encoding="utf-8")
            match_req = re.search(r"python_requires\s*=\s*['\"]([^'\"]+)['\"]", content)
            if match_req:
                match_ver = re.search(r"(\d+\.\d+(?:\.\d+)?)", match_req.group(1))
                if match_ver:
                    return match_ver.group(1)
            
            classifiers = re.findall(r"Programming Language :: Python :: (\d+\.\d+(?:\.\d+)?)", content)
            if classifiers:
                py3_versions = [v for v in classifiers if v.startswith("3.")]
                if py3_versions:
                    sorted_versions = sorted(list(set(py3_versions)), key=lambda x: [int(p) for p in x.split(".")])
                else:
                    sorted_versions = sorted(list(set(classifiers)), key=lambda x: [int(p) for p in x.split(".")])
                return sorted_versions[0]
        except Exception:
            pass

    # 5. Check setup.cfg
    setup_cfg_file = project_path / "setup.cfg"
    if setup_cfg_file.is_file():
        try:
            content = setup_cfg_file.read_text(encoding="utf-8")
            match_cfg = re.search(r"python_requires\s*=\s*([^\n]+)", content)
            if match_cfg:
                val = match_cfg.group(1).strip().strip("'\"")
                match_ver = re.search(r"(\d+\.\d+(?:\.\d+)?)", val)
                if match_ver:
                    return match_ver.group(1)
        except Exception:
            pass

    return None


def normalize_pkg_name(name: str) -> str:
    """Normalize a package name to lowercase with hyphens for consistent key mapping."""
    return re.sub(r"[-_.]+", "-", name).lower()


def extract_declared_requirements(manifest_path: Path) -> dict[str, str]:
    """Parse direct dependency requirements/constraints from the source manifest."""
    from packaging.requirements import Requirement
    requirements = {}
    if not manifest_path.is_file():
        return requirements

    name = manifest_path.name.lower()
    
    # 1. requirements.txt / requirements.in
    if (name.startswith("requirements") and (name.endswith(".txt") or name.endswith(".in"))) or name == "pip-packages.txt":
        try:
            lines = manifest_path.read_text(encoding="utf-8").splitlines()
            for line in lines:
                line = line.strip()
                if not line or line.startswith("#") or line.startswith("-"):
                    continue
                try:
                    req = Requirement(line)
                    key = normalize_pkg_name(req.name)
                    requirements[key] = line
                except Exception:
                    match = re.match(r"^([\w\-\.]+)", line)
                    if match:
                        key = normalize_pkg_name(match.group(1))
                        requirements[key] = line
        except Exception:
            pass

    # 2. pyproject.toml
    elif name == "pyproject.toml":
        try:
            with open(manifest_path, "rb") as f:
                data = tomllib.load(f)
            deps = data.get("project", {}).get("dependencies", [])
            for dep in deps:
                try:
                    req = Requirement(dep)
                    key = normalize_pkg_name(req.name)
                    requirements[key] = dep
                except Exception:
                    match = re.match(r"^([\w\-\.]+)", dep)
                    if match:
                        key = normalize_pkg_name(match.group(1))
                        requirements[key] = dep
        except Exception:
            pass

    # 3. setup.py
    elif name == "setup.py":
        try:
            content = manifest_path.read_text(encoding="utf-8")
            match = re.search(r"install_requires\s*=\s*\[(.*?)\]", content, re.DOTALL)
            if match:
                block = match.group(1)
                items = re.findall(r"['\"]([^'\"]+)['\"]", block)
                for item in items:
                    item = item.strip()
                    if not item:
                        continue
                    try:
                        req = Requirement(item)
                        key = normalize_pkg_name(req.name)
                        requirements[key] = item
                    except Exception:
                        pkg_match = re.match(r"^([\w\-\.]+)", item)
                        if pkg_match:
                            key = normalize_pkg_name(pkg_match.group(1))
                            requirements[key] = item
        except Exception:
            pass

    # 4. setup.cfg
    elif name == "setup.cfg":
        try:
            import configparser
            config = configparser.ConfigParser()
            config.read(manifest_path)
            if config.has_option("options", "install_requires"):
                block = config.get("options", "install_requires")
                for line in block.splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    try:
                        req = Requirement(line)
                        key = normalize_pkg_name(req.name)
                        requirements[key] = line
                    except Exception:
                        pkg_match = re.match(r"^([\w\-\.]+)", line)
                        if pkg_match:
                            key = normalize_pkg_name(pkg_match.group(1))
                            requirements[key] = line
        except Exception:
            pass

    return requirements


def detect_local_environments(project_dir: str | Path) -> list[str]:
    """Scan the project directory for standard virtual environment directories and custom python envs."""
    import os
    project_path = Path(project_dir)
    found = []
    if not project_path.is_dir():
        return found
    
    is_windows = os.name == "nt"
    sub_dir = "Scripts" if is_windows else "bin"
    exe_name = "python.exe" if is_windows else "python"
    
    # Directories to ignore during scanning
    ignored_names = {".git", ".pymolt", ".pymolt_cache", ".pytest_cache", "__pycache__", "tests", "pymolt"}
    
    try:
        for item in project_path.iterdir():
            if item.is_dir() and item.name not in ignored_names:
                bin_path = item / sub_dir / exe_name
                name_lower = item.name.lower()
                # If python executable exists or folder matches common env prefixes
                if bin_path.is_file() or any(p in name_lower for p in ("venv", "env", "conda")):
                    found.append(item.name)
    except Exception:
        pass
        
    return sorted(list(set(found)))


def detect_system_tools() -> dict[str, str | None]:
    """Scan the host system for availability of common environment and package tools."""
    import shutil
    import subprocess
    tools = {
        "uv": shutil.which("uv"),
        "poetry": shutil.which("poetry"),
        "conda": None,
        "pyenv": shutil.which("pyenv"),
        "docker": shutil.which("docker")
    }
    
    # Check Conda
    from pymolt.ingestion.conda_ingest import find_conda_executable_on_host
    try:
        tools["conda"] = find_conda_executable_on_host()
    except Exception:
        pass
        
    # Check Docker daemon liveness if docker is on PATH
    if tools["docker"]:
        try:
            res = subprocess.run(
                ["docker", "ps"],
                capture_output=True, timeout=3
            )
            if res.returncode != 0:
                tools["docker"] = None
        except Exception:
            tools["docker"] = None
            
    return tools
