"""Detect the frameworks a project declares, to query /succession.

Returns the project's DIRECT dependency names (from its manifest). We send the full
direct set and let the server match the ones it has succession knowledge for — the
catalog authority stays server-side, so pymolt needs no hardcoded framework list.
"""

from __future__ import annotations

from pathlib import Path

from pymolt.ingestion.detect import detect_sources, extract_declared_requirements


def detect_frameworks(project_dir: str | Path) -> list[str]:
    """Direct dependency names declared by the project (sorted, de-duplicated)."""
    names: set[str] = set()
    for source in detect_sources(project_dir):
        try:
            names.update(extract_declared_requirements(source.path))
        except Exception:  # a manifest we can't parse must not sink detection
            continue
    return sorted(names)


# Declared dependencies that are frameworks or framework-like tooling, as opposed to
# libraries (numpy, six, scipy...): the ones whose successor is a real question.
# Anything the curated knowledge indexes counts too (see ``plausible_frameworks``).
_FRAMEWORK_NAMES = frozenset({
    "aesara", "bottle", "caffe", "cherrypy", "chainer", "cntk", "django", "falcon", "flask",
    "jax", "keras", "lasagne", "mxnet", "nose", "nose2", "pylons", "pyramid", "pytensor",
    "pytorch", "sanic", "tensorflow", "theano", "torch", "tornado", "twisted", "web2py",
})


def _base_name(requirement: str) -> str:
    base = str(requirement).strip().lower()
    for sep in ("<", ">", "=", "~", "!", "[", ";", " "):
        base = base.split(sep)[0]
    return base.replace("_", "-")


def knowledge_coverage() -> list[str]:
    """The frameworks pymolt's curated succession knowledge has an entry for (sorted)."""
    from pymolt.knowledge import _load

    index = _load("succession").get("index") or {}
    return sorted({_base_name(key) for key in index})


def plausible_frameworks(declared: list[str]) -> list[str]:
    """The declared dependencies that are plausible frameworks (sorted, de-duplicated)."""
    known = _FRAMEWORK_NAMES | set(knowledge_coverage())
    return sorted({name for name in declared if _base_name(name) in known})
