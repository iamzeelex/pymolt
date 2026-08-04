import json
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from pymolt.adapters.subprocess_runner import run_command

logger = logging.getLogger(__name__)

# Upper bound (seconds) for the legacy environment setup and pip-compile steps
# so a hung pip download/resolve cannot block ingestion indefinitely.
LEGACY_TIMEOUT = 600


def find_python_interpreter(version: str, project_dir: Path | None = None) -> Path | None:
    """Search system PATH and common directories for an executable matching the requested Python version."""
    match = re.match(r"^(\d+)\.(\d+)", version)
    if not match:
        return None
    major, minor = match.group(1), match.group(2)
    target_ver = f"{major}.{minor}"

    def check_version(path: str) -> bool:
        try:
            res = subprocess.run(
                [path, "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
                capture_output=True, text=True, timeout=2
            )
            if res.returncode == 0 and res.stdout.strip() == target_ver:
                return True
        except Exception:
            pass
        return False

    # 1. Search project-local virtualenvs
    if project_dir and project_dir.is_dir():
        common_names = {".venv", "venv", "env", ".conda", "conda-env", "virtualenv"}
        is_windows = os.name == "nt"
        for item in project_dir.iterdir():
            if item.is_dir() and item.name in common_names:
                sub_dir = "Scripts" if is_windows else "bin"
                for name in [("python.exe" if is_windows else "python"), ("python3.exe" if is_windows else "python3")]:
                    bin_path = item / sub_dir / name
                    if bin_path.is_file() and check_version(str(bin_path)):
                        return bin_path

    # 2. Search Pyenv versions
    try:
        pyenv_versions_dir = Path.home() / ".pyenv" / "versions"
        if pyenv_versions_dir.is_dir():
            is_windows = os.name == "nt"
            sub_dir = "Scripts" if is_windows else "bin"
            for item in pyenv_versions_dir.iterdir():
                if item.is_dir() and item.name.startswith(target_ver):
                    for name in [("python.exe" if is_windows else "python"), ("python3.exe" if is_windows else "python3")]:
                        bin_path = item / sub_dir / name
                        if bin_path.is_file() and check_version(str(bin_path)):
                            return bin_path
    except Exception:
        pass

    # 3. Search system PATH candidates
    candidates = [
        f"python{target_ver}",
        f"python{major}",
        "python",
        sys.executable
    ]
    
    for cand in candidates:
        resolved = shutil.which(cand)
        if resolved and check_version(resolved):
            return Path(resolved)
    return None


def get_or_create_legacy_env(
    python_bin: Path, version: str, cache_root: Path | None = None,
    pip_tools_spec: str = "pip-tools<6.0", env_prefix: str = "legacy_env",
) -> Path:
    """Create or reuse a pip-tools virtual environment for the given Python version.

    The environment is cached under ``<cache_root>/.pymolt_cache`` so it lands
    next to the project rather than wherever the process happens to be running.
    ``cache_root`` defaults to the current working directory for compatibility.
    ``pip_tools_spec``/``env_prefix`` let callers request a modern pip-tools for
    a non-legacy ("system") resolve without colliding with the legacy env.
    """
    match = re.match(r"^(\d+)\.(\d+)", version)
    if not match:
        raise ValueError(f"Invalid Python version: {version}")
    major, minor = match.group(1), match.group(2)

    base = Path(cache_root) if cache_root else Path.cwd()
    env_dir = (base / ".pymolt_cache" / f"{env_prefix}_{major}_{minor}").resolve()

    is_windows = os.name == "nt"
    bin_dir = env_dir / ("Scripts" if is_windows else "bin")
    pip_exe = bin_dir / ("pip.exe" if is_windows else "pip")
    pip_compile_exe = bin_dir / ("pip-compile.exe" if is_windows else "pip-compile")

    # Return immediately if environment already exists and has pip-compile
    if env_dir.exists() and pip_compile_exe.is_file():
        return env_dir

    # Clean up dirty path if present
    if env_dir.exists():
        shutil.rmtree(env_dir)

    env_dir.parent.mkdir(exist_ok=True)

    # 1. Create virtualenv
    try:
        # First try standard venv
        subprocess.run([str(python_bin), "-m", "venv", str(env_dir)], check=True, capture_output=True)
    except Exception:
        # Fallback to virtualenv
        try:
            subprocess.run(["virtualenv", "-p", str(python_bin), str(env_dir)], check=True, capture_output=True)
        except Exception as e:
            raise ValueError(
                f"Failed to create legacy virtual environment for Python {version}.\n"
                f"Please ensure the 'virtualenv' package is installed globally (`pip install virtualenv`),\n"
                f"or that the 'venv' standard module is available on your local python.\n"
                f"Error: {e}"
            ) from e

    # 2. Install pip-tools (caller chooses the spec; legacy default supports old Pythons)
    try:
        # Upgrade pip & setuptools first to prevent installation issues
        subprocess.run([str(pip_exe), "install", "-U", "pip", "setuptools"], check=True, capture_output=True)
        subprocess.run([str(pip_exe), "install", pip_tools_spec], check=True, capture_output=True)
    except Exception as e:
        raise ValueError(
            f"Failed to install pip-tools in the legacy virtual environment.\n"
            f"Error: {e}"
        ) from e
        
    return env_dir


def _is_subpath(child: Path, parent: Path) -> bool:
    """Check if child is a subpath of parent."""
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def list_running_containers() -> list[dict[str, str]]:
    """Every running Docker container (id, name, python) — for interactive selection.

    Unlike :func:`find_running_containers_for_project`, this applies NO project
    bind-mount filter, so the user can pick any container. Python is probed
    best-effort; "" when it could not be determined.
    """
    if not shutil.which("docker"):
        return []
    try:
        res = subprocess.run(
            ["docker", "ps", "-q"], capture_output=True, text=True, check=True, timeout=5
        )
        ids = [line.strip() for line in res.stdout.splitlines() if line.strip()]
    except Exception:
        return []
    if not ids:
        return []
    try:
        data = json.loads(subprocess.run(
            ["docker", "inspect", *ids], capture_output=True, text=True, check=True, timeout=5
        ).stdout)
    except Exception:
        return []

    out: list[dict[str, str]] = []
    for d in data:
        cid = d.get("Id", "")[:12]
        name = d.get("Name", "").lstrip("/")
        python = ""
        for cmd in ("python", "python3"):
            try:
                r = subprocess.run(
                    ["docker", "exec", cid, cmd, "-c",
                     "import sys;print('%d.%d'%sys.version_info[:2])"],
                    capture_output=True, text=True, timeout=3,
                )
                if r.returncode == 0 and r.stdout.strip():
                    python = r.stdout.strip()
                    break
            except Exception:
                pass
        out.append({"id": cid, "name": name, "python": python})
    return out


def find_running_containers_for_project(project_dir: Path, version: str) -> list[dict[str, str]]:
    """Search for running Docker containers that bind-mount the project directory
    (or its parent) and have a matching python version.
    """
    if not shutil.which("docker"):
        return []

    try:
        # Get list of running container IDs
        res = subprocess.run(
            ["docker", "ps", "-q"],
            capture_output=True, text=True, check=True, timeout=5
        )
        container_ids = [line.strip() for line in res.stdout.splitlines() if line.strip()]
    except Exception:
        return []

    if not container_ids:
        return []

    try:
        inspect_res = subprocess.run(
            ["docker", "inspect"] + container_ids,
            capture_output=True, text=True, check=True, timeout=5
        )
        containers_data = json.loads(inspect_res.stdout)
    except Exception:
        return []

    matching_containers = []
    project_path_abs = project_dir.resolve()

    for data in containers_data:
        container_id = data.get("Id", "")[:12]
        name = data.get("Name", "").lstrip("/")
        config = data.get("Config", {})
        labels = config.get("Labels", {})
        mounts = data.get("Mounts", [])
        
        mounted_in_container = False
        container_cwd = None

        # 1. Check bind mounts
        for m in mounts:
            if m.get("Type") == "bind":
                src = m.get("Source", "")
                dest = m.get("Destination", "")
                if not src or not dest:
                    continue
                
                norm_src = src
                if norm_src.startswith("/host_mnt"):
                    norm_src = norm_src[len("/host_mnt"):]
                
                try:
                    src_path = Path(norm_src).resolve()
                    if project_path_abs == src_path or _is_subpath(project_path_abs, src_path):
                        mounted_in_container = True
                        rel_path = project_path_abs.relative_to(src_path)
                        container_cwd = str(Path(dest) / rel_path)
                        break
                except Exception:
                    pass

        # 2. Check labels (JetBrains or VS Code devcontainer metadata)
        if not mounted_in_container:
            vscode_folder = labels.get("devcontainer.localFolder")
            jb_sources = labels.get("com.intellij.devcontainer.sources.path")
            jb_workspace = labels.get("com.intellij.devcontainer.workspace.path")
            
            for folder_str in [vscode_folder, jb_sources]:
                if folder_str:
                    try:
                        folder_path = Path(folder_str).resolve()
                        if project_path_abs == folder_path or _is_subpath(project_path_abs, folder_path):
                            mounted_in_container = True
                            workspace_path = jb_workspace or config.get("WorkingDir") or "/workspace"
                            rel_path = project_path_abs.relative_to(folder_path)
                            container_cwd = str(Path(workspace_path) / rel_path)
                            break
                    except Exception:
                        pass

        if not mounted_in_container:
            continue

        if not container_cwd:
            container_cwd = config.get("WorkingDir") or "/workspace"

        # Check Python version inside the container
        target_major_minor = ".".join(version.split(".")[:2])
        matching_python = None
        for py_cmd in ["python", "python3"]:
            try:
                check_res = subprocess.run(
                    ["docker", "exec", container_id, py_cmd, "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
                    capture_output=True, text=True, timeout=3
                )
                if check_res.returncode == 0 and check_res.stdout.strip() == target_major_minor:
                    matching_python = py_cmd
                    break
            except Exception:
                pass

        if matching_python:
            matching_containers.append({
                "id": container_id,
                "name": name,
                "cwd": container_cwd,
                "python": matching_python
            })

    return matching_containers


def compile_legacy_in_container(manifest_path: Path, container_info: dict, version: str, constraint_file: Path | None = None) -> str:
    """Run legacy pip-compile compilation inside the matched running Docker container."""
    container_id = container_info["id"]
    container_cwd = container_info["cwd"]
    python_exe = container_info["python"]

    match = re.match(r"^(\d+)\.(\d+)", version)
    if not match:
        raise ValueError(f"Invalid Python version: {version}")
    major, minor = match.group(1), match.group(2)
    
    env_rel_path = f".pymolt_cache/legacy_env_container_{major}_{minor}"
    
    # Check if pip-compile exists inside the container's virtual environment
    test_cmd = ["docker", "exec", container_id, "[", "-f", f"{container_cwd}/{env_rel_path}/bin/pip-compile", "]"]
    res = subprocess.run(test_cmd, capture_output=True)
    
    if res.returncode != 0:
        # Create virtual environment inside container
        create_cmd = ["docker", "exec", "-w", container_cwd, container_id, python_exe, "-m", "venv", env_rel_path]
        run_command(create_cmd, check=True, timeout=LEGACY_TIMEOUT)

        # Install pip-tools inside container
        pip_path = f"{env_rel_path}/bin/pip"
        install_cmd = [
            "docker", "exec", "-w", container_cwd, container_id,
            pip_path, "install", "-U", "pip", "setuptools", "pip-tools<6.0"
        ]
        run_command(install_cmd, check=True, timeout=LEGACY_TIMEOUT)

    # Setup temporary output file within the bind-mounted directory
    pymolt_cache_dir = manifest_path.parent / ".pymolt_cache"
    pymolt_cache_dir.mkdir(exist_ok=True)
    
    import tempfile
    fd, temp_file_path = tempfile.mkstemp(dir=str(pymolt_cache_dir), suffix=".txt", prefix="pymolt_resolved_")
    os.close(fd)
    
    temp_path = Path(temp_file_path)
    temp_rel = temp_path.relative_to(manifest_path.parent)
    container_temp_path = f"{container_cwd}/{temp_rel}"

    try:
        manifest_name = manifest_path.name
        pip_compile_path = f"{env_rel_path}/bin/pip-compile"
        
        cmd = [
            "docker", "exec", "-w", container_cwd, container_id,
            pip_compile_path, manifest_name, "--output-file", container_temp_path
        ]
        if constraint_file:
            try:
                constraint_rel = constraint_file.absolute().relative_to(manifest_path.parent.absolute())
                container_constraint_path = f"{container_cwd}/{constraint_rel}"
                cmd.extend(["--constraint", container_constraint_path])
            except ValueError as e:
                raise ValueError(f"Constraint file {constraint_file} must reside inside the project directory for Docker container compilation.") from e

        run_command(cmd, check=True, timeout=LEGACY_TIMEOUT)
        
        compiled_content = temp_path.read_text(encoding="utf-8")
        return compiled_content
    finally:
        try:
            temp_path.unlink()
        except Exception:
            pass


def compile_legacy(
    manifest_path: Path, version: str, container_id: str | None = None,
    constraint_file: Path | None = None,
    pip_tools_spec: str = "pip-tools<6.0", env_prefix: str = "legacy_env",
) -> str:
    """Find an interpreter (local or inside a container), build a sandbox env, and compile via pip-compile.

    The defaults target legacy Pythons (``pip-tools<6.0``); the ``system`` resolver
    passes a modern spec/prefix to resolve a current project without uv."""
    # 1. If container_id is provided, resolve and execute inside the container
    if container_id:
        containers = find_running_containers_for_project(manifest_path.parent, version)
        matched = None
        for c in containers:
            if c["id"] == container_id or c["name"] == container_id:
                matched = c
                break
        
        if matched:
            return compile_legacy_in_container(manifest_path, matched, version, constraint_file=constraint_file)
        else:
            raise ValueError(f"Specified Docker container '{container_id}' was not found running or is not matching.")
            
    # 2. Fall back to local search
    python_bin = find_python_interpreter(version, project_dir=manifest_path.parent)
    if not python_bin:
        match = re.match(r"^(\d+)\.(\d+)", version)
        major_minor = f"{match.group(1)}.{match.group(2)}" if match else version
        raise ValueError(
            f"Python {version} interpreter was not found in your PATH.\n"
            f"To scan this legacy project, please install a local Python {major_minor} interpreter\n"
            f"and ensure it is available as 'python{major_minor}' or 'python'."
        )
        
    env_dir = get_or_create_legacy_env(
        python_bin, version, cache_root=manifest_path.parent,
        pip_tools_spec=pip_tools_spec, env_prefix=env_prefix,
    )
    
    is_windows = os.name == "nt"
    bin_dir = env_dir / ("Scripts" if is_windows else "bin")
    pip_compile_exe = bin_dir / ("pip-compile.exe" if is_windows else "pip-compile")
    
    import tempfile
    fd, temp_file_path = tempfile.mkstemp(suffix=".txt", prefix="pymolt_resolved_")
    os.close(fd)
    
    try:
        manifest_name = manifest_path.name
        manifest_dir = manifest_path.parent

        cmd = [
            str(pip_compile_exe),
            manifest_name,
            "--output-file",
            temp_file_path
        ]
        if constraint_file:
            cmd.extend(["--constraint", str(constraint_file.absolute())])
            
        # Run pip-compile relative to manifest dir for editable install pathing
        run_command(cmd, check=True, cwd=str(manifest_dir), timeout=LEGACY_TIMEOUT)
        
        compiled_content = Path(temp_file_path).read_text(encoding="utf-8")
        return compiled_content
    finally:
        try:
            os.remove(temp_file_path)
        except Exception:
            pass
