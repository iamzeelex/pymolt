"""Version comparisons that follow PEP 440's normalisation (``3.2`` is ``3.2.0``)."""

from __future__ import annotations

from packaging.markers import InvalidMarker, UndefinedComparison, UndefinedEnvironmentName
from packaging.requirements import InvalidRequirement, Requirement
from packaging.version import InvalidVersion, Version


def same_version(a: str | None, b: str | None) -> bool:
    """Whether ``a`` and ``b`` name the same release; plain text equality when either is
    not a PEP 440 version. None is never the same as anything."""
    if a is None or b is None:
        return False
    try:
        return Version(a) == Version(b)
    except InvalidVersion:
        return a.strip() == b.strip()


def marker_excludes(spec: str | None, python: str | None) -> bool:
    """Whether the environment marker of requirement ``spec`` is false on CPython
    ``python`` (``pyOpenSSL; python_full_version < "2.7.9"`` on 3.12): the requirement
    does not apply there. False when there is no marker or it cannot be evaluated."""
    if not spec or not python:
        return False
    try:
        marker = Requirement(spec).marker
        if marker is None:
            return False
        release = Version(python).release
        env = {"python_version": ".".join(str(p) for p in release[:2]),
               "python_full_version": ".".join(str(p) for p in (*release, 0, 0)[:3]),
               "implementation_name": "cpython", "platform_python_implementation": "CPython"}
        return not marker.evaluate(env)
    except (InvalidRequirement, InvalidVersion, InvalidMarker, UndefinedComparison,
            UndefinedEnvironmentName, TypeError, ValueError):
        return False
