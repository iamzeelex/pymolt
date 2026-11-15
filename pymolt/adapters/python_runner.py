"""Run a Python interpreter that lives somewhere else: today, a running docker container.

A legacy project's baseline is a container (Python 3.6, pandas 0.25, ...) that the host can
neither reproduce nor reach with a local interpreter. ``RunnerSpec`` records everything needed
to reach it again (name, id, image, platform, the bind mounts that connect its paths to the
host's); :func:`run_in` runs ``argv`` there with ``docker exec -i``, the request on stdin and
nothing copied in.

Every docker call goes through :func:`_docker`, so tests replace that one function. This
module is a thin system adapter: it imports nothing from the rest of pymolt (config and verify
models import *it*).
"""

from __future__ import annotations

import json
import platform as _platform
import posixpath
import shutil
import subprocess
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

EMULATION_TIMEOUT_FACTOR = 4
_INSPECT_TIMEOUT = 15

_ARCH = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}


class Mount(BaseModel):
    source: str  # host absolute path
    destination: str  # container path


class RunnerSpec(BaseModel):
    kind: Literal["container"] = "container"
    name: str
    container_id: str
    image: str | None = None
    # The image's content id: a rebuilt image under the same tag must not reuse cached answers.
    image_id: str | None = None
    platform: str | None = None  # "linux/amd64"
    emulated: bool = False  # the image's architecture differs from the host's
    workdir: str | None = None
    mounts: list[Mount] = Field(default_factory=list)
    executable: str = "python"  # inside the container


def _docker(args: list[str], *, stdin: str | None = None,
            timeout: float | None = None) -> subprocess.CompletedProcess:
    """The one place docker is called. Raises ``OSError`` when docker is absent."""
    if shutil.which("docker") is None:
        raise FileNotFoundError("docker is not on PATH")
    return subprocess.run(["docker", *args], input=stdin, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout, check=False)


def _host_arch() -> str:
    machine = _platform.machine().lower()
    return _ARCH.get(machine, machine)


def inspect_container(name_or_id: str) -> RunnerSpec | None:
    """The :class:`RunnerSpec` of a container (running or not); ``None`` if docker can't say."""
    try:
        res = _docker(["inspect", name_or_id], timeout=_INSPECT_TIMEOUT)
        if res.returncode != 0:
            return None
        data = json.loads(res.stdout)[0]
    except (OSError, subprocess.SubprocessError, ValueError, IndexError, TypeError, KeyError):
        return None
    config = data.get("Config") or {}
    image_id = data.get("Image") or None
    os_name = data.get("Platform") or "linux"
    arch: str | None = None
    # With the containerd image store the platform is on the container's manifest descriptor
    # (and `docker image inspect` answers an empty Os/Architecture).
    desc = (data.get("ImageManifestDescriptor") or {}).get("platform") or {}
    if desc.get("architecture"):
        os_name, arch = desc.get("os") or os_name, desc["architecture"]
    if image_id and arch is None:
        try:
            img = _docker(["image", "inspect", "--format", "{{.Os}}/{{.Architecture}}", image_id],
                          timeout=_INSPECT_TIMEOUT)
            if img.returncode == 0 and "/" in img.stdout.strip():
                got_os, got_arch = img.stdout.strip().split("/", 1)
                if got_arch:
                    os_name, arch = got_os or os_name, got_arch
        except (OSError, subprocess.SubprocessError):
            pass
    plat = f"{os_name}/{arch}" if arch else None
    mounts = [
        Mount(source=m["Source"], destination=m["Destination"])
        for m in data.get("Mounts") or []
        if m.get("Type") in (None, "bind") and m.get("Source") and m.get("Destination")
    ]
    return RunnerSpec(
        name=(data.get("Name") or name_or_id).lstrip("/"),
        container_id=data.get("Id") or name_or_id,
        image=config.get("Image") or None, image_id=image_id, platform=plat,
        emulated=bool(arch) and _ARCH.get(arch, arch) != _host_arch(),
        workdir=config.get("WorkingDir") or None, mounts=mounts,
    )


def _under(path: str, base: str) -> str | None:
    """``path`` relative to ``base`` (posix-style), or ``None`` if it is not inside it."""
    base = base.rstrip("/") or "/"
    if path == base:
        return ""
    prefix = base if base.endswith("/") else base + "/"
    return path[len(prefix):] if path.startswith(prefix) else None


def to_host(spec: RunnerSpec, path: str) -> str | None:
    """``/work/x`` -> ``<host source>/x`` through the mounts (longest destination wins)."""
    for m in sorted(spec.mounts, key=lambda m: -len(m.destination)):
        rest = _under(path, m.destination)
        if rest is not None:
            return str(Path(m.source) / rest) if rest else m.source
    return None


def to_container(spec: RunnerSpec, path: str) -> str | None:
    """The inverse of :func:`to_host` (longest source wins); ``None`` if not mounted."""
    for m in sorted(spec.mounts, key=lambda m: -len(m.source)):
        rest = _under(path, m.source)
        if rest is not None:
            return posixpath.join(m.destination, rest) if rest else m.destination
    return None


def is_running(name_or_id: str) -> bool:
    """True if a container with this id or name is running. The single liveness check."""
    try:
        res = _docker(["inspect", "--format", "{{.State.Running}}", name_or_id],
                      timeout=_INSPECT_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return False
    return res.returncode == 0 and res.stdout.strip() == "true"


def runner_available(spec: RunnerSpec) -> tuple[bool, str | None]:
    """``(usable, why_not)``: the container exists and is running."""
    try:
        res = _docker(["inspect", "--format", "{{.State.Running}}", spec.container_id],
                      timeout=_INSPECT_TIMEOUT)
    except FileNotFoundError:
        return False, "docker is not on PATH"
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"docker did not answer: {exc}"
    if res.returncode != 0:
        return False, f"container {spec.name} no longer exists (re-run setup)"
    if res.stdout.strip() != "true":
        return False, f"container {spec.name} is not running: `docker start {spec.name}`"
    return True, None


def run_in(
    spec: RunnerSpec | None, argv: list[str], *, stdin: str | None = None,
    cwd: str | None = None, timeout: float = 60.0, env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Run ``argv`` in the runner (``None`` = on this host). Raises like ``subprocess.run``.

    In a container: ``docker exec -i [-w cwd] <name> argv...`` with ``stdin``; the timeout is
    multiplied under emulation. ``cwd`` is a path *inside* the container (see
    :func:`to_container`).
    """
    if spec is None:
        return subprocess.run(argv, input=stdin, capture_output=True, text=True, cwd=cwd,
                              env=env, timeout=timeout, check=False)
    args = ["exec", "-i"]
    if cwd:
        args += ["-w", cwd]
    args += [spec.container_id, *argv]
    if spec.emulated:
        timeout = timeout * EMULATION_TIMEOUT_FACTOR
    return _docker(args, stdin=stdin, timeout=timeout)


__all__ = ["Mount", "RunnerSpec", "inspect_container", "is_running", "run_in",
           "runner_available", "to_container", "to_host"]
