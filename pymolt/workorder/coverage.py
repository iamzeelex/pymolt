"""Ledger coverage: the edited lines each in-effect ledger entry covers (design 2.3).

HOST side (3.12+). ``ledger_coverage(B, P, entries)``: ``B`` is the baseline capture's source
snapshot and ``P`` the post-migration one (project-relative path -> text), ``entries`` the
ledger's in-effect entries (``apply_ledger.in_effect_entries``). For each file in both:

1. **One alignment.** ``A = facts._line_map(B, P)``, the alignment provenance decides which lines
   are edited with. ``E_B`` and ``E_P`` are the lines ``A`` does not mark ``same``.
2. **Locate each entry.** ``anchor_new`` is resolved on P: its statement's lines that are in
   ``E_P`` are the entry's P lines; ``anchor_old`` on B gives its B lines the same way. A lost
   or ambiguous anchor gives that side none: a later edit took the statement over, or an
   earlier step's entry covers it. The ranges an entry was written with are never used.
3. **Conflicts.** A line several entries cover takes the strictest claim among them (the
   ladder: ``preserve`` > ``intended`` unattested > no claim > ``intended`` attested;
   ``facts._claim_rank``). An edited line no entry covers is a hand edit.
4. **Pairing only.** An entry left with no line on either side still offers its
   ``old_symbol -> new_symbol`` for pairing.

So an entry covers only lines the one alignment calls edited, and coverage depends only on B, P
and the statements of the current epochs: not on how many invocations produced them, in which
order, or how often a step was re-based.

**Not in the capture.** A step in effect none of whose new statements resolves in P, while the
statements it replaced (if it replaced any) still do, was applied after the post-migration
capture: P holds the code from before it. A step none of whose new or old statements resolves
in P was overtaken by a later edit (``superseded``): it is in the capture, but no line of P is
its own any more (``workorder.state`` still projects it ``not_in_capture`` when P holds an
older epoch of the step). Files missing from P tell nothing either way.

Attestation (a person's attested ``intended`` claim, design 5.6) is not read from the ledger:
``attested`` is a check the caller supplies, and none means no entry is attested, so a
forged in-tree field never lowers a judgement.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from pymolt.anchors import StatementIndex
from pymolt.apply_ledger import LedgerEntry

StepRef = tuple[str, str]          # (run_id, step_id)


@dataclass
class LedgerCoverage:
    """``entries``: every in-effect entry as a dict (its ledger fields, ``recorded``: the ranges
    it was written with, ``located``: ``{"old": [B lines], "new": [P lines]}``, ``attested``).
    ``not_in_capture`` / ``superseded``: steps, as ``(run_id, step_id)``."""

    entries: list[dict[str, Any]] = field(default_factory=list)
    not_in_capture: list[StepRef] = field(default_factory=list)
    superseded: list[StepRef] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def covering(self, file: str, line: int, side: int) -> dict[str, Any] | None:
        """The entry whose claim decides ``line`` (side 0: B, 1: P), or None (a hand edit)."""
        from pymolt.verify.facts import _claim_rank

        key = "old" if side == 0 else "new"
        best = None
        for entry in self.entries:
            if entry.get("file") == file and line in (entry["located"].get(key) or ()):
                if best is None or _claim_rank(entry) > _claim_rank(best):
                    best = entry
        return best


def _as_entry(item: LedgerEntry | Mapping[str, Any]) -> LedgerEntry:
    return item if isinstance(item, LedgerEntry) else LedgerEntry.model_validate(dict(item))


def _located(entry: LedgerEntry, old: Iterable[int], new: Iterable[int],
             attested: Callable[[LedgerEntry], Any] | None) -> dict[str, Any]:
    out = entry.as_dict()
    out["recorded"] = {"old": out.pop("old_lines"), "new": out.pop("new_lines")}
    out["located"] = {"old": sorted(set(old)), "new": sorted(set(new))}
    info = attested(entry) if attested is not None and entry.claim == "intended" else None
    out["attested"] = bool(info)
    if isinstance(info, Mapping):
        out["attestation"] = dict(info)        # who attested, and when (the report prints it)
    return out


def _index(text: str, python: Any) -> StatementIndex | None:
    from pymolt.workorder.edits import build_index

    return build_index(text, python)


def ledger_coverage(old_sources: Mapping[str, str], new_sources: Mapping[str, str],
                    entries: Iterable[LedgerEntry | Mapping[str, Any]], *,
                    python_old: Any = None, python_new: Any = None,
                    attested: Callable[[LedgerEntry], Any] | None = None) -> LedgerCoverage:
    """Locate ``entries`` in the two snapshots (see the module docstring). ``python_old`` /
    ``python_new``: the Python each snapshot's code is for (the base and the target).
    ``attested``: for an ``intended`` entry, the person attestation it carries, checked outside
    the ledger (a mapping with ``id``, ``user`` and ``at``, or a truthy value), else None."""
    from pymolt.verify.facts import _line_map

    items = [_as_entry(e) for e in entries]
    by_file: dict[str, list[LedgerEntry]] = defaultdict(list)
    for entry in items:
        by_file[entry.file].append(entry)
    out = LedgerCoverage()
    # Per step: did a new statement resolve in P, did a replaced/deleted one, was any checked.
    new_seen: dict[StepRef, list[bool]] = defaultdict(list)
    old_seen: dict[StepRef, list[bool]] = defaultdict(list)
    unanchored = 0
    for file, group in by_file.items():
        old, new = old_sources.get(file), new_sources.get(file)
        if old is None or new is None:
            out.entries.extend(_located(e, (), (), attested) for e in group)
            continue
        om, nm = _line_map(old, new)
        edited_old = {ln for ln, (state, _o) in om.items() if state != "same"}
        edited_new = {ln for ln, (state, _o) in nm.items() if state != "same"}
        index_old = _index(old, python_old)
        index_new = _index(new, python_new)
        for entry in group:
            ref = (entry.run_id, entry.step_id)
            lines_old: set[int] = set()
            lines_new: set[int] = set()
            if entry.anchor_old is None and entry.anchor_new is None \
                    and entry.kind != "pair":     # a recorded pairing anchor has no statement
                unanchored += 1
            if entry.anchor_new is not None and index_new is not None:
                found = index_new.resolve(entry.anchor_new)
                new_seen[ref].append(found.ok)
                if found.ok:
                    st = found.statement
                    lines_new = set(range(st.first, st.last + 1)) & edited_new
            if entry.anchor_old is not None:
                if index_old is not None:
                    found = index_old.resolve(entry.anchor_old)
                    if found.ok:
                        st = found.statement
                        lines_old = set(range(st.first, st.last + 1)) & edited_old
                if index_new is not None:
                    old_seen[ref].append(index_new.resolve(entry.anchor_old).ok)
            out.entries.append(_located(entry, lines_old, lines_new, attested))
    for ref in dict.fromkeys((e.run_id, e.step_id) for e in items):
        news, olds = new_seen.get(ref, []), old_seen.get(ref, [])
        if any(news) or not (news or olds):
            continue
        if not news:                                  # deletions only
            if olds and all(olds):
                out.not_in_capture.append(ref)
        elif any(olds) or not olds:
            out.not_in_capture.append(ref)
        else:
            out.superseded.append(ref)
    if unanchored:
        have = "entry has" if unanchored == 1 else "entries have"
        out.notes.append(f"{unanchored} apply-ledger {have} no statement anchors (the text did "
                         "not parse when it was recorded): they pair symbols only")
    for run, step in out.not_in_capture:
        out.notes.append(
            f"step {step} of migration run {run[:8]} is not in the post-migration capture: the "
            "captured code has the statements from before it, not its new ones, so its edits "
            "are not judged; capture post-migration again")
    return out


def located_by_recorded_lines(entries: Iterable[LedgerEntry | Mapping[str, Any]]
                              ) -> list[dict[str, Any]]:
    """The entries located by the ranges they were written with: the fallback for captures
    with no source snapshots, where nothing else can locate them."""
    out = []
    for item in entries:
        entry = _as_entry(item)
        old = range(entry.old_lines[0], entry.old_lines[1] + 1) if entry.old_lines else ()
        new = range(entry.new_lines[0], entry.new_lines[1] + 1) if entry.new_lines else ()
        out.append(_located(entry, old, new, None))
    return out


def setup_pythons(project_dir) -> tuple[str | None, str | None]:
    """The base and target Python setup chose (the baseline's and the post capture's code)."""
    from pathlib import Path

    from pymolt.ingestion.config import EnvConfig

    config = EnvConfig.load(Path(project_dir) / ".pymolt" / "env_config.json")
    if config is None:
        return None, None
    return config.base_python, config.target_python


__all__ = ["LedgerCoverage", "ledger_coverage", "located_by_recorded_lines", "setup_pythons"]
