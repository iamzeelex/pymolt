"""Compatibility shim: the Python 2 stdlib table now lives in :mod:`pymolt.core.stdlib`."""

from __future__ import annotations

from pymolt.core.stdlib import PY2_STDLIB, describe, is_py2_stdlib, py3_replacement

__all__ = ["PY2_STDLIB", "describe", "is_py2_stdlib", "py3_replacement"]
