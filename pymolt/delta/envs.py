"""Which interpreter answers for each side of a delta.

**old**: the baseline capture's interpreter (trace metadata ``executable``), else setup's
base Python environment, else a cached throwaway environment pinned to the migrations'
``from`` versions.
**new**: the configured target environment, else a cached throwaway environment built
with uv from the target manifest (``uv venv`` + ``uv pip install``, versions only).

A candidate is only accepted when it actually has every migrated package at the expected
version: a base env that was upgraded in place would otherwise report "no changes".
When nothing qualifies the side is unavailable and carries the reason; the engine then
marks the packages INCONCLUSIVE instead of reporting an empty delta.

The uv calls live here (a thin adapter over the system); everything else in
``pymolt.delta`` is pure over parsed data.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path

from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from pymolt.adapters.python_runner import RunnerSpec, run_in, runner_available
from pymolt.delta.models import Interpreter

log = logging.getLogger(__name__)

ENV_CACHE = Path(".pymolt") / "cache" / "envs"
_MARKER = "pymolt-env.json"
_TARGET_MANIFESTS = ("requirements-target.txt",)
_BUILD_TIMEOUT = 900

# Runs in the candidate interpreter (possibly 3.6): stdlib only, no f-strings.
_VERSIONS_SOURCE = r'''
import sys
sys.path[:] = [p for p in sys.path if p not in ("", ".")]
import json
names = json.loads(sys.stdin.read() or "[]")
out = {}
try:
    from importlib import metadata
except ImportError:
    try:
        import importlib_metadata as metadata
    except ImportError:
        metadata = None
for name in names:
    version = None
    if metadata is not None:
        try:
            version = metadata.version(name)
        except Exception:
            version = None
    if version is None:
        try:
            import pkg_resources
            version = pkg_resources.get_distribution(name).version
        except Exception:
            version = None
    out[name] = version
sys.stdout.write(json.dumps({"python": "%d.%d.%d" % tuple(sys.version_info[:3]),
                             "versions": out}))
'''


class Want:
    """One package pin a side must satisfy: (name, version)."""

    def __init__(self, name: str, version: str) -> None:
        self.name = name
        self.version = version

    def requirement(self) -> str:
        return f"{self.name}=={self.version}"


def same_version(a: str | None, b: str | None) -> bool:
    if a is None or b is None:
        return False
    try:
        return Version(a) == Version(b)
    except InvalidVersion:
        return a.strip() == b.strip()


def _child_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYMOLT_TRACE")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def python_in(env_dir: Path) -> Path | None:
    for candidate in (env_dir / "bin" / "python", env_dir / "Scripts" / "python.exe"):
        if candidate.is_file():
            return candidate
    return None


def same_minor(a: str | None, b: str | None) -> bool:
    """Python versions agree by major.minor ("3.6" == "3.6.15")."""
    def minor(value: str | None) -> tuple[str, ...] | None:
        parts = (value or "").strip().split(".")
        return tuple(parts[:2]) if len(parts) >= 2 and parts[0].isdigit() else None

    ma, mb = minor(a), minor(b)
    return ma is not None and ma == mb


def installed_versions(
    executable: str, names: Sequence[str], timeout: float = 60.0,
    runner: RunnerSpec | None = None,
) -> tuple[str | None, dict[str, str | None], str | None]:
    """``(python, {name: version|None}, error)`` as the interpreter itself reports them.

    ``runner``: the interpreter lives in that container (``executable`` is a path inside it).
    """
    where = f"{executable} in container {runner.name}" if runner else executable
    try:
        proc = run_in(runner, [executable, "-B", "-c", _VERSIONS_SOURCE],
                      stdin=json.dumps(list(names)), timeout=timeout,
                      env=None if runner else _child_env())
    except (OSError, subprocess.SubprocessError) as exc:
        return None, {}, f"could not run {where}: {exc}"
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-1:] or [""]
        return None, {}, f"{where} exited {proc.returncode}: {tail[0][:200]}"
    try:
        payload = json.loads(proc.stdout or "{}")
    except ValueError:
        return None, {}, f"{where} answered something that is not JSON"
    return payload.get("python"), dict(payload.get("versions") or {}), None


def _check(executable: str, wants: Sequence[Want], source: str, note: str | None,
           runner: RunnerSpec | None = None) -> Interpreter:
    """An Interpreter for ``executable`` with versions filled; ``reason`` set if unusable."""
    python, versions, error = installed_versions(executable, [w.name for w in wants],
                                                 runner=runner)
    interp = Interpreter(executable=executable, source=source, python=python, note=note,
                         versions=versions, runner=runner)
    if error:
        interp.executable, interp.reason = None, error
        return interp
    wrong = [
        f"{w.name} {versions.get(w.name) or 'missing'} (need {w.version})"
        for w in wants if not same_version(versions.get(w.name), w.version)
    ]
    if wrong:
        where = f"container {runner.name}" if runner else executable
        interp.reason = f"{where} has " + ", ".join(wrong)
    return interp


def _satisfied(interp: Interpreter) -> bool:
    return interp.executable is not None and interp.reason is None


def _matches(interp: Interpreter, wants: Sequence[Want]) -> int:
    if interp.executable is None:
        return 0
    return sum(1 for w in wants if same_version(interp.versions.get(w.name), w.version))


def _best(candidates: list[Interpreter], wants: Sequence[Want], tried: list[str],
          side: str) -> Interpreter:
    """No candidate has every pin: the one with the most, still flagged, or none at all.

    The engine computes the packages that candidate does have at the right version and
    marks the rest INCONCLUSIVE with ``reason``.
    """
    reason = "; ".join(t for t in tried if t) or f"no {side} interpreter found"
    ranked = sorted(candidates, key=lambda c: -_matches(c, wants))
    if ranked and _matches(ranked[0], wants) > 0:
        best = ranked[0]
        best.reason = reason
        return best
    return Interpreter(source="none", reason=reason)


def _rel(root: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else root / path


def throwaway_env(
    root: Path,
    python: str | None,
    requirements: Sequence[str] = (),
    manifest: Path | None = None,
    online: bool = False,
) -> tuple[Path | None, str | None, float | None]:
    """A cached env under ``.pymolt/cache/envs/<hash>/``: ``(python, reason, build_seconds)``.

    Built with ``uv venv`` + ``uv pip install`` from the pins (and/or the manifest) only;
    no project code is installed. A half-built env (no marker) is rebuilt.

    Offline unless ``online``: uv may then only use what its cache already holds (it never
    downloads a wheel or an interpreter). An env that cannot be built from the cache comes
    back as a reason, so the delta is stamped inconclusive instead of silently reaching
    the network.
    """
    uv = shutil.which("uv")
    key_parts = [f"python={python or ''}", *sorted(requirements)]
    if manifest is not None:
        try:
            key_parts.append("manifest=" + hashlib.sha256(manifest.read_bytes()).hexdigest())
        except OSError as exc:
            return None, f"cannot read target manifest {manifest}: {exc}", None
    digest = hashlib.sha256("\n".join(key_parts).encode("utf-8")).hexdigest()[:16]
    env_dir = root / ENV_CACHE / digest
    exe = python_in(env_dir)
    if exe is not None and (env_dir / _MARKER).is_file():
        return exe, None, None
    if uv is None:
        return None, "uv is not on PATH, so no throwaway environment can be built", None
    t0 = time.perf_counter()
    shutil.rmtree(env_dir, ignore_errors=True)
    env_dir.parent.mkdir(parents=True, exist_ok=True)
    offline = [] if online else ["--offline"]
    venv_cmd = [uv, "venv", "-q", *offline, str(env_dir)]
    if python:
        venv_cmd[2:2] = ["-p", python]
    steps = [venv_cmd]
    install = [uv, "pip", "install", "-q", *offline, "-p", str(env_dir / "bin" / "python")]
    if os.name == "nt":
        install[-1] = str(env_dir / "Scripts" / "python.exe")
    if manifest is not None:
        install += ["-r", str(manifest)]
    install += list(requirements)
    if manifest is not None or requirements:
        steps.append(install)
    for cmd in steps:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=_BUILD_TIMEOUT,
                                  cwd=str(root))
        except (OSError, subprocess.SubprocessError) as exc:
            shutil.rmtree(env_dir, ignore_errors=True)
            return None, f"`{' '.join(cmd[:3])}` failed: {exc}", None
        if proc.returncode != 0:
            shutil.rmtree(env_dir, ignore_errors=True)
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:] or [""]
            hint = ("" if online else " (offline: only uv's cache was used; rerun with --online "
                    "to let uv download, or create the target environment yourself)")
            return None, f"`{' '.join(cmd[:3])}` failed: {tail[0][:300]}{hint}", None
    exe = python_in(env_dir)
    if exe is None:
        return None, f"uv built {env_dir} but it has no interpreter", None
    (env_dir / _MARKER).write_text(json.dumps({
        "python": python, "requirements": sorted(requirements),
        "manifest": str(manifest) if manifest else None,
    }, indent=2), encoding="utf-8")
    return exe, None, round(time.perf_counter() - t0, 2)


def _baseline_slot(root: Path):
    from pymolt.verify.models import ContractState

    state = ContractState.load(root / ".pymolt" / "contract_state.json")
    return state.baseline if state is not None else None


def _baseline_trace(root: Path) -> Path | None:
    slot = _baseline_slot(root)
    if slot is None or not slot.trace_path:
        return None
    return _rel(root, slot.trace_path)


def _runners(root: Path, config) -> list[RunnerSpec]:
    """The containers that hold the baseline: the baseline slot's, then setup's (deduplicated).

    A config written before ``EnvConfig.runner`` existed names its container only; the spec
    is rebuilt from docker then.
    """
    out: list[RunnerSpec] = []
    slot = _baseline_slot(root)
    if slot is not None and slot.runner is not None:
        out.append(slot.runner)
    spec = config.runner if config is not None else None
    if spec is None and config is not None and config.container_id \
            and config.selected_tool.value == "container":
        from pymolt.adapters.python_runner import inspect_container

        spec = inspect_container(config.container_id)
    if spec is not None and all(spec.container_id != r.container_id for r in out):
        out.append(spec)
    return out


def baseline_runner(root: Path) -> RunnerSpec | None:
    """The first running container that holds the baseline (slot, then setup), or None."""
    root = Path(root)
    for spec in _runners(root, _config(root)):
        if runner_available(spec)[0]:
            return spec
    return None


def _config(root: Path):
    from pymolt.ingestion.config import EnvConfig

    return EnvConfig.load(root / ".pymolt" / "env_config.json")


def resolve_old(root: Path, wants: Sequence[Want], online: bool = False) -> Interpreter:
    """The interpreter whose installed packages are the migrations' ``from`` side."""
    root = Path(root)
    tried: list[str] = []
    candidates: list[Interpreter] = []
    config = _config(root)
    for spec in _runners(root, config):
        ok, why = runner_available(spec)
        if not ok:
            tried.append(why or f"container {spec.name} is unavailable")
            continue
        interp = _check(spec.executable, wants, "container",
                        f"baseline container {spec.name}"
                        + (f" ({spec.image})" if spec.image else ""), runner=spec)
        if _satisfied(interp):
            return interp
        candidates.append(interp)
        tried.append(interp.reason or "")
    trace = _baseline_trace(root)
    if trace is not None:
        from pymolt.verify.identity_resolver import select_interpreter

        choice = select_interpreter(trace=trace)
        if choice.source == "trace-metadata":
            interp = _check(choice.executable, wants, "trace-metadata",
                            f"baseline capture interpreter ({choice.executable})")
            if _satisfied(interp):
                return interp
            candidates.append(interp)
            tried.append(interp.reason or "")
        elif choice.note:
            tried.append(choice.note)
    base_python = config.base_python if config is not None else None
    base_exe: str | None = None
    if config is not None and config.system_python and os.sep in config.system_python:
        base_exe = config.system_python
    elif base_python:
        from pymolt.ingestion.fallback_compiler import find_python_interpreter

        found = find_python_interpreter(base_python, project_dir=root)
        base_exe = str(found) if found else None
        if base_exe is None:
            tried.append(f"no Python {base_python} interpreter found for the base environment")
    if base_exe:
        interp = _check(base_exe, wants, "base-env", f"setup's base environment ({base_exe})")
        if _satisfied(interp):
            return interp
        candidates.append(interp)
        tried.append(interp.reason or "")
    exe, reason, built = throwaway_env(root, base_python, [w.requirement() for w in wants],
                                       online=online)
    if exe is not None:
        interp = _check(str(exe), wants, "throwaway",
                        f"throwaway environment pinned to the from-versions ({exe.parent.parent})")
        interp.built_seconds = built
        if _satisfied(interp):
            return interp
        candidates.append(interp)
        tried.append(interp.reason or "")
    else:
        tried.append(reason or "")
    return _best(candidates, wants, tried, "old")


def target_manifest(root: Path) -> Path | None:
    for name in _TARGET_MANIFESTS:
        path = root / name
        if path.is_file():
            return path
    return None


def resolve_new(root: Path, wants: Sequence[Want], online: bool = False) -> Interpreter:
    """The interpreter whose installed packages are the migrations' ``to`` side."""
    root = Path(root)
    tried: list[str] = []
    candidates: list[Interpreter] = []
    config = _config(root)
    if config is not None and config.target_env_path:
        exe = python_in(_rel(root, config.target_env_path))
        if exe is not None:
            interp = _check(str(exe), wants, "target-env",
                            f"configured target environment ({config.target_env_path})")
            if _satisfied(interp):
                return interp
            candidates.append(interp)
            tried.append(interp.reason or "")
        else:
            tried.append(f"target environment {config.target_env_path} has no interpreter")
    target_python = config.target_python if config is not None else None
    manifest = target_manifest(root)
    attempts: list[tuple[list[str], Path | None, str]] = []
    if manifest is not None:
        attempts.append(([], manifest, f"throwaway environment from {manifest.name}"))
    attempts.append(([w.requirement() for w in wants], None,
                     "throwaway environment pinned to the to-versions"))
    for requirements, man, label in attempts:
        exe, reason, built = throwaway_env(root, target_python, requirements, man,
                                           online=online)
        if exe is None:
            tried.append(reason or "")
            continue
        interp = _check(str(exe), wants, "throwaway", f"{label} ({exe.parent.parent})")
        interp.built_seconds = built
        if _satisfied(interp):
            return interp
        candidates.append(interp)
        tried.append(interp.reason or "")
    return _best(candidates, wants, tried, "new")


def wants_for(migrations, side: str) -> list[Want]:
    out: list[Want] = []
    seen: set[str] = set()
    for m in migrations:
        key = canonicalize_name(m.name)
        if key in seen:
            continue
        seen.add(key)
        out.append(Want(m.name, m.from_version if side == "old" else m.to_version))
    return out
