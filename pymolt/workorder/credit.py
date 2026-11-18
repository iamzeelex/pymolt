"""Which predicted risks a step fixed, judged on the post-migration code (design 5.5).

HOST side (3.12+). A risk is credited to a step only at verify time, by statement, when:

1. the risk is linked to the step: the step receipt's persisted ``risk_ids`` (2.4), or, for a
   step not applied yet, the plan's ``PredictedRisk.step_id``;
2. the step is in effect (a current epoch, its run on disk);
3. in the post-migration snapshot P, the step's effect removed or replaced the risk's
   statement: the risk's statement (anchored on the baseline snapshot B, at its site) no longer
   resolves in P, and one of the step's new statements in that file does (or the step only
   deleted statements there). For a symbol an import binds, replacing the binding import counts
   too (H4's import move): every import of B that names the symbol is gone from P, and an
   import the step wrote resolves there.

Then ``fix`` is ``fixed``. A linked step in effect whose effect did not touch the risk's
statement in P is ``recorded-not-fixing``: scored as unfixed. A linked step not in effect
(planned, skipped, rolled back) is ``unfixed``. A risk with no link has no fix.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class StepLink:
    run_id: str | None
    step_id: str
    in_effect: bool
    fix: str            # "fixed" | "recorded-not-fixing" | "unfixed"


def _index(text: str | None, python: Any):
    from pymolt.workorder.edits import build_index

    return build_index(text, python) if text is not None else None


def _rel(root: Path, file: str | None) -> str | None:
    if not file:
        return None
    path = Path(file)
    if path.is_absolute():
        try:
            return path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            return None
    return path.as_posix()


class _Sources:
    """B and P, indexed once per file."""

    def __init__(self, old: dict | None, new: dict | None, python_old, python_new) -> None:
        self.old, self.new = old or {}, new or {}
        self.python_old, self.python_new = python_old, python_new
        self._cache: dict[tuple[int, str], Any] = {}

    def index(self, side: int, file: str):
        key = (side, file)
        if key not in self._cache:
            text = (self.old if side == 0 else self.new).get(file)
            self._cache[key] = _index(text, self.python_old if side == 0 else self.python_new)
        return self._cache[key]


def _new_anchors(step, ledger, run_id: str, file: str) -> tuple[list, bool]:
    """``(the step's new statements in file, whether it deleted any there)``: a recorded
    step's from its current epoch's entries, a pymolt step's from its plan edit."""
    event = ledger.epoch_event(run_id, step.id) if ledger is not None else None
    if event is not None and event.event == "record":
        mine = [e for e in ledger.entries if e.epoch == event.seq and e.step_id == step.id
                and e.file == file]
        return ([e.anchor_new for e in mine if e.anchor_new is not None],
                any(e.anchor_old is not None and e.anchor_new is None for e in mine))
    edit = step.edit_for(file) if step is not None else None
    if edit is None:
        return [], False
    return list(edit.anchors_new), bool(edit.effect.delete)


def _import_rebound(risk, file: str, src: _Sources, news: list) -> bool:
    """H4: every import of B naming the risk's symbol is gone from P, and an import the step
    wrote resolves there."""
    old_idx, new_idx = src.index(0, file), src.index(1, file)
    if old_idx is None or new_idx is None:
        return False
    symbol = str(risk.symbol or "")
    leaf, module = symbol.rsplit(".", 1)[-1], symbol.rsplit(".", 1)[0]
    binders = [st for st in old_idx.statements if st.is_import
               and (leaf in st.text or (module and module in st.text))]
    if not binders:
        return False
    for st in binders:
        if new_idx.resolve(old_idx.anchor(st, file=file)).ok:
            return False
    return any(a.scope == "<module>" and new_idx.resolve(a).ok for a in news
               if "import" in (a.stmt or ""))


def _statement_fixed(root: Path, risk, step, ledger, run_id: str, src: _Sources,
                     where: list) -> bool:
    file = _rel(root, risk.site.file)
    if file is None:
        return False
    old_idx, new_idx = src.index(0, file), src.index(1, file)
    if old_idx is None or new_idx is None:
        return False
    anchor = None
    if risk.site.line:
        anchor = old_idx.anchor_at(risk.site.line, file=file)
    if anchor is None:                  # a static site: the plan's anchor for it
        anchor = next((a for a in where if a.file == file and (
            (risk.site.text and a.site_text == risk.site.text) or a.func == risk.site.func)),
            None)
    if anchor is None:
        return False
    news, deleted = _new_anchors(step, ledger, run_id, file)
    if new_idx.resolve(anchor).ok:
        return _import_rebound(risk, file, src, news)
    same = [a for a in news if a.scope == anchor.scope] or news
    if any(new_idx.resolve(a).ok for a in same):
        return True
    return deleted and not news


def step_links(root: str | Path, risks, *, old_sources, new_sources, python_old=None,
               python_new=None) -> dict[str, StepLink]:
    """``risk id -> StepLink`` for every risk linked to a step (module docstring)."""
    from pymolt.apply_ledger import chain_runs, load_ledger
    from pymolt.migration_plan import MigrationPlan, _plan_runs
    from pymolt.migration_state import MigrationReceipt, plans_in_effect

    root = Path(root).resolve()
    receipt = MigrationReceipt.load(root)
    ledger = load_ledger(root, receipt=receipt)
    chain = chain_runs(receipt)
    current = ledger.current_epochs() if ledger is not None else {}
    plans = {}
    for plan in [*plans_in_effect(root, receipt), MigrationPlan.load(root)]:
        if plan is not None:
            runs = _plan_runs(root, plan.plan_id)
            plans.setdefault(runs[-1].run_id if runs else plan.plan_id, plan)
    # The persisted links first (latest run first), then the saved plan's own.
    linked: dict[str, tuple[str | None, str]] = {}
    if receipt is not None:
        for key in reversed(list(receipt.steps)):
            run, _sep, sid = key.partition(":")
            for rid in receipt.steps[key].risk_ids:
                linked.setdefault(rid, (run, sid))
    for run, plan in plans.items():
        for risk in plan.risks.risks if plan.risks is not None else ():
            if risk.step_id:
                linked.setdefault(risk.id, (run, risk.step_id))
    src = _Sources(old_sources, new_sources, python_old, python_new)
    out: dict[str, StepLink] = {}
    for risk in risks:
        ref = linked.get(risk.id)
        if ref is None:
            continue
        run, sid = ref
        live = (run, sid) in current and (not chain or run in chain)
        if not live:
            out[risk.id] = StepLink(run, sid, False, "unfixed")
            continue
        plan = plans.get(run)
        step = plan.step(sid) if plan is not None else None
        if step is None and plan is not None:
            from pymolt.workorder.state import applicable_steps

            step = next((s for s in applicable_steps(plan) if s.id == sid), None)
        where = list(step.where) if step is not None else []
        fixed = step is not None and _statement_fixed(root, risk, step, ledger, run, src, where)
        out[risk.id] = StepLink(run, sid, True, "fixed" if fixed else "recorded-not-fixing")
    return out


__all__ = ["StepLink", "step_links"]
