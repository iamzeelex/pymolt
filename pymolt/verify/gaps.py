"""Coverage-aware blind-spot map for dependency usage (host side, >=3.12).

The boundary tracer only sees behavior where code actually runs — so a suite at 45% coverage
yields behavioral facts for 45% of your dependency interactions and *nothing* for the rest.
You cannot fabricate behavior for code that never executed, but you CAN make the blind zones
explicit and actionable: statically find every place your code touches a dependency, overlay
the coverage map, and report which of those sites tests exercise vs which are blind.

This pairs with ``boundary_tracer`` (what behavior happened) and ``coverage`` (what ran):
``gaps`` answers "where does my code use dependencies that tests never reach?" — the list to
either write tests for or exercise by running the app under the Mode-B watcher.

Pure functions over an AST scan + a coverage.py JSON report; unit-tested without a real suite.
"""
import ast
import os
import sys

from pydantic import BaseModel, Field


class UsageSite(BaseModel):
    """One place project code references a dependency symbol (from static analysis)."""

    rel_path: str       # file path relative to the project root
    line: int
    dependency: str     # top-level dependency, e.g. "flask"
    symbol: str         # resolved symbol, e.g. "flask.views.MethodView"
    kind: str           # import | call | attribute | subclass | decorator
    covered: bool | None = None  # set by classify_gaps: True/False, or None if unknown


class GapReport(BaseModel):
    """Per-run blind-spot map: dependency-usage sites split into exercised vs blind."""

    sites: list[UsageSite] = Field(default_factory=list)
    covered: int = 0
    blind: int = 0
    unknown: int = 0

    def by_dependency(self) -> dict:
        """{dependency: {"covered": n, "blind": n}} for a quick per-dep view."""
        out: dict = {}
        for s in self.sites:
            d = out.setdefault(s.dependency, {"covered": 0, "blind": 0, "unknown": 0})
            d["covered" if s.covered else ("blind" if s.covered is False else "unknown")] += 1
        return out

    def blind_sites(self) -> list[UsageSite]:
        return [s for s in self.sites if s.covered is False]


# ── static scan: where does project code touch dependencies? ───────────────────
def _attr_chain(node):
    parts = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        parts.reverse()
        return cur.id, ".".join(parts)
    return None, ""


def _is_dependency(top: str, local: set, deps: set | None) -> bool:
    from pymolt.core.legacy_stdlib import is_py2_stdlib

    if not top or top in local:
        return False
    if top in sys.stdlib_module_names:
        return False
    # Python 2's stdlib is missing from this interpreter's list, so it would
    # otherwise count as a third-party usage site to cover with tests.
    if is_py2_stdlib(top):
        return False
    if deps is not None:
        return top in deps
    return True


def scan_file(path: str, rel_path: str, local: set, deps: set | None) -> list:
    """Find dependency-usage sites in one .py file (imports + calls/attrs/bases/decorators)."""
    try:
        tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
    except (SyntaxError, UnicodeDecodeError):
        return []

    names: dict = {}          # local name -> fully-qualified import path
    sites: list = []
    seen: set = set()

    def emit(symbol, kind, line):
        top = symbol.split(".", 1)[0]
        if not _is_dependency(top, local, deps):
            return
        key = (symbol, line)
        if key in seen:
            return
        seen.add(key)
        sites.append(UsageSite(rel_path=rel_path, line=line, dependency=top,
                               symbol=symbol, kind=kind))

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                names[a.asname or a.name] = a.name
                if a.asname is None and "." in a.name:
                    names.setdefault(a.name.split(".")[0], a.name.split(".")[0])
                emit(a.name, "import", node.lineno)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative (intra-project) import
                continue
            mod = node.module or ""
            for a in node.names:
                fqn = f"{mod}.{a.name}" if mod else a.name
                names[a.asname or a.name] = fqn
                emit(fqn, "import", node.lineno)

    def resolve(expr):
        root, dotted = _attr_chain(expr)
        if root is None or root not in names:
            return None
        return names[root] + dotted[len(root):]

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            sym = resolve(node.func)
            if sym:
                emit(sym, "call", node.func.lineno)
        elif isinstance(node, ast.ClassDef):
            for base in node.bases:
                sym = resolve(base)
                if sym:
                    emit(sym, "subclass", base.lineno)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                target = dec.func if isinstance(dec, ast.Call) else dec
                sym = resolve(target)
                if sym:
                    emit(sym, "decorator", target.lineno)
        elif isinstance(node, ast.Attribute):
            sym = resolve(node)
            if sym:
                emit(sym, "attribute", node.lineno)

    return sites


_DEFAULT_EXCLUDES = {".venv", "venv", "tests", "test", "build", "dist", ".git", "__pycache__"}


def _local_packages(project_dir: str) -> set:
    """Top-level package names that ARE the project (dirs with __init__.py)."""
    local = set()
    for name in os.listdir(project_dir):
        p = os.path.join(project_dir, name)
        if os.path.isdir(p) and os.path.exists(os.path.join(p, "__init__.py")):
            local.add(name)
    return local


def scan_usage_sites(project_dir: str, deps=None, exclude_dirs=None, packages=None) -> list:
    """Scan a project for every place ITS OWN code references a dependency symbol.

    Only the project's own package(s) are scanned — by default the top-level dirs with an
    ``__init__.py`` (so ``examples/``, ``setup.py``, vendored trees etc. are not mistaken for
    your code). Pass ``packages`` to scan specific subtrees, or fall back to the whole tree when
    there is no package (flat scripts).
    """
    project_dir = os.path.abspath(project_dir)
    local = _local_packages(project_dir)
    excludes = _DEFAULT_EXCLUDES if exclude_dirs is None else set(exclude_dirs)
    deps_set = set(deps) if deps is not None else None

    roots = packages if packages is not None else sorted(local)
    scan_dirs = [os.path.join(project_dir, r) for r in roots] if roots else [project_dir]

    sites: list = []
    for base in scan_dirs:
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in excludes]
            for fn in files:
                if not fn.endswith(".py"):
                    continue
                path = os.path.join(root, fn)
                rel = os.path.relpath(path, project_dir)
                sites.extend(scan_file(path, rel, local, deps_set))
    return sites


# ── overlay coverage onto the static sites ─────────────────────────────────────
def _coverage_executed_by_relpath(coverage_data: dict, cov_root: str | None) -> dict:
    """{relative_path: set(executed_lines)} from a coverage.py JSON report.

    Paths are made project-relative (via ``cov_root``, e.g. the container workdir) so a scan on
    the host matches a coverage run from a different absolute root.
    """
    out: dict = {}
    for path, info in (coverage_data.get("files") or {}).items():
        # coverage.py stores paths relative to its run dir; only remap absolute ones.
        rel = os.path.relpath(path, cov_root) if (cov_root and os.path.isabs(path)) else path
        executed = set(info.get("executed_lines", []) if isinstance(info, dict) else [])
        out[os.path.normpath(rel)] = executed
    return out


def classify_gaps(sites: list, coverage_data: dict, cov_root: str | None = None) -> GapReport:
    """Mark each usage site covered/blind by overlaying a coverage.py JSON report.

    A site is *covered* if its line executed, *blind* if the file is in the report but the line
    did not execute (or the file is absent — never imported/run). Lines in files coverage never
    tracked at all are left ``unknown`` rather than guessed.
    """
    executed = _coverage_executed_by_relpath(coverage_data, cov_root)
    report = GapReport()
    for s in sites:
        rel = os.path.normpath(s.rel_path)
        if rel in executed:
            s.covered = s.line in executed[rel]
        else:
            # absent from a non-empty report => never ran => blind; empty report => unknown
            s.covered = False if executed else None
        report.sites.append(s)
        if s.covered is True:
            report.covered += 1
        elif s.covered is False:
            report.blind += 1
        else:
            report.unknown += 1
    return report
