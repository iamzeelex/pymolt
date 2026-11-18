"""One recipe's change to one file, as a step edit: hunks, an effect and anchors (design 1.5.2).

HOST side. ``make_edit(file, before, after)`` diffs the two texts line by line (difflib, the
physical lines the tokenizer counts, endings kept), finds the statements each changed region
meets on either side (``pymolt.anchors``), cancels the statements whose key did not change (a
comment or a blank line edited), and pairs the rest within each region: a replaced statement
is ``[old key, new key]``, an unpaired one an insertion or a deletion, and an unpaired import
added at module scope an inserted import. The anchors are made on ``before`` and ``after``
themselves, so each step's anchors describe the text that step ran on.

``apply_hunks`` rebuilds an after-text from its before-text; the plan stores no intermediate
text, only T0 (``changes[].old_source``) and each step's hunks.
"""

from __future__ import annotations

import difflib
import hashlib
import re
from collections.abc import Iterable

from pymolt.anchors import MODULE, AnchorError, Statement, StatementIndex
from pymolt.workorder.models import Effect, Hunk, StepEdit

_LINE = re.compile(r"[^\r\n]*(?:\r\n|\r|\n)|[^\r\n]+\Z")


class HunkMismatch(ValueError):
    """A hunk's old lines are not where it says in the text it is applied to."""


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def split_lines(text: str) -> list[str]:
    """Physical lines with their endings, as the tokenizer and anchors count them (``\\r\\n``,
    ``\\r`` and ``\\n`` only: ``str.splitlines`` also splits on form feeds)."""
    return _LINE.findall(text)


def opcodes(before: str, after: str) -> list[tuple[str, int, int, int, int]]:
    """The changed difflib opcodes between two texts' lines (0-based, half-open)."""
    a, b = split_lines(before), split_lines(after)
    matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
    return [op for op in matcher.get_opcodes() if op[0] != "equal"]


def hunks_of(before: str, after: str) -> list[Hunk]:
    a, b = split_lines(before), split_lines(after)
    out: list[Hunk] = []
    for _tag, i1, i2, j1, j2 in opcodes(before, after):
        out.append(Hunk(old_start=i1 + 1, old=a[i1:i2], new_start=j1 + 1, new=b[j1:j2]))
    return out


def apply_hunks(text: str, hunks: Iterable[Hunk]) -> str:
    """``text`` with ``hunks`` applied in order (their positions are ``text``'s). Raises
    ``HunkMismatch`` when a hunk's old lines are not at its ``old_start``."""
    lines = split_lines(text)
    out: list[str] = []
    pos = 0
    for hunk in hunks:
        start = hunk.old_start - 1
        if start < pos or lines[start:start + len(hunk.old)] != hunk.old:
            raise HunkMismatch(f"hunk at line {hunk.old_start} does not match the text")
        out.extend(lines[pos:start])
        out.extend(hunk.new)
        pos = start + len(hunk.old)
    out.extend(lines[pos:])
    return "".join(out)


def build_index(text: str, python=None) -> StatementIndex | None:
    """The text's statement index, or None when it neither parses nor tokenizes."""
    try:
        return StatementIndex.build(text, python=python)
    except AnchorError:
        return None


def _met(index: StatementIndex | None, start: int, stop: int) -> list[Statement]:
    """The statements whose own lines meet the 0-based half-open line range ``start..stop``;
    for an empty range (a pure insertion or deletion at ``start``), the statements it splits:
    those holding both the line before it and the line after it."""
    if index is None:
        return []
    if stop > start:
        return index.statements_in(start + 1, stop)
    if start == 0:
        return []
    return [st for st in index.statements_in(start, start + 1)
            if st.first <= start and st.last >= start + 1]


def make_edit(file: str, before: str, after: str, *, python_before=None, python_after=None,
              index_before: StatementIndex | None = None,
              index_after: StatementIndex | None = None,
              whole_file: bool = False) -> StepEdit:
    """The step edit turning ``before`` into ``after`` in ``file`` (see the module docstring).
    ``python_before``/``python_after``: the Python each text is for (anchors parse with it).
    ``whole_file``: one hunk replacing every line (a schema-1 plan's change), with the effect
    and anchors still computed per changed region."""
    if index_before is None:
        index_before = build_index(before, python_before)
    if index_after is None:
        index_after = build_index(after, python_after)
    ops = opcodes(before, after)
    effect = Effect(key_kind="ast" if (
        index_before is not None and index_after is not None
        and index_before.key_kind == "ast" and index_after.key_kind == "ast") else "tokens")
    anchors_old: dict[int, object] = {}
    anchors_new: dict[int, object] = {}
    seen_old: set[int] = set()
    seen_new: set[int] = set()
    hunk_ids: list[list[str]] = []
    for _tag, i1, i2, j1, j2 in ops:
        olds = [st for st in _met(index_before, i1, i2) if id(st) not in seen_old]
        news = [st for st in _met(index_after, j1, j2) if id(st) not in seen_new]
        seen_old.update(id(st) for st in olds)
        seen_new.update(id(st) for st in news)
        removed, added = _cancel(olds, news)
        ids: list[str] = []
        for st in removed:
            anchor = index_before.anchor(st, file=file)
            anchors_old[id(st)] = anchor
            ids.append(anchor.id)
        for st in added:
            anchor = index_after.anchor(st, file=file)
            anchors_new[id(st)] = anchor
            ids.append(anchor.id)
        hunk_ids.append(list(dict.fromkeys(ids)))
        for old, new in zip(removed, added, strict=False):
            effect.replace.append((old.key, new.key))
        for old in removed[len(added):]:
            effect.delete.append(old.key)
        for new in added[len(removed):]:
            if new.is_import and new.scope == MODULE:
                effect.imports_inserted.append(new.key)
            else:
                effect.insert.append(new.key)
    if whole_file:
        a, b = split_lines(before), split_lines(after)
        all_ids = list(dict.fromkeys(i for ids in hunk_ids for i in ids))
        hunks = [Hunk(old_start=1, old=a, new_start=1, new=b, anchors=all_ids)] if ops else []
    else:
        a, b = split_lines(before), split_lines(after)
        hunks = [Hunk(old_start=i1 + 1, old=a[i1:i2], new_start=j1 + 1, new=b[j1:j2],
                      anchors=ids)
                 for (_t, i1, i2, j1, j2), ids in zip(ops, hunk_ids, strict=True)]
    return StepEdit(file=file, before_sha256=sha256(before), after_sha256=sha256(after),
                    hunks=hunks, effect=effect, anchors_old=list(anchors_old.values()),
                    anchors_new=list(anchors_new.values()))


def _cancel(olds: list[Statement], news: list[Statement]
            ) -> tuple[list[Statement], list[Statement]]:
    """The statements left once those with the same key on both sides cancel out (the region
    changed a comment or a blank line around them, not them), each side in source order."""
    pool: dict[str, int] = {}
    for st in news:
        pool[st.key] = pool.get(st.key, 0) + 1
    kept_new_keys: dict[str, int] = {}
    removed: list[Statement] = []
    for st in olds:
        if pool.get(st.key, 0) > 0:
            pool[st.key] -= 1
            kept_new_keys[st.key] = kept_new_keys.get(st.key, 0) + 1
        else:
            removed.append(st)
    added: list[Statement] = []
    for st in news:
        if kept_new_keys.get(st.key, 0) > 0:
            kept_new_keys[st.key] -= 1
        else:
            added.append(st)
    return removed, added
