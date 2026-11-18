"""The old side of a delta, read statically from the old release's source.

Used when no old interpreter can be reached (no container running, no host Python of that
version). ``static_probe`` answers the same question ``probe.py`` answers (``resolve(path)``:
existence, kind, owner, canonical name, signature, bases, deprecation prose per step of an
import path), but from the old wheel or sdist (``delta/acquire.py``) with griffe
(``allow_inspection=False``: nothing is imported or executed).

The answer is deliberately weaker than a probe's, and says so:

- **unknown, never absent**, when the reader cannot be sure: a base class that does not
  resolve (so inherited members are invisible), a namespace subpackage griffe cannot load,
  a compiled module, a module that builds names dynamically (``__getattr__`` it cannot
  read, ``setattr``, ``globals()``, an unresolved ``import *``), a name defined only under
  ``TYPE_CHECKING``, an attribute of a computed value. Such a step has ``exists: None``
  and ``unknown: <why>``; the diff never turns it into a fact. (griffe 2.0.2 loses inherited
  members when a base lives in an unloadable namespace subpackage: 56 of 111 flask 2.0.3 ->
  3.0.0 "removals" measured in axiom_graph were false. The new side is probed live, so that
  error class does not reach it.)
- **aliases and re-exports are followed** whatever ``__all__`` says; an alias to a name
  outside the archive (``flask.Markup`` -> ``markupsafe.Markup``) exists, with the target as
  its canonical name and no kind;
- **PEP 562** names come from ``delta/fetch.py``'s scope reader (``present``,
  ``module_warnings``);
- **defaults** are kept only when they are literals, spelled by the probe's own
  ``default_repr``; anything else is ``UNEVALUATED`` and never compared;
- a signature is only reported when it is certain: a decorator that may rewrite it, a
  dataclass-like class, or an ``__init__`` that is not found gives ``shape: None``.

Results are cached with the acquired release; nothing here touches the network.
"""

from __future__ import annotations

import ast
import builtins
import logging
import re
import time
import warnings
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from pymolt.delta import fetch as fetch_mod
from pymolt.delta import probe as probe_mod
from pymolt.delta.acquire import Acquired

log = logging.getLogger(__name__)

UNEVALUATED = "<unevaluated>"
_MAX_SRC = 400_000
_ID_MAX_SUBMODULES = 120
_ID_SKIP_SUBMODULES = ("tests", "test", "conftest", "setup", "_version")
_SAFE_DECORATORS = {
    "classmethod", "staticmethod", "abstractmethod", "abc.abstractmethod", "wraps",
    "functools.wraps", "lru_cache", "functools.lru_cache", "cache", "functools.cache",
    "contextmanager", "contextlib.contextmanager", "overload", "typing.overload",
    "final", "typing.final", "property", "cached_property", "functools.cached_property",
}
_HAZARD = re.compile(r"\bsetattr\s*\(|\.__dict__\b|\bglobals\s*\(|\blocals\s*\(|\bsys\.modules\b")
_WILDCARD = re.compile(r"^\s*from\s+(\.*)([\w\.]*)\s+import\s+\*", re.MULTILINE)
_DEL = re.compile(r"^del\s+([A-Za-z_][\w\s,]*?)\s*(?:#.*)?$", re.MULTILINE)
_SYS_MODULES_SET = re.compile(r"sys\.modules\[[^\]]+\]\s*=(?!=)")
_DEF_GETATTR = re.compile(r"^def\s+__getattr__\s*\(", re.MULTILINE)


@dataclass
class _Outcome:
    status: str  # "found" | "absent" | "unknown"
    obj: Any = None
    owner: str = ""
    owner_module: str = ""
    receiver: str = ""
    why: str = ""
    pep562: bool = False
    submodule: str | None = None  # dotted name of a submodule found on disk
    compiled_or_namespace: str | None = None


def _expr_text(expr: Any) -> str:
    return str(expr) if expr is not None else ""


def _strip_subscript(text: str) -> str:
    return re.sub(r"\[.*\]\s*$", "", text.strip())


def _decorator_name(dec: Any) -> str:
    value = getattr(dec, "value", dec)
    text = _expr_text(value)
    return _strip_subscript(text.split("(", 1)[0])


class StaticReader:
    """Reads one release's import paths with griffe. One instance per acquired release."""

    def __init__(self, acquired: Acquired, dist: str) -> None:
        import griffe

        self.acquired = acquired
        self.dist = dist
        self.search_dir = Path(acquired.search_dir)
        self.roots = set(acquired.roots)
        self._griffe = griffe
        self._loader = griffe.GriffeLoader(search_paths=[str(self.search_dir)],
                                           allow_inspection=False)
        self._loaded: dict[str, str | None] = {}  # root -> error (None = loaded)
        self._src: dict[str, str | None] = {}
        self._counts: dict[str, dict[str, int]] = {}
        self._identities: dict[str, dict[str, list[str]]] = {}

    # ── loading ─────────────────────────────────────────────────────────────────────

    def _ensure_root(self, root: str) -> str | None:
        if root in self._loaded:
            return self._loaded[root]
        error: str | None = None
        griffe_log = logging.getLogger("griffe")
        previous = griffe_log.level
        griffe_log.setLevel(logging.CRITICAL)
        try:
            with warnings.catch_warnings():  # old source: invalid escapes, py2 idioms
                warnings.simplefilter("ignore")
                self._loader.load(root)
        except Exception as exc:  # noqa: BLE001 — a package griffe cannot load is unknown
            error = f"griffe could not load {root}: {type(exc).__name__}: {str(exc)[:160]}"
        finally:
            griffe_log.setLevel(previous)
        self._loaded[root] = error
        return error

    def _member(self, path: str) -> Any:
        try:
            return self._loader.modules_collection.get_member(path)
        except Exception:  # noqa: BLE001
            return None

    def _fs_kind(self, parts: Sequence[str]) -> str | None:
        """What ``a.b.c`` is on disk: package | module | namespace | compiled | None.

        Names are matched against the directory listing, so a case-insensitive file system
        (macOS) cannot make ``Child`` the module ``child.py``.
        """
        parent = self.search_dir.joinpath(*parts[:-1])
        try:
            names = {p.name for p in parent.iterdir()}
        except OSError:
            return None
        leaf = parts[-1]
        if leaf in names and (parent / leaf).is_dir():
            inner = {p.name for p in (parent / leaf).iterdir()}
            if "__init__.py" in inner or "__init__.pyi" in inner:
                return "package"
            return "namespace"
        if f"{leaf}.py" in names or f"{leaf}.pyi" in names:
            return "module"
        if any(n.startswith(leaf + ".") and n.endswith((".so", ".pyd")) for n in names):
            return "compiled"
        return None

    def _rel(self, path: Any) -> str | None:
        try:
            return Path(path).resolve().relative_to(self.search_dir.resolve()).as_posix()
        except (ValueError, OSError, TypeError):
            return None

    def _source_of_module(self, mod: Any) -> str | None:
        key = getattr(mod, "path", "")
        if key not in self._src:
            try:
                self._src[key] = Path(mod.filepath).read_text(encoding="utf-8", errors="replace")
            except (OSError, TypeError, AttributeError):
                self._src[key] = None
        return self._src[key]

    # ── resolving objects ───────────────────────────────────────────────────────────

    def _final(self, obj: Any) -> tuple[Any, str | None]:
        """``(target, external_path)``: an alias followed to its end, else the last hop's path.

        Hops are followed one at a time: an alias chain that ends in a compiled module is
        named by that module's path (``pandas._libs.tslibs.timestamps.Timestamp``), not by
        the first alias on the way (``pandas.core.api.Timestamp``).
        """
        cur, hops = obj, 0
        while getattr(cur, "is_alias", False) and hops < 30:
            hops += 1
            path = getattr(cur, "target_path", None)
            nxt = self._member(path) if path else None
            if nxt is None:
                return None, path
            cur = nxt
        if getattr(cur, "is_alias", False):
            return None, getattr(cur, "target_path", None)
        if type(cur).__name__ == "Attribute":
            value = getattr(cur, "value", None)
            if type(value).__name__ in ("ExprName", "ExprAttribute"):
                try:
                    path = cur.parent.resolve(_expr_text(value))
                    target = self._member(path)
                    if target is not None:
                        inner, ext = self._final(target)
                        if inner is not None and type(inner).__name__ in (
                                "Class", "Function", "Module"):
                            return inner, None
                        if inner is None and ext:
                            return None, ext
                except Exception:  # noqa: BLE001
                    pass
        return cur, None

    def _bases(self, cls: Any) -> tuple[list[Any], list[str] | None, bool]:
        """``(resolved base classes, base names for comparison | None, complete)``.

        ``names`` is None when any base cannot be named; ``complete`` is False when any base
        is not a class found in the archive (inherited members may then be invisible).
        """
        resolved: list[Any] = []
        names: list[str] | None = []
        complete = True
        for expr in cls.bases:
            node = getattr(expr, "left", expr)  # Generic[T] -> Generic
            text = _strip_subscript(_expr_text(node))
            if text == "object":
                if names is not None:
                    names.append("builtins.object")
                continue
            path: str | None
            try:
                path = node.canonical_path
            except Exception:  # noqa: BLE001
                try:
                    path = cls.resolve(text)
                except Exception:  # noqa: BLE001
                    path = None
            if path == text and "." not in text and text in vars(builtins):
                path = None  # a builtin (Exception, dict, ...), not a name of the archive
            if type(node).__name__ == "ExprCall":  # with_metaclass(Meta, Base): not a name
                path = None
                text = ""
            if path is None:
                complete = False
                if text in vars(builtins) and names is not None:
                    names.append("builtins." + text)
                else:
                    names = None
                continue
            found = self._member(path)
            target = self._final(found)[0] if found is not None else None
            if target is not None and type(target).__name__ == "Class":
                resolved.append(target)
                if names is not None:
                    names.append(target.path)
            else:
                complete = False
                # a base outside the archive is named by its import path; one inside it that is
                # not a class (``_Extension = get_unpatched(...)``) cannot be named truthfully
                if found is None and path.split(".")[0] not in self.roots and names is not None:
                    names.append(path)
                else:
                    names = None
        if names is not None and not names:
            names = ["builtins.object"]
        return resolved, names, complete

    def _mro(self, cls: Any) -> tuple[list[Any], bool]:
        out: list[Any] = []
        complete = True
        seen: set[int] = set()

        def walk(c: Any) -> None:
            nonlocal complete
            if id(c) in seen:
                return
            seen.add(id(c))
            out.append(c)
            bases, _names, ok = self._bases(c)
            complete &= ok
            for b in bases:
                walk(b)

        walk(cls)
        return out, complete

    def _class_hazards(self, mro: list[Any]) -> str | None:
        for c in mro:
            if "__getattr__" in c.members or "__getattribute__" in c.members:
                return f"{c.path} defines __getattr__"
            if getattr(c, "keywords", None):
                return f"{c.path} has a metaclass or class keywords"
            if c.decorators:
                return f"{c.path} is decorated"
            mod = c.module
            src = self._source_of_module(mod) if mod is not None else None
            if src is None:
                return f"source of {c.path} is unreadable"
            if _HAZARD.search(src) or re.search(rf"\b{re.escape(c.name)}\.\w+\s*=[^=]", src):
                return f"{mod.path} sets attributes dynamically"
        return None

    # ── lookups ─────────────────────────────────────────────────────────────────────

    def _wildcards_resolved(self, mod: Any, depth: int = 0, seen: set[str] | None = None
                            ) -> str | None:
        """Why a ``from x import *`` anywhere below ``mod`` cannot be followed, else None."""
        seen = seen if seen is not None else set()
        if mod.path in seen or depth > 8:
            return None
        seen.add(mod.path)
        src = self._source_of_module(mod)
        if src is None:
            return f"source of {mod.path} is unreadable"
        for rel, name in _WILDCARD.findall(src):
            if rel:
                base = mod.path if mod.is_package else mod.path.rsplit(".", 1)[0]
                for _ in range(len(rel) - 1):
                    base = base.rsplit(".", 1)[0]
                target = f"{base}.{name}" if name else base
            else:
                target = name
            found = self._member(target)
            if found is None or type(found).__name__ != "Module":
                return f"{mod.path} star-imports {target}, which is not readable in the archive"
            inner = self._wildcards_resolved(found, depth + 1, seen)
            if inner:
                return inner
        return None

    def _module_hazard(self, mod: Any) -> str | None:
        src = self._source_of_module(mod)
        if src is None:
            return f"source of {mod.path} is unreadable"
        if _DEF_GETATTR.search(src):
            return f"{mod.path} has a __getattr__ that does not name it"
        if _HAZARD.search(src):
            return f"{mod.path} builds names dynamically"
        return self._wildcards_resolved(mod)

    def _deleted(self, mod: Any, attr: str) -> bool:
        """``del attr`` at the module's top level (the name existed only while it ran)."""
        src = self._source_of_module(mod) or ""
        for m in _DEL.finditer(src):
            if attr in {n.strip() for n in m.group(1).split(",")}:
                return True
        return False

    def _bindings(self, mod: Any) -> dict[str, int]:
        """How many times each name is bound at the module's top level (``if``/``try`` bodies
        included, ``def``/``class`` bodies not): more than one means which one wins depends on
        the interpreter, and a static reader cannot say."""
        key = "bindings:" + mod.path
        if key in self._counts:
            return self._counts[key]
        counts: dict[str, int] = {}
        src = self._source_of_module(mod) or ""
        try:
            tree = ast.parse(src)
        except (SyntaxError, ValueError):
            tree = ast.Module(body=[], type_ignores=[])

        def bump(name: str) -> None:
            counts[name] = counts.get(name, 0) + 1

        def walk(body: list[ast.stmt]) -> None:
            for node in body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    bump(node.name)
                elif isinstance(node, (ast.Import, ast.ImportFrom)):
                    for a in node.names:
                        if a.name != "*":
                            bump(a.asname or a.name.split(".")[0])
                elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    for t in targets:
                        for n in ast.walk(t):
                            if isinstance(n, ast.Name):
                                bump(n.id)
                else:
                    for field_name in ("body", "orelse", "finalbody"):
                        walk(getattr(node, field_name, None) or [])
                    for h in getattr(node, "handlers", None) or []:
                        walk(h.body)

        walk(tree.body)
        self._counts[key] = counts
        return counts

    def _lookup_module(self, mod: Any, attr: str) -> _Outcome:
        src = self._source_of_module(mod) or ""
        if _SYS_MODULES_SET.search(src):  # a shim that swaps itself for another module
            return _Outcome("unknown", why=f"{mod.path} replaces itself in sys.modules")
        found = mod.members.get(attr)
        if found is not None and self._deleted(mod, attr):
            return _Outcome("absent")
        if found is not None and self._bindings(mod).get(attr, 1) > 1:
            return _Outcome("unknown", why=f"{attr} is bound more than once in {mod.path} "
                                           "(a conditional definition)")
        if found is not None:
            if getattr(found, "runtime", True) is False:
                return _Outcome("unknown", why=f"{attr} is defined only under TYPE_CHECKING")
            return _Outcome("found", found, f"{mod.path}.{attr}", mod.path,
                            f"{mod.path}.{attr}")
        if mod.is_package:
            sub = self._fs_kind([*mod.path.split("."), attr])
            if sub is not None:
                return _Outcome("found", None, f"{mod.path}.{attr}", mod.path,
                                f"{mod.path}.{attr}",
                                submodule=f"{mod.path}.{attr}", compiled_or_namespace=sub)
        src = self._source_of_module(mod)
        if src is not None and _DEF_GETATTR.search(src) and fetch_mod.present(src, [attr]):
            return _Outcome("found", None, f"{mod.path}.{attr}", mod.path,
                            f"{mod.path}.{attr}", pep562=True)
        hazard = self._module_hazard(mod)
        if hazard:
            return _Outcome("unknown", why=hazard)
        return _Outcome("absent")

    def _lookup_class(self, cls: Any, attr: str) -> _Outcome:
        mro, complete = self._mro(cls)
        for c in mro:
            found = c.members.get(attr)
            if found is None:
                continue
            labels = getattr(found, "labels", set())
            if "instance-attribute" in labels and "class-attribute" not in labels:
                return _Outcome("unknown", why=f"{attr} is an instance attribute of {c.path}")
            if getattr(found, "runtime", True) is False:
                return _Outcome("unknown", why=f"{attr} is defined only under TYPE_CHECKING")
            return _Outcome("found", found, f"{c.path}.{attr}", c.module.path,
                            f"{cls.path}.{attr}")
        if not complete:
            return _Outcome("unknown", why=f"a base class of {cls.path} does not resolve in the "
                                           "archive, so inherited members are invisible")
        if attr.startswith("__") and attr.endswith("__"):
            return _Outcome("unknown", why=f"{attr} may come from object")
        hazard = self._class_hazards(mro)
        if hazard:
            return _Outcome("unknown", why=hazard)
        return _Outcome("absent")

    # ── describing a found object ───────────────────────────────────────────────────

    def _shape(self, func: Any, labels: set[str], decorators: list[str]) -> list | None:
        if any(d not in _SAFE_DECORATORS for d in decorators):
            return None
        out = []
        params = list(func.parameters)
        if "classmethod" in labels and params:
            params = params[1:]
        for p in params:
            default = getattr(p, "default", None)
            if default is None:
                shown = None
            else:
                text = _expr_text(default)
                try:
                    shown = probe_mod.default_repr(ast.literal_eval(text))
                except Exception:  # noqa: BLE001 — not a literal: never compared
                    shown = UNEVALUATED
            out.append([p.name, p.kind.name.upper(), shown])
        return out

    def _class_shape(self, cls: Any) -> list | None:
        mro, complete = self._mro(cls)
        if not complete or any(c.decorators for c in mro):
            return None
        for c in mro:
            init = c.members.get("__init__")
            if init is None:
                continue
            target, _ = self._final(init)
            if target is None or type(target).__name__ != "Function":
                return None
            return self._shape(target, set(target.labels),
                               [_decorator_name(d) for d in target.decorators])
        return None

    def _mine(self, obj: Any) -> dict:
        """The probe's ``_mine``: ``warn()`` prose, one-line forwarding, decorators."""
        try:
            src = obj.source
        except Exception:  # noqa: BLE001
            return {}
        if not src or len(src) > _MAX_SRC:
            return {}
        import textwrap

        try:
            tree = ast.parse(textwrap.dedent(src))
        except SyntaxError:
            return {}
        node = tree.body[0] if tree.body else None
        forwards = None
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            body = [
                s for s in node.body
                if not (isinstance(s, ast.Expr) and (
                    probe_mod._const_str(s.value) is not None
                    or (isinstance(s.value, ast.Call)
                        and (probe_mod._name(s.value.func) or "").endswith("warn"))))
            ]
            if len(body) == 1 and isinstance(body[0], ast.Return) \
                    and isinstance(body[0].value, ast.Call):
                forwards = probe_mod._name(body[0].value.func)
        out: dict[str, Any] = {}
        warns = probe_mod._warn_calls(tree)
        if warns:
            out["warns"] = warns
        if forwards:
            out["forwards_to"] = forwards
        decorators = [probe_mod._name(d) for d in getattr(node, "decorator_list", [])]
        decorators = [d for d in decorators if d]
        if decorators:
            out["decorators"] = decorators
        return out

    def _deprecated_decorator(self, obj: Any) -> str | None:
        for dec in getattr(obj, "decorators", []):
            value = getattr(dec, "value", dec)
            if type(value).__name__ != "ExprCall":
                continue
            if _decorator_name(dec).split(".")[-1] != "deprecated":
                continue
            args = getattr(value, "arguments", [])
            if args:
                try:
                    msg = ast.literal_eval(_expr_text(args[0]))
                except Exception:  # noqa: BLE001
                    continue
                if isinstance(msg, str):
                    return msg[:400]
        return None

    def _describe(self, step: dict, target: Any, ext: str | None, out: _Outcome,
                  dist_roots: set[str]) -> str:
        """Fill ``step`` from the resolved object; return what the next step walks through:
        ``"module" | "class" | "opaque" | "external"``."""
        if target is None:
            if ext and ext.startswith("builtins."):
                built = getattr(builtins, ext[len("builtins."):], None)
                if built is not None:
                    step["kind"] = "class" if isinstance(built, type) else "callable"
            leaf = (ext or "").rsplit(".", 1)[-1]
            inside = bool(ext) and ext.split(".")[0] in dist_roots
            # A name in a compiled module: a class keeps its defining path (``__module__``),
            # a builtin function is named by where it was reached, as the probe names it.
            step["canonical"] = ext if (ext and (not inside or leaf[:1].isupper())) else out.owner
            return "opaque" if inside else "external"
        name = type(target).__name__
        if name == "Module":
            step["kind"] = "module"
            step["canonical"] = target.path
            return "module"
        if name == "Class":
            step["kind"] = "class"
            step["canonical"] = target.path
            step["defined_as"] = target.path
            step["shape"] = self._class_shape(target)
            _bases, names, _ok = self._bases(target)
            if names is not None:
                step["bases"] = names
            decs = [_decorator_name(d) for d in target.decorators]
            step.update(self._mine(target))
            if decs and "decorators" not in step:
                step["decorators"] = decs
            doc = self._doc(target)
            if doc:
                step["doc"] = doc
            return "class"
        if name == "Function":
            step["kind"] = "callable"
            step["canonical"] = target.path
            step["defined_as"] = target.path
            decs = [_decorator_name(d) for d in target.decorators]
            step["shape"] = self._shape(target, set(target.labels), decs)
            step.update(self._mine(target))
            doc = self._doc(target)
            if doc:
                step["doc"] = doc
            return "opaque"
        # Attribute: a plain value; the probe names such a step by where it was accessed
        step["canonical"] = out.owner
        if "property" in getattr(target, "labels", set()):
            step["kind"] = "property"
        else:
            value = getattr(target, "value", None)
            try:
                step["kind"] = type(ast.literal_eval(_expr_text(value))).__name__
            except Exception:  # noqa: BLE001
                pass
        return "opaque"

    def _identity_index(self, root: str) -> dict[str, list[str]]:
        """``canonical path -> public spellings`` over the root's exports and its public
        submodules one level down: the static twin of the probe's same-object search."""
        index = self._identities.get(root)
        if index is not None:
            return index
        index = self._identities[root] = {}
        top = self._member(root)
        if top is None:
            return index
        mods = [top] + [
            m for name, m in sorted(top.members.items())
            if type(m).__name__ == "Module" and not name.startswith("_")
            and name not in _ID_SKIP_SUBMODULES
        ][:_ID_MAX_SUBMODULES]
        for mod in mods:
            for name, member in mod.members.items():
                if name.startswith("_"):
                    continue
                target = self._final(member)[0] if (
                    getattr(member, "is_alias", False) or type(member).__name__ == "Attribute"
                ) else member
                if target is None or type(target).__name__ not in ("Class", "Function"):
                    continue
                index.setdefault(target.path, []).append(f"{mod.path}.{name}")
        return index

    def _same_object_as(self, canonical: str, spelling: str) -> list[str]:
        found = sorted({c for c in self._identity_index(spelling.split(".")[0]).get(canonical, [])
                        if c != spelling},
                       key=lambda c: (any(part.startswith("_") for part in c.split(".")),
                                      c.count("."), c))
        return found[:4]

    def _doc(self, obj: Any) -> dict:
        doc = getattr(getattr(obj, "docstring", None), "value", None)
        if not doc:
            return {}
        return probe_mod._doc_deprecations(SimpleNamespace(__doc__=doc))

    # ── one path ────────────────────────────────────────────────────────────────────

    def resolve(self, path: str) -> dict:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return self._resolve(path)

    def _resolve(self, path: str) -> dict:
        parts = path.split(".")
        root = parts[0]
        for i in range(len(parts), 0, -1):
            kind = self._fs_kind(parts[:i])
            if kind is not None:
                break
        else:
            return {"error": f"static: import root {root} not found in {self.acquired.file}",
                    "unknown": "import root not found", "transient": True, "steps": [],
                    "module": None}
        module_name = ".".join(parts[:i])
        rest = parts[i:]
        out: dict[str, Any] = {"module": module_name, "steps": []}
        mod = None
        why = ""
        if kind in ("package", "module"):
            err = self._ensure_root(root)
            mod = self._member(module_name) if err is None else None
            if mod is None:
                why = err or f"griffe did not load {module_name}"
        else:
            why = (f"{module_name} is a namespace subpackage griffe cannot load"
                   if kind == "namespace" else f"{module_name} is a compiled module")
        if mod is not None:
            rel = self._rel(mod.filepath)
            if rel:
                out["module_file"] = rel
        if not rest:
            out["exists"] = True
            return out
        if mod is None:
            out["steps"].append(self._unknown_step(rest[0], module_name, why))
            out["unknown"] = why
            out["exists"] = None
            return out
        self._walk(mod, module_name, rest, out)
        return out

    def _unknown_step(self, attr: str, owner_module: str, why: str) -> dict:
        return {"attr": attr, "owner": f"{owner_module}.{attr}", "owner_module": owner_module,
                "dep": True, "receiver_path": f"{owner_module}.{attr}", "exists": None,
                "unknown": why}

    def _walk(self, mod: Any, module_name: str, rest: list[str], out: dict) -> None:
        cur: Any = mod
        cur_kind = "module"
        prefix = module_name
        for idx, attr in enumerate(rest):
            if cur_kind == "external":
                out["steps"].append({"attr": attr, "owner": f"{prefix}.{attr}", "dep": False})
                out["exists"] = True
                return
            if cur_kind == "opaque":
                step = self._unknown_step(attr, prefix.rsplit(".", 1)[0] if "." in prefix
                                          else prefix,
                                          f"{prefix} is a value or function; its attributes "
                                          "are not evaluated statically")
                out["steps"].append(step)
                out["unknown"] = step["unknown"]
                out["exists"] = None
                return
            res = (self._lookup_module(cur, attr) if cur_kind == "module"
                   else self._lookup_class(cur, attr))
            if res.status == "unknown":
                owner_module = cur.path if cur_kind == "module" else cur.module.path
                step = self._unknown_step(attr, owner_module, res.why)
                step["receiver_path"] = f"{cur.path}.{attr}"
                out["steps"].append(step)
                out["unknown"] = res.why
                out["exists"] = None
                return
            if res.status == "absent":
                owner_module = cur.path if cur_kind == "module" else cur.module.path
                out["steps"].append({
                    "attr": attr, "owner": f"{cur.path}.{attr}", "owner_module": owner_module,
                    "dep": True, "receiver_path": f"{cur.path}.{attr}", "exists": False,
                    "error": f"static: no {attr} in {cur.path} of {self.acquired.file}",
                })
                out["exists"] = False
                return
            step = {"attr": attr, "owner": res.owner, "owner_module": res.owner_module,
                    "dep": res.owner_module.split(".")[0] in self.roots,
                    "receiver_path": res.receiver, "exists": True}
            if not step["dep"]:
                out["steps"].append({"attr": attr, "owner": res.owner, "dep": False})
                return
            owner_obj = self._member(res.owner_module)
            rel = self._rel(getattr(owner_obj, "filepath", None)) if owner_obj else None
            if rel:
                step["owner_file"] = rel
            if res.submodule is not None:
                step["kind"] = "module"
                step["canonical"] = res.submodule
                target = self._member(res.submodule) if res.compiled_or_namespace in (
                    "package", "module") else None
                if target is not None:
                    cur, cur_kind, prefix = target, "module", res.submodule
                else:
                    cur_kind, prefix = "opaque", res.submodule
            elif res.pep562:
                step["canonical"] = res.owner
                src = self._source_of_module(cur) or ""
                warns = [{"message": m} for m in fetch_mod.module_warnings(src, [attr])]
                if warns:
                    step["getattr_warns"] = warns
                cur_kind, prefix = "opaque", res.owner
            else:
                target, ext = self._final(res.obj)
                cur_kind = self._describe(step, target, ext, res, self.roots)
                if target is not None and type(target).__name__ in ("Class", "Function"):
                    same = self._same_object_as(target.path, res.receiver)
                    if same:
                        step["same_object_as"] = same
                dep_msg = self._deprecated_decorator(target) if target is not None else None
                if dep_msg:
                    step["deprecated_attr"] = dep_msg
                if cur_kind in ("module", "class"):
                    cur = target
                    prefix = target.path
                else:
                    prefix = res.owner if target is None else target.path
                    if (idx < len(rest) - 1 and step.get("kind") is None
                            and cur_kind == "opaque"):
                        # ``six.moves`` is a value here but a module at runtime: say nothing
                        step["exists"] = None
                        step["unknown"] = (f"{res.owner} is a value of unknown kind in the "
                                           "middle of a path")
                        out["steps"].append(step)
                        out["unknown"] = step["unknown"]
                        out["exists"] = None
                        return
            out["steps"].append(step)
        out["exists"] = True


# ── whole request ─────────────────────────────────────────────────────────────────────


def static_probe(
    sources: Mapping[str, Acquired], paths: Iterable[str],
) -> tuple[dict[str, Any], dict[str, dict]]:
    """``(meta, results)`` shaped like a probe's output, for ``{dist: Acquired}`` and paths.

    A path whose root belongs to none of the distributions is skipped, as the probe skips it.
    """
    t0 = time.perf_counter()
    readers: dict[str, StaticReader] = {}
    by_root: dict[str, str] = {}
    dists: dict[str, Any] = {}
    for dist, acq in sources.items():
        readers[dist] = StaticReader(acq, dist)
        dists[dist] = {"version": acq.version, "roots": list(acq.roots)}
        for r in acq.roots:
            by_root.setdefault(r, dist)
    results: dict[str, dict] = {}
    for path in paths:
        dist = by_root.get(path.split(".")[0])
        if dist is None:
            continue
        try:
            results[path] = readers[dist].resolve(path)
        except Exception as exc:  # noqa: BLE001 — one bad path never costs the others
            results[path] = {"error": f"static: {type(exc).__name__}: {str(exc)[:200]}",
                             "unknown": "reader error", "transient": True, "steps": [],
                             "module": None}
    meta = {
        "python": None, "executable": None, "basis": "static",
        "source": "; ".join(sorted(f"{d}: {a.source}" for d, a in sources.items())),
        "site_dirs": [], "dists": dists, "roots": sorted(by_root),
        "static_s": round(time.perf_counter() - t0, 3),
    }
    return meta, results
