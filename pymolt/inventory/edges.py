"""Static 'edge inventory' of declared dependencies.

A migration is complicated less by ordinary PyPI deps than by the *edges*:
dependency groups (dev/extras), layered ``-r``/``-c`` includes, private indexes
(``--index-url``/``--extra-index-url``/``--find-links``), and non-index sources
(git/url/local-editable). This module classifies every declared dependency and
manifest directive — purely from the source files, offline — so the audit can
surface what needs special handling before anything is resolved.
"""

import logging
import re
import tomllib
from collections import Counter
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement
from pydantic import BaseModel, Field

from pymolt.core.enums import EdgeKind
from pymolt.ingestion.detect import detect_sources, normalize_pkg_name

logger = logging.getLogger(__name__)

_VCS_MARKERS = ("git+", "hg+", "svn+", "bzr+")


class DepEdge(BaseModel):
    raw: str
    name: str | None = None
    kind: EdgeKind = EdgeKind.PYPI
    extras: list[str] = Field(default_factory=list)
    marker: str | None = None
    editable: bool = False
    group: str = "main"        # main | dev | test | docs | <extra/group name>
    source_file: str = ""
    #: True when ``name`` was read off a VCS/URL path rather than declared. A repo
    #: is usually named after its package, but not always (python-dateutil ships
    #: `dateutil`), so the guess is labelled instead of asserted.
    name_inferred: bool = False
    #: Why this edge needs attention beyond an ordinary version bump — e.g. a
    #: transport that no longer exists. Rendered next to the edge.
    blocker: str | None = None


class IndexDirective(BaseModel):
    kind: str                  # index-url | extra-index-url | find-links
    value: str
    source_file: str = ""


class DependencyInventory(BaseModel):
    edges: list[DepEdge] = Field(default_factory=list)
    includes: list[str] = Field(default_factory=list)      # -r targets
    constraints: list[str] = Field(default_factory=list)   # -c targets
    indexes: list[IndexDirective] = Field(default_factory=list)
    manifests: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    def counts_by_kind(self) -> dict[str, int]:
        return dict(Counter(e.kind.value for e in self.edges))

    def counts_by_group(self) -> dict[str, int]:
        return dict(Counter(e.group for e in self.edges))

    def non_pypi_edges(self) -> list[DepEdge]:
        return [e for e in self.edges if e.kind != EdgeKind.PYPI]


def _egg_name(spec: str) -> str | None:
    match = re.search(r"#egg=([\w\-\.]+)", spec)
    return normalize_pkg_name(match.group(1)) if match else None


def _name_from_url(spec: str) -> str | None:
    """Last resort for a VCS/URL edge: the repository's own name.

    Without this a `git+git://github.com/blaze/odo.git` line renders as a bare
    dash — pymolt knows the dependency exists but cannot say what it is, which
    is the least useful thing to tell someone triaging a dead project. The repo
    name is usually the package name; callers mark it inferred.
    """
    without_fragment = spec.split("#", 1)[0].split("?", 1)[0].rstrip("/")
    tail = without_fragment.rsplit("/", 1)[-1]
    tail = tail.split("@", 1)[0]                 # drop a @ref / @tag suffix
    if tail.endswith(".git"):
        tail = tail[:-4]
    tail = tail.strip()
    return normalize_pkg_name(tail) if tail and not tail.startswith("-") else None


#: Transports that no longer work, and what they mean for a migration. The
#: unauthenticated git protocol was switched off by GitHub in January 2022, so a
#: manifest still using it cannot install at all — a blocker that outranks every
#: version conflict in the same file, and one that looks like nothing in a table
#: of package names.
_DEAD_TRANSPORTS = {
    "git://": "the git:// protocol was disabled by GitHub in 2022 — this will not "
              "fetch; re-point it at https://",
    "http://": "plain http is refused by PyPI and most indexes — use https://",
}


def _transport_blocker(spec: str) -> str | None:
    lowered = spec.lower()
    for transport, reason in _DEAD_TRANSPORTS.items():
        if transport in lowered:
            return reason
    return None


def _group_for_filename(name: str) -> str:
    n = name.lower()
    if "dev" in n:
        return "dev"
    if "test" in n:
        return "test"
    if "doc" in n:
        return "docs"
    return "main"


def _make_edge(raw: str, group: str, source_file: str, editable: bool = False) -> DepEdge:
    """Classify a single requirement spec into a :class:`DepEdge`."""
    s = raw.strip()
    name = marker = url = None
    extras_list: list[str] = []
    has_vcs = any(v in s for v in _VCS_MARKERS)

    parsed = None
    try:
        parsed = Requirement(s)
    except InvalidRequirement:
        parsed = None

    if parsed is not None:
        name = normalize_pkg_name(parsed.name)
        extras_list = sorted(parsed.extras)
        marker = str(parsed.marker) if parsed.marker else None
        url = parsed.url
        if url:
            if has_vcs or any(v in url for v in _VCS_MARKERS):
                kind = EdgeKind.VCS
            elif url.startswith("file:"):
                kind = EdgeKind.LOCAL
            else:
                kind = EdgeKind.URL
        else:
            kind = EdgeKind.PYPI
    else:
        if has_vcs:
            kind, name = EdgeKind.VCS, _egg_name(s)
        elif s.startswith(("http://", "https://")):
            kind = EdgeKind.URL
        elif s.startswith((".", "/")) or s.startswith("file:"):
            kind, name = EdgeKind.LOCAL, _egg_name(s)
        else:
            kind = EdgeKind.UNKNOWN

    if editable and kind == EdgeKind.PYPI:
        kind = EdgeKind.LOCAL  # `-e .` and friends are local checkouts

    # Nothing declared the name, but the URL still carries one.
    name_inferred = False
    if name is None and kind in (EdgeKind.VCS, EdgeKind.URL):
        name = _name_from_url(url or s)
        name_inferred = name is not None

    return DepEdge(
        raw=s, name=name, kind=kind, extras=extras_list,
        name_inferred=name_inferred, blocker=_transport_blocker(url or s),
        marker=marker, editable=editable, group=group, source_file=source_file,
    )


def _parse_requirements_file(path: Path, group: str, inv: DependencyInventory,
                             visited: set, queue: list) -> None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as e:
        logger.warning("Could not read requirements file %s: %s", path, e)
        return

    inv.manifests.append(path.name)
    for raw_line in lines:
        line = raw_line.split(" #", 1)[0].strip()  # strip inline comments (keep #egg=)
        if not line or line.startswith("#"):
            continue
        low = line.lower()

        if low.startswith(("-r ", "--requirement ")):
            target = line.split(None, 1)[1].strip()
            inv.includes.append(target)
            inc = (path.parent / target).resolve()
            if inc not in visited:
                visited.add(inc)
                queue.append((inc, _group_for_filename(inc.name)))
            continue
        if low.startswith(("-c ", "--constraint ")):
            inv.constraints.append(line.split(None, 1)[1].strip())
            continue
        if low.startswith(("-i ", "--index-url ")):
            inv.indexes.append(IndexDirective(kind="index-url", value=line.split(None, 1)[1].strip(), source_file=path.name))
            continue
        if low.startswith("--extra-index-url "):
            inv.indexes.append(IndexDirective(kind="extra-index-url", value=line.split(None, 1)[1].strip(), source_file=path.name))
            continue
        if low.startswith(("-f ", "--find-links ")):
            inv.indexes.append(IndexDirective(kind="find-links", value=line.split(None, 1)[1].strip(), source_file=path.name))
            continue
        if line.startswith("--"):  # other pip flags (--hash, --no-binary, ...)
            continue

        editable = False
        if low.startswith(("-e ", "--editable ")):
            editable = True
            line = line.split(None, 1)[1].strip()
        inv.edges.append(_make_edge(line, group, path.name, editable))


def _poetry_edge(name: str, spec, group: str, source_file: str) -> DepEdge:
    if isinstance(spec, dict):
        if "git" in spec:
            kind = EdgeKind.VCS
        elif "url" in spec:
            kind = EdgeKind.URL
        elif "path" in spec:
            kind = EdgeKind.LOCAL
        else:
            kind = EdgeKind.PYPI
        extras = list(spec.get("extras", []) or [])
        return DepEdge(raw=f"{name} = {spec}", name=normalize_pkg_name(name), kind=kind,
                       extras=extras, group=group, source_file=source_file)
    return DepEdge(raw=f"{name} = {spec}", name=normalize_pkg_name(name),
                   kind=EdgeKind.PYPI, group=group, source_file=source_file)


def _parse_pyproject(path: Path, inv: DependencyInventory) -> None:
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        logger.warning("Could not read pyproject %s: %s", path, e)
        return

    inv.manifests.append(path.name)
    project = data.get("project", {})
    for dep in project.get("dependencies", []):
        inv.edges.append(_make_edge(dep, "main", path.name))
    for extra, deps in (project.get("optional-dependencies") or {}).items():
        for dep in deps:
            inv.edges.append(_make_edge(dep, extra, path.name))
    # PEP 735 dependency groups
    for grp, deps in (data.get("dependency-groups") or {}).items():
        for dep in deps:
            if isinstance(dep, str):
                inv.edges.append(_make_edge(dep, grp, path.name))

    poetry = data.get("tool", {}).get("poetry", {})
    for name, spec in (poetry.get("dependencies") or {}).items():
        if name.lower() != "python":
            inv.edges.append(_poetry_edge(name, spec, "main", path.name))
    for name, spec in (poetry.get("dev-dependencies") or {}).items():
        inv.edges.append(_poetry_edge(name, spec, "dev", path.name))
    for gname, gtbl in (poetry.get("group") or {}).items():
        for name, spec in (gtbl.get("dependencies") or {}).items():
            inv.edges.append(_poetry_edge(name, spec, gname, path.name))


def _parse_pipfile(path: Path, inv: DependencyInventory) -> None:
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        logger.warning("Could not read Pipfile %s: %s", path, e)
        return
    inv.manifests.append(path.name)
    for section, group in (("packages", "main"), ("dev-packages", "dev")):
        for name, spec in (data.get(section) or {}).items():
            inv.edges.append(_poetry_edge(name, spec, group, path.name))


def build_inventory(project_dir: str | Path) -> DependencyInventory:
    """Scan a project's PyPI manifests and classify every dependency edge."""
    project_path = Path(project_dir)
    inv = DependencyInventory()
    try:
        sources = detect_sources(project_path)
    except OSError:
        sources = []

    req_queue: list = []
    visited: set = set()
    saw_setup = False

    for src in sources:
        name = src.path.name.lower()
        if (name.startswith("requirements") and name.endswith((".txt", ".in"))) or name == "pip-packages.txt":
            p = src.path.resolve()
            if p not in visited:
                visited.add(p)
                req_queue.append((p, _group_for_filename(name)))
        elif name == "pyproject.toml":
            _parse_pyproject(src.path, inv)
        elif name == "pipfile":
            _parse_pipfile(src.path, inv)
        elif name in ("setup.py", "setup.cfg"):
            saw_setup = True

    # Process requirements files, following -r includes transitively.
    while req_queue:
        path, group = req_queue.pop(0)
        _parse_requirements_file(path, group, inv, visited, req_queue)

    if saw_setup:
        inv.notes.append("setup.py/setup.cfg present — its install_requires/extras_require "
                         "are not yet edge-classified (follow-up).")

    return inv
