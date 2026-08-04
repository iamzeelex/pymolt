"""A focused Dockerfile analyzer (not a regex on FROM).

Resolves ARG/ENV variables, walks the multi-stage graph, identifies the runtime
stage, follows ``COPY --from`` of virtualenvs to reason about the *effective*
interpreter, and extracts the OS hint and the install toolchain — each with a
confidence level. Best-effort and offline: a private/digest base or a missing
variable yields an honest ``unknown`` rather than a guess.
"""

import logging
import re

from pymolt.core.enums import Confidence
from pymolt.discovery.models import DockerfileSurface, DockerStage

logger = logging.getLogger(__name__)

_OS_HINTS = ("alpine", "slim", "bullseye", "bookworm", "buster", "stretch",
             "windowsservercore", "nanoserver")
_INSTALL_TOOLS = {
    "poetry": re.compile(r"\bpoetry\s+(install|lock)\b"),
    "pipenv": re.compile(r"\bpipenv\s+(install|sync|lock)\b"),
    "conda": re.compile(r"\b(conda|mamba|micromamba)\s+(env\s+create|create|install)\b"),
    "uv": re.compile(r"\buv\s+(pip|sync|lock|venv)\b"),
    "pdm": re.compile(r"\bpdm\s+(install|sync|lock)\b"),
    "pip": re.compile(r"\bpip3?\s+install\b"),
}
_VAR_RE = re.compile(r"\$\{(\w+)(?::-([^}]*))?\}|\$(\w+)")


def _logical_lines(content: str) -> list[tuple[str, str]]:
    """Yield (keyword, rest) instruction tuples, honouring line continuations and
    the optional ``# escape=`` parser directive."""
    raw = content.splitlines()
    escape = "\\"
    for ln in raw:
        s = ln.strip()
        if s.startswith("#"):
            m = re.match(r"#\s*escape\s*=\s*(\S)", s)
            if m:
                escape = m.group(1)
            continue
        break

    out: list[tuple[str, str]] = []
    buf = ""
    for ln in raw:
        stripped = ln.strip()
        if stripped.startswith("#"):
            continue
        if not stripped and not buf:
            continue
        if ln.rstrip().endswith(escape):
            buf += ln.rstrip()[:-1] + " "
            continue
        buf += ln
        text = buf.strip()
        buf = ""
        if not text:
            continue
        parts = text.split(None, 1)
        out.append((parts[0], parts[1] if len(parts) > 1 else ""))
    if buf.strip():
        parts = buf.strip().split(None, 1)
        out.append((parts[0], parts[1] if len(parts) > 1 else ""))
    return out


def _resolve_vars(value: str, variables: dict) -> str:
    def repl(m: re.Match) -> str:
        name = m.group(1) or m.group(3)
        default = m.group(2)
        val = variables.get(name)
        if val is not None:
            return val
        return default if default is not None else ""
    return _VAR_RE.sub(repl, value)


def _parse_arg(rest: str) -> tuple[str, str | None]:
    if "=" in rest:
        name, default = rest.split("=", 1)
        return name.strip(), default.strip().strip("'\"")
    return rest.strip(), None


def _parse_env(rest: str) -> list[tuple[str, str]]:
    # ENV KEY=value [KEY2=value2]  OR  ENV KEY value
    pairs = []
    if "=" in rest:
        for m in re.finditer(r"(\w+)=(\"[^\"]*\"|'[^']*'|\S+)", rest):
            pairs.append((m.group(1), m.group(2).strip("'\"")))
    else:
        parts = rest.split(None, 1)
        if len(parts) == 2:
            pairs.append((parts[0], parts[1].strip().strip("'\"")))
    return pairs


def _os_hint(image: str) -> str | None:
    low = image.lower()
    for hint in _OS_HINTS:
        if hint in low:
            return hint
    if "ubuntu" in low:
        return "ubuntu"
    if "debian" in low:
        return "debian"
    return None


def _python_from_image(image: str, stage_names: set) -> tuple[str | None, Confidence, str | None]:
    """Return (python_version, confidence, note) for a resolved base image."""
    img = image.strip()
    if not img:
        return None, Confidence.UNKNOWN, "base image resolved to empty (unresolved variable)"
    if img in stage_names:
        return None, Confidence.UNKNOWN, None  # inherits from a prior stage (resolved later)
    if "@sha256:" in img:
        return None, Confidence.UNKNOWN, "digest-pinned base — version opaque without registry"

    repo, _, tag = img.partition(":")
    is_python = repo.lower().endswith("python")
    if not is_python:
        return None, Confidence.UNKNOWN, f"non-python base '{repo}' — version not in FROM"

    if not tag or tag in ("latest", *_OS_HINTS):
        return None, Confidence.FLOATING, f"floating tag '{tag or 'latest'}' — runtime version unknown"

    m = re.match(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?", tag)
    if not m:
        return None, Confidence.FLOATING, f"non-numeric tag '{tag}'"
    if m.group(2) is None:
        return m.group(1), Confidence.FLOATING, f"major-only tag '{tag}' — minor floats"
    version = ".".join(p for p in (m.group(1), m.group(2), m.group(3)) if p)
    return version, Confidence.PINNED, None


def parse_dockerfile(content: str, path: str) -> DockerfileSurface:
    surface = DockerfileSurface(path=path)
    global_args: dict = {}
    stage_vars: dict = {}
    stage_names: set = set()
    current: DockerStage | None = None

    for keyword, rest in _logical_lines(content):
        kw = keyword.upper()

        if kw == "ARG":
            name, default = _parse_arg(rest)
            if current is None:
                global_args[name] = default
            else:
                if default is None and name in global_args:
                    default = global_args[name]  # re-declared global -> inherit default
                stage_vars[name] = default

        elif kw == "ENV":
            for k, v in _parse_env(rest):
                stage_vars[k] = _resolve_vars(v, stage_vars)

        elif kw == "FROM":
            stage_vars = {}  # ENV does not carry across stages
            spec = re.sub(r"--platform=\S+\s*", "", rest).strip()
            tokens = spec.split()
            name = None
            if len(tokens) >= 3 and tokens[1].upper() == "AS":
                name = tokens[2]
            raw_image = tokens[0] if tokens else ""
            image = _resolve_vars(raw_image, {**global_args})
            py, conf, note = _python_from_image(image, stage_names)
            current = DockerStage(
                index=len(surface.stages), name=name, base_image=image,
                python_version=py, confidence=conf, os_hint=_os_hint(image), note=note,
            )
            surface.stages.append(current)
            if name:
                stage_names.add(name)

        elif kw == "RUN" and current is not None:
            resolved = _resolve_vars(rest, stage_vars)
            for tool, pat in _INSTALL_TOOLS.items():
                if pat.search(resolved) and tool not in surface.install_tools:
                    surface.install_tools.append(tool)
            # Non-python base: try to recover a version from package installs.
            if current.python_version is None and current.confidence == Confidence.UNKNOWN:
                am = re.search(r"python3?\.?(\d+\.\d+|\d{1,2})", resolved)
                if am and "python3." in resolved:
                    pm = re.search(r"python3\.(\d+)", resolved)
                    if pm:
                        current.python_version = f"3.{pm.group(1)}"
                        current.confidence = Confidence.INFERRED
                        current.note = "version inferred from a RUN package install"

        elif kw == "COPY" and current is not None:
            m = re.search(r"--from=(\S+)", rest)
            if m and re.search(r"(venv|virtualenv|site-packages|/\.local)", rest):
                ref = m.group(1)
                if not ref.isdigit():
                    current.copies_venv_from.append(ref)

        elif kw in ("CMD", "ENTRYPOINT") and current is not None:
            current.is_runtime = True

    _finalize(surface)
    return surface


def _finalize(surface: DockerfileSurface) -> None:
    stages = surface.stages
    if not stages:
        surface.notes.append("no FROM instruction found")
        return

    by_name = {s.name: s for s in stages if s.name}

    # Runtime stage: the last stage with CMD/ENTRYPOINT, else the last stage.
    runtime = next((s for s in reversed(stages) if s.is_runtime), stages[-1])

    # Follow stage-name inheritance to a concrete python.
    resolved = runtime
    seen = set()
    while resolved.python_version is None and resolved.base_image in by_name and resolved.base_image not in seen:
        seen.add(resolved.base_image)
        resolved = by_name[resolved.base_image]

    surface.runtime_python = resolved.python_version
    surface.runtime_confidence = resolved.confidence
    surface.os_hint = runtime.os_hint or resolved.os_hint

    # Effective-interpreter check: a venv copied from a builder on a different
    # Python than the runtime base is a classic ABI-mismatch footgun.
    for ref in runtime.copies_venv_from:
        builder = by_name.get(ref)
        if builder and builder.python_version and surface.runtime_python \
                and builder.python_version.split(".")[:2] != surface.runtime_python.split(".")[:2]:
            surface.notes.append(
                f"venv built on Python {builder.python_version} (stage '{ref}') is copied into "
                f"a Python {surface.runtime_python} runtime — ABI mismatch risk"
            )

    if surface.os_hint == "alpine":
        surface.notes.append("Alpine (musl) base — many C-extension wheels are unavailable; "
                             "expect source builds")
