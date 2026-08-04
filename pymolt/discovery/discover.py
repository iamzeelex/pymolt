"""Walk a repo, find project roots (monorepo-aware), and build the SurfaceMap."""

import logging
import re
import tomllib
from pathlib import Path

from pymolt.core.enums import Confidence
from pymolt.core.generated import is_pymolt_generated
from pymolt.discovery.dockerfile import parse_dockerfile
from pymolt.discovery.models import ProjectRoot, SurfaceMap, VersionEvidence
from pymolt.discovery.tox_nox import parse_noxfile, parse_tox_ini, parse_tox_pyproject

logger = logging.getLogger(__name__)

_IGNORE_DIRS = {
    ".git", ".hg", ".svn", ".venv", "venv", "env", "node_modules", "vendor",
    "__pycache__", ".tox", ".nox", ".mypy_cache", ".pytest_cache", ".pymolt",
    ".pymolt_cache", "site-packages", "build", "dist", ".eggs",
}
_MANIFEST_NAMES = {
    "pyproject.toml", "setup.py", "setup.cfg", "pipfile", "environment.yml",
    "environment.yaml",
}
_MAX_DEPTH = 6

# Directory names whose *contents* belong to something other than the project:
# fixtures, samples and third-party trees carry their own manifests and would
# otherwise be reported as roots of yours — and walked, and resolved.
_FOREIGN_PARENTS = {
    "tests", "test", "testing", "testdata", "fixtures", "fixture",
    "examples", "example", "samples", "third_party", "thirdparty",
    "vendored", "external", "contrib",
}


def _submodule_paths(repo_root: Path) -> set[str]:
    """Repo-relative paths declared as git submodules.

    A submodule is somebody else's repository that happens to sit inside this
    checkout. Its manifests describe their project, not yours.
    """
    gitmodules = repo_root / ".gitmodules"
    if not gitmodules.is_file():
        return set()
    paths: set[str] = set()
    try:
        for line in gitmodules.read_text(encoding="utf-8", errors="replace").splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() == "path":
                paths.add(value.strip().strip("/"))
    except OSError:
        return set()
    return paths


def _foreign_reason(rel: str, submodules: set[str]) -> str | None:
    """Why this manifest-bearing directory is not a root of this project."""
    if not rel or rel == ".":
        return None
    parts = rel.split("/")
    for sub in submodules:
        if rel == sub or rel.startswith(sub + "/"):
            return "git submodule"
    for i, part in enumerate(parts[:-1] if len(parts) > 1 else parts):
        if part.lower() in _FOREIGN_PARENTS:
            return f"inside {'/'.join(parts[: i + 1])}/"
    return None


def _is_manifest(name: str) -> bool:
    low = name.lower()
    if low in _MANIFEST_NAMES:
        return True
    return (low.startswith("requirements") and low.endswith((".txt", ".in"))) or low == "pip-packages.txt"


# Suffixes that mean a "dockerfile.*" name is really a source/text file, not a
# Dockerfile variant (e.g. our own pymolt/discovery/dockerfile.py).
_NON_DOCKERFILE_SUFFIXES = {"py", "pyc", "pyi", "txt", "md", "rst", "json", "toml", "cfg"}


def _is_dockerfile(name: str) -> bool:
    low = name.lower()
    if low == "dockerfile" or low.endswith(".dockerfile"):
        return True
    if low.startswith("dockerfile."):
        # Dockerfile.prod / Dockerfile.dev — but not dockerfile.py and friends.
        return low.rsplit(".", 1)[-1] not in _NON_DOCKERFILE_SUFFIXES
    return False


def _dir_is_root(root: Path, entries: list[str]) -> bool:
    """Does this directory look like the top of a project?

    An importable package is a *part* of a project, never the top of one: a
    real project root does not sit next to an ``__init__.py``. Without this a
    source module that happens to be named ``setup.py`` — a perfectly ordinary
    name inside a package — turns its own directory into a phantom root, and
    then gets resolved as if it declared dependencies.
    """
    package = "__init__.py" in entries
    for name in entries:
        # `setup.py` / `setup.cfg` are only packaging files at the top of a
        # tree. Inside a package they are ordinary modules — and "setup" is an
        # ordinary module name — so they prove nothing there. Every other
        # manifest still counts: a package with its own requirements.txt or
        # pyproject.toml really is a sub-project.
        if package and name.lower() in ("setup.py", "setup.cfg"):
            continue
        # pymolt's own emitted manifests are outputs, not evidence of a project.
        if _is_manifest(name) and not is_pymolt_generated(root / name):
            return True
        if _is_dockerfile(name):
            return True
    return False


def _manifest_version_evidence(root: Path) -> list[VersionEvidence]:
    """Per-source Python-version hints from a root's manifests (each kept separate)."""
    out: list[VersionEvidence] = []

    pv = root / ".python-version"
    if pv.is_file():
        try:
            val = pv.read_text(encoding="utf-8").strip().splitlines()[0].strip()
            if re.match(r"^\d+\.\d+", val):
                out.append(VersionEvidence(source=".python-version", source_file=".python-version",
                                           version=val, confidence=Confidence.PINNED))
        except (OSError, IndexError):
            pass

    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        try:
            with open(pyproject, "rb") as f:
                data = tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError):
            data = {}
        rp = data.get("project", {}).get("requires-python")
        if rp:
            m = re.search(r"(\d+\.\d+(?:\.\d+)?)", rp)
            out.append(VersionEvidence(source="pyproject:requires-python", source_file="pyproject.toml",
                                       version=m.group(1) if m else None,
                                       confidence=Confidence.INFERRED, kind="floor",
                                       constraint=rp.strip(), note=f"constraint '{rp}'"))
        poetry_py = data.get("tool", {}).get("poetry", {}).get("dependencies", {}).get("python")
        if isinstance(poetry_py, str):
            m = re.search(r"(\d+\.\d+(?:\.\d+)?)", poetry_py)
            out.append(VersionEvidence(source="pyproject:poetry.python", source_file="pyproject.toml",
                                       version=m.group(1) if m else None,
                                       confidence=Confidence.INFERRED, kind="floor",
                                       constraint=poetry_py.strip(), note=f"constraint '{poetry_py}'"))

    pipfile = root / "Pipfile"
    if pipfile.is_file():
        try:
            with open(pipfile, "rb") as f:
                data = tomllib.load(f)
            req = data.get("requires", {})
            ver = req.get("python_full_version") or req.get("python_version")
            if ver:
                out.append(VersionEvidence(source="Pipfile:requires", source_file="Pipfile",
                                           version=str(ver), confidence=Confidence.PINNED))
        except (OSError, tomllib.TOMLDecodeError):
            pass

    return out


def _build_root(root: Path, repo_root: Path, entries: list[str]) -> ProjectRoot:
    rel = "." if root == repo_root else str(root.relative_to(repo_root))
    pr = ProjectRoot(path=rel)
    # pymolt-generated target manifests are the tool's output, never an input.
    pr.manifests = sorted(
        n for n in entries if _is_manifest(n) and not is_pymolt_generated(root / n)
    )

    for name in sorted(n for n in entries if _is_dockerfile(n)):
        try:
            surface = parse_dockerfile((root / name).read_text(encoding="utf-8"), name)
        except OSError as e:
            logger.warning("Could not read %s: %s", root / name, e)
            continue
        pr.dockerfiles.append(surface)
        pr.version_evidence.append(VersionEvidence(
            source="Dockerfile:FROM", source_file=name,
            version=surface.runtime_python, confidence=surface.runtime_confidence,
            note="; ".join(surface.notes) or None,
        ))

    if "tox.ini" in (e.lower() for e in entries):
        tox = parse_tox_ini(root / next(e for e in entries if e.lower() == "tox.ini"))
        pr.tox_nox.append(tox)
    if "pyproject.toml" in entries:
        ptox = parse_tox_pyproject(root / "pyproject.toml")
        if ptox and ptox.declared_versions:
            pr.tox_nox.append(ptox)
    if "noxfile.py" in entries:
        pr.tox_nox.append(parse_noxfile(root / "noxfile.py"))

    # tox/nox declare a *test matrix* (a support window), not a single intended
    # runtime — so they are NOT folded into version_evidence (which drives the
    # divergence check). They are surfaced separately as a tested range.
    pr.version_evidence = _manifest_version_evidence(root) + pr.version_evidence
    return pr


def build_surface_map(root_dir: str | Path) -> SurfaceMap:
    repo_root = Path(root_dir).resolve()
    smap = SurfaceMap(root_dir=str(repo_root))
    if not repo_root.is_dir():
        smap.notes.append(f"{repo_root} is not a directory")
        return smap

    submodules = _submodule_paths(repo_root)
    for dirpath, _dirnames, filenames in _walk(repo_root):
        if not _dir_is_root(dirpath, filenames):
            continue
        root = _build_root(dirpath, repo_root, filenames)
        reason = _foreign_reason(root.path, submodules)
        if reason is not None:
            smap.excluded_roots.append(f"{root.path} — {reason}")
        else:
            smap.project_roots.append(root)

    # Primary first: the checkout root is what the user means by "my project";
    # everything else is ordered nearest-first so a monorepo reads top-down.
    smap.project_roots.sort(key=lambda r: (r.path not in ("", "."), r.path.count("/"), r.path))
    if len(smap.project_roots) > 1:
        smap.notes.append(f"{len(smap.project_roots)} project roots (monorepo)")
    if smap.excluded_roots:
        n = len(smap.excluded_roots)
        smap.notes.append(
            f"{n} manifest-bearing director{'y' if n == 1 else 'ies'} excluded "
            "(submodule/fixture/vendored) — see excluded_roots"
        )
    return smap


def _walk(repo_root: Path):
    """Depth-bounded walk that prunes ignored directories."""
    import os
    for dirpath, dirnames, filenames in os.walk(repo_root):
        path = Path(dirpath)
        depth = len(path.relative_to(repo_root).parts)
        if depth >= _MAX_DEPTH:
            dirnames[:] = []
        # Prune ignored dirs, all dot-directories (.git/.venv/.research/.old/…),
        # and *.egg-info — these are caches/vendored/hidden, never the project.
        dirnames[:] = [
            d for d in dirnames
            if d not in _IGNORE_DIRS and not d.startswith(".") and not d.endswith(".egg-info")
        ]
        yield path, dirnames, filenames
