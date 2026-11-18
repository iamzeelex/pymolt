"""Apply's rules: what one step does to one file's current text (design 3.2, step 4).

HOST side, pure over texts: nothing here reads or writes a file. ``decide(step, edit, C)`` takes
the step's edit of a file and the file's current text C (the disk, or the text an earlier step
of the same invocation left) and returns the first rule that matches:

1. **fast path**: ``sha(C) == before_sha256``. The after-text is the plan's: C with the step's
   hunks applied.
2. **reviewed**: ``sha(C) == after_sha256``. The text is byte for byte what pymolt reviewed; write
   nothing. The before-text is rebuilt from the plan (the hunks undone on C), so the epoch the
   caller records names both texts and its entries come from the plan's change.
3. **re-base**: every ``anchors_old`` resolves on C. The recipe runs again with its sites
   restricted to those statements (``rerun``, given by the caller); the result is written when
   its effect equals the step's and it changes exactly the statements the anchors name.
   Otherwise the step is stale: the recipe now does something else here.
4. **present**: every ``anchors_old`` is lost, and the effect sits in its slots. The anchor's
   scope is defined exactly once; for each replaced or deleted statement, the anchor's
   neighbours stand around exactly one statement with the new key (adjacent, for a deletion);
   every inserted import is at module scope and every inserted statement resolves. Write
   nothing and record no epoch: the person's text is judged as a hand edit.
5. **stale**: anything else. The reasons name each lost or ambiguous anchor.

A ``manifest-pin`` step's file is a requirements file, not Python: its effect is over the keys
of the lines it pins (``workorder.env``). Rules 1 and 2 are the same; rule 3 rewrites each
pinned line wherever it moved; rule 4 holds when every pinned line already reads the target's
pin.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from pymolt.anchors import MODULE, Anchor, Resolution, StatementIndex
from pymolt.workorder.edits import (
    HunkMismatch,
    apply_hunks,
    build_index,
    make_edit,
    sha256,
    split_lines,
)
from pymolt.workorder.models import Hunk, PlanStep, StepEdit

FAST, REVIEWED, REBASE, PRESENT, STALE = 1, 2, 3, 4, 5
RULE_NAMES = {FAST: "fast path", REVIEWED: "already at the reviewed text", REBASE: "re-based",
              PRESENT: "present", STALE: "stale"}

#: ``rerun(step, edit, C, lines) -> C'``: the step's recipe run on C, restricted to ``lines``
#: (the 1-based lines of the statements its anchors resolved to; None: anywhere). It raises
#: ``ValueError`` when the recipe cannot run there.
Rerun = Callable[[PlanStep, StepEdit, str, frozenset[int] | None], str]


@dataclass
class FileDecision:
    """What one step does to one file. ``before``/``after``: the step's before- and after-text
    here (rules 1 to 3); ``reasons``: why it is stale (rule 5) or why it is not present."""

    file: str
    rule: int
    current: str
    before: str | None = None
    after: str | None = None
    reasons: list[str] = field(default_factory=list)

    @property
    def writes(self) -> bool:
        return self.rule in (FAST, REBASE) and self.after is not None \
            and self.after != self.current

    @property
    def epoch(self) -> bool:
        """The step gets an epoch from this file (rules 1 to 3)."""
        return self.rule in (FAST, REVIEWED, REBASE)


@dataclass(frozen=True)
class Pythons:
    """The Python each side of a step's texts is for: T0 is the base's, later texts the
    target's. A text is indexed with the target's grammar, then the base's."""

    base: Any = None
    target: Any = None

    def index(self, text: str) -> StatementIndex | None:
        index = build_index(text, self.target)
        if (index is None or index.key_kind != "ast") and self.base \
                and self.base != self.target:
            other = build_index(text, self.base)
            if other is not None and other.key_kind == "ast":
                return other
        return index


def unapply_hunks(text: str, hunks: Iterable[Hunk]) -> str:
    """``text`` (a step's after-text) with ``hunks`` undone: its before-text. Raises
    ``HunkMismatch`` when a hunk's new lines are not at its ``new_start``."""
    lines = split_lines(text)
    out: list[str] = []
    pos = 0
    for hunk in hunks:
        start = hunk.new_start - 1
        if start < pos or lines[start:start + len(hunk.new)] != hunk.new:
            raise HunkMismatch(f"hunk at line {hunk.new_start} does not match the text")
        out.extend(lines[pos:start])
        out.extend(hunk.old)
        pos = start + len(hunk.new)
    out.extend(lines[pos:])
    return "".join(out)


def is_manifest(step: PlanStep) -> bool:
    return step.type == "manifest-pin"


def rerunnable(step: PlanStep) -> bool:
    recipe = step.do.recipe
    return recipe is not None and recipe.rerunnable


def _short(text: str | None, cap: int = 60) -> str:
    one = " ".join(str(text or "").split())
    return one if len(one) <= cap else one[:cap] + "…"


def describe(anchor: Anchor, found: Resolution) -> str:
    """One lost or ambiguous anchor, named for a person."""
    return (f"{anchor.file}: `{_short(anchor.stmt)}` in {anchor.scope} is {found.status}"
            + (f" ({found.reason})" if found.reason else ""))


def decide(step: PlanStep, edit: StepEdit, current: str, *, pythons: Pythons = Pythons(),
           rerun: Rerun | None = None) -> FileDecision:
    """The rule that decides ``step``'s edit of one file on its current text (module
    docstring). ``rerun``: re-basing (rule 3); None leaves it out."""
    file = edit.file
    sha = sha256(current)
    if sha == edit.before_sha256:
        try:
            after = apply_hunks(current, edit.hunks)
        except HunkMismatch:
            after = None
        if after is None or sha256(after) != edit.after_sha256:
            return FileDecision(file, STALE, current, reasons=[
                f"{file}: the plan's hunks do not rebuild its own after-text (the plan file "
                "was changed)"])
        return FileDecision(file, FAST, current, before=current, after=after)
    if sha == edit.after_sha256:
        try:
            before = unapply_hunks(current, edit.hunks)
        except HunkMismatch:
            before = None
        if before is None or sha256(before) != edit.before_sha256:
            return FileDecision(file, STALE, current, reasons=[
                f"{file}: the plan's hunks do not rebuild its own before-text (the plan file "
                "was changed)"])
        return FileDecision(file, REVIEWED, current, before=before, after=current)
    if is_manifest(step):
        return _decide_manifest(step, edit, current, rerun=rerun)
    return _decide_code(step, edit, current, pythons=pythons, rerun=rerun)


# ── manifests ─────────────────────────────────────────────────────────────────────────────


def _decide_manifest(step: PlanStep, edit: StepEdit, current: str, *,
                     rerun: Rerun | None) -> FileDecision:
    """Rules 3 to 5 for a ``manifest-pin`` step: its effect is over the lines it pins. Re-based:
    each pinned line found once, wherever it moved, and rewritten to the target's pin. Present:
    every pinned line already reads the target's pin. Stale: anything else."""
    from pymolt.workorder.env import line_key, rewrite_pins

    file = edit.file
    pins = list(step.do.recipe.pins) if step.do.recipe is not None else []
    want = {tuple(pair) for pair in edit.effect.replace}
    if rerun is not None and pins:
        after, done = rewrite_pins(current, pins)
        got = {(line_key(p.old), line_key(p.new)) for p in pins}
        if done == len(pins) and got == want:
            return FileDecision(file, REBASE, current, before=current, after=after)
    have = [line_key(line) for line in split_lines(current)]
    if pins and all(have.count(line_key(p.new)) == 1 and line_key(p.old) not in have
                    for p in pins):
        return FileDecision(file, PRESENT, current)
    reasons = []
    for pin in pins:
        count = have.count(line_key(pin.old))
        if count != 1 and line_key(pin.new) not in have:
            state = "gone" if count == 0 else f"there {count} times"
            reasons.append(f"{file}: the line `{_short(pin.old.strip())}` is {state}, and "
                           f"`{_short(pin.new.strip())}` is not there either")
    if not reasons:
        reasons.append(f"{file}: some of its lines already read the target's pin and some do "
                       "not")
    return FileDecision(file, STALE, current, reasons=reasons)


# ── Python files ──────────────────────────────────────────────────────────────────────────


def _decide_code(step: PlanStep, edit: StepEdit, current: str, *, pythons: Pythons,
                 rerun: Rerun | None) -> FileDecision:
    file = edit.file
    index = pythons.index(current)
    if index is None:
        return FileDecision(file, STALE, current, reasons=[
            f"{file}: the file neither parses nor tokenizes, so its statements cannot be found"])
    found = [(a, index.resolve(a)) for a in edit.anchors_old]
    lost = [(a, r) for a, r in found if not r.ok]
    if not lost:
        if rerun is not None and rerunnable(step):
            lines = None
            if found:
                lines = frozenset(n for _a, r in found
                                  for n in range(r.statement.first, r.statement.last + 1))
            decision = _rebase(step, edit, current, index, lines, [r for _a, r in found],
                               pythons=pythons, rerun=rerun)
            if decision.rule == REBASE or found:
                return decision
            # A pure insertion whose recipe now does something else: it may be present.
            why = present_reasons(edit, index)
            return FileDecision(file, PRESENT, current) if not why else decision
        why = [] if found else present_reasons(edit, index)
        if found or why:
            return FileDecision(file, STALE, current, reasons=[_unreviewed(step, file)])
        return FileDecision(file, PRESENT, current)
    if len(lost) == len(found):
        why = present_reasons(edit, index)
        if not why:
            return FileDecision(file, PRESENT, current)
        return FileDecision(file, STALE, current,
                            reasons=[describe(a, r) for a, r in lost] + why)
    return FileDecision(file, STALE, current, reasons=[describe(a, r) for a, r in lost])


def _unreviewed(step: PlanStep, file: str) -> str:
    if not rerunnable(step):
        return (f"{file}: the text is neither the step's before-text nor its after-text, and "
                "its recipe is the plan's frozen whole-file change, which pymolt cannot apply "
                "to another text")
    return (f"{file}: the text is neither the step's before-text nor its after-text, and "
            "pymolt applies this step only to the text it reviewed")


def _rebase(step: PlanStep, edit: StepEdit, current: str, index: StatementIndex,
            lines: frozenset[int] | None, resolved: list[Resolution], *, pythons: Pythons,
            rerun: Rerun) -> FileDecision:
    """Rule 3: the recipe run on C at the resolved statements, kept when it does exactly what
    the plan reviewed there."""
    file = edit.file
    try:
        rebased = rerun(step, edit, current, lines)
    except Exception as exc:  # noqa: BLE001 - a recipe that cannot run here is a stale step
        return FileDecision(file, STALE, current, reasons=[
            f"{file}: its recipe could not run on the current text "
            f"({type(exc).__name__}: {_short(str(exc), 120)})"])
    if rebased == current:
        return FileDecision(file, STALE, current, reasons=[
            f"{file}: its recipe changes nothing in the current text"])
    try:
        got = make_edit(file, current, rebased, index_before=index,
                        index_after=pythons.index(rebased))
    except Exception as exc:  # noqa: BLE001
        return FileDecision(file, STALE, current, reasons=[
            f"{file}: the re-based text cannot be compared ({type(exc).__name__})"])
    if not got.effect.same_as(edit.effect):
        return FileDecision(file, STALE, current, reasons=[
            f"{file}: its recipe now gives another change here than the plan reviewed "
            f"({_effect_delta(edit.effect, got.effect)})"])
    expected = sorted((r.statement.scope, r.statement.first) for r in resolved)
    touched = sorted((a.scope, a.line) for a in got.anchors_old)
    if expected != touched:
        return FileDecision(file, STALE, current, reasons=[
            f"{file}: its recipe now changes other statements than the ones the plan reviewed"])
    return FileDecision(file, REBASE, current, before=current, after=rebased)


def _effect_delta(want, got) -> str:
    def count(effect) -> str:
        return (f"{len(effect.replace)} replaced, {len(effect.insert)} inserted, "
                f"{len(effect.delete)} deleted, {len(effect.imports_inserted)} import(s)")
    return f"reviewed: {count(want)}; now: {count(got)}"


def present_reasons(edit: StepEdit, index: StatementIndex) -> list[str]:
    """Why the step's effect is not already in its slots in ``index``'s text (rule 4); [] when
    it is. The scope must be defined once; each replaced statement's new key must stand between
    the old one's neighbours (a neighbour the same step replaced counts by either key); a
    deleted statement's neighbours must be adjacent; inserted imports must be at module scope;
    inserted statements must resolve."""
    effect = edit.effect
    if effect.is_empty():
        return [f"{edit.file}: the step changes no statement, so only its exact texts tell"]
    olds = [old for old, _new in effect.replace]
    translate = {old: new for old, new in effect.replace if olds.count(old) == 1}
    why: list[str] = []
    for anchor in edit.anchors_old:
        defined = index.definitions(anchor.scope)
        if defined != 1:
            state = "missing" if defined == 0 else f"defined {defined} times"
            why.append(f"{anchor.file}: scope {anchor.scope} is {state}, so `"
                       f"{_short(anchor.stmt)}`'s slot cannot be found")
            continue
        news = {new for old, new in effect.replace if old == anchor.key}
        deleted = anchor.key in effect.delete
        if not _in_slot(index, anchor, news, deleted, translate):
            what = "its new statement is not" if news else "nothing else is"
            why.append(f"{anchor.file}: {what} between the neighbours of `"
                       f"{_short(anchor.stmt)}` in {anchor.scope}")
    module = {st.key for st in index.in_scope(MODULE) if st.is_import}
    for key in effect.imports_inserted:
        if key not in module:
            why.append(f"{edit.file}: an import the step inserts is not at module scope")
    inserted = set(effect.insert)
    for anchor in edit.anchors_new:
        if anchor.key in inserted and not index.resolve(anchor).ok:
            why.append(f"{anchor.file}: the statement it inserts, `{_short(anchor.stmt)}`, is "
                       f"not in {anchor.scope}")
    return why


def _in_slot(index: StatementIndex, anchor: Anchor, news: set[str], deleted: bool,
             translate: dict[str, str]) -> bool:
    variants = [anchor]
    moved = anchor.model_copy(update={"prev": translate.get(anchor.prev, anchor.prev),
                                      "next": translate.get(anchor.next, anchor.next)})
    if (moved.prev, moved.next) != (anchor.prev, anchor.next):
        variants.insert(0, moved)
    for candidate in variants:
        if news:
            slot = index.slot(candidate, width=1)
            if slot.ok and len(slot.statements) == 1 and slot.statements[0].key in news:
                return True
        if deleted and index.slot(candidate, width=0).ok:
            return True
    return False


__all__ = [
    "FAST", "PRESENT", "REBASE", "REVIEWED", "RULE_NAMES", "STALE", "FileDecision", "Pythons",
    "Rerun", "decide", "describe", "is_manifest", "present_reasons", "rerunnable",
    "unapply_hunks",
]
