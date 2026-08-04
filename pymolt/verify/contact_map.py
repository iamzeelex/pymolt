"""Static contact map — the cheap 'where does our code touch third-party code' pass.

Built on LibCST (no PyCG): per file, LibCST's QualifiedNameProvider resolves each
call's target to a fully-qualified name following imports, and we keep only the
calls whose target is a third-party dependency (not stdlib, not first-party, not
pymolt's own tooling). This is the static **denominator** — every direct contact
point that *could* run, found without executing anything — to be filled in by the
dynamic boundary trace and its blind spots (``gaps``).

Attribution is function-level (caller qualname → dependency qualname); exact
call-site lines are the dynamic trace's job (``where`` in the contract).

Honest scope: this resolves *direct, syntactically-visible* calls (the right
granularity for a contact map). Like any pure-static pass it does not follow
dynamic dispatch, monkeypatching, or ``getattr`` — that is exactly what the
dynamic trace confirms.
"""

import logging
import os
import sys
from pathlib import Path

import libcst as cst
from libcst.metadata import (
    MetadataWrapper,
    QualifiedNameProvider,
    QualifiedNameSource,
)
from pydantic import BaseModel, Field

from pymolt.core.legacy_stdlib import describe, is_py2_stdlib

logger = logging.getLogger(__name__)

_IGNORE_DIRS = {
    ".git", ".hg", ".svn", ".venv", "venv", "env", "node_modules", "vendor",
    "__pycache__", ".tox", ".nox", ".mypy_cache", ".pytest_cache", ".pymolt",
    ".pymolt_cache", "site-packages", "build", "dist", ".eggs",
}

# pymolt's own injected tooling — if the watcher bundle (export-watcher) was written
# into the project, its `pymolt_trace` package is *not* a real dependency.
_IGNORE_DEPS = {"pymolt", "pymolt_trace"}


class Contact(BaseModel):
    caller: str          # our qualname, e.g. "myapp.views.handler"
    target: str          # dependency qualname, e.g. "flask.cli.with_appcontext"
    dep: str             # top-level dependency, e.g. "flask"
    file: str | None = None  # our file the caller is defined in (best-effort)


class ContactMap(BaseModel):
    root: str
    files_scanned: int = 0
    contacts: list[Contact] = Field(default_factory=list)
    by_dep: dict[str, list[str]] = Field(default_factory=dict)  # dep -> sorted targets
    notes: list[str] = Field(default_factory=list)


def _python_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _IGNORE_DIRS and not d.endswith(".egg-info")]
        for name in filenames:
            if name.endswith(".py"):
                files.append(Path(dirpath) / name)
    return files


def _file_for_qualname(qualname: str, file_by_module: dict[str, str]) -> str | None:
    """The file of the longest module prefix of a qualname (handles Class.method)."""
    parts = qualname.split(".")
    for i in range(len(parts), 0, -1):
        candidate = ".".join(parts[:i])
        if candidate in file_by_module:
            return file_by_module[candidate]
    return None


def _module_name(file: Path, root: Path) -> str:
    rel = file.relative_to(root).with_suffix("")
    parts = list(rel.parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


class _ContactVisitor(cst.CSTVisitor):
    """Collect our-code → dependency call contacts in one file via LibCST.

    Tracks the enclosing def/class nesting to attribute each call to its caller
    qualname, and uses QualifiedNameProvider to resolve each call target.
    """

    METADATA_DEPENDENCIES = (QualifiedNameProvider,)

    def __init__(self, module_name: str) -> None:
        self.module_name = module_name
        self._scope: list[str] = []
        self.contacts: list[tuple[str, str, str]] = []  # (caller, target, dep_top)

    def visit_FunctionDef(self, node: cst.FunctionDef) -> None:
        self._scope.append(node.name.value)

    def leave_FunctionDef(self, node: cst.FunctionDef) -> None:
        self._scope.pop()

    def visit_ClassDef(self, node: cst.ClassDef) -> None:
        self._scope.append(node.name.value)

    def leave_ClassDef(self, node: cst.ClassDef) -> None:
        self._scope.pop()

    def _caller(self) -> str:
        return ".".join([self.module_name, *self._scope]) if self._scope else self.module_name

    def visit_Call(self, node: cst.Call) -> None:
        for q in self.get_metadata(QualifiedNameProvider, node.func, set()):
            # Only imported symbols cross the third-party boundary; locally
            # defined and builtin calls never do.
            if q.source != QualifiedNameSource.IMPORT:
                continue
            name = q.name
            if name.startswith("."):
                continue  # relative import → first-party
            self.contacts.append((self._caller(), name, name.split(".")[0]))


def build_contact_map(project_dir: str | Path, dependencies: set[str] | None = None) -> ContactMap:
    """Resolve our-code → dependency contacts statically with LibCST.

    ``dependencies`` (top-level names) optionally restricts what counts as a
    third-party target; otherwise anything that is neither stdlib nor first-party
    is treated as a dependency.
    """
    root = Path(project_dir).resolve()
    cmap = ContactMap(root=str(root))
    files = _python_files(root)
    cmap.files_scanned = len(files)
    if not files:
        cmap.notes.append("no Python files found")
        return cmap

    our_top_levels = {_module_name(f, root).split(".")[0] for f in files if _module_name(f, root)}
    file_by_module = {_module_name(f, root): str(f.relative_to(root)) for f in files}
    stdlib = sys.stdlib_module_names
    by_dep: dict[str, set[str]] = {}
    legacy_imports: set[str] = set()

    for file in files:
        module_name = _module_name(file, root)
        try:
            wrapper = MetadataWrapper(cst.parse_module(file.read_text(encoding="utf-8")))
            visitor = _ContactVisitor(module_name)
            wrapper.visit(visitor)
        except Exception as e:  # noqa: BLE001 — best-effort; one bad file never aborts
            logger.warning("Contact scan failed for %s: %s", file, e)
            cmap.notes.append(f"scan failed for {file.relative_to(root)}: {e}")
            continue

        for caller, target, dep_top in visitor.contacts:
            if dep_top in our_top_levels or dep_top in stdlib or dep_top in _IGNORE_DEPS:
                continue  # internal, stdlib, or pymolt's own tooling — not a contact
            if is_py2_stdlib(dep_top):
                # Python 2's standard library is absent from this interpreter's
                # stdlib_module_names, so it reads as third-party. It is neither:
                # it is evidence the code has not been ported yet, reported below.
                legacy_imports.add(dep_top)
                continue
            if dependencies is not None and dep_top not in dependencies:
                continue
            call_file = _file_for_qualname(caller, file_by_module)
            cmap.contacts.append(
                Contact(caller=caller, target=target, dep=dep_top, file=call_file)
            )
            by_dep.setdefault(dep_top, set()).add(target)

    # De-duplicate contacts (same caller→target can appear from repeated calls).
    seen: set[tuple[str, str]] = set()
    unique: list[Contact] = []
    for c in sorted(cmap.contacts, key=lambda c: (c.dep, c.target, c.caller)):
        key = (c.caller, c.target)
        if key not in seen:
            seen.add(key)
            unique.append(c)
    cmap.contacts = unique
    cmap.by_dep = {dep: sorted(targets) for dep, targets in sorted(by_dep.items())}
    if legacy_imports:
        cmap.notes.append(
            f"Python-2 stdlib imported ({len(legacy_imports)}): {describe(legacy_imports)} "
            "— not dependencies; this code has not been ported to Python 3 yet."
        )
    return cmap
