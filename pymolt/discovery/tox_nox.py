"""Parse tox.ini / [tool.tox] / noxfile.py for declared Python versions + test deps."""

import ast
import configparser
import logging
import re
import tomllib
from pathlib import Path

from pymolt.discovery.models import ToxNoxSurface

logger = logging.getLogger(__name__)


def _expand_factors(envlist: str) -> list[str]:
    """Split an envlist into env names, expanding one level of ``{a,b}`` factors."""
    items = [e.strip() for e in re.split(r"[,\n]", envlist) if e.strip()]
    expanded: list[str] = []
    for item in items:
        m = re.search(r"\{([^}]*)\}", item)
        if m:
            for choice in m.group(1).split(","):
                expanded.append(item[:m.start()] + choice.strip() + item[m.end():])
        else:
            expanded.append(item)
    return expanded


def _version_from_env(env: str) -> str | None:
    """'py311' -> '3.11', 'py36' -> '3.6', 'py3' -> '3'. PyPy/others -> None."""
    m = re.match(r"py(\d)(\d+)", env)
    if m:
        return f"{m.group(1)}.{m.group(2)}"
    m = re.match(r"py(\d)$", env)
    if m:
        return m.group(1)
    return None


def _parse_tox_ini_text(text: str, path: str) -> ToxNoxSurface:
    surface = ToxNoxSurface(path=path, kind="tox")
    parser = configparser.ConfigParser()
    try:
        parser.read_string(text)
    except configparser.Error as e:
        surface.notes.append(f"could not parse tox config: {e}")
        return surface

    envlist = ""
    if parser.has_option("tox", "envlist"):
        envlist = parser.get("tox", "envlist")
    versions: list[str] = []
    for env in _expand_factors(envlist):
        v = _version_from_env(env)
        if v and v not in versions:
            versions.append(v)
        elif env.startswith("pypy"):
            surface.notes.append(f"non-CPython env '{env}'")
    surface.declared_versions = versions

    # Best-effort test deps from [testenv]* sections (strip factor prefixes).
    for section in parser.sections():
        if section == "testenv" or section.startswith("testenv:"):
            if parser.has_option(section, "deps"):
                for line in parser.get(section, "deps").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    line = re.sub(r"^[\w.,{}-]+:\s*", "", line)  # drop 'py36:' factor guard
                    if line and not line.startswith("-r"):
                        surface.extra_deps.append(line)
    return surface


def parse_tox_ini(path: Path) -> ToxNoxSurface:
    try:
        return _parse_tox_ini_text(path.read_text(encoding="utf-8"), path.name)
    except OSError as e:
        logger.warning("Could not read %s: %s", path, e)
        return ToxNoxSurface(path=path.name, kind="tox", notes=[str(e)])


def parse_tox_pyproject(path: Path) -> ToxNoxSurface | None:
    """Parse a tox 4 ``[tool.tox]`` table (native ``env_list`` or ``legacy_tox_ini``)."""
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    tox = data.get("tool", {}).get("tox")
    if not tox:
        return None
    if "legacy_tox_ini" in tox:
        return _parse_tox_ini_text(tox["legacy_tox_ini"], path.name)
    surface = ToxNoxSurface(path=path.name, kind="tox")
    env_list = tox.get("env_list") or tox.get("envlist") or []
    if isinstance(env_list, str):
        env_list = _expand_factors(env_list)
    for env in env_list:
        v = _version_from_env(str(env))
        if v and v not in surface.declared_versions:
            surface.declared_versions.append(v)
    return surface


def parse_noxfile(path: Path) -> ToxNoxSurface:
    surface = ToxNoxSurface(path=path.name, kind="nox")
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError) as e:
        surface.notes.append(f"could not parse noxfile: {e}")
        return surface

    versions: list[str] = []
    dynamic = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_session = (isinstance(func, ast.Attribute) and func.attr == "session") or \
                     (isinstance(func, ast.Name) and func.id == "session")
        if not is_session:
            continue
        for kw in node.keywords:
            if kw.arg != "python":
                continue
            value = kw.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                versions.append(value.value)
            elif isinstance(value, (ast.List, ast.Tuple)):
                for elt in value.elts:
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                        versions.append(elt.value)
                    else:
                        dynamic = True
            else:
                dynamic = True

    surface.declared_versions = list(dict.fromkeys(versions))
    if dynamic:
        surface.notes.append("noxfile sets some session python values dynamically — partial")
    return surface
