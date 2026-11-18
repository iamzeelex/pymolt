"""How to verify a step, and how big it is (design 1.5.5); the rest of ``do.manual``.

HOST side, over the plan's steps, its risks and the baseline capture's executed record.

``verify``: ``judge`` follows the claim (``strict`` for ``preserve``, ``expectation`` for
``intended``, ``none`` without one). Its ``sites`` are the step's ``where`` anchors plus its
sites with no line, each with the baseline calls of the step's risks there and whether the
baseline recorded it: a site of a risk the trace observed is ``recorded``; a site reached only
statically is ``ran-not-recorded`` when the executed record has a line of its function,
``not-run`` when the record covers the file and none of them ran, ``unknown`` without a record.
``uncovered`` counts the ``not-run`` and ``unobservable`` sites: unless a test reaches them they
end ``needs_evidence``. ``tests`` stays None (the capture records no test ids).

``effort.size``, first match: ``none`` (pymolt does it); ``large`` (a behaviour change, no
known successor, or more than 20 sites); ``medium`` (the signature differs, 6 to 20 sites, or a
review); ``trivial`` (one site, a known successor, the same signature); ``small`` (up to 5 sites
and a known successor). ``why`` names the rule.

``do.manual`` gets, best effort and as data: the signature change of a SIGNATURE fact, the
first site's statement (``before_after.before``), and pitfalls: the sites' baseline warnings,
parameters they left at their defaults, and a call-time default.
"""

from __future__ import annotations

import difflib
from collections import defaultdict
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from pymolt.workorder.models import (
    TEXT_CAP,
    BeforeAfter,
    PlanStep,
    SignatureChange,
    StepEffort,
    VerifySite,
)

TESTS_NOTE = "the baseline capture does not record which tests ran a site"
_UNCOVERED = ("not-run", "unobservable")


def _cap(text: Any, cap: int = TEXT_CAP) -> str:
    one = " ".join(str(text).split())
    return one if len(one) <= cap else one[:cap - 1] + "…"


class Executed:
    """The baseline capture's executed record, by project-relative file."""

    def __init__(self, record: Any, mapping: Mapping[str, str]):
        self.record = record
        self.lines: dict[str, set[int]] = defaultdict(set)
        self.functions: dict[str, set[int]] = defaultdict(set)
        for raw, lines in (record.lines or {}).items():
            if raw in mapping:
                self.lines[mapping[raw]] |= set(lines)
        for raw, lines in (record.functions or {}).items():
            if raw in mapping:
                self.functions[mapping[raw]] |= set(lines)

    @classmethod
    def load(cls, trace: str | Path | None, root: Path, files: Iterable[str]) -> Executed | None:
        if trace is None:
            return None
        try:
            from pymolt.verify.report import _load_executed, _map_files

            record = _load_executed(trace)
            if record is None:
                return None
            raw = set(record.lines) | set(record.functions)
            mapping, _note = _map_files(raw, root, list(files))
            return cls(record, mapping)
        except Exception:  # noqa: BLE001 - coverage is best effort: "unknown"
            return None

    def coverage(self, rel: str, func: str | None, t0) -> str:
        """A site reached only statically (no line): did its function run?"""
        index = t0.index(rel)
        known = rel in self.lines or rel in self.functions
        if index is None or not func:
            return "not-run" if known and self.record.complete else "unknown"
        body = [st for st in index.statements
                if st.scope.rsplit(".", 1)[-1] == func or (func == "<module>"
                                                          and st.scope == "<module>")]
        if not body:
            return "unknown"
        lines = self.lines.get(rel) or set()
        if any(n in lines for st in body for n in range(st.first, st.last + 1)):
            return "ran-not-recorded"
        first, last = min(st.first for st in body) - 1, max(st.end for st in body)
        if any(first <= n <= last for n in self.functions.get(rel) or ()):
            return "ran-not-recorded"
        if known or self.record.complete:
            return "not-run"
        return "unknown"


def _sites(step: PlanStep, risks: list[Any], t0, executed: Executed | None
           ) -> list[VerifySite]:
    """One site per ``where`` anchor, then one per site with no line."""
    by_anchor: dict[str, list[Any]] = defaultdict(list)
    lineless: dict[tuple[str, str], list[Any]] = defaultdict(list)
    for risk in risks:
        anchor = t0.anchor(risk.site.file, risk.site.line, risk.symbol)
        if anchor is not None:
            by_anchor[(anchor.file, anchor.scope, anchor.key)].append(risk)
        else:
            lineless[(risk.site.file, risk.site.func)].append(risk)
    out: list[VerifySite] = []
    for anchor in step.where:
        here = by_anchor.get((anchor.file, anchor.scope, anchor.key)) or []
        coverage = "recorded" if any(r.basis == "trace" for r in here) else "unknown"
        out.append(VerifySite(anchor=anchor.id, file=anchor.file, func=anchor.func,
                              baseline_calls=sum(r.calls for r in here), coverage=coverage))
    for (rel, func), here in lineless.items():
        if any(r.basis == "trace" for r in here):
            coverage = "recorded"
        else:
            coverage = executed.coverage(rel, func, t0) if executed is not None else "unknown"
        out.append(VerifySite(file=rel, func=func, baseline_calls=sum(r.calls for r in here),
                              coverage=coverage))
    return out


# ── effort ────────────────────────────────────────────────────────────────────────────────


def _successor_known(step: PlanStep) -> bool:
    manual = step.do.manual
    if manual is None:
        return True
    if step.type in ("manual-rewrite", "review"):
        return bool(manual.suggestion) or step.type == "review"
    if step.type == "python-manual" and not step.why.risk_ids:
        return True                           # a compile finding: the message says what to do
    if step.type in ("target-env", "manifest-pin"):
        return True
    return bool(manual.successor) or ("Rewrite these sites by hand:" in manual.summary)


def _signature_differs(step: PlanStep) -> bool:
    delta = step.why.delta
    return delta is not None and delta.kind == "SIGNATURE"


def _same_signature(step: PlanStep) -> bool:
    delta = step.why.delta
    return delta is not None and delta.kind == "MOVED"      # the same object, elsewhere


def effort_of(step: PlanStep) -> StepEffort:
    """The size rule of 1.5.5 (module docstring), with the rule that applied in ``why``."""
    sites = max(step.effort.sites, len(step.verify.sites), len(step.where))
    files = step.effort.files or len(step.files)
    if step.executor == "pymolt":
        return StepEffort(size="none", sites=sites, files=files, why="executor pymolt")

    def make(size: str, why: str) -> StepEffort:
        return StepEffort(size=size, sites=sites, files=files, why=why)

    known = _successor_known(step)
    if step.type == "behaviour-change":
        return make("large", "a behaviour change: a person decides the outcome")
    if not known:
        return make("large", "no successor is known")
    if sites > 20:
        return make("large", f"{sites} sites (more than 20)")
    if _signature_differs(step):
        return make("medium", "the signature differs")
    if sites > 5:
        return make("medium", f"{sites} sites (6 to 20)")
    if step.type == "review":
        return make("medium", "a review")
    if sites <= 1 and _same_signature(step):
        return make("trivial", "one site, a known successor, the same signature")
    return make("small", f"{sites} site(s) (up to 5) and a known successor")


# ── do.manual details ─────────────────────────────────────────────────────────────────────


def _call_time(value: str | None) -> bool:
    if value is None:
        return False
    text = value.strip()
    return text == "None" or (text.startswith("<") and text.endswith(">"))


def _pitfalls(step: PlanStep, risks: list[Any]) -> list[str]:
    out: list[str] = []

    def add(text: str) -> None:
        item = _cap(text)
        if item not in out:
            out.append(item)

    for warning in step.why.baseline.warnings:
        add(f"the baseline warned here: {warning}")
    for risk in risks:
        for line in risk.evidence or ():
            if isinstance(line, str) and line.startswith("baseline: ") and "default" in line:
                add(line[len("baseline: "):])
    delta = step.why.delta
    if delta is not None and delta.kind == "DEFAULT" and _call_time(delta.old):
        add(f"old default {delta.old} is decided at call time: passing it may not restore the "
            "old behaviour")
    return out[:10]


def _signature(step: PlanStep) -> SignatureChange | None:
    delta = step.why.delta
    if delta is None or delta.kind != "SIGNATURE" or (delta.old is None and delta.new is None):
        return None
    diff = [line.rstrip("\n") for line in difflib.unified_diff(
        [f"{delta.param or 'signature'}: {delta.old}\n"],
        [f"{delta.param or 'signature'}: {delta.new}\n"], lineterm="", n=0)
        if not line.startswith(("---", "+++", "@@"))]
    return SignatureChange(old=delta.old, new=delta.new, diff=[_cap(d) for d in diff])


def finish_steps(steps: list[PlanStep], risks: Any, *, root: str | Path, t0,
                 baseline_trace: str | Path | None = None,
                 baseline_command: list[str] | None = None) -> None:
    """Fill every step's ``verify``, ``effort`` and its ``do.manual`` details, in place."""
    root = Path(root)
    by_step: dict[str, list[Any]] = defaultdict(list)
    for risk in getattr(risks, "risks", None) or ():
        if risk.step_id:
            by_step[risk.step_id].append(risk)
    static_files = {r.site.file for r in getattr(risks, "risks", None) or ()
                    if r.site.line is None}
    executed = Executed.load(baseline_trace, root, static_files) if static_files else None
    for step in steps:
        mine = by_step.get(step.id) or []
        verify = step.verify
        if step.phase == "env":
            verify.judge = "none"
        elif step.claim == "preserve":
            verify.judge = "strict"
        elif step.claim == "intended":
            verify.judge, verify.expect = "expectation", verify.expect or "any"
        else:
            verify.judge = "none"
        if step.phase != "env":
            verify.sites = _sites(step, mine, t0, executed)
            verify.tests, verify.note = None, verify.note or TESTS_NOTE
            verify.command = list(baseline_command) if baseline_command else None
            verify.uncovered = sum(1 for s in verify.sites if s.coverage in _UNCOVERED)
        step.effort = effort_of(step)
        manual = step.do.manual
        if manual is not None and step.phase != "env":
            manual.signature = manual.signature or _signature(step)
            if manual.before_after is None and step.where:
                manual.before_after = BeforeAfter(before=_cap(step.where[0].stmt, 500))
            manual.pitfalls = (manual.pitfalls or _pitfalls(step, mine))[:10]
