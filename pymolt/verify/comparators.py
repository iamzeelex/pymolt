"""Comparator profiles for before/after boundary recordings.

The built-in profiles cover the two useful defaults: exact normalized values
and privacy-preserving type/structure shapes.  A custom profile is an explicit
Python extension point for domain invariants that cannot be expressed by either
default.  Loading custom code is never implicit: the caller must name a
``module:callable`` and therefore opts into executing that module.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from enum import StrEnum

from pymolt.verify.contract import build_contract_diff
from pymolt.verify.diff import build_boundary_diff
from pymolt.verify.models import BoundaryDiff


class ComparatorProfile(StrEnum):
    EXACT = "exact"
    SHAPE = "shape"
    CUSTOM = "custom"


class ComparatorError(ValueError):
    """A comparator profile or custom comparator is unusable."""


TraceComparator = Callable[[str, str], BoundaryDiff | dict]


def load_custom_comparator(spec: str) -> TraceComparator:
    """Resolve an explicitly requested ``module:callable`` comparator."""
    module_name, separator, attribute = spec.partition(":")
    if not separator or not module_name or not attribute:
        raise ComparatorError(
            "custom comparator must be written as 'module:callable'"
        )
    try:
        module = importlib.import_module(module_name)
        comparator = getattr(module, attribute)
    except (ImportError, AttributeError) as exc:
        raise ComparatorError(f"cannot load custom comparator {spec!r}: {exc}") from exc
    if not callable(comparator):
        raise ComparatorError(f"custom comparator {spec!r} is not callable")
    return comparator


def compare_traces(
    old_path: str,
    new_path: str,
    *,
    profile: ComparatorProfile | str = ComparatorProfile.EXACT,
    custom: TraceComparator | str | None = None,
) -> BoundaryDiff:
    """Compare two traces using an exact, shape, or explicit custom profile.

    A custom comparator receives the two artifact paths and returns either a
    :class:`BoundaryDiff` or a dictionary validating as one.  This deliberately
    keeps domain-specific tolerances outside pymolt's trusted core while
    preserving the same verdict and rendering contract.
    """
    try:
        selected = ComparatorProfile(profile)
    except ValueError as exc:
        choices = ", ".join(item.value for item in ComparatorProfile)
        raise ComparatorError(
            f"unknown comparator profile {profile!r}; choose {choices}"
        ) from exc

    if selected is ComparatorProfile.EXACT:
        return build_boundary_diff(old_path, new_path)
    if selected is ComparatorProfile.SHAPE:
        return build_contract_diff(old_path, new_path)
    if custom is None:
        raise ComparatorError(
            "the custom profile requires a 'module:callable' comparator"
        )
    comparator = load_custom_comparator(custom) if isinstance(custom, str) else custom
    try:
        result = comparator(old_path, new_path)
        return result if isinstance(result, BoundaryDiff) else BoundaryDiff.model_validate(result)
    except ComparatorError:
        raise
    except Exception as exc:
        raise ComparatorError(f"custom comparator failed: {exc}") from exc
