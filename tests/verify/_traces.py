"""Test helper: diff two trace files (load each with its own sidecar site_text, then
``diff_facts``)."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from pymolt.verify.facts import DiffContext, DiffProfile, FactDiff, diff_facts, load_facts


def diff_traces(
    old_path: str | Path,
    new_path: str | Path,
    *,
    root: str | Path | None = None,
    old_root: str | Path | None = None,
    new_root: str | Path | None = None,
    profile: DiffProfile | str = DiffProfile.EXACT,
    context: DiffContext | None = None,
    order_causes: Sequence = (),
) -> FactDiff:
    """``old_root``/``new_root`` default to ``root``; pass them separately when the two
    captures ran from different checkouts (or a container and the host)."""
    old = load_facts(old_path, root=old_root if old_root is not None else root)
    new = load_facts(new_path, root=new_root if new_root is not None else root)
    return diff_facts(old, new, profile=profile, context=context, order_causes=order_causes)
