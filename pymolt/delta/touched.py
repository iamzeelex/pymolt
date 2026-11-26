"""Touched spellings the runtime's code names and the contact map cannot see.

The baseline records a callee by its *code* name, so an aliased attribute loses the spelling
the project wrote (``G.node[n]`` is recorded as ``Graph.nodes``; ``self.assertEquals`` as
``assertEqual``), and attributes inherited through a project class
(``class User(BaseModel)`` ... ``User.__fields__``, ``self.assertEquals`` in a
``unittest.TestCase`` subclass) never name the dependency at all. Two generic finders:

* :func:`alias_spellings`: for a baseline fact whose site statement spells an attribute other
  than the fact's own leaf, ``<receiver type>.<attr>`` (a project receiver resolves to its first
  dependency base);
* :func:`inherited_spellings`: a static scan of the project's classes: ``self.x`` /
  ``cls.x`` / ``Project.x`` where ``x`` is defined by no project class in the hierarchy becomes
  ``<first dependency base>.x``;
* :func:`receiver_spellings`: for a baseline call on a receiver, the spelling the call reaches
  through it, ``<receiver's class>.<attribute>``, plus the same attribute under each public
  spelling the class may have (``pandas.core.frame.DataFrame.groupby`` and
  ``pandas.DataFrame.groupby`` for ``df.groupby``, which the old version recorded as
  ``NDFrame.groupby``, the class that defined it). Where the call resolves in the new version
  depends on the receiver's class, not on where the old version defined the method.

Pure functions over parsed data; nothing here imports a dependency or runs project code.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from pymolt.core.stdlib import is_stdlib as _stdlib_top

_ATTR = re.compile(r"\.([A-Za-z_]\w*)")
# Value markers that name the value's type (``_normalize``): an object, a class (a ``cls``
# receiver), a value kept by type only, a structure (a pandas frame), a value shown by its shape,
# a dataclass, a cycle broken here.
_MARKERS = ("__object__", "__type__", "__opaque__", "__structure__", "__shape__",
            "__dataclass__", "__cycle__")
_SELF_KEYS = ("self", "cls", "obj")
MAX_RECEIVER_SPELLINGS = 3000
_SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".tox", ".nox",
              "build", "dist", ".pymolt", ".pymolt_cache"}
_MAX_FILES = 4000


def is_stdlib(spelling: str) -> bool:
    return _stdlib_top(spelling.split(".")[0])   # every version's stdlib, not the host's


# ── project classes and their dependency bases ───────────────────────────────────────────


class _ProjectClass:
    def __init__(self, dotted: str, bases: list[str]) -> None:
        self.dotted = dotted          # project-side dotted name, "app.models.User"
        self.bases = bases            # resolved dotted names (project or dependency)
        self.defined: set[str] = set()  # attributes the class body itself defines


def _module_name(root: Path, path: Path) -> str:
    rel = path.relative_to(root).with_suffix("")
    parts = list(rel.parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _py_files(root: Path) -> list[Path]:
    out: list[Path] = []
    for p in sorted(root.rglob("*.py")):
        rel = p.relative_to(root).parts
        if any(part in _SKIP_DIRS or part.endswith(".egg-info") or
               (part.startswith(".") and part not in (".",)) for part in rel[:-1]):
            continue
        out.append(p)
        if len(out) >= _MAX_FILES:
            break
    return out


def _imports(tree: ast.Module, module: str) -> dict[str, str]:
    """Local name -> dotted target, from the module's import statements."""
    names: dict[str, str] = {}
    pkg = module.rsplit(".", 1)[0] if "." in module else ""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.asname:
                    names[a.asname] = a.name
                else:
                    names[a.name.split(".")[0]] = a.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parts = pkg.split(".") if pkg else []
                parts = parts[: max(0, len(parts) - (node.level - 1))]
                base = ".".join([*parts, base] if base else parts)
            for a in node.names:
                if a.name != "*":
                    names[a.asname or a.name] = f"{base}.{a.name}" if base else a.name
    return names


def _dotted(node: ast.AST, names: dict[str, str]) -> str | None:
    if isinstance(node, ast.Name):
        return names.get(node.id)
    if isinstance(node, ast.Attribute):
        head = _dotted(node.value, names)
        return f"{head}.{node.attr}" if head else None
    return None


def _defined_attrs(cls: ast.ClassDef) -> set[str]:
    out: set[str] = set()
    for stmt in cls.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(stmt.name)
        elif isinstance(stmt, ast.Assign):
            for t in stmt.targets:
                for n in ast.walk(t):
                    if isinstance(n, ast.Name):
                        out.add(n.id)
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            out.add(stmt.target.id)
    # attributes bound on self anywhere in the class
    for n in ast.walk(cls):
        if (isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Store)
                and isinstance(n.value, ast.Name) and n.value.id in ("self", "cls")):
            out.add(n.attr)
    return out


_Parsed = list[tuple[Path, str, ast.Module, dict[str, str]]]


def project_classes(root: Path) -> tuple[dict[str, _ProjectClass], _Parsed]:
    """``{dotted class name: _ProjectClass}`` and the parsed files, for ``root``."""
    classes: dict[str, _ProjectClass] = {}
    parsed: _Parsed = []
    for path in _py_files(root):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeDecodeError, ValueError):
            continue
        module = _module_name(root, path)
        names = _imports(tree, module)
        parsed.append((path, module, tree, names))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                bases = []
                for b in node.bases:
                    d = _dotted(b, names)
                    if d is None and isinstance(b, ast.Name):
                        d = f"{module}.{b.id}"  # a class of the same module
                    if d:
                        bases.append(d)
                pc = _ProjectClass(f"{module}.{node.name}", bases)
                pc.defined = _defined_attrs(node)
                classes[pc.dotted] = pc
    return classes, parsed


def dependency_bases(dotted: str, classes: dict[str, _ProjectClass],
                     _seen: set[str] | None = None) -> list[str]:
    """The dependency (non-project) bases of a project class, nearest first (MRO-ish)."""
    seen = _seen if _seen is not None else set()
    if dotted in seen or dotted not in classes:
        return []
    seen.add(dotted)
    out: list[str] = []
    for b in classes[dotted].bases:
        if b in classes:
            out += dependency_bases(b, classes, seen)
        elif b != "object":
            out.append(b)
    return out


def _ancestors_define(dotted: str, attr: str, classes: dict[str, _ProjectClass],
                      seen: set[str] | None = None) -> bool:
    seen = seen if seen is not None else set()
    if dotted in seen or dotted not in classes:
        return False
    seen.add(dotted)
    if attr in classes[dotted].defined:
        return True
    return any(_ancestors_define(b, attr, classes, seen) for b in classes[dotted].bases)


def inherited_spellings(root: Path) -> set[str]:
    """``<first dependency base>.<attr>`` for every ``self.x`` / ``cls.x`` / ``Project.x`` whose
    ``x`` no project class in the hierarchy defines."""
    classes, parsed = project_classes(Path(root))
    out: set[str] = set()
    for _path, module, tree, names in parsed:
        for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
            own = f"{module}.{cls.name}"
            if own not in classes:
                continue
            for node in ast.walk(cls):
                if not (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)):
                    continue
                recv = node.value.id
                if recv in ("self", "cls"):
                    target = own
                else:
                    target = _project_target(recv, module, names, classes)
                if target is None or node.attr.startswith("__") and node.attr.endswith("__") \
                        and node.attr not in ("__fields__",):
                    continue
                if _ancestors_define(target, node.attr, classes):
                    continue
                bases = dependency_bases(target, classes)
                if bases:
                    out.add(f"{bases[0]}.{node.attr}")
        # ``Project.attr`` outside any class body (module functions)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                target = _project_target(node.value.id, module, names, classes)
                if target and not _ancestors_define(target, node.attr, classes):
                    bases = dependency_bases(target, classes)
                    if bases and not (node.attr.startswith("__") and node.attr.endswith("__")
                                      and node.attr != "__fields__"):
                        out.add(f"{bases[0]}.{node.attr}")
    return out


def _project_target(name: str, module: str, names: dict[str, str],
                    classes: dict[str, _ProjectClass]) -> str | None:
    cand = names.get(name) or f"{module}.{name}"
    return cand if cand in classes else None


# ── aliased attributes: the site's spelling vs the recorded code name ────────────────────


def _receiver_type(fact: Any) -> str | None:
    """The class of the call's receiver as the baseline recorded it: ``self`` or ``cls``; for a
    call recorded under a decorator's generic wrapper (``...<locals>.wrapper(*args,
    **kwargs)``, whose own signature binds nothing), the first positional argument it got."""
    inp = getattr(fact, "input", None)
    bound = (inp or {}).get("bound") if isinstance(inp, dict) else None
    if not isinstance(bound, dict):
        return None
    for key in _SELF_KEYS:
        val = bound.get(key)
        if isinstance(val, dict):
            for marker in _MARKERS:
                if isinstance(val.get(marker), str) and "." in val[marker]:
                    return val[marker]
    symbol = getattr(fact, "symbol", None) or ""
    if not bound and "<locals>" in symbol and isinstance(inp.get("varargs"), dict):
        first = (inp["varargs"].get("__tuple__") or [None])[0]
        if isinstance(first, dict):
            for marker in _MARKERS:
                if isinstance(first.get(marker), str) and "." in first[marker]:
                    return first[marker]
    return None


def public_candidates(name: str) -> list[str]:
    """The spellings that may name the class or function ``name`` (``module.Qual``): itself,
    then each enclosing package's export of the same leaf, nearest first, the top-level
    package last (``pandas.core.frame.DataFrame``, ``pandas.core.DataFrame``,
    ``pandas.DataFrame``). Which of them really names it is for the old interpreter to say."""
    parts = name.split(".")
    out = [name]
    for i in range(len(parts) - 2, 0, -1):
        out.append(".".join(parts[:i]) + "." + parts[-1])
    return out


def receiver_class(fact: Any, root: Path | None = None,
                   classes: dict[str, _ProjectClass] | None = None) -> str | None:
    """The dependency class a call's receiver had: ``type(self)`` (or ``cls``) as the baseline
    recorded it, a project class resolved to its first dependency base (``classes``:
    ``project_classes(root)``). None when the call has no recorded receiver."""
    recv = _receiver_type(fact)
    if recv is None:
        return None
    if classes is not None and recv in classes:
        bases = dependency_bases(recv, classes)
        return bases[0] if bases else None
    return recv


def receiver_spellings(facts: Iterable[Any], root: Path | None = None) -> dict[str, str]:
    """``{spelling: receiver class}``: for every baseline call on a receiver of a dependency
    class, the attribute it called under that class and under each of its public candidates
    (:func:`public_candidates`). A generated or nested name (``<locals>``, ``<lambda>``) is no
    attribute anyone spells and is skipped; so are standard-library classes (the dependency
    delta does not cover them). Bounded by ``MAX_RECEIVER_SPELLINGS``."""
    classes: dict[str, _ProjectClass] | None = None
    if root is not None:
        try:
            classes, _ = project_classes(Path(root))
        except OSError:
            classes = None
    out: dict[str, str] = {}
    seen: set[tuple[str, str]] = set()
    for f in facts:
        symbol = getattr(f, "symbol", None)
        if not isinstance(symbol, str) or "." not in symbol or "<" in symbol:
            continue
        cls = receiver_class(f, root, classes)
        if cls is None or "<" in cls or is_stdlib(cls):
            continue
        leaf = symbol.rsplit(".", 1)[1]
        if (cls, leaf) in seen:
            continue
        seen.add((cls, leaf))
        for cand in public_candidates(cls):
            out.setdefault(f"{cand}.{leaf}", cls)
        if len(out) >= MAX_RECEIVER_SPELLINGS:
            break
    return out


def alias_spellings(facts: Iterable[Any], root: Path | None = None) -> set[str]:
    """``<receiver type>.<attr>`` for baseline facts whose site statement spells an attribute
    that is not the fact's own leaf and not the leaf of another fact on the same statement.

    A project receiver type resolves to its first dependency base (``root`` needed)."""
    facts = list(facts)
    by_site: dict[tuple, set[str]] = {}
    for f in facts:
        by_site.setdefault((f.file, f.func, (f.text or "").strip()), set()).add(
            f.symbol.rsplit(".", 1)[-1])
    classes: dict[str, _ProjectClass] | None = None
    out: set[str] = set()
    for f in facts:
        text = (f.text or "").strip()
        leaf = f.symbol.rsplit(".", 1)[-1]
        if not text or leaf.startswith("__"):
            continue
        attrs = set(_ATTR.findall(text))
        if leaf in attrs:
            continue
        unexplained = attrs - by_site.get((f.file, f.func, text), set())
        if not unexplained:
            continue
        recv = _receiver_type(f)
        if recv is None:
            continue
        types = [recv]
        if root is not None:
            if classes is None:
                classes, _ = project_classes(Path(root))
            if recv in classes:
                bases = dependency_bases(recv, classes)
                types = bases[:1]
        for t in types:
            for attr in sorted(unexplained):
                out.add(f"{t}.{attr}")
    return out
