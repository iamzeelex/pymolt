import logging
import os
import re
import shutil
import subprocess
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


def _classify_manifest(path: Path) -> DiscoveredSource | None:
    name = path.name.lower()
    if name == "conda-lock.yml":
        return DiscoveredSource(path=path, mode=Mode.CONDA, is_lock=True, fixation=SourceFixation.PINNED)
    if name in ("environment.yml", "environment.yaml"):
        return DiscoveredSource(path=path, mode=Mode.CONDA, is_lock=False, fixation=SourceFixation.INTENT)
    if name in ("poetry.lock", "pipfile.lock", "uv.lock"):
        return DiscoveredSource(path=path, mode=Mode.PYPI, is_lock=True, fixation=SourceFixation.PINNED)
    if name == "pyproject.toml":
        return DiscoveredSource(path=path, mode=Mode.PYPI, is_lock=False, fixation=_classify_pyproject_fixation(path))
    if name == "pipfile":
        return DiscoveredSource(path=path, mode=Mode.PYPI, is_lock=False, fixation=SourceFixation.INTENT)
    if name in ("setup.py", "setup.cfg"):
        return DiscoveredSource(path=path, mode=Mode.PYPI, is_lock=False, fixation=SourceFixation.INTENT)
    if (name.startswith("requirements") and (name.endswith(".txt") or name.endswith(".in"))) or name == "pip-packages.txt":
        return DiscoveredSource(path=path, mode=Mode.PYPI, is_lock=False, fixation=_classify_requirements_fixation(path))
    return None


def _source_priority(s: DiscoveredSource) -> tuple[int, int, str]:
    name = s.path.name.lower()
    if s.is_lock:
        return (0, 0, name)
    if "requirement" in name or name in ("pyproject.toml", "pipfile", "environment.yml", "environment.yaml"):
        return (1, 0, name)
    if name == "setup.py":
        return (1, 1, name)
    if name == "setup.cfg":
        return (1, 2, name)
    return (1, 3, name)


def detect_sources(project_dir: str | Path, *, include_generated: bool = False) -> list[DiscoveredSource]:
    """Scan the project directory for supported dependency manifests and lockfiles.
    
    Searches the immediate project directory (and standard locations like requirements/)
    for lock files and raw manifests across PyPI and Conda ecosystems.
    """
    project_path = Path(project_dir)
    sources = []
    
    if not project_path.is_dir():
        return sources

    # 1. Search immediate directory
    for item in project_path.iterdir():
        if item.is_file():
            if not include_generated and is_pymolt_generated(item):
                continue
            classified = _classify_manifest(item)
            if classified:
                sources.append(classified)

    # 2. Search requirements/ subdirectory if it exists
    req_dir = project_path / "requirements"
    if req_dir.is_dir():
        for item in req_dir.iterdir():
            if item.is_file() and item.suffix in [".txt", ".in", ".pip"]:
                if not include_generated and is_pymolt_generated(item):
                    continue
                fixation = _classify_requirements_fixation(item)
                sources.append(DiscoveredSource(
                    path=item,
                    mode=Mode.PYPI,
                    is_lock=False,
                    fixation=fixation
                ))

    # Sort sources: lock files first (is_lock=True), then standard manifests (requirements, pyproject, setup.py > setup.cfg)
    sources.sort(key=_source_priority)
    return sources


def _extract_pyvenv_version(venv_dir: Path) -> str | None:
    """Read python version from pyvenv.cfg or binary."""
    cfg_file = venv_dir / "pyvenv.cfg"
    if cfg_file.is_file():
        try:
            for line in cfg_file.read_text(encoding="utf-8", errors="ignore").splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    k, v = k.strip().lower(), v.strip()
                    if k in ("version_info", "version"):
                        match = re.match(r"^(\d+\.\d+)", v)
                        if match:
                            return match.group(1)
        except Exception:
            pass
    return None


def _pick_best_classifier_version(classifiers: list[str], project_path: Path) -> str | None:
    """Select the best Python version from classifiers:
    1. Highest installed version matching classifiers
    2. Otherwise, highest declared version (not the lowest!)
    """
    py3_versions = [v for v in classifiers if v.startswith("3.")]
    if py3_versions:
        sorted_versions = sorted(list(set(py3_versions)), key=lambda x: [int(p) for p in x.split(".")])
    else:
        sorted_versions = sorted(list(set(classifiers)), key=lambda x: [int(p) for p in x.split(".")])
    
    if not sorted_versions:
        return None

    try:
        from pymolt.ingestion.fallback_compiler import find_python_interpreter
        for v in reversed(sorted_versions):
            if find_python_interpreter(v, project_path):
                return v
    except Exception:
        pass

    # Fallback to the latest/highest declared version
    return sorted_versions[-1]


def detect_project_python_version(project_dir: str | Path) -> str | None:
    """Search for the project Python version configuration.
    
    Checks active/local virtualenvs, .python-version, pyproject.toml, Pipfile, setup.py, setup.cfg.
    """
    project_path = Path(project_dir)

    # 1. Check project-local virtualenvs
    common_env_names = (".venv", "venv", "env", ".conda", "conda-env", "virtualenv")
    if project_path.is_dir():
        for name in common_env_names:
            env_dir = project_path / name
            if env_dir.is_dir():
                v = _extract_pyvenv_version(env_dir)
                if v:
                    return v

    # 2. Check .python-version
    python_version_file = project_path / ".python-version"
    if python_version_file.is_file():
        try:
            val = python_version_file.read_text(encoding="utf-8").strip()
            if val and re.match(r"^\d+\.\d+", val):
                return val
        except Exception:
            pass

    # 3. Check pyproject.toml
    pyproject_file = project_path / "pyproject.toml"
    if pyproject_file.is_file():
        try:
            with open(pyproject_file, "rb") as f:
                data = tomllib.load(f)
            requires_python = data.get("project", {}).get("requires-python")
            if not requires_python:
                requires_python = data.get("tool", {}).get("poetry", {}).get("dependencies", {}).get("python")
            if requires_python:
                match = re.search(r"(\d+\.\d+(?:\.\d+)?)", requires_python)
                if match:
                    return match.group(1)
        except Exception:
            pass

    # 4. Check Pipfile
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

    # 5. Check setup.py
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
                picked = _pick_best_classifier_version(classifiers, project_path)
                if picked:
                    return picked
        except Exception:
            pass

    # 6. Check setup.cfg
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

            classifiers = re.findall(r"Programming Language :: Python :: (\d+\.\d+(?:\.\d+)?)", content)
            if classifiers:
                picked = _pick_best_classifier_version(classifiers, project_path)
                if picked:
                    return picked
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
            blocks = re.findall(r"(?:install_requires|install_reqs|requirements|requires|deps)\s*=\s*\[(.*?)\]", content, re.DOTALL)
            for block in blocks:
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

    if not requirements and name == "setup.cfg" and (manifest_path.parent / "setup.py").is_file():
        return extract_declared_requirements(manifest_path.parent / "setup.py")

    return requirements


class DiscoveredEnvironment(BaseModel):
    name: str
    path: Path
    executable: Path
    version: str | None = None
    kind: str  # "project_venv", "active_venv", "conda", "pyenv", "poetry", "pipenv", "uv", "system"


def discover_python_environments(project_dir: str | Path = ".") -> list[DiscoveredEnvironment]:
    """Scan the project and host system for all available Python environments and interpreters,
    similar to IDE interpreter discovery in PyCharm and VS Code.
    """
    project_path = Path(project_dir).expanduser().resolve()
    is_windows = os.name == "nt"
    sub_dir = "Scripts" if is_windows else "bin"
    exe_name = "python.exe" if is_windows else "python"
    exe_name_3 = "python3.exe" if is_windows else "python3"

    envs: list[DiscoveredEnvironment] = []
    seen_executables: set[Path] = set()

    def add_env(name: str, path: Path, exe: Path, kind: str, ver: str | None = None):
        try:
            exe_resolved = exe.resolve()
            if exe_resolved in seen_executables:
                return
            if not exe.is_file() and not (path / "pyvenv.cfg").is_file():
                return
            seen_executables.add(exe_resolved)
            if not ver:
                ver = _extract_pyvenv_version(path)
            if not ver and exe.is_file():
                try:
                    res = subprocess.run(
                        [str(exe), "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
                        capture_output=True, text=True, timeout=2
                    )
                    if res.returncode == 0:
                        ver = res.stdout.strip()
                except Exception:
                    pass
            envs.append(DiscoveredEnvironment(
                name=name,
                path=path,
                executable=exe,
                version=ver,
                kind=kind,
            ))
        except Exception:
            pass

    # 1. Project-local virtual environments
    ignored_names = {".git", ".pymolt", ".pymolt_cache", ".pytest_cache", "__pycache__", "tests", "pymolt"}
    if project_path.is_dir():
        try:
            for item in project_path.iterdir():
                if item.is_dir() and item.name not in ignored_names:
                    bin_path = item / sub_dir / exe_name
                    bin_path3 = item / sub_dir / exe_name_3
                    target_exe = bin_path if bin_path.is_file() else bin_path3
                    if target_exe.is_file() or (item / "pyvenv.cfg").is_file():
                        add_env(f"{item.name} (project)", item, target_exe, "project_venv")
        except Exception:
            pass

    # 2. Currently active environment
    active_venv = os.environ.get("VIRTUAL_ENV")
    if active_venv:
        av_path = Path(active_venv)
        exe = av_path / sub_dir / exe_name
        add_env("Active Virtualenv (VIRTUAL_ENV)", av_path, exe, "active_venv")

    active_conda = os.environ.get("CONDA_PREFIX")
    if active_conda:
        ac_path = Path(active_conda)
        exe = ac_path / sub_dir / exe_name
        add_env(f"Active Conda ({ac_path.name})", ac_path, exe, "conda")

    # 3. Pyenv versions (~/.pyenv/versions/*)
    pyenv_dir = Path.home() / ".pyenv" / "versions"
    if pyenv_dir.is_dir():
        try:
            for item in pyenv_dir.iterdir():
                if item.is_dir():
                    exe = item / sub_dir / exe_name
                    if not exe.is_file():
                        exe = item / sub_dir / exe_name_3
                    if exe.is_file():
                        add_env(f"pyenv: {item.name}", item, exe, "pyenv")
        except Exception:
            pass

    # 4. Conda environments (~/.conda/envs/*, ~/miniconda3/envs/*, ~/anaconda3/envs/*)
    conda_base_dirs = [
        Path.home() / ".conda" / "envs",
        Path.home() / "miniconda3" / "envs",
        Path.home() / "anaconda3" / "envs",
        Path.home() / "miniforge3" / "envs",
        Path("/opt/conda/envs"),
    ]
    for cb in conda_base_dirs:
        if cb.is_dir():
            try:
                for item in cb.iterdir():
                    if item.is_dir():
                        exe = item / sub_dir / exe_name
                        if exe.is_file():
                            add_env(f"conda: {item.name}", item, exe, "conda")
            except Exception:
                pass

    # 5. Poetry cache environments (~/.cache/pypoetry/virtualenvs/*)
    poetry_dir = Path.home() / ".cache" / "pypoetry" / "virtualenvs"
    if poetry_dir.is_dir():
        try:
            for item in poetry_dir.iterdir():
                if item.is_dir():
                    exe = item / sub_dir / exe_name
                    if exe.is_file():
                        add_env(f"poetry: {item.name}", item, exe, "poetry")
        except Exception:
            pass

    # 6. System / Toolchain Python interpreters
    system_candidates = [
        "python3.13", "python3.12", "python3.11", "python3.10", "python3.9", "python3.8", "python3.7", "python3", "python",
        "/opt/homebrew/bin/python3", "/opt/homebrew/bin/python3.12", "/opt/homebrew/bin/python3.11",
        "/usr/local/bin/python3", "/usr/bin/python3"
    ]
    for cand in system_candidates:
        resolved = shutil.which(cand) or (Path(cand) if Path(cand).is_file() else None)
        if resolved:
            p_res = Path(resolved)
            if p_res.is_file():
                add_env(f"System ({p_res.name})", p_res.parent, p_res, "system")

    return envs


def detect_local_environments(project_dir: str | Path) -> list[str]:
    """Scan the project directory for standard virtual environment directories and custom python envs."""
    project_path = Path(project_dir)
    found = []
    if not project_path.is_dir():
        return found

    is_windows = os.name == "nt"
    sub_dir = "Scripts" if is_windows else "bin"
    exe_name = "python.exe" if is_windows else "python"

    ignored_names = {".git", ".pymolt", ".pymolt_cache", ".pytest_cache", "__pycache__", "tests", "pymolt"}

    try:
        for item in project_path.iterdir():
            if item.is_dir() and item.name not in ignored_names:
                bin_path = item / sub_dir / exe_name
                name_lower = item.name.lower()
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
