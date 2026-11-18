"""Read ``install_requires`` / ``extras_require`` out of ``setup.py`` and ``setup.cfg``.

One extractor for every consumer (scan's edge inventory, ingestion's declared
requirements). ``setup.py`` is *parsed*, never executed: a literal list, a
module-level name bound to a literal list, and a ``+`` of those are read; a
file read, a function call or a comprehension is *computed*, and is reported
as a note instead of being guessed (honesty over silent best-effort).
"""

from __future__ import annotations

import ast
import configparser
from dataclasses import dataclass, field
from pathlib import Path

#: ``setup()`` keywords read, and the group a bare ``install_requires`` belongs to.
MAIN_GROUP = "main"

_MAX_DEPTH = 8


@dataclass
class SetupRequirement:
    raw: str
    group: str = MAIN_GROUP        # "main" or the extra's name
    marker: str | None = None      # from an ``extra:marker`` key


@dataclass
class SetupDeps:
    requirements: list[SetupRequirement] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def specs(self) -> list[str]:
        return [r.raw for r in self.requirements]


class _Computed(Exception):
    """The value is not a literal pymolt may read without running the file."""


def _split_extra_key(key: str) -> tuple[str, str | None]:
    """``'fast'`` -> (fast, None); ``':python_version < "3"'`` -> (main, marker)."""
    extra, sep, marker = key.partition(":")
    extra, marker = extra.strip(), marker.strip()
    return (extra or MAIN_GROUP), ((marker or None) if sep else None)


def _clean(lines: list[str]) -> list[str]:
    out = []
    for line in lines:
        line = line.split(" #", 1)[0].strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


# ── setup.py ────────────────────────────────────────────────────────────────

def _module_bindings(tree: ast.Module) -> dict[str, ast.expr]:
    """Module-level ``name = <expr>`` (last binding wins); ``name += <expr>`` makes it computed."""
    env: dict[str, ast.expr] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    env[target.id] = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value:
            env[node.target.id] = node.value
        elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
            env[node.target.id] = ast.Call(func=ast.Name(id="__augmented__", ctx=ast.Load()),
                                           args=[], keywords=[])
    return env


def _strings(node: ast.expr, env: dict[str, ast.expr], depth: int = 0) -> list[str]:
    """A list/tuple/str of requirement strings, or raise :class:`_Computed`."""
    if depth > _MAX_DEPTH:
        raise _Computed
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return _clean(node.value.splitlines())
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        out: list[str] = []
        for el in node.elts:
            if isinstance(el, ast.Constant) and isinstance(el.value, str):
                out.extend(_clean([el.value]))
            else:
                raise _Computed
        return out
    if isinstance(node, ast.Name) and node.id in env:
        return _strings(env[node.id], env, depth + 1)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _strings(node.left, env, depth + 1) + _strings(node.right, env, depth + 1)
    raise _Computed


def _extras(node: ast.expr, env: dict[str, ast.expr], depth: int = 0) -> dict[str, list[str]]:
    if depth > _MAX_DEPTH:
        raise _Computed
    if isinstance(node, ast.Name) and node.id in env:
        return _extras(env[node.id], env, depth + 1)
    if not isinstance(node, ast.Dict):
        raise _Computed
    out: dict[str, list[str]] = {}
    for key, value in zip(node.keys, node.values):
        if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
            raise _Computed        # ``**spread`` (key None) or a computed key
        out.setdefault(key.value, []).extend(_strings(value, env, depth + 1))
    return out


def _setup_calls(tree: ast.Module) -> list[ast.Call]:
    calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name == "setup":
                calls.append(node)
    return calls


def parse_setup_py(path: str | Path) -> SetupDeps:
    deps = SetupDeps()
    try:
        tree = ast.parse(Path(path).read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError, ValueError) as e:
        deps.notes.append(f"{Path(path).name} could not be parsed ({e.__class__.__name__}); "
                          "install_requires not read")
        return deps
    env = _module_bindings(tree)
    calls = _setup_calls(tree)
    if not calls:
        return deps
    # The call carrying the most keywords is the real one (helpers named `setup` exist).
    call = max(calls, key=lambda c: len(c.keywords))
    for kw in call.keywords:
        if kw.arg == "install_requires":
            try:
                for spec in _strings(kw.value, env):
                    deps.requirements.append(SetupRequirement(spec))
            except _Computed:
                deps.notes.append("computed install_requires not read")
        elif kw.arg == "extras_require":
            try:
                for key, specs in _extras(kw.value, env).items():
                    group, marker = _split_extra_key(key)
                    deps.requirements.extend(SetupRequirement(s, group, marker) for s in specs)
            except _Computed:
                deps.notes.append("computed extras_require not read")
    return deps


# ── setup.cfg ───────────────────────────────────────────────────────────────

def _cfg_lines(value: str) -> list[str]:
    return _clean(value.splitlines())


def parse_setup_cfg(path: str | Path) -> SetupDeps:
    deps = SetupDeps()
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str  # extras names and markers are case-sensitive
    try:
        parser.read(path, encoding="utf-8")
    except (OSError, configparser.Error, UnicodeDecodeError) as e:
        deps.notes.append(f"{Path(path).name} could not be parsed ({e.__class__.__name__})")
        return deps

    def read(section: str, option: str, label: str, group: str, marker: str | None = None) -> None:
        value = parser.get(section, option, fallback="").strip()
        if value.startswith(("file:", "attr:")):
            deps.notes.append(f"computed {label} not read")
            return
        deps.requirements.extend(SetupRequirement(s, group, marker) for s in _cfg_lines(value))

    if parser.has_section("options"):
        for opt in ("install_requires", "install-requires"):
            if parser.has_option("options", opt):
                read("options", opt, "install_requires", MAIN_GROUP)
    if parser.has_section("options.extras_require"):
        for key in parser.options("options.extras_require"):
            group, marker = _split_extra_key(key)
            read("options.extras_require", key, "extras_require", group, marker)
    return deps


def parse_setup_file(path: str | Path) -> SetupDeps:
    """Dispatch on the file name (``setup.py`` or ``setup.cfg``)."""
    if Path(path).name.lower() == "setup.cfg":
        return parse_setup_cfg(path)
    return parse_setup_py(path)
