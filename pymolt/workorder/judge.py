"""Each step's judgement from one verify (design 5.3, 5.4).

HOST side (3.12+). ``verify`` builds the whole report as always; this module then reads, per
step in effect, the part of it that is the step's: the fact entries on the lines the ledger
assigns to it (``FactEntry.step_id``, from ``workorder.coverage``), the facts at its ``where``
anchors resolved on the baseline's source (and their pairs), and the predicted risks linked to
it. A step's judgement, first match:

1. ``not_in_capture``: its new statements do not resolve in the post-migration capture;
2. ``failed``: an entry of it fails;
3. ``needs_review``: an entry of it is a review item (or not comparable);
4. ``needs_evidence``: one of its sites has no compared fact (not exercised), or an edited site
   of it could not be paired (unverified);
5. ``verified``: every site compared and judged by its claim (an ACCEPTED entry counts as
   compared equal).

Only a verdict on the baseline and post-migration captures judges steps (5.4), and only steps
in effect with sites: an env step (or one with no site and no entry) takes no verify status.
``verification.json`` keeps every judgement under ``steps``, with the run it is for
(``steps_run``); ``project_steps`` applies one only for the step's current epoch.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

#: Exit codes of ``verify --step`` by judgement.
EXIT = {"verified": 0, "failed": 1, "needs_review": 4, "needs_evidence": 4,
        "not_in_capture": 4}


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


def _label(e) -> str:
    site = e.old_site or e.new_site
    where = f"{site.file}:{site.line}" if site is not None else "?"
    return f"{e.finding or e.category.value} {e.symbol} @ {where}"


def _where_entries(root: Path, step, entries, old_sources, python) -> tuple[list, list[str]]:
    """The entries at the step's ``where`` anchors (resolved on the baseline's source), and the
    anchors no compared fact reached."""
    from pymolt.workorder.edits import build_index

    found: list = []
    missing: list[str] = []
    indexes: dict[str, Any] = {}
    for anchor in step.where:
        text = (old_sources or {}).get(anchor.file)
        if anchor.file not in indexes:
            indexes[anchor.file] = build_index(text, python) if text is not None else None
        index = indexes[anchor.file]
        res = index.resolve(anchor) if index is not None else None
        if res is None or not res.ok:
            missing.append(f"{anchor.file}: `{anchor.stmt[:60]}` (not in the baseline's source)")
            continue
        st = res.statement
        mine = [e for e in entries if e.old_site is not None
                and _rel(root, e.old_site.file) == anchor.file and e.old_site.line is not None
                and st.first <= e.old_site.line <= max(st.last, st.end)]
        if not any(e.comparison.value in ("equal", "changed") or e.accepted for e in mine):
            missing.append(f"{anchor.file}:{st.first} `{anchor.stmt[:60]}` (no compared fact)")
        found.extend(mine)
    return found, missing


def judge_steps(project_dir: str | Path, report, calibration=None
                ) -> tuple[str | None, dict[str, dict]]:
    """``(run id, step id -> judgement)`` for the saved plan's steps in effect (module
    docstring); ``(None, {})`` when the report did not compare the two captures."""
    from pymolt.migration_plan import MigrationPlan, _pythons
    from pymolt.workorder.state import applicable_steps, project_steps

    compared = getattr(report, "compared", None)
    fdiff = getattr(report, "_fact_diff", None)
    if (compared is None or compared.after_capture != "post-migration"
            or compared.before_capture != "baseline" or fdiff is None):
        return None, {}
    root = Path(project_dir).resolve()
    plan = MigrationPlan.load(root)
    if plan is None or not plan.steps:
        return None, {}
    projection = project_steps(root, plan)
    ctx = getattr(report, "_accepts", None)
    old_sources = getattr(ctx, "old_sources", None)
    base, _target = _pythons(root, plan)
    by_id = {s.id: s for s in [*plan.steps, *applicable_steps(plan)]}
    risks: dict[str, list] = {}
    for r in (calibration.risks if calibration is not None else ()):
        if r.step_id:
            risks.setdefault(r.step_id, []).append(
                {"id": r.id, "result": r.result.value, "fix": r.fix})
    out: dict[str, dict] = {}
    for state in projection.steps:
        step = by_id.get(state.id)
        if step is None or state.epoch is None or step.phase == "env":
            continue
        base_judgement = {"epoch": state.epoch, "risks": risks.get(step.id, [])}
        if state.status == "not_in_capture":
            out[step.id] = {**base_judgement, "status": "not_in_capture", "entries": 0,
                            "failing": [], "review": [], "unexercised": []}
            continue
        mine = [e for e in fdiff.entries if e.step_id == step.id
                and e.step_run in (None, projection.run_id)]
        at_where, missing = _where_entries(root, step, fdiff.entries, old_sources, base)
        seen = {id(e) for e in mine}
        mine += [e for e in at_where if id(e) not in seen]
        if not mine and not step.where and not step.edits:
            continue                                  # no site: no verify status
        failing = [_label(e) for e in mine if e.fails]
        review = [_label(e) for e in mine if not e.fails and (e.review or e.inconclusive)]
        unexercised = list(missing) + [_label(e) for e in mine if e.unverified]
        if not mine and step.edits:
            unexercised.append("none of its statements was exercised by the capture")
        status = ("failed" if failing else "needs_review" if review else
                  "needs_evidence" if unexercised else "verified")
        out[step.id] = {**base_judgement, "status": status, "entries": len(mine),
                        "failing": failing[:20], "review": review[:20],
                        "unexercised": unexercised[:20],
                        "accepted": sum(1 for e in mine if e.accepted)}
    return projection.run_id, out


__all__ = ["EXIT", "judge_steps"]
