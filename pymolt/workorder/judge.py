"""Each step's judgement from one verify (design 5.3, 5.4).

HOST side (3.12+). ``verify`` builds the whole report as always; this module then reads, per
step in effect, the part of it that is the step's: the fact entries on the lines the ledger
assigns to it (``FactEntry.step_id``, from ``workorder.coverage``), the facts at its ``where``
anchors resolved on the baseline's source (and their pairs), and the predicted risks linked to
it. A step's judgement, first match:

1. ``not_in_capture``: its new statements do not resolve in the post-migration capture;
2. ``failed``: an entry of it fails;
3. ``needs_review``: an entry of it is a review item a person must look at (``review``);
4. ``needs_evidence`` (design decision 7): one of its sites has no compared fact (not
   exercised), or an edited site of it could not be paired (unverified) (``unexercised``); an
   entry of it could not be compared (``uncomparable``); or a difference at it may be run-to-run
   noise no baseline replicate measured yet, or a difference of the two captures' environments
   (``noise``). ``evidence`` names the first of noise, environment, not exercised, uncomparable
   it has, and ``settle`` what settles it;
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


def _evidence(noise: list, unexercised: list, uncomparable: list) -> tuple[str, str] | None:
    """``(cause, what settles it)`` for a step that needs evidence: noise a baseline replicate
    settles first (the cheapest, and it can turn the rest), then an environment difference, a
    site not exercised, a fact that could not be compared. ``noise``: (entry, cause) pairs."""
    from pymolt.verify.inconclusive import (
        SETTLE_ENVIRONMENT,
        SETTLE_NOISE,
        settle_uncomparable,
    )

    if any(why == "noise" for _e, why in noise):
        return "noise", SETTLE_NOISE
    if noise:
        return "environment", SETTLE_ENVIRONMENT
    if unexercised:
        return "not-exercised", ("add a test that reaches its sites, then capture "
                                 "post-migration again")
    if uncomparable:
        return "uncomparable", settle_uncomparable(uncomparable)
    return None


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
        # A fact that reached it but is a review item or could not be compared is judged as
        # such (``review``, ``uncomparable``), not as a site no run reached.
        if not any(e.comparison.value in ("equal", "changed") or e.accepted or e.inconclusive
                   for e in mine):
            missing.append(f"{anchor.file}:{st.first} `{anchor.stmt[:60]}` (no compared fact)")
        found.extend(mine)
    return found, missing


def judge_steps(project_dir: str | Path, report, calibration=None
                ) -> tuple[str | None, dict[str, dict]]:
    """``(run id, step id -> judgement)`` for the saved plan's steps in effect (module
    docstring); ``(None, {})`` when the report did not compare the two captures."""
    from pymolt.migration_plan import MigrationPlan, _pythons
    from pymolt.verify.inconclusive import NEEDS_REVIEW, entry_cause
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
    measured = bool(getattr(fdiff, "noise_measured", None))
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
                            "failing": [], "review": [], "unexercised": [],
                            "uncomparable": [], "noise": []}
            continue
        mine = [e for e in fdiff.entries if e.step_id == step.id
                and e.step_run in (None, projection.run_id)]
        at_where, missing = _where_entries(root, step, fdiff.entries, old_sources, base)
        seen = {id(e) for e in mine}
        mine += [e for e in at_where if id(e) not in seen]
        if not mine and not step.where and not step.edits:
            continue                                  # no site: no verify status
        failing = [_label(e) for e in mine if e.fails]
        # Each entry that is neither failing nor settled, by who settles it: a person (review),
        # or more evidence (an uncomparable fact; noise or an environment difference).
        review, uncomparable, noise = [], [], []
        for e in mine:
            found = entry_cause(e, noise_measured=measured)
            if found is None:
                continue
            kind, why = found
            if kind is NEEDS_REVIEW:
                review.append(e)
            elif why == "uncomparable":
                uncomparable.append(e)
            else:
                noise.append((e, why))
        unexercised = list(missing) + [_label(e) for e in mine if e.unverified]
        if not mine and step.edits:
            unexercised.append("none of its statements was exercised by the capture")
        status = ("failed" if failing else "needs_review" if review else
                  "needs_evidence" if unexercised or uncomparable or noise else "verified")
        judgement = {**base_judgement, "status": status, "entries": len(mine),
                     "failing": failing[:20], "review": [_label(e) for e in review][:20],
                     "unexercised": unexercised[:20],
                     "uncomparable": [_label(e) for e in uncomparable][:20],
                     "noise": [_label(e) for e, _why in noise][:20],
                     "accepted": sum(1 for e in mine if e.accepted)}
        if status == "needs_evidence":
            judgement["evidence"], judgement["settle"] = _evidence(noise, unexercised,
                                                                   uncomparable)
        out[step.id] = judgement
    return projection.run_id, out


__all__ = ["EXIT", "judge_steps"]
