"""Findings a person accepts: ``verify --accept FINDING_ID`` (design 5.2).

HOST side (3.12+). Wave G left open a reviewed-change record that turns a reviewed difference
into ACCEPTED, keyed by symbol, site and an outcome fingerprint, printed, and expiring when the
fingerprint changes. Here ACCEPTED comes only from a person's attestation (``pymolt.attest``):

- **Finding ids.** Every failing or review finding gets ``sha1(class | mechanism | symbol |
  file | func | site_text)[:10]`` (``stamp_findings``), and each of its entries the finding's
  **fingerprint**: sha256 of the canonical JSON of its new outcomes (the masked, compared
  values).
- **Accepting** (``accept_finding``, a person on a terminal): an ``accept`` ledger event with the
  finding, its symbol, its statement's anchors on both captures' sources, the fingerprint, both
  captures' identities (trace sha256 and ``captured_at``) and the attestation id. On an edited
  line an ``intended`` step covers, the accept is that step's. On an unedited line, or a hand
  edit, it creates an ad-hoc step ``x-<finding id>`` (``type: review``, ``executor: person``,
  ``claim: intended``) that exists in the ledger only. A line a ``preserve`` claim covers is
  refused: a change there fails by design.
- **Honouring** (``apply_accepts``, every verify): the latest accept of a finding holds only
  while its attestation verifies, its symbol and anchors resolve to the same statement, the
  fingerprint is equal, and neither capture was replaced. Otherwise it has **expired**: the
  finding stands again, and the report says why. A held accept marks the finding's entries
  ACCEPTED: compared equal for the verdict, and always printed with who accepted it and when.
- **``--strict``** reviews every ACCEPTED entry instead (an attested expectation's too).
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: The verdict classes a person can accept.
ACCEPTABLE = ("failing", "review")


class AcceptRefusal(ValueError):
    """``verify --accept`` refused (exit 2); ``hint``: what to do instead."""

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint


# ── finding ids and fingerprints ────────────────────────────────────────────────────────────


def finding_id(entry, cls: str) -> str:
    from pymolt.verify.fact_report import mechanism

    site = entry.old_site or entry.new_site
    parts = [cls, mechanism(entry), entry.symbol, site.file if site else "",
             site.func if site else "", (site.text or "") if site else ""]
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:10]


def _outcomes(outcomes) -> list:
    return [o.model_dump(mode="json") for o in outcomes or ()]


_SAMPLE = 120


def _sample(outcomes) -> str:
    """An outcome as the accept prompt shows it: its JSON, cut to ``_SAMPLE`` characters with an
    ellipsis that says it was cut. A person attests to what the prompt shows."""
    text = json.dumps(_outcomes(outcomes), default=str)
    return text if len(text) <= _SAMPLE else text[:_SAMPLE] + "…"


def fingerprint(entries) -> str:
    """sha256 of the canonical JSON of the entries' new outcomes (masked values)."""
    rows = sorted(json.dumps(_outcomes(e.new), sort_keys=True, default=str) for e in entries)
    return hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()


def stamp_findings(fdiff) -> dict[str, list]:
    """Give every failing or review entry its finding id and fingerprint; returns id ->
    entries."""
    from pymolt.verify.fact_report import verdict_class

    groups: dict[str, list] = defaultdict(list)
    for e in fdiff.entries:
        cls = verdict_class(e)
        if cls in ACCEPTABLE:
            e.finding = finding_id(e, cls)
            groups[e.finding].append(e)
    for fid, group in groups.items():
        fp = fingerprint(group)
        for e in group:
            e.fingerprint = fp
    return dict(groups)


# ── the context verify honours accepts in ───────────────────────────────────────────────────


@dataclass
class AcceptContext:
    """What ``apply_accepts`` checks an accept against: the project, its accept events, the
    identities of the two captures compared, their sources (for the anchors), and ``strict``."""

    root: Path
    events: list = field(default_factory=list)
    captures: dict[str, Any] = field(default_factory=dict)
    old_sources: dict[str, str] | None = None
    new_sources: dict[str, str] | None = None
    python_old: Any = None
    python_new: Any = None
    strict: bool = False


def capture_identity(path: str | Path | None, captured_at: str | None) -> dict[str, Any] | None:
    if not path:
        return None
    try:
        digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None
    return {"sha256": digest, "captured_at": captured_at}


def context_for(root: Path, trace: str | Path, against: str | Path, *, old_sources,
                new_sources, strict: bool = False) -> AcceptContext:
    """The accept context of one comparison (``against`` the baseline side, ``trace`` the
    post-migration side)."""
    from pymolt.apply_ledger import load_ledger
    from pymolt.verify.service import load_contract_state, same_slot_path
    from pymolt.workorder.coverage import setup_pythons

    state = load_contract_state(root)

    def captured_at(path, slot):
        return slot.captured_at if slot is not None and same_slot_path(
            root, str(path), slot.trace_path) else None

    ledger = load_ledger(root)
    events = [e for e in (ledger.events if ledger is not None else ()) if e.event == "accept"]
    base, target = setup_pythons(root)
    return AcceptContext(
        root=root, events=events,
        captures={"baseline": capture_identity(against, captured_at(against, state.baseline)),
                  "post": capture_identity(trace, captured_at(trace, state.post_migration))},
        old_sources=old_sources, new_sources=new_sources, python_old=base, python_new=target,
        strict=strict)


def _rel(root: Path, file: str | None) -> str | None:
    if not file:
        return None
    path = Path(file)
    if path.is_absolute():
        try:
            return path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            return file
    return path.as_posix()


def site_anchors(entry, ctx: AcceptContext) -> tuple[dict | None, dict | None]:
    """The anchors of the finding's statement on the baseline side's and the post side's
    sources (None where a side has no site or no source)."""
    from pymolt.workorder.edits import build_index

    out: list[dict | None] = []
    for site, sources, python in ((entry.old_site, ctx.old_sources, ctx.python_old),
                                  (entry.new_site, ctx.new_sources, ctx.python_new)):
        rel = _rel(ctx.root, site.file) if site is not None else None
        text = (sources or {}).get(rel) if rel else None
        index = build_index(text, python) if text is not None and site.line else None
        anchor = index.anchor_at(site.line, file=rel) if index is not None else None
        out.append(anchor.model_dump(mode="json") if anchor is not None else None)
    return out[0], out[1]


def _same_statement(a: dict | None, b: dict | None) -> bool:
    if a is None or b is None:
        return a is b
    return all(a.get(k) == b.get(k) for k in ("file", "scope", "func", "key"))


def _who(event) -> str:
    extra = getattr(event, "model_extra", None) or {}
    who = extra.get("user") or event.actor or "a person"
    return f"{who} on {event.at}" if event.at else who


def _expired(event, group, ctx: AcceptContext, store) -> str | None:
    """Why an accept no longer holds (None: it holds)."""
    extra = event.model_extra or {}
    check = store.check(event.attestation, kind="accept", project=ctx.root,
                        finding=extra.get("finding"), fingerprint=extra.get("fingerprint"),
                        captures=extra.get("captures"), step=event.step_id)
    if not check.ok:
        return check.reason
    held = extra.get("captures") or {}
    for side, name in (("baseline", "baseline capture"), ("post", "post-migration capture")):
        if held.get(side) != ctx.captures.get(side):
            return f"the {name} it was accepted on was replaced"
    if not group:
        return "the finding is not in this verdict any more"
    if fingerprint(group) != extra.get("fingerprint"):
        return "the outcome changed since (its fingerprint differs)"
    rep = group[0]
    if rep.symbol != extra.get("symbol"):
        return "the finding names another symbol"
    old, new = site_anchors(rep, ctx)
    if not (_same_statement(old, extra.get("anchor_old"))
            and _same_statement(new, extra.get("anchor_new"))):
        return "its statement changed"
    if any(e.provenance == "ledger" for e in group):
        return "its line is now covered by a preserve claim"
    steps = {e.step_id for e in group if e.provenance == "intended" and e.step_id}
    if steps and event.step_id not in steps:
        return f"its line is now step {', '.join(sorted(steps))}'s"
    return None


def apply_accepts(fdiff, ctx: AcceptContext | None) -> list[str]:
    """Honour the latest accept of each finding (module docstring); ``--strict`` turns every
    ACCEPTED entry into a review item. Returns the report's notes."""
    if ctx is None:
        return []
    from pymolt.attest import AttestationStore

    notes: list[str] = []
    groups: dict[str, list] = defaultdict(list)
    for e in fdiff.entries:
        if getattr(e, "finding", None):
            groups[e.finding].append(e)
    latest: dict[str, Any] = {}
    for event in sorted(ctx.events, key=lambda e: e.seq):
        fid = (event.model_extra or {}).get("finding")
        if fid:
            latest[fid] = event
    store = AttestationStore()
    for fid, event in latest.items():
        group = groups.get(fid)
        why = _expired(event, group, ctx, store)
        if why:
            notes.append(f"the accept of finding {fid} (by {_who(event)}) has expired: {why}; "
                         "it stands as found again")
            continue
        extra = event.model_extra or {}
        for e in group:
            e.accepted = {"how": f"accept {fid}", "by": extra.get("user") or event.actor,
                          "at": event.at, "attestation": event.attestation,
                          "step": event.step_id}
            e.notes.append(f"accepted: finding {fid} accepted by a person")
    if ctx.strict:
        for e in fdiff.entries:
            if e.accepted:
                how = e.accepted.get("how") or "accepted"
                e.review = (f"accepted change ({how}, by {e.accepted.get('by') or 'a person'}): "
                            "--strict reviews every accepted change")
                e.accepted = None
    return notes


# ── verify --accept ─────────────────────────────────────────────────────────────────────────


@dataclass
class Accepted:
    finding: str
    step: str
    adhoc: bool
    attestation: str
    symbol: str


def _run_for_accept(root: Path) -> str:
    """The run an accept belongs to: the open run, else the saved plan's, else none."""
    from pymolt.migration_plan import MigrationPlan
    from pymolt.migration_state import MigrationReceipt

    receipt = MigrationReceipt.load(root)
    if receipt is not None:
        open_runs = [r.run_id for r in receipt.runs if not r.closed]
        if open_runs:
            return open_runs[-1]
        return receipt.run_id
    plan = MigrationPlan.load(root)
    return plan.plan_id if plan is not None else "0" * 32


def accept_finding(project_dir: str | Path, fdiff, finding: str, *, attester,
                   ctx: AcceptContext) -> Accepted:
    """Accept one finding of ``fdiff`` (the comparison ``ctx`` describes) as a person (module
    docstring). Raises ``AcceptRefusal``."""
    from pymolt.apply_ledger import LedgerEvent, append_ledger
    from pymolt.attest import project_id
    from pymolt.migration_plan import _migration_lock, _now

    root = Path(project_dir).resolve()
    group = [e for e in fdiff.entries if getattr(e, "finding", None) == finding]
    if not group:
        known = sorted({e.finding for e in fdiff.entries if getattr(e, "finding", None)})
        raise AcceptRefusal(
            f"no finding {finding} in this verdict"
            + (f" (its findings: {', '.join(known[:10])})" if known else
               " (it has no failing or review finding)"),
            hint="`pymolt verify .` prints each finding's id.")
    covered = sorted({e.step_id or "?" for e in group if e.provenance == "ledger"})
    if covered:
        raise AcceptRefusal(
            f"finding {finding} is on a line step {', '.join(covered)} claims to preserve: a "
            "change there fails by design and cannot be accepted",
            hint="Roll the step back, or record it again with `--claim intended` (a person).")
    if not ctx.captures.get("baseline") or not ctx.captures.get("post"):
        raise AcceptRefusal("an accept binds both captures, and this comparison lacks one")
    steps = sorted({e.step_id for e in group if e.provenance == "intended" and e.step_id})
    step = steps[0] if len(steps) == 1 else f"x-{finding}"
    adhoc = step.startswith("x-")
    rep = group[0]
    anchor_old, anchor_new = site_anchors(rep, ctx)
    fp = fingerprint(group)
    site = rep.old_site or rep.new_site
    where = f"{site.file}:{site.line}" if site else "?"
    sample = _sample(rep.old), _sample(rep.new)
    text = (f"Accept finding {finding}: {rep.symbol} at {where}, {sample[0]} -> {sample[1]} "
            f"({len(group)} entr{'y' if len(group) == 1 else 'ies'}), as "
            + (f"step {step}'s intended change" if not adhoc else
               f"an intended change (ad-hoc step {step})")
            + ". It expires when the outcome or either capture changes.")
    record, why = attester.ask(text, {
        "kind": "accept", "project": project_id(root), "finding": finding,
        "fingerprint": fp, "captures": ctx.captures, "step": step}) if attester else (
        None, "no person can attest here (--agent, --json or MCP)")
    if record is None:
        raise AcceptRefusal(f"verify --accept is a person's attested act: {why}",
                            hint="A person runs `pymolt verify . --accept ID` on a terminal "
                                 "and types the nonce it prints.")
    event = LedgerEvent(
        event="accept", run_id=_run_for_accept(root), step_id=step, at=_now(),
        executor="person", actor=record.get("user"), attestation=record["id"],
        finding=finding, symbol=rep.symbol, anchor_old=anchor_old, anchor_new=anchor_new,
        fingerprint=fp, captures=ctx.captures, user=record.get("user"),
        adhoc={"type": "review", "executor": "person", "claim": "intended"} if adhoc else None)
    with _migration_lock(root, "verify --accept"):
        append_ledger(root, [(event, [])])
    return Accepted(finding=finding, step=step, adhoc=adhoc, attestation=record["id"],
                    symbol=rep.symbol)


__all__ = ["ACCEPTABLE", "AcceptContext", "AcceptRefusal", "Accepted", "accept_finding",
           "apply_accepts", "capture_identity", "context_for", "fingerprint", "finding_id",
           "site_anchors", "stamp_findings"]
