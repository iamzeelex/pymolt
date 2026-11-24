"""Risks to steps, and the steps pymolt leaves to an agent or a person (design 1.1, 1.5.4).

HOST side, pure over the plan's parts and the T0 texts. Every predicted risk gets a
``step_id``:

- **a pymolt step** when the plan fixes the risk (``fixed_by_plan``, computed as before by
  ``verify.predict``): the step whose effect rewrote the risk's statement on T0, or the import
  that binds its symbol there, with a recipe spelling the risk's symbol; else one whose recipe
  the fixing change named; else the first that changes the file. The risk's id joins the step's
  ``why.risk_ids`` and its site the step's ``where``;
- **a manual step** otherwise, one per (scope, package, symbol, fact kind, parameter) across its
  sites: ``api-change`` (agent) for a removal, a move, a signature change or a deprecation;
  ``behaviour-change`` (person) for a changed default, a warning the baseline recorded with no
  delta fact, or a Python BEHAVIOR fact; ``python-manual`` (agent) for any other Python risk the
  plan leaves. The tier is the highest among the sites.

Each H5 item (a rule that matched but is not applied) becomes a ``manual-rewrite`` step per
(rule, file); each rule advisory a ``manual-rewrite`` (``not-applied``) or a ``review``
(``precondition``) step per (rule, file); the target's compile findings a ``python-manual``
step per kind, which also takes the risks made from those findings (they get no step of their
own). Low BLIND risks are steps too (PLAN.md collapses them).

A manual step's claim follows the kind of successor its delta fact shows (``default_claim``):
``preserve`` for the same object, a behaviour change, or no fact about the successor;
``intended`` with the expectation ``representation`` for another object. ``why.claim`` says
which, so PLAN.md and the briefs show it.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from pymolt.anchors import MODULE, Anchor, StatementIndex
from pymolt.workorder.edits import build_index
from pymolt.workorder.models import (
    PHASES,
    TEXT_CAP,
    ManualDo,
    PlanStep,
    StepBaseline,
    StepDelta,
    StepDo,
    StepEffort,
    StepVerify,
    StepWhy,
    step_id,
    step_key,
)

_TIERS = ("certain", "high", "medium", "low")
_EVIDENCE_CAP = 10


def _cap(text: Any, cap: int = TEXT_CAP) -> str:
    one = " ".join(str(text).split())
    return one if len(one) <= cap else one[:cap - 1] + "…"


def _short(symbol: str | None) -> str:
    if not symbol:
        return "?"
    parts = symbol.split(".")
    if len(parts) > 1 and parts[-1] in ("__init__", "__new__", "__call__"):
        return parts[-2]                  # a constructor: the class names it
    return ".".join(parts[-2:])


def _tier(value: Any) -> str | None:
    text = getattr(value, "value", value)
    return text if text in _TIERS else None


def _max_tier(tiers: Iterable[str | None]) -> str | None:
    found = [t for t in tiers if t in _TIERS]
    return min(found, key=_TIERS.index) if found else None


class T0:
    """Each file's text before the migration (the plan's ``old_source`` for a changed file,
    else the file on disk) and its statement index, built once."""

    def __init__(self, root: Path, changes: Iterable[Any], python: Any = None):
        self.root, self.python = root, python
        self.text: dict[str, str | None] = {c.path: c.old_source for c in changes}
        self._index: dict[str, StatementIndex | None] = {}

    def index(self, rel: str) -> StatementIndex | None:
        if rel not in self._index:
            if rel not in self.text:
                try:
                    self.text[rel] = (self.root / rel).read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError, ValueError):
                    self.text[rel] = None
            text = self.text[rel]
            self._index[rel] = build_index(text, self.python) if text is not None else None
        return self._index[rel]

    def anchor(self, rel: str | None, line: int | None, symbol: str | None = None
               ) -> Anchor | None:
        if not rel or not line:
            return None
        index = self.index(rel)
        return index.anchor_at(line, file=rel, symbol=symbol) if index is not None else None


# ── linking a fixed risk to its pymolt step ───────────────────────────────────────────────


def _spellings(risk: Any) -> set[str]:
    names = {risk.symbol}
    for fact in [risk.fact, *(risk.facts or ())]:
        if isinstance(fact, Mapping) and isinstance(fact.get("symbol"), str):
            names.add(fact["symbol"])
    return names


def _leaf(name: str) -> str:
    parts = name.split(".")
    if len(parts) > 1 and parts[-1] in ("__init__", "__new__", "__call__"):
        return parts[-2]
    return parts[-1]


def _recipe_names(step: PlanStep) -> set[str]:
    recipe = step.do.recipe
    names = {step.symbol.old} if step.symbol.old else set()
    if recipe is not None:
        names |= {p.old_qualname for p in recipe.patterns}
        if recipe.rule is not None:
            names |= {n for n in (recipe.rule.old_qualname, recipe.rule.match) if n}
            # A template rule names its call by the method it matches (`$S.append(...)`).
            if recipe.rule.match and "." in recipe.rule.match:
                names.add(recipe.rule.match.split("(")[0].rsplit(".", 1)[-1])
    return names


def _spelled(step: PlanStep, spellings: set[str]) -> bool:
    names = _recipe_names(step)
    leaves = {_leaf(n) for n in names}
    return any(s in names or _leaf(s) in leaves for s in spellings)


def _consumed(step: PlanStep, rel: str) -> set[tuple[str, str]]:
    edit = step.edit_for(rel)
    if edit is None:
        return set()
    keys = edit.effect.old_keys()
    return {(a.scope, a.key) for a in edit.anchors_old if a.key in keys}


def _import_fullnames(text: str) -> set[str]:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return set()
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.update(f"{node.module}.{alias.name}" for alias in node.names)
    return out


def _binding_imports(index: StatementIndex | None, spellings: set[str]
                     ) -> set[tuple[str, str]]:
    """The module-level imports of T0 that bind a name the risk's symbol is read through."""
    if index is None:
        return set()
    out = set()
    for st in index.in_scope(MODULE):
        if not st.is_import:
            continue
        for full in _import_fullnames(st.text):
            if any(s == full or s.startswith(full + ".") for s in spellings):
                out.add((st.scope, st.key))
    return out


def fixing_step(risk: Any, automated: list[PlanStep], t0: T0) -> PlanStep | None:
    """The pymolt step that fixes a ``fixed_by_plan`` risk (see the module docstring)."""
    rel = risk.site.file
    candidates = [s for s in automated if s.edit_for(rel) is not None]
    if not candidates:
        return None
    spellings = _spellings(risk)
    index = t0.index(rel)
    st = index.statement_at(risk.site.line, risk.symbol) if (
        index is not None and risk.site.line) else None
    if st is not None:
        for step in candidates:
            if (st.scope, st.key) in _consumed(step, rel) and _spelled(step, spellings):
                return step
    binding = _binding_imports(index, spellings)
    for step in candidates:
        if binding & _consumed(step, rel) and _spelled(step, spellings):
            return step
    named = set(getattr(risk, "fixed_by", None) or ())
    for strict in (True, False):
        for step in candidates:
            keys = set(step.do.recipe.keys if step.do.recipe else ())
            if keys & named and (not strict or _spelled(step, spellings)):
                return step
    return candidates[0]


# ── manual steps from risks ───────────────────────────────────────────────────────────────


def _fact(risk: Any) -> dict:
    return risk.fact if isinstance(risk.fact, Mapping) else {}


def _risk_type(risk: Any) -> str:
    kind = _fact(risk).get("kind")
    if risk.scope == "python":
        return "behaviour-change" if kind == "BEHAVIOR" else "python-manual"
    if kind is None or kind == "DEFAULT":
        return "behaviour-change"
    return "api-change"


_PHASE_OF = {"manual-rewrite": "mechanical", "api-change": "signature",
             "python-manual": "signature", "behaviour-change": "behaviour",
             "review": "behaviour"}
_EXECUTOR_OF = {"manual-rewrite": "agent", "api-change": "agent", "python-manual": "agent",
                "behaviour-change": "person", "review": "person"}


def _title(step_type: str, risk: Any, target: str | None) -> str:
    fact = _fact(risk)
    kind, param = fact.get("kind"), fact.get("param")
    name = f"`{_short(risk.symbol)}`"
    successor = fact.get("successor")
    where = f" in {risk.package} {target}" if risk.package and target else ""
    if step_type == "behaviour-change":
        if kind == "DEFAULT":
            return _cap(f"{name}: default `{param}` {fact.get('old')} → {fact.get('new')}", 120)
        if kind == "BEHAVIOR":
            return _cap(f"{name}: its behaviour changes on Python {target or '?'}", 120)
        return _cap(f"{name}: it warned in the baseline", 120)
    if risk.scope == "python":
        return _cap(f"{name}: {(kind or 'changed').lower()} on Python {target or '?'}", 120)
    if kind == "REMOVED":
        return _cap(f"{name}: removed{where}" + (f" → `{_short(successor)}`" if successor
                                                 else ""), 120)
    if kind == "MOVED":
        return _cap(f"{name}: moved" + (f" to `{successor}`" if successor else ""), 120)
    if kind == "SIGNATURE":
        return _cap(f"{name}: its signature changes" + (f" for `{param}`" if param else ""),
                    120)
    if kind == "DEPRECATED":
        return _cap(f"{name}: deprecated" + (f" → `{_short(successor)}`" if successor else ""),
                    120)
    return _cap(f"{name}: {(kind or 'changed').lower()}", 120)


def _summary(step_type: str, risk: Any, hint: str = "") -> str:
    fact = _fact(risk)
    kind, param, successor = fact.get("kind"), fact.get("param"), fact.get("successor")
    if step_type == "behaviour-change":
        if kind == "DEFAULT":
            # Passing the old default explicitly keeps the old outcome only if the new version
            # still gives that value its old meaning (pandas 2 reads numeric_only=None as False),
            # which the delta does not know: say so instead of promising it.
            new = f" to {fact.get('new')}" if fact.get("new") is not None else ""
            return (f"Decide at each site: the default of {param} changed from "
                    f"{fact.get('old')}{new}. Passing {param}={fact.get('old')} keeps the old "
                    "outcome only if the new version still reads that value the old way: check "
                    "it, or keep the new default (a person attests the new outcome).")
        if kind == "BEHAVIOR":
            return "Check the code at these sites against the new behaviour."
        return ("The baseline warned at these sites; find what the warning announces and act "
                "on it.")
    if risk.scope == "python":
        return "Rewrite these sites by hand" + (f": {hint}" if hint else ".")
    if successor:
        return f"Replace the calls with `{successor}`, keeping each call's outcome."
    if kind == "SIGNATURE":
        return f"Adapt the calls to the new signature{f' of {param}' if param else ''}."
    if kind == "DEPRECATED":
        return "Move off the deprecated API; no successor is known."
    return "No successor is known: rewrite these sites by hand, keeping each call's outcome."


def _hint(risk: Any) -> str:
    for fact in (risk.fact, *(risk.facts or ())):
        for item in (fact or {}).get("evidence") or () if isinstance(fact, Mapping) else ():
            if isinstance(item, str) and item.startswith("hint: "):
                return _cap(item[6:])
    return ""


# ── the claim a manual step defaults to ───────────────────────────────────────────────────
#
# Owner decision 2026-11-18: the default claim follows the kind of successor the step moves
# to, read from the deciding delta fact only (``pymolt.delta/1``):
# - the same object (MOVED), or the same object with a changed signature (SIGNATURE):
#   ``preserve``. The outcome must stay the same, so any change fails;
# - another object: REMOVED (the object is reachable nowhere in the new version, so whatever
#   replaces it is another object), a DEPRECATED symbol whose successor the delta names, or a
#   spelling the delta shows rebound to another object: ``intended`` with the expectation
#   ``representation``. Verify (design 5.1) then reports a representation-only change as DRIFT
#   and fails a different value or a new raise;
# - a behaviour change: ``preserve``. The fix is expected to keep the old outcome;
# - no fact about the successor (a DEPRECATED symbol with none named, a rule, a compile
#   finding): ``preserve``, and the reason says the claim was not loosened.
# A recorded edit starts from this claim; only a person's attestation can loosen it (record.py).

REPRESENTATION = "representation"
_NOT_LOOSENED = "not loosened for lack of evidence: "


def _rebound(fact: Mapping[str, Any]) -> str | None:
    for item in fact.get("evidence") or ():
        if isinstance(item, str) and item.startswith("rebound: "):
            return item[len("rebound: "):]
    return None


def default_claim(step_type: str, risk: Any | None) -> tuple[str, str | None, str]:
    """``(claim, expect, reason)`` of an agent or person step, from the deciding delta fact of
    its lead risk (``None`` for a step made from a rule or a compile finding). See above."""
    if step_type == "behaviour-change":
        return "preserve", None, "a behaviour change: the fix is expected to keep the old outcome"
    if step_type == "review":
        return "preserve", None, "a review of pymolt's rewrite, which claims to keep each outcome"
    fact = _fact(risk) if risk is not None else {}
    kind, successor = fact.get("kind"), fact.get("successor")
    name = f"`{_short(risk.symbol)}`" if risk is not None else "?"
    another = None
    if kind == "MOVED":
        return "preserve", None, _cap(f"the delta shows {name} is the same object as "
                                      f"`{successor or fact.get('new') or '?'}` (MOVED)")
    if kind == "SIGNATURE":
        rebound = _rebound(fact)
        if rebound is None:
            return "preserve", None, (f"the delta shows {name} stays the same object; only its "
                                      "signature changes (SIGNATURE)")
        another = f"the delta shows the spelling {name} now names another object ({rebound})"
    elif kind == "REMOVED":
        another = (f"the delta shows {name} is removed and the same object is reachable nowhere "
                   "else (REMOVED), so its successor"
                   + (f" `{successor}`" if successor else "") + " is another object")
    elif kind == "DEPRECATED" and successor:
        another = (f"the delta names `{successor}` as the successor of the deprecated {name} "
                   "(DEPRECATED): another object")
    if another is not None:
        return "intended", REPRESENTATION, _cap(another)
    if kind == "DEPRECATED":
        return "preserve", None, _cap(f"{_NOT_LOOSENED}the delta names no successor for the "
                                      f"deprecated {name}")
    return "preserve", None, _NOT_LOOSENED + "no delta fact about a successor"


def _claimed(step_type: str, risk: Any | None) -> tuple[str, StepVerify, str]:
    """A manual step's ``claim``, its ``verify`` (judge and expectation) and ``why.claim``."""
    claim, expect, reason = default_claim(step_type, risk)
    judge = "strict" if claim == "preserve" else "expectation"
    return claim, StepVerify(judge=judge, expect=expect), reason


def _delta(risk: Any) -> StepDelta | None:
    fact = _fact(risk)
    if not fact:
        return None

    def text(value: Any) -> str | None:
        return None if value is None else _cap(value)

    return StepDelta(kind=fact.get("kind"), param=fact.get("param"), old=text(fact.get("old")),
                     new=text(fact.get("new")))


def _evidence(risks: list[Any]) -> list[str]:
    out: list[str] = []
    for risk in risks:
        for line in [risk.reason, *(risk.evidence or ())]:
            item = _cap(line)
            if item and item not in out:
                out.append(item)
    return out[:_EVIDENCE_CAP]


def _warnings(risks: list[Any]) -> list[str]:
    out: list[str] = []
    for risk in risks:
        for w in risk.warnings or ():
            item = _cap(f"{w.category}: {w.message}")
            if item not in out:
                out.append(item)
    return out[:_EVIDENCE_CAP]


def _where(risks: list[Any], t0: T0) -> list[Anchor]:
    out: dict[str, Anchor] = {}
    for risk in risks:
        anchor = t0.anchor(risk.site.file, risk.site.line, risk.symbol)
        if anchor is not None and anchor.id not in out:
            out[anchor.id] = anchor
    return list(out.values())


def _order_key(risks: list[Any]) -> tuple:
    first = min(risks, key=lambda r: (r.site.file, r.site.line or 0))
    return (_TIERS.index(_max_tier(_tier(r.tier) for r in risks) or "low"), first.site.file,
            first.site.line or 0, first.symbol)


def _risk_step(group: list[Any], step_type: str, target: str | None, t0: T0,
               python_move: tuple[str, str] | None,
               versions: Mapping[str, tuple[str | None, str | None]]) -> dict:
    lead = group[0]
    fact = _fact(lead)
    scope = "python" if lead.scope == "python" else "dependency"
    ident = "|".join(str(x) for x in (scope, lead.package, lead.symbol, fact.get("kind"),
                                      fact.get("param")))
    files = list(dict.fromkeys(r.site.file for r in group))
    from_, to = ((python_move or (None, None)) if scope == "python"
                 else versions.get(lead.package or "", (None, None)))
    claim, verify, reason = _claimed(step_type, lead)
    return dict(
        key=step_key(step_type, ident), type=step_type, phase=_PHASE_OF[step_type],
        title=_title(step_type, lead, to), executor=_EXECUTOR_OF[step_type],
        claim=claim, basis="manual", scope=scope,
        package=lead.package if scope == "dependency" else None, from_=from_, to=to,
        symbol={"old": lead.symbol, "new": fact.get("successor")},
        where=_where(group, t0),
        why=StepWhy(evidence=_evidence(group), tier=_max_tier(_tier(r.tier) for r in group),
                    risk_ids=[r.id for r in group],
                    baseline=StepBaseline(calls=sum(r.calls for r in group), sites=len(group),
                                          warnings=_warnings(group)),
                    delta=_delta(lead), claim=reason),
        do=StepDo(manual=ManualDo(summary=_summary(step_type, lead, _hint(lead)),
                                  successor=fact.get("successor"))),
        verify=verify,
        effort=StepEffort(sites=len(group), files=len(files)),
    )


# ── H5 items, advisories, compile findings ────────────────────────────────────────────────


def _rule_parts(key: str) -> tuple[str, str]:
    """``(match, rewrite)`` of a recipe key ``kind:match→rewrite``."""
    head, _sep, rewrite = key.partition("→")
    return head.partition(":")[2] or head, rewrite


def _item_steps(manual_items: list[Any], advisories: list[Any], t0: T0) -> list[dict]:
    out: list[dict] = []
    for item in manual_items:
        match, rewrite = _rule_parts(item.rule)
        where = [a for a in (t0.anchor(item.path, n) for n in item.lines) if a is not None]
        claim, verify, reason = _claimed("manual-rewrite", None)
        out.append(dict(
            key=step_key("manual-rewrite", f"{item.rule}|{item.path}"), type="manual-rewrite",
            phase="mechanical", title=_cap(f"Rewrite by hand: `{match}`", 120),
            executor="agent", claim=claim, basis="manual", scope="dependency",
            where=where, why=StepWhy(evidence=[_cap(item.reason)], claim=reason),
            do=StepDo(manual=ManualDo(summary=_cap(item.reason),
                                      suggestion=_cap(rewrite) if rewrite else None)),
            verify=verify,
            effort=StepEffort(sites=item.sites or len(where), files=1 if item.path else 0),
        ))
    groups: dict[tuple, list[Any]] = defaultdict(list)
    for adv in advisories:
        groups[(adv.severity, adv.rule or "", adv.path)].append(adv)
    for (severity, rule, path), items in groups.items():
        review = severity == "precondition"
        step_type = "review" if review else "manual-rewrite"
        match, rewrite = _rule_parts(rule) if rule else ("", "")
        reasons = list(dict.fromkeys(_cap(a.reason) for a in items))
        where = list({a.id: a for a in (t0.anchor(path, adv.line) for adv in items)
                      if a is not None}.values())
        label = f"`{match}`" if match else path
        claim, verify, reason = _claimed(step_type, None)
        out.append(dict(
            key=step_key(step_type, f"{rule}|{path}|{severity}"), type=step_type,
            phase=_PHASE_OF[step_type],
            title=_cap(f"Review the rewrite of {label}" if review
                       else f"Rewrite by hand: {label}", 120),
            executor=_EXECUTOR_OF[step_type], claim=claim, basis="manual",
            scope="dependency", where=where,
            why=StepWhy(evidence=reasons[:_EVIDENCE_CAP], claim=reason),
            do=StepDo(manual=ManualDo(summary=reasons[0],
                                      suggestion=_cap(rewrite) if rewrite else None)),
            verify=verify,
            effort=StepEffort(sites=len(items), files=1),
        ))
    return out


def _compile_kind(finding: Mapping[str, Any]) -> str:
    return str(finding.get("kind") or "finding")


def _compile_risks(risks: Iterable[Any], findings: list[Mapping[str, Any]]
                   ) -> dict[int, str]:
    """The risks ``verify.predict`` made from compile findings (a python risk with no fact, its
    symbol the finding's kind, at the finding's file and line), by ``id()``, to their finding's
    kind: they belong to that kind's compile step, not to a step of their own (QA H6: the same
    SyntaxWarning sites were planned twice)."""
    where = {(f.get("file"), f.get("line"), str(f.get("kind") or "SyntaxError")): _compile_kind(f)
             for f in findings}
    out: dict[int, str] = {}
    for risk in risks:
        kind = where.get((risk.site.file, risk.site.line, risk.symbol))
        if kind is not None and risk.scope == "python" and not _fact(risk):
            out[id(risk)] = kind
    return out


def _compile_steps(findings: list[Mapping[str, Any]], t0: T0,
                   python_move: tuple[str, str] | None, target: str | None,
                   risks: Mapping[str, list[Any]] | None = None) -> list[dict]:
    """One ``python-manual`` step per kind of compile finding; ``risks``: the risks made from
    the findings, by kind, which it takes (ids, tier, baseline)."""
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for f in findings:
        groups[_compile_kind(f)].append(f)
    out = []
    for kind, items in groups.items():
        linked = list((risks or {}).get(kind) or ())
        where = list({a.id: a for a in (t0.anchor(f.get("file"), f.get("line")) for f in items)
                      if a is not None}.values())
        evidence = []
        for f in items:
            line = _cap(f"{f.get('file')}:{f.get('line')}: {f.get('message')}"
                        + (" (it fails on the base too)" if f.get("base") else ""))
            if line not in evidence:
                evidence.append(line)
        claim, verify, reason = _claimed("python-manual", None)
        out.append(dict(
            key=step_key("python-manual", f"compile|{kind}"), type="python-manual",
            phase="signature",
            title=f"{kind} on Python {target or '?'}: {len(items)} site(s)",
            executor="agent", claim=claim, basis="manual", scope="python",
            from_=(python_move or (None, None))[0], to=(python_move or (None, None))[1],
            where=where, why=StepWhy(
                evidence=evidence[:_EVIDENCE_CAP], claim=reason,
                tier=_max_tier(_tier(r.tier) for r in linked), risk_ids=[r.id for r in linked],
                baseline=StepBaseline(calls=sum(r.calls for r in linked), sites=len(linked),
                                      warnings=_warnings(linked))),
            do=StepDo(manual=ManualDo(summary=f"Make these files compile on Python "
                                              f"{target or 'the target'} without the {kind}.")),
            verify=verify,
            effort=StepEffort(sites=len(items),
                              files=len({f.get("file") for f in items})),
            _risks=linked,
        ))
    return out


# ── the build ─────────────────────────────────────────────────────────────────────────────


def link_and_manual_steps(automated: list[PlanStep], *, root: str | Path, changes: list[Any],
                          risks: Any, manual_items: list[Any], advisories: list[Any],
                          compile_findings: list[Mapping[str, Any]],
                          dependencies: Iterable[Any] = (),
                          python_move: tuple[str, str] | None = None,
                          t0: T0 | None = None) -> list[PlanStep]:
    """Link every risk to a step (setting ``PredictedRisk.step_id``) and return the whole
    work order: the automated steps, then the manual ones, ordered by phase and numbered per
    phase. The automated steps keep their order and ids."""
    versions = {getattr(d, "name", ""): (getattr(d, "from_version", None),
                                         getattr(d, "to_version", None)) for d in dependencies}
    base = python_move[0] if python_move else None
    target_python = python_move[1] if python_move else None
    t0 = t0 if t0 is not None else T0(Path(root), changes, base)
    linked: dict[str, list[Any]] = defaultdict(list)
    groups: dict[tuple, list[Any]] = {}
    compiled = _compile_risks(getattr(risks, "risks", None) or (), compile_findings)
    by_kind: dict[str, list[Any]] = defaultdict(list)
    for risk in getattr(risks, "risks", None) or ():
        if id(risk) in compiled:
            by_kind[compiled[id(risk)]].append(risk)
            continue
        if risk.fixed_by_plan:
            step = fixing_step(risk, automated, t0)
            if step is not None:
                risk.step_id = step.id
                linked[step.id].append(risk)
                continue
        fact = _fact(risk)
        key = (risk.scope, risk.package, risk.symbol, fact.get("kind"), fact.get("param"))
        groups.setdefault(key, []).append(risk)
    for step in automated:
        _attach(step, linked.get(step.id) or [], t0)

    drafts: list[dict] = []
    for group in sorted(groups.values(), key=_order_key):
        step_type = _risk_type(group[0])
        target = (target_python if group[0].scope == "python"
                  else versions.get(group[0].package or "", (None, None))[1])
        drafts.append(dict(_risk_step(group, step_type, target, t0, python_move, versions),
                           _risks=group))
    drafts += _item_steps(manual_items, advisories, t0)
    drafts += _compile_steps(compile_findings, t0, python_move, target_python, by_kind)

    counters = {phase: 0 for phase in PHASES}
    for step in automated:
        counters[step.phase] += 1
    out_by_phase: dict[str, list[PlanStep]] = {phase: [] for phase in PHASES}
    for step in automated:
        out_by_phase[step.phase].append(step)
    for draft in drafts:
        group = draft.pop("_risks", None) or []
        counters[draft["phase"]] += 1
        step = PlanStep(id=step_id(draft["phase"], counters[draft["phase"]]), **draft)
        for risk in group:
            risk.step_id = step.id
        out_by_phase[step.phase].append(step)
    return [s for phase in PHASES for s in out_by_phase[phase]]


def _attach(step: PlanStep, risks: list[Any], t0: T0) -> None:
    """A pymolt step's linked risks: their ids, tier, baseline and sites."""
    if not risks:
        return
    step.why.risk_ids = list(dict.fromkeys([*step.why.risk_ids, *(r.id for r in risks)]))
    step.why.tier = _max_tier([step.why.tier, *(_tier(r.tier) for r in risks)])
    step.why.baseline = StepBaseline(calls=sum(r.calls for r in risks), sites=len(risks),
                                     warnings=_warnings(risks))
    if step.why.delta is None:
        step.why.delta = _delta(risks[0])
    for line in _evidence(risks):
        if line not in step.why.evidence and len(step.why.evidence) < _EVIDENCE_CAP:
            step.why.evidence.append(line)
    known = {(a.file, a.scope, a.key) for a in step.where}
    for anchor in _where(risks, t0):
        if (anchor.file, anchor.scope, anchor.key) not in known:
            step.where.append(anchor)
            known.add((anchor.file, anchor.scope, anchor.key))
