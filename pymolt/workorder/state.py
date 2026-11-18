"""Each step's status, projected from the ledger (design 1.3).

HOST side, read-only. The plan file is immutable: a step's status there is always ``planned``.
Its live status comes from the ledger of the run that applies the plan: a current epoch
(``applied`` by an ``apply`` event, ``recorded`` by a ``record``), an ended one (``rolled_back``,
``skipped``), or none; and from its dependencies (``blocked`` while one is not in effect).

``applicable_steps`` names the steps ``pymolt apply`` writes: the plan's pymolt code steps when
their hunks rebuild every changed file from ``changes[].old_source`` to ``new_source``, else the
plan's one whole-file step (``v1``).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from pymolt.workorder.models import PlanStep

#: Step types ``pymolt apply`` writes.
CODE_TYPES = ("codemod", "python-fix")


def applicable_steps(plan) -> list[PlanStep]:
    """The steps ``apply`` writes, in plan order (see the module docstring)."""
    from pymolt.migration_plan import _ledger_steps

    return [step for step, _files in _ledger_steps(plan)]


@dataclass
class RunView:
    """What the ledger says about one run's steps. ``current``: step id -> the seq of its
    current epoch; ``last``: step id -> what the ledger last says of it (``applied``,
    ``recorded``, ``rolled_back``, ``skipped``); ``kinds``: seq -> event kind."""

    run_id: str | None
    current: dict[str, int] = field(default_factory=dict)
    last: dict[str, str] = field(default_factory=dict)
    kinds: dict[int, str] = field(default_factory=dict)

    @classmethod
    def of(cls, ledger: Any, run_id: str | None) -> RunView:
        view = cls(run_id)
        if ledger is None or run_id is None:
            return view
        view.current = {sid: seq for (run, sid), seq in ledger.current_epochs().items()
                        if run == run_id}
        view.last = {sid: state for (run, sid), state in ledger.step_states().items()
                     if run == run_id}
        view.kinds = {e.seq: e.event for e in ledger.events}
        return view

    def in_effect(self, step_id: str) -> bool:
        return step_id in self.current

    def status(self, step_id: str) -> str | None:
        """``applied`` / ``recorded`` (a current epoch), ``rolled_back``, ``skipped``, or None
        (the ledger never saw the step)."""
        seq = self.current.get(step_id)
        if seq is not None:
            return "recorded" if self.kinds.get(seq) == "record" else "applied"
        return self.last.get(step_id)


def basic_statuses(steps: Iterable[PlanStep], view: RunView,
                   extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """``step id -> status`` from the ledger alone: its ledger status, else ``blocked`` (a
    dependency not in effect) or ``planned``. ``extra``: statuses the caller knows better
    (``present``, ``stale``)."""
    out: dict[str, str] = {}
    for step in steps:
        known = (extra or {}).get(step.id) or view.status(step.id)
        if known is not None:
            out[step.id] = known
        elif any(not view.in_effect(dep) for dep in step.depends_on):
            out[step.id] = "blocked"
        else:
            out[step.id] = "planned"
    return out


def remaining(steps: Iterable[PlanStep], statuses: Mapping[str, str]) -> dict[str, list[str]]:
    """The steps not in effect, by status, in plan order."""
    out: dict[str, list[str]] = {}
    for step in steps:
        status = statuses.get(step.id, "planned")
        if status in ("applied", "recorded"):
            continue
        out.setdefault(status, []).append(step.id)
    return out


__all__ = ["CODE_TYPES", "RunView", "applicable_steps", "basic_statuses", "remaining"]
