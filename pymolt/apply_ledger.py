"""The apply ledger: each step's epochs, and the statements each epoch changed with its claim.

HOST side (3.12+, pydantic). ``.pymolt/apply_ledger.json`` is the provenance verify judges an
edited call site by. A statement pymolt itself rewrote was promised to behave the same
(``claim: preserve``), so any difference falsifies the promise; a line a person edited carries
no such promise. Design: ``docs/design/apply-work-order.md`` 2.1-2.2 and 2.5.

Format (``pymolt.apply_ledger/2``, append-only, written under the migration lock)::

    {"schema": "pymolt.apply_ledger/2",
     "events": [{"seq": 1, "event": "apply", "run_id": ..., "step_id": "mech-1", "at": ...,
                 "executor": "pymolt", "actor": "pymolt", "attestation": null,
                 "files": [{"file": "app/x.py", "before": <sha256>, "after": <sha256>}],
                 "risk_ids": [...], "wrote": true}, ...],
     "entries": [{"epoch": 1, "run_id": ..., "step_id": "mech-1", "file": "app/x.py",
                  "old_lines": [a, b], "new_lines": [c, d], "anchor_old": {...},
                  "anchor_new": {...}, "kind": "rename-call", "rule": "rename-call:pkg.old→pkg.new",
                  "claim": "preserve", "basis": "same-signature", "expect": null,
                  "old_symbol": "pkg.old", "new_symbol": "pkg.new"}, ...]}

An event is ``apply`` (``wrote``: false when the file already held the reviewed after-text),
``record``, ``rollback`` (``restored``: whether it rewrote a file), ``skip``, ``abort`` or
``accept``. ``files`` name each file's text before and after the event by sha256; the texts
themselves are in the blob store (``pymolt.workorder.blobs``).

**Epochs.** An entry's ``epoch`` is the ``seq`` of the ``apply`` or ``record`` event that wrote
it. A step's current epoch is the ``seq`` of its latest ``apply``/``record`` event, unless a later
``rollback`` with ``restored: true`` or a ``skip`` ended it; a rollback that rewrote no file
keeps it. An entry is in effect when its epoch is its step's current epoch and its run is in
the receipt chain: entries of a superseded epoch (a re-apply or a re-record wrote a newer one)
or of an ended one are audit data only.

**Entries** name statements, not lines. Each changed region of a file (a difflib opcode over
its physical lines) is split into the statements it meets on either side (``pymolt.anchors``);
those whose key did not change cancel out, and the rest pair up in order: a replaced statement
is one entry with ``anchor_old`` and ``anchor_new``, a removed or an added one an entry with one
anchor. The ranges (1-based, inclusive, in the entry's own before/after text, clipped to the
region) are kept for the audit only: verify locates an entry by its anchors
(``pymolt.workorder.coverage``). A region whose text no grammar or tokenizer reads gets one
entry with its ranges and no anchors.

**Attribution** is by step: the entry takes the step's claim, basis and symbols. A whole-file
step (a schema-1 plan, or a plan with no steps) never recorded where one recipe ends and the
next begins, so each region is attributed to the recipe whose old or new name it holds
(``_recipe_for``, v1's rule) and takes that recipe's claim. ``claim`` is ``preserve`` with
``basis`` set to what vouches for it (``same-object``, ``same-signature``, ``verified-rule``,
``python-fixer``), or null when nothing does (a heuristic recipe: judged as a hand edit).

**A schema-1 ledger** (``pymolt.apply_ledger/1``: one run, archived by the next apply) is read
as events ``seq 1: apply`` (and ``seq 2: rollback, restored: true`` when the receipt shows the
run's files restored) and entries of epoch 1, step ``v1``, anchored on the run's frozen plan
(``migrations/<run>/plan.json``). The first v2 write rewrites it as v2. Archived v1 ledgers
(``.pymolt/archive/apply_ledger-*.json``) are ignored.
"""

from __future__ import annotations

import difflib
import hashlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from itertools import zip_longest
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer
from pydantic_core import ValidationError

from pymolt.anchors import Anchor, Statement, StatementIndex
from pymolt.state_io import (
    StateFileError,
    atomic_write_bytes,
    atomic_write_json,
    keep_remedy,
    read_state_json,
    validation_detail,
)

LEDGER_SCHEMA = "pymolt.apply_ledger/2"
LEDGER_SCHEMA_V1 = "pymolt.apply_ledger/1"
LEDGER_KIND = "apply ledger"
LEDGER_REMEDY = keep_remedy(
    "what each apply step changed, which rollback and verify read")
#: The claims a schema-1 ledger and plan name; each is a ``basis`` of ``preserve`` in v2.
CLAIMS = ("same-object", "same-signature", "verified-rule", "python-fixer")
#: A v2-only basis: the library's own code proves the rewrite (successors v2).
LIBRARY_PROOF = "library-proof"
#: The step a schema-1 run is read as (``pymolt.workorder.v1``).
V1_STEP = "v1"

EventKind = Literal["apply", "record", "rollback", "skip", "abort", "accept"]
EntryClaim = Literal["preserve", "intended"]


def ledger_path(project_dir: str | Path) -> Path:
    return Path(project_dir) / ".pymolt" / "apply_ledger.json"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ── claims (what a recipe vouches for) ────────────────────────────────────────────────────


def claim_for(item: Any) -> str | None:
    """The claim a codemod pattern or rule makes about its edit (None: it makes none). A rule
    (it has a ``match``) that is not verified makes none, whatever its evidence says: its
    golden pair did not pass, so nothing vouches that its edit keeps the behaviour."""
    if getattr(item, "scope", "dependency") == "python":
        return "python-fixer"
    if getattr(item, "match", None) is not None and getattr(item, "confidence", None) != \
            "verified":
        return None
    evidence = [str(e) for e in getattr(item, "evidence", None) or ()]
    if any(e.startswith("pymolt.delta proof") for e in evidence):
        return "library-proof"     # a forward, alias, twin, rename helper or identity proved it
    if any(e.startswith("pymolt.delta MOVED") for e in evidence):
        return "same-object"
    if any(e.startswith("pymolt.delta") for e in evidence):
        return "same-signature"  # the delta engine only proposes a rewrite after checking it
    if getattr(item, "confidence", None) == "verified":
        return "verified-rule"
    return None


def normalize_claim(claim: str | None) -> tuple[str | None, str | None]:
    """``(claim, basis)`` of a v1 claim: ``preserve`` with the v1 claim as its basis, or
    ``(None, None)`` where v1 had none. A v2 claim passes through with no basis."""
    if claim in CLAIMS:
        return "preserve", claim
    if claim in ("preserve", "intended"):
        return claim, None
    return None, None


def _parse_recipe(key: str) -> tuple[str, str | None, str | None]:
    head, sep, new = key.partition("→")
    kind, _, old = head.partition(":")
    return kind, (old or None), (new or None) if sep else None


def _recipe_for(recipes: list[str], old_text: str, new_text: str) -> str | None:
    """The recipe that most plausibly produced a hunk: the one whose old leaf is in the old
    lines, else whose new leaf or module is in the new lines; the only one if there is one.
    Used only where a step cannot say (a whole-file step, a schema-1 ledger)."""
    def leaf(q: str | None) -> str:
        return (q or "").rsplit(".", 1)[-1]

    for key in recipes:
        _kind, old, new = _parse_recipe(key)
        if old and (leaf(old) in old_text or old in old_text) and old_text:
            return key
    for key in recipes:
        _kind, old, new = _parse_recipe(key)
        mod = (new or "").rsplit(".", 1)[0]
        if new and (leaf(new) in new_text or (mod and mod in new_text)):
            return key
    return recipes[0] if len(recipes) == 1 else None


def build_entries(file: str, old: str, new: str, recipes: list[str],
                  claims: dict[str, str]) -> list[dict[str, Any]]:
    """Schema-1 entries: one per difflib opcode over ``str.splitlines``, attributed by
    ``_recipe_for``. Kept for reading and testing v1 ledgers; v2 writes ``entries_for``."""
    old_lines, new_lines = old.splitlines(), new.splitlines()
    out: list[dict[str, Any]] = []
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        key = _recipe_for(recipes, "\n".join(old_lines[i1:i2]), "\n".join(new_lines[j1:j2]))
        kind, old_sym, new_sym = _parse_recipe(key) if key else (None, None, None)
        out.append({
            "file": file,
            "old_lines": [i1 + 1, i2] if i2 > i1 else None,
            "new_lines": [j1 + 1, j2] if j2 > j1 else None,
            "kind": kind, "rule": key, "claim": claims.get(key) if key else None,
            "old_symbol": old_sym, "new_symbol": new_sym,
        })
    return out


# ── models ────────────────────────────────────────────────────────────────────────────────


class EventFile(BaseModel):
    """One file an event touched: its text before and after the event, by sha256."""

    file: str
    before: str | None = None
    after: str | None = None


class LedgerEvent(BaseModel):
    """One thing that happened to a step. Fields an event kind adds (``finding``, ``fingerprint``
    and ``captures`` on an ``accept``) are kept as they are."""

    model_config = ConfigDict(extra="allow")

    seq: int = 0
    event: EventKind
    run_id: str
    step_id: str
    at: str
    executor: str | None = None
    actor: str | None = None
    attestation: str | None = None
    files: list[EventFile] = Field(default_factory=list)
    risk_ids: list[str] = Field(default_factory=list)
    wrote: bool | None = None          # apply: false when the file already held the after-text
    restored: bool | None = None       # rollback: whether it rewrote a file
    reason: str | None = None          # skip: why a person declined the step


class LedgerEntry(BaseModel):
    """One statement an epoch changed in one file (see the module docstring)."""

    epoch: int = 0
    run_id: str
    step_id: str
    file: str
    old_lines: tuple[int, int] | None = None
    new_lines: tuple[int, int] | None = None
    anchor_old: Anchor | None = None
    anchor_new: Anchor | None = None
    kind: str | None = None
    rule: str | None = None
    claim: EntryClaim | None = None
    basis: str | None = None
    expect: str | None = None
    old_symbol: str | None = None
    new_symbol: str | None = None
    # A successor rewrite (successors v2): the keywords it renamed (old -> new), the member it
    # renamed (old -> new), and the defaults it wrote out (parameter -> the old default's
    # repr). Verify maps the new side's inputs back by them (``verify.successor_inputs``).
    param_map: dict[str, str] | None = None
    member_map: dict[str, str] | None = None
    materialized: dict[str, str] | None = None

    @model_serializer(mode="wrap")
    def _omit_unset_maps(self, handler: Any) -> dict[str, Any]:
        # optional additions: an entry without them dumps as before
        data = handler(self)
        for key in ("param_map", "member_map", "materialized"):
            if data.get(key) is None:
                data.pop(key, None)
        return data

    def as_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class Ledger(BaseModel):
    """The whole ledger. ``adapted_from``: 1 when it was read from a schema-1 file (it is then
    rewritten as v2 on the first write); ``notes``: what reading it could not do. Neither is
    saved."""

    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)

    schema_: str = Field(default=LEDGER_SCHEMA, alias="schema")
    events: list[LedgerEvent] = Field(default_factory=list)
    entries: list[LedgerEntry] = Field(default_factory=list)
    adapted_from: int | None = Field(default=None, exclude=True)
    notes: list[str] = Field(default_factory=list, exclude=True)

    def next_seq(self) -> int:
        return max((e.seq for e in self.events), default=0) + 1

    def current_epochs(self) -> dict[tuple[str, str], int]:
        """``(run_id, step_id) -> seq`` of each step's current epoch (steps with none are
        left out): its latest ``apply``/``record``, unless a later restoring ``rollback`` or a
        ``skip`` ended it."""
        current: dict[tuple[str, str], int] = {}
        for event in sorted(self.events, key=lambda e: e.seq):
            key = (event.run_id, event.step_id)
            if event.event in ("apply", "record"):
                current[key] = event.seq
            elif (event.event == "rollback" and event.restored) or event.event == "skip":
                current.pop(key, None)
        return current

    def step_states(self) -> dict[tuple[str, str], str]:
        """``(run_id, step_id) ->`` what the ledger last says of the step: ``applied``,
        ``recorded``, ``rolled_back`` (in effect still, when its rollback restored no file) or
        ``skipped``. Steps the ledger never saw are left out."""
        states: dict[tuple[str, str], str] = {}
        names = {"apply": "applied", "record": "recorded", "rollback": "rolled_back",
                 "skip": "skipped"}
        for event in sorted(self.events, key=lambda e: e.seq):
            if event.event in names:
                states[(event.run_id, event.step_id)] = names[event.event]
        return states

    def in_effect(self, runs: Iterable[str] | None = None) -> list[LedgerEntry]:
        """The entries of the current epochs, of ``runs`` only (None: of every run)."""
        current = self.current_epochs()
        allowed = None if runs is None else set(runs)
        return [e for e in self.entries
                if current.get((e.run_id, e.step_id)) == e.epoch
                and (allowed is None or e.run_id in allowed)]

    def epoch_event(self, run_id: str, step_id: str) -> LedgerEvent | None:
        """The event that opened the step's current epoch (None: it has none)."""
        seq = self.current_epochs().get((run_id, step_id))
        return next((e for e in self.events if e.seq == seq), None) if seq else None


# ── entries from texts ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class EntryAttrs:
    """What an entry says about who vouches for it (from its step, or a whole-file recipe)."""

    kind: str | None = None
    rule: str | None = None
    claim: str | None = None
    basis: str | None = None
    expect: str | None = None
    old_symbol: str | None = None
    new_symbol: str | None = None
    param_map: dict[str, str] | None = None
    member_map: dict[str, str] | None = None
    materialized: dict[str, str] | None = None


def successor_maps(patterns: Iterable[Any]) -> dict[str, dict[str, str] | None]:
    """``{param_map, member_map, materialized}`` of a step's successor patterns: the keywords a
    ``rename-keyword`` renames; the member a ``rename-member`` or a member ``forward-template``
    renames, and the keywords the forward hands on under another name; the defaults a
    ``forward-template`` may write out (those the successor changed)."""
    params: dict[str, str] = {}
    members: dict[str, str] = {}
    written: dict[str, str] = {}
    for p in patterns:
        kind = getattr(p, "kind", None)
        template = getattr(p, "template", None) or {}
        if kind == "rename-keyword":
            params.update(getattr(p, "params", None) or {})
        if kind in ("rename-member", "forward-template") and template.get("member_leaf"):
            old_leaf = p.old_qualname.rsplit(".", 1)[-1]
            new_leaf = p.new_qualname.rsplit(".", 1)[-1]
            if old_leaf != new_leaf:
                members[old_leaf] = new_leaf
        if kind != "forward-template":
            continue
        old_shape = {q[0]: q for q in template.get("old_shape") or ()}
        callee = [q for q in template.get("callee_new_shape") or ()
                  if q[1] in ("POSITIONAL_ONLY", "POSITIONAL_OR_KEYWORD")]
        if callee and callee[0][0] in ("self", "cls") and template.get("member_leaf"):
            callee = callee[1:]
        callee_defaults = {q[0]: q[2] for q in template.get("callee_new_shape") or ()}
        for i, desc in enumerate(template.get("args") or ()):
            if isinstance(desc, dict) and "p" in desc and i < len(callee):
                name = callee[i][0]
                if name != desc["p"]:
                    params[desc["p"]] = name
                spec = old_shape.get(desc["p"])
                if spec and spec[2] is not None and str(spec[2]) != str(
                        callee_defaults.get(name)):
                    written[name] = str(spec[2])
        for key, desc in template.get("kwargs") or ():
            if key == "**" or not (isinstance(desc, dict) and "p" in desc):
                continue
            if key != desc["p"]:
                params[desc["p"]] = key
            spec = old_shape.get(desc["p"])
            if spec and spec[2] is not None and str(spec[2]) != str(callee_defaults.get(key)):
                written[key] = str(spec[2])
    return {"param_map": params or None, "member_map": members or None,
            "materialized": written or None}


def attrs_for_recipe(key: str | None, claims: dict[str, str]) -> EntryAttrs:
    """A whole-file region's attributes: its recipe and that recipe's claim (v1's rule)."""
    if key is None:
        return EntryAttrs()
    kind, old, new = _parse_recipe(key)
    claim, basis = normalize_claim(claims.get(key))
    return EntryAttrs(kind=kind, rule=key, claim=claim, basis=basis, old_symbol=old,
                      new_symbol=new)


def _met(index: StatementIndex | None, start: int, stop: int) -> list[Statement]:
    """The statements whose own lines meet the 0-based half-open range ``start..stop``; for an
    empty range (a pure insertion or deletion at ``start``), those it splits."""
    if index is None:
        return []
    if stop > start:
        return index.statements_in(start + 1, stop)
    if start == 0:
        return []
    return [st for st in index.statements_in(start, start + 1)
            if st.first <= start and st.last >= start + 1]


def _cancel(olds: list[Statement], news: list[Statement]
            ) -> tuple[list[Statement], list[Statement]]:
    """The statements left once those with the same key on both sides cancel (the region
    changed a comment or a blank line around them, not them), each side in source order."""
    pool: dict[str, int] = {}
    for st in news:
        pool[st.key] = pool.get(st.key, 0) + 1
    kept: dict[str, int] = {}
    removed: list[Statement] = []
    for st in olds:
        if pool.get(st.key, 0) > 0:
            pool[st.key] -= 1
            kept[st.key] = kept.get(st.key, 0) + 1
        else:
            removed.append(st)
    added: list[Statement] = []
    for st in news:
        if kept.get(st.key, 0) > 0:
            kept[st.key] -= 1
        else:
            added.append(st)
    return removed, added


def _clip(st: Statement | None, start: int, stop: int) -> tuple[int, int] | None:
    """The statement's own lines inside the region's 0-based half-open ``start..stop``, 1-based."""
    if st is None or stop <= start:
        return None
    first, last = max(st.first, start + 1), min(st.last, stop)
    return (first, last) if first <= last else None


def _index(text: str, python: Any) -> StatementIndex | None:
    from pymolt.workorder.edits import build_index

    return build_index(text, python)


def entries_for(file: str, before: str, after: str,
                attrs: EntryAttrs | Callable[[str, str], EntryAttrs], *, run_id: str,
                step_id: str, python_before: Any = None, python_after: Any = None,
                index_before: StatementIndex | None = None,
                index_after: StatementIndex | None = None,
                anchored: bool = True) -> list[LedgerEntry]:
    """The entries of one step's change to one file (see the module docstring). ``attrs``: the
    step's attributes, or a function of a region's old and new text (a whole-file step).
    ``anchored=False``: one entry per region, by its lines only."""
    from pymolt.workorder.edits import opcodes, split_lines

    if anchored and index_before is None:
        index_before = _index(before, python_before)
    if anchored and index_after is None:
        index_after = _index(after, python_after)
    anchored = anchored and index_before is not None and index_after is not None
    a, b = split_lines(before), split_lines(after)
    seen_old: set[int] = set()
    seen_new: set[int] = set()
    out: list[LedgerEntry] = []
    for _tag, i1, i2, j1, j2 in opcodes(before, after):
        region = attrs("".join(a[i1:i2]), "".join(b[j1:j2])) if callable(attrs) else attrs
        common = {"run_id": run_id, "step_id": step_id, "file": file, "kind": region.kind,
                  "rule": region.rule, "claim": region.claim, "basis": region.basis,
                  "expect": region.expect, "old_symbol": region.old_symbol,
                  "new_symbol": region.new_symbol, "param_map": region.param_map,
                  "member_map": region.member_map, "materialized": region.materialized}
        if not anchored:
            out.append(LedgerEntry(**common, old_lines=(i1 + 1, i2) if i2 > i1 else None,
                                   new_lines=(j1 + 1, j2) if j2 > j1 else None))
            continue
        olds = [st for st in _met(index_before, i1, i2) if id(st) not in seen_old]
        news = [st for st in _met(index_after, j1, j2) if id(st) not in seen_new]
        seen_old.update(id(st) for st in olds)
        seen_new.update(id(st) for st in news)
        removed, added = _cancel(olds, news)
        for old, new in zip_longest(removed, added):
            out.append(LedgerEntry(
                **common, old_lines=_clip(old, i1, i2), new_lines=_clip(new, j1, j2),
                anchor_old=index_before.anchor(old, file=file) if old is not None else None,
                anchor_new=index_after.anchor(new, file=file) if new is not None else None))
    return out


def step_attrs(step: Any) -> EntryAttrs:
    """The attributes a (non whole-file) step gives each of its entries."""
    recipe = getattr(step.do, "recipe", None)
    keys = list(getattr(recipe, "keys", None) or ())
    kind, old, new = _parse_recipe(keys[0]) if keys else (None, None, None)
    symbol = step.symbol
    maps = successor_maps(getattr(recipe, "patterns", None) or ()) if recipe is not None else {}
    return EntryAttrs(
        kind=kind or (recipe.kind if recipe is not None else step.type),
        rule=" + ".join(keys) if keys else None,
        claim=step.claim, basis=step.basis,
        expect=getattr(step.verify, "expect", None),
        old_symbol=symbol.old or old, new_symbol=symbol.new or new, **maps)


# ── reading and writing ───────────────────────────────────────────────────────────────────

_UNSET: Any = object()


def load_ledger(project_dir: str | Path, *, receipt: Any = _UNSET) -> Ledger | None:
    """The ledger (a schema-1 file read as v2); None when there is none. A damaged ledger, or
    one written by a newer pymolt, raises ``StateFileError``: read as none, apply and verify
    would forget what each step changed. ``receipt``: the project's ``MigrationReceipt``
    (loaded when not given), which says whether a schema-1 run was rolled back."""
    path = ledger_path(project_dir)
    data = read_state_json(path, kind=LEDGER_KIND, newest=2, remedy=LEDGER_REMEDY,
                           version_of=_ledger_version)
    if data is None:
        return None
    if data.get("schema") != LEDGER_SCHEMA and receipt is _UNSET:
        from pymolt.migration_state import MigrationReceipt

        receipt = MigrationReceipt.load(project_dir)
    try:
        if data.get("schema") == LEDGER_SCHEMA:
            return Ledger.model_validate(data)
        return _from_v1(Path(project_dir), data, receipt)
    except ValidationError as exc:
        raise StateFileError(path, kind=LEDGER_KIND, problem="damaged",
                             detail=validation_detail(exc), remedy=LEDGER_REMEDY) from exc


def _ledger_version(data: dict[str, Any]) -> int:
    """A ledger's schema number: ``pymolt.apply_ledger/N``; a schema-1 ledger may have no
    ``schema`` (and has no ``events``)."""
    schema = data.get("schema")
    if schema is None and "events" not in data:
        return 1
    prefix = "pymolt.apply_ledger/"
    if isinstance(schema, str) and schema.startswith(prefix) and schema[len(prefix):].isdigit():
        return int(schema[len(prefix):])
    raise ValueError(f"its schema is {schema!r}, not an apply ledger's")


def save_ledger(project_dir: str | Path, ledger: Ledger) -> Path:
    """Write the ledger atomically (the caller holds the migration lock)."""
    return atomic_write_json(ledger_path(project_dir),
                             ledger.model_dump(mode="json", by_alias=True))


def append_ledger(project_dir: str | Path,
                  records: Iterable[tuple[LedgerEvent, list[LedgerEntry]]], *,
                  ledger: Ledger | None = None) -> Ledger:
    """Append events, each with its entries (empty for anything but ``apply``/``record``), and
    save. Each event gets the next ``seq``; its entries get that ``seq`` as their epoch. A
    schema-1 ledger is rewritten as v2. The caller holds the migration lock."""
    current = ledger if ledger is not None else (load_ledger(project_dir) or Ledger())
    out = current.model_copy(deep=True)
    for event, entries in records:
        seq = out.next_seq()
        out.events.append(event.model_copy(update={"seq": seq}))
        if event.event in ("apply", "record"):
            out.entries.extend(e.model_copy(update={"epoch": seq}) for e in entries)
    save_ledger(project_dir, out)
    return out


@dataclass
class LedgerSnapshot:
    """The ledger file's bytes before a write, to restore when the mutation it records fails."""

    path: Path
    data: bytes | None

    @classmethod
    def take(cls, project_dir: str | Path) -> LedgerSnapshot:
        path = ledger_path(project_dir)
        try:
            return cls(path, path.read_bytes())
        except OSError:
            return cls(path, None)

    def restore(self) -> None:
        if self.data is None:
            try:
                self.path.unlink()
            except OSError:
                pass
            return
        atomic_write_bytes(self.path, self.data)


# ── in effect (verify's input) ────────────────────────────────────────────────────────────


def chain_runs(receipt: Any) -> set[str]:
    """The runs whose code is on disk, by the receipt: its chain (a receipt set), or the run it
    records while that run is in effect (a schema-1 receipt); empty without a receipt."""
    if receipt is None:
        return set()
    chain = getattr(receipt, "chain_run_ids", None)
    if callable(chain):
        return set(chain())
    run = getattr(receipt, "run_id", None)
    return {run} if run and getattr(receipt, "in_effect", False) else set()


def in_effect_entries(project_dir: str | Path, receipt: Any
                      ) -> tuple[list[LedgerEntry] | None, str | None, Ledger | None]:
    """``(entries, note, ledger)``: the ledger's in-effect entries (2.2), or None with a note
    saying why when a ledger exists and none of its steps is in effect; ``(None, None, None)``
    without a ledger. A rollback that restored files ends its steps' epochs: their lines are
    no longer pymolt's, so a later edit there is judged as a hand edit."""
    ledger = load_ledger(project_dir, receipt=receipt)
    if ledger is None:
        return None, None, None
    runs = chain_runs(receipt)
    check = getattr(receipt, "run_in_effect", None)
    effective = [r for r in runs if (check(r) if callable(check) else
                                     getattr(receipt, "in_effect", False))]
    current = ledger.current_epochs()
    # A chain run with a current epoch, or a run in effect that changed no file (it has
    # nothing to record): the ledger speaks for the code on disk.
    if any(run in runs for run, _step in current) or any(
            _changed_nothing(receipt, run) for run in effective):
        return ledger.in_effect(runs), None, ledger
    latest = max(ledger.events, key=lambda e: e.seq).run_id if ledger.events else None
    if not effective:
        why = "no apply run is in effect"
    else:
        named = getattr(receipt, "run_id", None)
        why = f"run {str(named if named in effective else effective[-1])[:8]} is the one in effect"
    return None, (f"the apply ledger of run {(latest or 'unknown')[:8]} is not used ({why}): "
                  "edits are judged as hand edits"), ledger


def _changed_nothing(receipt: Any, run: str) -> bool:
    """The run is in effect and changed no file: no step receipt, and the receipt says no
    codemod was required (the latest run's summary)."""
    steps_of = getattr(receipt, "steps_of", None)
    if callable(steps_of) and steps_of(run):
        return False
    return run == getattr(receipt, "run_id", None) and bool(
        getattr(receipt, "no_codemods_required", False))


# ── schema 1 ──────────────────────────────────────────────────────────────────────────────


def _v1_texts(root: Path, run: str | None) -> tuple[dict[str, tuple[str, str]], Any, Any]:
    """The frozen plan's ``{path: (old_source, new_source)}`` and its base and target Python."""
    if not run:
        return {}, None, None
    from pymolt.migration_plan import MigrationPlan

    try:
        plan = MigrationPlan.load(root, root / ".pymolt" / "migrations" / run / "plan.json")
    except StateFileError:
        raise                           # a damaged frozen plan is not read as missing
    except Exception:  # noqa: BLE001 - a run id that is no directory name, an odd plan
        plan = None
    if plan is None:
        return {}, None, None
    texts = {c.path: (c.old_source, c.new_source) for c in plan.changes}
    return texts, (plan.python or {}).get("from"), plan.target_python


def _v1_restored(receipt: Any, run: str | None) -> tuple[bool, str | None]:
    """Whether the receipt shows the schema-1 run's files restored, and when."""
    if receipt is None or not run or getattr(receipt, "run_id", None) != run:
        return False, None
    steps = getattr(receipt, "steps", None) or {}
    step = steps.get(f"{run}:{V1_STEP}") if isinstance(steps, dict) else None
    holder = step if step is not None else receipt
    # A rollback that restored no file ([]) leaves the run in effect; one from before
    # ``files_restored`` was recorded (None) did not (``MigrationReceipt.in_effect``).
    return (getattr(holder, "status", None) == "rolled_back"
            and getattr(holder, "files_restored", None) != [],
            getattr(holder, "rolled_back_at", None))


def _from_v1(root: Path, data: dict[str, Any], receipt: Any) -> Ledger:
    run = data.get("run_id") if isinstance(data.get("run_id"), str) else None
    at = str(data.get("applied_at") or "")
    raw = [e for e in data.get("entries") or () if isinstance(e, dict)]
    texts, base, target = _v1_texts(root, run)
    ledger = Ledger(adapted_from=1)
    files = list(dict.fromkeys(str(e.get("file")) for e in raw if e.get("file")))
    for path in texts:
        if path not in files:
            files.append(path)
    ledger.events.append(LedgerEvent(
        seq=1, event="apply", run_id=run or "unknown", step_id=V1_STEP, at=at,
        executor="pymolt", actor="pymolt", wrote=True,
        files=[EventFile(file=f, before=_sha256(texts[f][0]) if f in texts else None,
                         after=_sha256(texts[f][1]) if f in texts else None) for f in files]))
    restored, rolled_at = _v1_restored(receipt, run)
    if restored:
        ledger.events.append(LedgerEvent(
            seq=2, event="rollback", run_id=run or "unknown", step_id=V1_STEP,
            at=str(rolled_at or at), executor="pymolt", actor="pymolt", restored=True))
    if raw and not texts:
        ledger.notes.append(
            f"the schema-1 apply ledger of run {(run or 'unknown')[:8]} has no frozen plan to "
            "anchor its entries on: they pair symbols only")
    indexes: dict[str, tuple[StatementIndex | None, StatementIndex | None]] = {}
    seen: set[tuple[int, int]] = set()      # (side, id(statement)): one entry per statement
    for item in raw:
        file = str(item.get("file") or "")
        claim, basis = normalize_claim(item.get("claim"))
        common = {"epoch": 1, "run_id": str(item.get("run_id") or run or "unknown"),
                  "step_id": V1_STEP, "file": file, "kind": item.get("kind"),
                  "rule": item.get("rule"), "claim": claim, "basis": basis,
                  "old_symbol": item.get("old_symbol"), "new_symbol": item.get("new_symbol")}
        old_rng, new_rng = _range(item.get("old_lines")), _range(item.get("new_lines"))
        if file in texts and file not in indexes:
            indexes[file] = (_index(texts[file][0], base), _index(texts[file][1], target))
        index_old, index_new = indexes.get(file, (None, None))
        if index_old is None or index_new is None:
            ledger.entries.append(LedgerEntry(**common, old_lines=old_rng, new_lines=new_rng))
            continue
        olds = [st for st in (index_old.statements_in(*old_rng) if old_rng else [])
                if (0, id(st)) not in seen]
        news = [st for st in (index_new.statements_in(*new_rng) if new_rng else [])
                if (1, id(st)) not in seen]
        seen.update((0, id(st)) for st in olds)
        seen.update((1, id(st)) for st in news)
        removed, added = _cancel(olds, news)
        if not removed and not added:
            continue
        o0, o1 = (old_rng[0] - 1, old_rng[1]) if old_rng else (0, 0)
        n0, n1 = (new_rng[0] - 1, new_rng[1]) if new_rng else (0, 0)
        for old, new in zip_longest(removed, added):
            ledger.entries.append(LedgerEntry(
                **common, old_lines=_clip(old, o0, o1), new_lines=_clip(new, n0, n1),
                anchor_old=index_old.anchor(old, file=file) if old is not None else None,
                anchor_new=index_new.anchor(new, file=file) if new is not None else None))
    return ledger


def _range(value: Any) -> tuple[int, int] | None:
    if isinstance(value, list | tuple) and len(value) == 2 \
            and all(isinstance(v, int) for v in value) and value[0] <= value[1]:
        return int(value[0]), int(value[1])
    return None


__all__ = [
    "CLAIMS", "LEDGER_SCHEMA", "LEDGER_SCHEMA_V1", "V1_STEP", "EntryAttrs", "EventFile",
    "Ledger", "LedgerEntry", "LedgerEvent", "LedgerSnapshot", "append_ledger",
    "attrs_for_recipe", "build_entries", "chain_runs", "claim_for", "entries_for",
    "in_effect_entries", "ledger_path", "load_ledger", "normalize_claim",
    "save_ledger", "step_attrs",
]
