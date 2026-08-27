"""
axiom_graph — Release-Sequenced API Delta Engine

Standalone research service. Computes the chronological API delta between
two arbitrary versions of a Python package via:
  - Release sequencing (PyPI API + git tags)
  - Griffe structural API diff (Layer 1: fast filter)
  - SSA / value-flow analysis of changed functions (Layer 2: deep)
  - AST deprecation mining
  - Test-suite diff mining
  - Signal fusion → Active → Deprecated → Removed state machine

Usage:
    from axiom_graph import compute_full_delta
    delta = compute_full_delta("pandas", "1.3.5", "2.0.0")
"""

from axiom_graph.core.models import (
    ApiState,
    BreakingChange,
    ChangeRisk,
    FullDelta,
    PairwiseDelta,
)

__all__ = [
    "compute_full_delta",
    "FullDelta",
    "PairwiseDelta",
    "BreakingChange",
    "ApiState",
    "ChangeRisk",
]

__version__ = "0.1.0"


def __getattr__(name: str):
    """Lazy import for heavy entry points (avoids griffe on unit-test import)."""
    if name == "compute_full_delta":
        from axiom_graph.core.pipeline import compute_full_delta  # noqa: PLC0415
        return compute_full_delta
    raise AttributeError(f"module 'axiom_graph' has no attribute {name!r}")
