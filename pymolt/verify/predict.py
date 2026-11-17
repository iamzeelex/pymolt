"""Predicted risks: what the migration will probably break, per call site, before it runs.

HOST side (pydantic). Joins the **baseline facts** (symbol, call site, the input with its
``defaulted`` parameters, outcomes, runtime warnings) with the **delta facts** the local delta
engine computed for the plan's migrations (``pymolt.delta/1``: REMOVED / MOVED / SIGNATURE /
DEFAULT / DEPRECATED per symbol and parameter) and rates every call site:

=========  ==============================================================================
certain    the site's symbol is REMOVED (MOVED is its own, milder kind)
high       a parameter the site left at its default has a DEFAULT change between concrete
           values, or one resolved at call time (None / a sentinel) that the site's own
           baseline warning names; or the site emitted a deprecation-family warning
medium     a SIGNATURE change touching a parameter the site passes (or a new required one),
           or a DEFAULT change to or from None / a sentinel that no warning at the site names
           (a call-time default: the behaviour may well not change)
low        DEPRECATED in the old version without a recorded warning at the site, MOVED, a
           class-level SIGNATURE change, or any change on a symbol reached only statically
           (BLIND: the baseline never ran it)
=========  ==============================================================================

One risk per (site, symbol) at its strongest tier; ``fact`` is the delta fact that decided
it (None when the warning alone did), ``facts`` every delta fact that matched, ``evidence``
the reasons in words. ``fixed_by_plan`` says a planned codemod rewrites the site (the line
changes and the symbol's name leaves it).

``calibrate`` scores the predictions against the post-migration fact diff, using the
entries at the risk's site for the risk's own symbol (a line can hold several calls; a
REWRITTEN entry counts whatever its symbol, since position pairing ignores names): a risk is
a **hit** when such an entry CHANGED, DISAPPEARED, or was REWRITTEN with a different
outcome; **prevented** when the plan fixed the site and the rewrite kept the behaviour (the
prediction is not disproved: the plan acted on it); a **miss** when the site was compared
and did not change; **not exercised** when the post-migration capture never compared it.
The other calls on a hit risk's line count as its consequences; failing entries no risk
covers are listed as **unpredicted**.

Never fabricates: without delta facts the prediction rests on the baseline warnings alone
and says so (``status``/``notes``); a trace that predates warning capture is noted too.
"""

from __future__ import annotations

import difflib
import hashlib
import logging
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from pymolt.verify.facts import (
    CallWarning,
    Comparison,
    Fact,
    FactCategory,
    FactDiff,
    FactEntry,
    FactSet,
    load_facts,
    warnings_acc,
)

logger = logging.getLogger(__name__)

RISKS_SCHEMA = "pymolt.risks/1"
CALIBRATION_SCHEMA = "pymolt.calibration/1"
_RECEIVERS = ("self", "cls")
_ABSENT = (None, "", "<absent>", "absent", "missing", "<missing>")


def _call_time_default(value: Any) -> bool:
    """A default resolved when the call runs (``None``, ``<no_default>``, ``<sentinel>``,
    ``<object object at ...>``) rather than a value: moving to or from one says the
    parameter's handling changed, not that its effective value did."""
    if value is None:
        return False                        # no default recorded at all, not a None default
    text = str(value).strip()
    return text == "None" or (text.startswith("<") and text.endswith(">"))


class RiskTier(StrEnum):
    CERTAIN = "certain"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


TIER_ORDER = {RiskTier.CERTAIN: 0, RiskTier.HIGH: 1, RiskTier.MEDIUM: 2, RiskTier.LOW: 3}


class RiskSite(BaseModel):
    file: str
    func: str
    line: int | None = None
    text: str | None = None

    def label(self) -> str:
        return f"{self.file}:{self.line if self.line is not None else '?'}"


class PredictedRisk(BaseModel):
    id: str                                  # stable: symbol + site
    tier: RiskTier
    symbol: str
    package: str | None = None
    site: RiskSite
    basis: Literal["trace", "static"] = "trace"   # observed in the baseline / BLIND
    reason: str
    fact: dict[str, Any] | None = None       # the deciding delta fact (schema form)
    facts: list[dict[str, Any]] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    warnings: list[CallWarning] = Field(default_factory=list)  # the site's baseline warnings
    calls: int = 0                           # baseline calls at the site
    fixed_by_plan: bool = False
    fixed_by: list[str] = Field(default_factory=list)  # recipe keys of the planned change
    # "dependency" (a package's API) or "python" (the stdlib between interpreter versions).
    scope: str = "dependency"
    reached_at: Literal["import", "call"] | None = None   # python sites: when it fails
    guard: dict[str, Any] | None = None      # python sites: the syntactic guard, if any
    fix: str | None = None                   # python sites: curated fix kind (advisory: manual)


class ListedSite(BaseModel):
    """A python site that is listed, not rated: guarded, version-dead or pre-existing."""

    kind: Literal["guarded", "version-dead", "pre-existing"]
    symbol: str
    site: RiskSite
    scope: str = "python"
    reached_at: Literal["import", "call"] | None = None
    guard: dict[str, Any] | None = None
    reason: str
    fact: dict[str, Any] | None = None


class DeltaSummary(BaseModel):
    package: str
    subject: str = "dependency"
    from_version: str | None = None
    to_version: str | None = None
    quality: str | None = None
    changes: int = 0
    notes: list[str] = Field(default_factory=list)


class PredictionReport(BaseModel):
    """The risks a plan predicts, and what they rest on."""

    schema_: str = Field(default=RISKS_SCHEMA, alias="schema")
    # complete: delta facts for every migration and a warning-capturing baseline;
    # partial: some evidence missing (see notes); unavailable: nothing to predict from.
    status: Literal["complete", "partial", "unavailable"] = "complete"
    risks: list[PredictedRisk] = Field(default_factory=list)
    deltas: list[DeltaSummary] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    # python sites listed, not rated (see ListedSite): guarded fallbacks / version-dead
    # branches, and names absent on the base too (pre-existing, not caused by the migration).
    guarded: list[ListedSite] = Field(default_factory=list)
    pre_existing: list[ListedSite] = Field(default_factory=list)

    model_config = ConfigDict(serialize_by_alias=True, validate_by_name=True,
                              validate_by_alias=True)

    # Type relocations the plan's delta reports found (see ``MigrationPlan.type_relocations``);
    # carried to the plan, not part of the risks' own schema.
    _type_relocations: list[dict[str, str]] = PrivateAttr(default_factory=list)

    @property
    def fixed(self) -> list[PredictedRisk]:
        return [r for r in self.risks if r.fixed_by_plan]

    @property
    def needs_attention(self) -> list[PredictedRisk]:
        return [r for r in self.risks if not r.fixed_by_plan]

    def counts(self) -> dict[str, int]:
        out = {t.value: 0 for t in RiskTier}
        for r in self.risks:
            out[r.tier.value] += 1
        return out


# ── planned rewrites ──────────────────────────────────────────────────────────────────────


class _Block(BaseModel):
    start: int          # first changed old line (1-based)
    end: int            # last changed old line (inclusive)
    old_text: str
    new_text: str
    recipes: list[str] = Field(default_factory=list)


def planned_rewrites(changes: Iterable[Any]) -> dict[str, list[_Block]]:
    """Changed line ranges per (project-relative) file of a plan's frozen changes
    (``PlannedFileChange``-like: ``path``, ``old_source``, ``new_source``, ``recipes``)."""
    out: dict[str, list[_Block]] = defaultdict(list)
    for change in changes or ():
        path = _attr(change, "path")
        old, new = _attr(change, "old_source"), _attr(change, "new_source")
        if not isinstance(path, str) or not isinstance(old, str) or not isinstance(new, str):
            continue
        recipes = list(_attr(change, "recipes") or [])
        old_lines, new_lines = old.splitlines(), new.splitlines()
        matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag in ("replace", "delete") and i2 > i1:
                out[Path(path).as_posix()].append(_Block(
                    start=i1 + 1, end=i2, old_text="\n".join(old_lines[i1:i2]),
                    new_text="\n".join(new_lines[j1:j2]), recipes=recipes))
    return dict(out)


def _leaf(symbol: str) -> str:
    parts = symbol.split(".")
    if len(parts) > 1 and parts[-1] in ("__init__", "__new__", "__call__"):
        return parts[-2]
    return parts[-1]


def _fix_for(site: RiskSite, symbol: str,
             rewrites: Mapping[str, list[_Block]]) -> _Block | None:
    """The planned change that rewrites this call: its line changes and the symbol's name is
    no longer on it. A static site (no line) is fixed when its function's text loses it."""
    blocks = rewrites.get(site.file) or ()
    leaf = _leaf(symbol)
    for block in blocks:
        if site.line is not None and not (block.start <= site.line <= block.end):
            continue
        if site.line is None and leaf not in block.old_text:
            continue
        if leaf not in block.new_text:
            return block
    return None


# ── the join ──────────────────────────────────────────────────────────────────────────────


class _SiteGroup:
    """Every baseline fact of one symbol at one call site."""

    def __init__(self, symbol: str, file: str, func: str, line: int | None, text: str | None):
        self.symbol, self.file, self.func, self.line, self.text = symbol, file, func, line, text
        self.calls = 0
        self.passed: set[str] = set()
        self.defaulted: set[str] = set()
        self.defaulted_known = False
        self.warnings: dict[tuple[str, str], int] = {}
        self.raised: set[str] = set()
        # The site names the symbol by its spelling (python sites): a MOVED fact about that
        # spelling means the spelling itself vanished (D12).
        self.spelling_site = False

    def add(self, fact: Fact) -> None:
        self.calls += fact.count
        self.passed |= _passed_params(fact.input)
        if fact.defaulted is not None:
            self.defaulted_known = True
            for names in fact.defaulted:
                self.defaulted.update(names)
        if fact.warnings:
            for key, n in warnings_acc(fact.warnings).items():
                self.warnings[key] = self.warnings.get(key, 0) + n
        if self.text is None and fact.text is not None:
            self.text = fact.text
        for o in fact.outcomes:
            if o.kind == "raise":
                self.raised.add(str(o.value))

    def left_at_default(self, param: str) -> bool | None:
        """True if the site left ``param`` at its default (None: defaults not reported and
        the site did not pass it either, so it was defaulted unless it has no default)."""
        if param in self.defaulted:
            return True
        if param in self.passed:
            return False
        return None if not self.defaulted_known else False

    def site(self) -> RiskSite:
        return RiskSite(file=self.file, func=self.func, line=self.line, text=self.text)


def _passed_params(inputs: Any) -> set[str]:
    """Parameter names a call passed explicitly (``bound`` minus the receiver, plus
    ``**kwargs`` keys, in either v2 or the wrap fallback form)."""
    out: set[str] = set()
    if not isinstance(inputs, dict):
        return out
    bound = inputs.get("bound")
    if isinstance(bound, dict):
        out.update(k for k in bound if k not in _RECEIVERS)
    kwargs = inputs.get("kwargs")
    if isinstance(kwargs, dict):
        pairs = kwargs.get("__dict__") if "__dict__" in kwargs else list(kwargs.items())
        for pair in pairs or ():
            if isinstance(pair, list | tuple) and pair and isinstance(pair[0], str):
                out.add(pair[0])
    return out


def _attr(obj: Any, name: str) -> Any:
    return obj.get(name) if isinstance(obj, Mapping) else getattr(obj, name, None)


def _as_dict(report: Any) -> dict[str, Any] | None:
    if isinstance(report, Mapping):
        return dict(report)
    dump = getattr(report, "model_dump", None)
    if callable(dump):
        try:
            return dump(mode="json")
        except TypeError:
            return dump()
    return None


class _DeltaIndex:
    """Every non-UNCHANGED delta fact by each of its spellings."""

    def __init__(self, reports: Iterable[Mapping[str, Any]]):
        self.by_name: dict[str, list[tuple[str, dict]]] = defaultdict(list)
        for rep in reports:
            package = str(rep.get("package") or "")
            for fact in rep.get("facts") or ():
                if not isinstance(fact, Mapping) or fact.get("kind") in (None, "UNCHANGED"):
                    continue
                fact = dict(fact)
                for name in [fact.get("symbol"), *(fact.get("aliases") or ())]:
                    if isinstance(name, str) and name:
                        self.by_name[name].append((package, fact))

    def __bool__(self) -> bool:
        return bool(self.by_name)

    def match(self, symbol: str) -> list[tuple[str, dict, str]]:
        """``(package, fact, how)`` for every fact about ``symbol``: the symbol itself, the
        class of a constructor, or an enclosing object (class, module) that was removed."""
        out: list[tuple[str, dict, str]] = []
        seen: set[int] = set()

        def add(items: list[tuple[str, dict]], how: str, whole_only: bool) -> None:
            for package, fact in items:
                if id(fact) in seen:
                    continue
                if whole_only and (fact.get("param") or fact.get("kind") != "REMOVED"):
                    continue
                seen.add(id(fact))
                out.append((package, fact, how))

        add(self.by_name.get(symbol, []), "symbol", False)
        parts = symbol.split(".")
        if len(parts) > 2 and parts[-1] in ("__init__", "__new__"):
            add(self.by_name.get(".".join(parts[:-1]), []), "constructor", False)
        for i in range(len(parts) - 1, 1, -1):
            add(self.by_name.get(".".join(parts[:i]), []), "enclosing", True)
        return out


def _fact_label(package: str, fact: Mapping[str, Any]) -> str:
    kind = fact.get("kind")
    param = fact.get("param")
    text = f"delta: {kind} {fact.get('symbol')}"
    if param:
        text += f"({param})"
    if kind == "DEFAULT":
        text += f" default {fact.get('old')} -> {fact.get('new')}"
    elif fact.get("successor"):
        text += f" -> {fact['successor']}"
    return text + (f" [{package}]" if package else "")


def _rate(group: _SiteGroup, package: str, fact: dict, how: str
          ) -> tuple[RiskTier, str, list[str]] | None:
    """The tier one delta fact gives one site (None: no risk for this site)."""
    kind = fact.get("kind")
    param = fact.get("param")
    evidence = [_fact_label(package, fact), *(f"delta evidence: {e}" for e in
                                              fact.get("evidence") or ())]
    if how != "symbol":
        evidence.append(f"matched by {how}")
    if kind == "REMOVED":
        return RiskTier.CERTAIN, "the symbol is removed in the new version", evidence
    if kind == "MOVED":
        where = f" to {fact['successor']}" if fact.get("successor") else ""
        if group.spelling_site and group.symbol in (fact.get("symbol"),
                                                    *(fact.get("aliases") or ())):
            there = f"; the same object is at {fact['successor']}" if fact.get("successor") else ""
            return (RiskTier.CERTAIN, f"the spelling {group.symbol} no longer exists{there}",
                    evidence)
        return RiskTier.LOW, f"the symbol moved{where}; the old spelling may stop working", \
            evidence
    if kind == "DEFAULT" and param:
        left = group.left_at_default(param)
        if left is False:
            return None                     # the site passes it: the default never applies
        how_known = ("left at its default" if left else
                     "not passed (defaults not reported: inferred left at its default)")
        evidence.append(f"baseline: {param} {how_known} at this site")
        change = f"the default of {param} changes ({fact.get('old')} -> {fact.get('new')})"
        if _call_time_default(fact.get("old")) or _call_time_default(fact.get("new")):
            if any(param in msg for _cat, msg in group.warnings):
                return (RiskTier.HIGH, f"{change}, and this site's baseline warning names "
                        f"{param}", evidence)
            evidence.append("delta: a call-time default (None or a sentinel) on one side: "
                            "the effective value may not change")
            return (RiskTier.MEDIUM, f"{change}; this site relies on it, but the default is "
                    "resolved at call time", evidence)
        return RiskTier.HIGH, f"{change} and this site relies on it", evidence
    if kind == "SIGNATURE":
        if not param:
            return RiskTier.LOW, "the class's signature (bases) changed", evidence
        if param in group.passed:
            evidence.append(f"baseline: this site passes {param}")
            return RiskTier.MEDIUM, f"the signature changes for {param}, which this site " \
                "passes", evidence
        if fact.get("old") in _ABSENT and fact.get("new") not in _ABSENT:
            evidence.append(f"baseline: this site does not pass the new parameter {param}")
            return RiskTier.MEDIUM, f"a new required parameter {param} that this site does " \
                "not pass", evidence
        return None
    if kind == "DEPRECATED":
        if fact.get("deprecated_in_old"):
            return (RiskTier.LOW, "deprecated in the old version, no warning recorded at this "
                    "site", evidence)
        return RiskTier.LOW, "deprecated in the new version", evidence
    return None


def _risk_id(symbol: str, site: RiskSite) -> str:
    raw = "|".join((symbol, site.file, site.func, str(site.line), site.text or ""))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _warning_texts(acc: Mapping[tuple[str, str], int]) -> list[str]:
    return [f"baseline warning: {c}: {m}" + (f" (x{n})" if n > 1 else "")
            for (c, m), n in sorted(acc.items())]


def predict_risks(
    baseline: FactSet,
    deltas: Iterable[Any] = (),
    *,
    static_contacts: Iterable[Any] | None = None,
    changes: Iterable[Any] | None = None,
    root: str | Path | None = None,
) -> PredictionReport:
    """Rate every baseline call site against the delta facts (see the module docstring).

    ``deltas``: ``DeltaReport`` objects or their ``model_dump()`` dicts. ``static_contacts``:
    the contact map's contacts (``target``/``file``/``caller``), for BLIND sites.
    ``changes``: the plan's frozen file changes, for ``fixed_by_plan``."""
    report = PredictionReport()
    if isinstance(deltas, Mapping):              # {package: DeltaReport}
        deltas = list(deltas.values())
    reports = [d for d in (_as_dict(r) for r in deltas or ()) if d is not None]
    for rep in reports:
        facts = [f for f in rep.get("facts") or () if isinstance(f, Mapping)]
        report.deltas.append(DeltaSummary(
            package=str(rep.get("package") or "?"),
            from_version=rep.get("from") or rep.get("from_version"),
            to_version=rep.get("to") or rep.get("to_version"),
            quality=rep.get("quality"),
            changes=sum(1 for f in facts if f.get("kind") not in (None, "UNCHANGED")),
            notes=[str(n) for n in rep.get("notes") or ()]))
        if rep.get("quality") in ("partial", "inconclusive"):
            report.status = "partial"
            report.notes.append(
                f"delta for {rep.get('package')} is {rep.get('quality')}: "
                + ("; ".join(str(n) for n in rep.get("notes") or ()) or "no reason given"))
    index = _DeltaIndex(reports)
    rewrites = planned_rewrites(changes or ())

    groups: dict[tuple, _SiteGroup] = {}
    for fact in baseline.facts:
        key = (fact.symbol, fact.file, fact.func, fact.line)
        group = groups.get(key)
        if group is None:
            group = groups[key] = _SiteGroup(fact.symbol, fact.file, fact.func, fact.line,
                                             fact.text)
        group.add(fact)
    for sw in baseline.unsampled_warnings:
        key = (sw.symbol, sw.file, sw.func, sw.line)
        group = groups.get(key)
        if group is None:
            group = groups[key] = _SiteGroup(sw.symbol, sw.file, sw.func, sw.line, sw.text)
        for k, n in warnings_acc(sw.warnings).items():
            group.warnings[k] = group.warnings.get(k, 0) + n

    matched_facts: set[int] = set()
    for group in groups.values():
        best: tuple[RiskTier, str, dict | None, str | None] | None = None
        evidence: list[str] = []
        facts: list[dict] = []
        if group.warnings:
            evidence.extend(_warning_texts(group.warnings))
            (cat, msg), _n = sorted(group.warnings.items())[0]
            best = (RiskTier.HIGH, f"it emitted {cat} in the baseline: {msg}", None, None)
        for package, fact, how in index.match(group.symbol) if index else ():
            matched_facts.add(id(fact))
            rated = _rate(group, package, fact, how)
            if rated is None:
                continue
            tier, reason, ev = rated
            if (fact.get("kind") == "DEPRECATED" and fact.get("deprecated_in_old")
                    and group.warnings):
                tier = RiskTier.HIGH            # the warning at this site confirms it
                reason = "deprecated in the old version, and it warned at this site"
            facts.append(fact)
            evidence.extend(e for e in ev if e not in evidence)
            if best is None or TIER_ORDER[tier] < TIER_ORDER[best[0]] or (
                    best[2] is None and TIER_ORDER[tier] == TIER_ORDER[best[0]]):
                best = (tier, reason, fact, package)
        if best is None:
            continue
        tier, reason, fact, package = best
        if fact is not None and group.warnings:
            reason += "; it warned in the baseline"
        site = group.site()
        report.risks.append(PredictedRisk(
            id=_risk_id(group.symbol, site), tier=tier, symbol=group.symbol,
            package=package or (_package_of(facts) if facts else None), site=site,
            basis="trace", reason=reason, fact=fact, facts=facts, evidence=evidence,
            warnings=[CallWarning(category=c, message=m, count=n)
                      for (c, m), n in sorted(group.warnings.items())],
            calls=group.calls))

    if index and static_contacts is not None:
        _add_static_risks(report, index, matched_facts, static_contacts, root)

    for risk in report.risks:
        block = _fix_for(risk.site, risk.symbol, rewrites)
        if block is not None:
            risk.fixed_by_plan = True
            risk.fixed_by = list(block.recipes)
            risk.evidence.append(
                f"plan: rewrites {risk.site.file}:{block.start}"
                + (f"-{block.end}" if block.end != block.start else "")
                + (f" ({', '.join(block.recipes)})" if block.recipes else ""))
    report.risks.sort(key=lambda r: (r.fixed_by_plan, TIER_ORDER[r.tier], r.site.file,
                                     r.site.line or 0, r.symbol))
    if not reports:
        report.status = "partial" if report.risks or baseline.facts else "unavailable"
        report.notes.append("no delta facts: risks rest on the baseline's runtime warnings "
                            "alone")
    if baseline.warnings_capture != "on":
        report.status = "partial" if report.status == "complete" else report.status
        report.notes.append(
            "the baseline was captured without runtime-warning capture: a site with no "
            "recorded warning may still have warned" if baseline.warnings_capture is None
            else f"baseline warning capture: {baseline.warnings_capture}")
    return report


def _package_of(facts: list[dict]) -> str | None:
    for fact in facts:
        sym = fact.get("symbol")
        if isinstance(sym, str) and sym:
            return sym.split(".", 1)[0]
    return None


def _add_static_risks(report: PredictionReport, index: _DeltaIndex, matched: set[int],
                      contacts: Iterable[Any], root: str | Path | None) -> None:
    """LOW risks for changes on symbols reached only statically: a contact whose target has
    a delta fact no baseline call site matched (the baseline never ran it)."""
    seen: set[tuple] = set()
    for contact in contacts:
        target = _attr(contact, "target")
        if not isinstance(target, str):
            continue
        hits = [(p, f, h) for p, f, h in index.match(target) if id(f) not in matched]
        if not hits:
            continue
        file = _relative(_attr(contact, "file"), root)
        caller = _attr(contact, "caller") or "?"
        func = str(caller).rsplit(".", 1)[-1]
        key = (target, file, func)
        if key in seen:
            continue
        seen.add(key)
        site = RiskSite(file=file, func=func)
        evidence = ["reached only statically (BLIND): the baseline never ran this contact"]
        facts = []
        for package, fact, how in hits:
            facts.append(fact)
            evidence.append(_fact_label(package, fact))
            if how != "symbol":
                evidence.append(f"matched by {how}")
        report.risks.append(PredictedRisk(
            id=_risk_id(target, site), tier=RiskTier.LOW, symbol=target,
            package=hits[0][0] or None, site=site, basis="static",
            reason=f"{hits[0][1].get('kind')} on a symbol the baseline never exercised",
            fact=hits[0][1], facts=facts, evidence=evidence))


def _relative(file: Any, root: str | Path | None) -> str:
    if not isinstance(file, str) or not file:
        return "?"
    p = Path(file)
    if root is not None and p.is_absolute():
        try:
            return p.resolve().relative_to(Path(root).resolve()).as_posix()
        except ValueError:
            pass
    return p.as_posix()


# ── the plan's side: compute deltas, then predict ─────────────────────────────────────────


def compute_plan_deltas(project_dir: str | Path, migrations: list[Any]) -> tuple[list, list[str]]:
    """``pymolt.delta.compute_deltas`` for the plan's migrations (offline, cached), or
    ``([], [why not])`` when the engine is unavailable or fails."""
    if not migrations:
        return [], []
    try:
        from pymolt.delta import compute_deltas
    except ImportError as exc:
        return [], [f"delta engine unavailable ({exc})"]
    try:
        return list(compute_deltas(project_dir, migrations, symbols=None, online=False,
                                   use_cache=True)), []
    except Exception as exc:  # noqa: BLE001 - a failed delta degrades the prediction only
        logger.warning("delta computation failed: %s", exc)
        return [], [f"delta computation failed: {type(exc).__name__}: {exc}"]


def collect_type_relocations(delta_reports: Iterable[Any]) -> list[dict[str, str]]:
    """The reports' ``type_relocations`` merged, deduplicated by (old, new)."""
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, str]] = []
    for r in delta_reports or ():
        for item in (_as_dict(r) or {}).get("type_relocations") or ():
            key = (item.get("old"), item.get("new"))
            if key not in seen and all(key):
                seen.add(key)
                out.append(dict(item))
    return out


def predict_plan_risks(
    project_dir: str | Path,
    baseline_trace: str | Path,
    migrations: list[Any],
    changes: Iterable[Any] = (),
    *,
    delta_reports: list[Any] | None = None,
) -> PredictionReport:
    """The risks a plan predicts from its baseline capture and its migrations' deltas.

    ``delta_reports`` given: used as they are (tests, a caller that already has them);
    otherwise computed with ``compute_plan_deltas``. Never raises: a missing input is a note
    and a lower ``status``."""
    root = Path(project_dir).resolve()
    trace = Path(baseline_trace)
    if not trace.is_absolute():
        trace = root / trace
    try:
        baseline = load_facts(trace, root=root)
    except OSError as exc:
        return PredictionReport(status="unavailable",
                                notes=[f"cannot read the baseline capture: {exc}"])
    notes: list[str] = []
    if delta_reports is None:
        delta_reports, notes = compute_plan_deltas(root, list(migrations or ()))
    if isinstance(delta_reports, Mapping):
        delta_reports = list(delta_reports.values())
    static = None
    if any((_as_dict(r) or {}).get("facts") for r in delta_reports):
        try:
            from pymolt.verify.contact_map import build_contact_map

            static = build_contact_map(root).contacts
        except Exception as exc:  # noqa: BLE001 - BLIND sites are a refinement
            notes.append(f"static contacts unavailable: {exc}")
    report = predict_risks(baseline, delta_reports, static_contacts=static, changes=changes,
                           root=root)
    report._type_relocations = collect_type_relocations(delta_reports)
    report.notes = notes + report.notes
    if notes and report.status == "complete":
        report.status = "partial"
    return report


# ── python risks: the stdlib between interpreter versions ─────────────────────────────────


def _python_fact(index: dict[str, dict], spelling: str) -> tuple[dict | None, str]:
    """The python delta fact for a spelling: its own, else the removal of an enclosing
    module (``imp`` for ``imp.reload``)."""
    if spelling in index:
        return index[spelling], "symbol"
    parts = spelling.split(".")
    for i in range(len(parts) - 1, 0, -1):
        fact = index.get(".".join(parts[:i]))
        if fact is not None and fact.get("kind") == "REMOVED":
            return fact, "enclosing"
    return None, "symbol"


def _cap(tier: RiskTier, ceiling: RiskTier) -> RiskTier:
    return ceiling if TIER_ORDER[tier] < TIER_ORDER[ceiling] else tier


def _fix_kind(fact: Mapping[str, Any]) -> str | None:
    for item in fact.get("evidence") or ():
        if isinstance(item, str) and item.startswith("fix: "):
            return item[5:].strip() or None
    return None


def _recipe_old(recipe: str) -> str | None:
    """``old`` of a recipe key ``kind:old→new`` (see ``recipe_key``)."""
    head, sep, _ = recipe.partition("→")
    return head.split(":", 1)[1] if sep and ":" in head else None


def _python_fix_for(site: RiskSite, symbol: str,
                    rewrites: Mapping[str, list[_Block]]) -> _Block | None:
    """The planned change that rewrites this python site: a changed block covers its line and
    one of the block's recipes rewrites exactly this spelling. (``_fix_for`` asks whether the
    symbol's leaf left the text; ``collections.Iterable`` → ``collections.abc.Iterable`` keeps
    it, so a python site is matched by recipe instead.)"""
    if site.line is None:
        return None
    for block in rewrites.get(site.file) or ():
        if block.start <= site.line <= block.end and any(
                _recipe_old(r) == symbol for r in block.recipes):
            return block
    return None


def _import_removed(site: RiskSite, symbol: str,
                    rewrites: Mapping[str, list[_Block]]) -> _Block | None:
    """The planned change that deletes this module-import site's import entirely."""
    if site.line is None or not symbol or "." in symbol:
        return None
    pattern = re.compile(rf"^\s*(?:import\s+{re.escape(symbol)}\b|from\s+"
                         rf"{re.escape(symbol)}\s+import\b)", re.M)
    for block in rewrites.get(site.file) or ():
        if (block.start <= site.line <= block.end and pattern.search(block.old_text)
                and not pattern.search(block.new_text)):
            return block
    return None


def predict_python_risks(
    report: Any,
    sites: Iterable[Any],
    *,
    base: str | None,
    target: str | None,
    compile_findings: Iterable[Mapping[str, Any]] | None = None,
    changes: Iterable[Any] | None = None,
    runtime_facts: Iterable[Fact] | None = None,
    root: str | Path | None = None,
) -> PredictionReport:
    """Rate the project's stdlib sites against the python delta (``subject="python"``).

    ``runtime_facts``: the baseline's stdlib calls (``python_runtime_facts``); each call site
    is rated against the delta fact of the symbol it called (``root`` makes their files
    project-relative), so a runtime-observed call that passes a parameter Python removed is
    CERTAIN (HIGH when a side was not probed), and a site the baseline never ran stays LOW.

    =========  ==============================================================================
    certain    absent on the probed target, present on the base (probe or data), unguarded
               (REMOVED, or MOVED whose own spelling vanished: D12); a target SyntaxError the
               base compiles
    high       the same with absence from data only (no target interpreter), or a base
               presence resting only on an import statement; a SyntaxError whose base compile
               was not observed
    medium     present on the target only through a third-party shim; under a broad ``except``;
               a site a BEHAVIOR fact names (code that iterates what the target orders
               differently, e.g. a layered TypedDict's ``__annotations__`` on 3.9+)
    low        deprecated but present on the target; removed after the target; a SyntaxWarning
    listed     guarded fallbacks, version-dead branches, absent on the base too
    =========  ==============================================================================

    Evidence caps the tier, it never rounds up: a data-only absence is never ``certain``.
    ``changes``: the plan's frozen file changes; a site one of them rewrites is
    ``fixed_by_plan`` (advisory-only removals have no rewrite and stay manual)."""
    from pymolt.pyversion.guards import evaluate_guard, parse_version

    out = PredictionReport()
    rep = _as_dict(report) or {}
    facts = [f for f in rep.get("facts") or () if isinstance(f, Mapping)]
    index: dict[str, dict] = {}
    for fact in facts:
        for name in [fact.get("symbol"), *(fact.get("aliases") or ())]:
            if isinstance(name, str) and name and (name not in index or
                                                   fact.get("kind") != "UNCHANGED"):
                index[name] = dict(fact)
    quality = rep.get("quality")
    out.deltas.append(DeltaSummary(
        package=str(rep.get("package") or "python"), subject="python",
        from_version=rep.get("from") or base, to_version=rep.get("to") or target,
        quality=quality,
        changes=sum(1 for f in facts if f.get("kind") not in (None, "UNCHANGED")),
        notes=[str(n) for n in rep.get("notes") or ()]))
    if quality in ("partial", "inconclusive"):
        out.status = "partial"
        out.notes.append(f"python delta is {quality}: "
                         + ("; ".join(str(n) for n in rep.get("notes") or ())
                            or "no reason given"))
    target_version = parse_version(target)
    present = None
    try:
        from pymolt.core import stdlib as _stdlib

        def present(dotted: str) -> bool | None:
            return _stdlib.present_in(dotted, target or "")
    except ImportError:
        pass

    seen: set[tuple] = set()
    for site in sites or ():
        spelling = _attr(site, "spelling")
        file, line = _attr(site, "file"), _attr(site, "line")
        if not isinstance(spelling, str) or not isinstance(file, str):
            continue
        key = (spelling, file, line)
        if key in seen:
            continue
        seen.add(key)
        fact, how = _python_fact(index, spelling)
        if fact is None:
            continue
        risk_site = RiskSite(file=file, func="", line=line if isinstance(line, int) else None)
        reached = _attr(site, "reached_at")
        guard = _attr(site, "guard")
        guard = dict(guard) if isinstance(guard, Mapping) else None
        if fact.get("kind") == "UNCHANGED":
            if any("pre-existing" in str(e) for e in fact.get("evidence") or ()):
                out.pre_existing.append(ListedSite(
                    kind="pre-existing", symbol=spelling, site=risk_site, reached_at=reached,
                    guard=guard, fact=fact,
                    reason=f"{spelling} is absent on the base ({base}) too: not caused by "
                           "the migration"))
            continue
        broad = False
        if guard is not None:
            kind = guard.get("kind")
            if kind == "version":
                live = (evaluate_guard(str(guard.get("detail") or ""), target_version, present)
                        if target_version else None)
                if live is None or live is False:
                    why = (f"the version test `{guard.get('detail')}` is false on Python "
                           f"{target}: this branch never runs there" if live is False else
                           f"guarded by a version test (`{guard.get('detail')}`) that could "
                           "not be evaluated")
                    out.guarded.append(ListedSite(
                        kind="version-dead" if live is False else "guarded", symbol=spelling,
                        site=risk_site, reached_at=reached, guard=guard, fact=fact, reason=why))
                    continue
                guard = {**guard, "live_on_target": True}
            elif kind == "broad":
                broad = True
            else:
                out.guarded.append(ListedSite(
                    kind="guarded", symbol=spelling, site=risk_site, reached_at=reached,
                    guard=guard, fact=fact,
                    reason=f"{kind}: {guard.get('detail') or 'a try/except around the import'}; "
                           "the code already handles its absence"))
                continue
        group = _SiteGroup(spelling, file, "", risk_site.line, None)
        group.spelling_site = True
        rated = _rate(group, "python", fact, how)
        if rated is None:
            continue
        tier, reason, evidence = rated
        basis = fact.get("basis") or {}
        origin = fact.get("origin")
        if fact.get("kind") == "DEPRECATED":
            if origin and origin != "stdlib":
                tier = RiskTier.MEDIUM
                reason = f"present on Python {target} only through {origin}, not the stdlib"
            elif (fact.get("removed_in") and target_version
                  and (parse_version(fact["removed_in"]) or (0, 0)) > target_version):
                tier = RiskTier.LOW
                reason = (f"deprecated; removed in Python {fact['removed_in']}, after the "
                          f"target {target}")
            else:
                tier = RiskTier.LOW
                reason = f"deprecated since Python {fact.get('deprecated_in') or '?'}, " \
                         "still present on the target"
        else:
            if fact.get("removed_in"):
                reason += f" (removed in Python {fact['removed_in']})"
            if basis.get("new") == "knowledge" and tier is RiskTier.CERTAIN:
                tier = RiskTier.HIGH
                reason += "; absence is from data only (the target was not probed)"
            if basis.get("old") == "import-statement" and tier is RiskTier.CERTAIN:
                tier = RiskTier.HIGH
                reason += "; presence on the base rests on the import statement alone"
            if broad:
                tier = _cap(tier, RiskTier.MEDIUM)
                reason += "; under a broad except, so it degrades silently"
        evidence = [*evidence, f"site: {file}:{line} ({_attr(site, 'kind') or '?'}, "
                    f"reaches the target at {reached or '?'})"]
        out.risks.append(PredictedRisk(
            id=_risk_id(spelling, risk_site), tier=tier, symbol=spelling, package="python",
            site=risk_site, basis="static", reason=reason, fact=fact, facts=[fact],
            evidence=evidence, scope="python", reached_at=reached, guard=guard,
            fix=_fix_kind(fact)))

    _behavior_risks(out, facts, base, target)

    for finding in compile_findings or ():
        file, line = finding.get("file"), finding.get("line")
        kind = str(finding.get("kind") or "SyntaxError")
        if not isinstance(file, str):
            continue
        risk_site = RiskSite(file=file, func="", line=line if isinstance(line, int) else None)
        message = str(finding.get("message") or kind)
        known = finding.get("base")
        if kind == "SyntaxError" and known is True:
            out.pre_existing.append(ListedSite(
                kind="pre-existing", symbol=kind, site=risk_site,
                reason=f"{message}: the file does not compile on the base either"))
            continue
        if kind == "SyntaxError":
            tier = RiskTier.CERTAIN if known is False else RiskTier.HIGH
            reason = f"the file does not compile on Python {target}: {message}" + (
                "" if known is False else "; the base compile was not observed")
        else:
            tier, reason = RiskTier.LOW, f"{kind} on Python {target}: {message}"
        out.risks.append(PredictedRisk(
            id=_risk_id(kind, risk_site), tier=tier, symbol=kind, package="python",
            site=risk_site, basis="static", reason=reason, scope="python",
            reached_at="import",
            evidence=[f"compile check on Python {finding.get('python') or target}"]))

    if runtime_facts:
        _python_runtime_risks(out, runtime_facts, index, target, target_version, root,
                              {(sp, f, ln) for sp, f, ln in seen})

    rewrites = planned_rewrites(changes or ())
    for risk in out.risks:
        if risk.scope != "python":
            continue
        # A whole-module import the plan drops (it became unused after the module's calls
        # were rewritten: ``import imp`` once ``imp.reload`` -> ``importlib.reload``) is
        # fixed, advisory or not: listing it as manual would be a false "needs attention".
        block = _import_removed(risk.site, risk.symbol, rewrites)
        if block is None and risk.fix != "advisory":
            block = _python_fix_for(risk.site, risk.symbol, rewrites)
        if block is not None:
            risk.fixed_by_plan = True
            risk.fixed_by = list(block.recipes)
            risk.evidence.append(
                f"plan: rewrites {risk.site.file}:{block.start}"
                + (f"-{block.end}" if block.end != block.start else ""))
    out.risks.sort(key=lambda r: (r.fixed_by_plan, TIER_ORDER[r.tier], r.site.file,
                                  r.site.line or 0, r.symbol))
    return out


def _behavior_risks(out: PredictionReport, facts: Iterable[Mapping[str, Any]],
                    base: str | None, target: str | None) -> None:
    """A MEDIUM risk at every site a BEHAVIOR fact names: the code there still runs, but what
    it builds changes (a dict's key order); it matters only where that reaches bytes or text,
    so it is never rated higher."""
    for fact in facts:
        if fact.get("kind") != "BEHAVIOR":
            continue
        cause = fact.get("behavior") if isinstance(fact.get("behavior"), Mapping) else {}
        name = str(cause.get("class") or fact.get("symbol") or "?").rsplit(".", 1)[-1]
        changed_in = fact.get("changed_in") or "?"
        old, new = cause.get("old") or [], cause.get("new") or []
        for site in cause.get("sites") or ():
            file, line = site.get("file"), site.get("line")
            if not isinstance(file, str):
                continue
            risk_site = RiskSite(file=file, func="", line=line if isinstance(line, int) else None)
            how = str(site.get("how") or "iteration")
            evidence = [_fact_label("python", fact),
                        f"behavior: {cause.get('cause') or '?'} ({cause.get('effect') or '?'})",
                        f"order on Python {base}: {', '.join(old)}",
                        f"order on Python {target}: {', '.join(new)}",
                        f"site: {file}:{line} ({how})",
                        "medium: the order matters only where it reaches bytes or text",
                        *(e for e in fact.get("evidence") or () if str(e).startswith("hint: "))]
            out.risks.append(PredictedRisk(
                id=_risk_id(str(fact.get("symbol")), risk_site), tier=RiskTier.MEDIUM,
                symbol=str(fact.get("symbol")), package="python", site=risk_site,
                basis="static", scope="python", reached_at="call", fact=dict(fact),
                facts=[dict(fact)], evidence=evidence, fix=_fix_kind(fact),
                reason=(f"Python {changed_in} puts {name}'s base fields first (Python {base}: "
                        f"its own fields first); the {how} here changes key order")))


def python_runtime_facts(baseline: FactSet | Iterable[Fact], root: str | Path | None = None
                         ) -> list[Fact]:
    """The baseline's calls into the standard library made from the project's own files.

    Dropped: a call whose site is outside the project (the runtime's own frames, or stdlib code
    calling stdlib code), a module the capture never traces (``verify.service``'s
    ``_STDLIB_NOT_TRACED``), a module-level ``__getattr__`` (a lookup mechanism, not an API;
    the spelling's own site already carries the removal) and a local function."""
    from pymolt.core import stdlib as stdlib_data
    from pymolt.verify.service import _STDLIB_NOT_TRACED

    facts = baseline.facts if isinstance(baseline, FactSet) else list(baseline)
    out: list[Fact] = []
    for fact in facts:
        top = fact.symbol.split(".")[0]
        if top in _STDLIB_NOT_TRACED or not stdlib_data.is_stdlib(top):
            continue
        if fact.symbol.endswith(".__getattr__") and fact.symbol.count(".") <= 2:
            continue
        if "<" in fact.symbol or not fact.file:
            continue
        if Path(fact.file).is_absolute():
            if root is None or _relative(fact.file, root) == Path(fact.file).as_posix():
                continue                          # outside the project
        out.append(fact)
    return out


def _aliased_python_fact(index: dict[str, dict], group: Any) -> dict | None:
    """A python fact for a spelling the site wrote but the runtime did not record
    (``self.assertEquals(...)`` is recorded as ``unittest.case.TestCase.assertEqual``): the
    receiver's spelling plus an attribute the statement names, among the facts that are not
    UNCHANGED."""
    parent, _, leaf = group.symbol.rpartition(".")
    if not parent or not group.text:
        return None
    parts = parent.split(".")
    parents = list(dict.fromkeys([parent, f"{parts[0]}.{parts[-1]}"]))
    for attr in dict.fromkeys(re.findall(r"\.([A-Za-z_]\w*)", group.text)):
        if attr == leaf:
            continue
        for p in parents:
            fact = index.get(f"{p}.{attr}")
            if fact is not None and fact.get("kind") not in (None, "UNCHANGED"):
                return fact
    return None


def _python_runtime_risks(out: PredictionReport, facts: Iterable[Fact], index: dict[str, dict],
                          target: str | None, target_version: Any, root: str | Path | None,
                          seen: set[tuple]) -> None:
    """Rate every observed call site of a stdlib symbol against the python delta."""
    groups: dict[tuple, _SiteGroup] = {}
    for fact in facts:
        file = _relative(fact.file, root)
        key = (fact.symbol, file, fact.func, fact.line)
        group = groups.get(key)
        if group is None:
            group = groups[key] = _SiteGroup(fact.symbol, file, fact.func, fact.line, fact.text)
            group.spelling_site = True
        group.add(fact)
    for group in groups.values():
        if (group.symbol, group.file, group.line) in seen:
            continue                              # the static pass rated this spelling here
        delta, how = _python_fact(index, group.symbol)
        if delta is None or delta.get("kind") == "UNCHANGED":
            delta = _aliased_python_fact(index, group)
            if delta is None:
                continue
            how = "symbol"
            group.symbol = str(delta.get("symbol") or group.symbol)  # the spelling the site wrote
        basis = delta.get("basis") or {}
        kind, param = delta.get("kind"), delta.get("param")
        if kind == "SIGNATURE" and param and delta.get("new") in _ABSENT:
            if param not in group.passed:
                continue                          # the baseline call did not use it
            both = basis.get("old") == "probe" and basis.get("new") == "probe"
            tier = RiskTier.CERTAIN if both else RiskTier.HIGH
            reason = (f"the call passes {param}, which Python {target} removed from "
                      f"{group.symbol}")
            if not both:
                reason += "; a side was not probed"
            evidence = [_fact_label("python", delta),
                        *(f"delta evidence: {e}" for e in delta.get("evidence") or ()),
                        f"baseline: this site passes {param}"]
        else:
            rated = _rate(group, "python", delta, how)
            if rated is None:
                continue
            tier, reason, evidence = rated
            if kind == "DEPRECATED":
                tier = RiskTier.HIGH if group.warnings else RiskTier.LOW
                reason = (f"deprecated in Python {target} (still present)"
                          + ("; it warned in the baseline" if group.warnings else ""))
            elif kind in ("REMOVED", "MOVED"):
                if basis.get("new") == "knowledge" and tier is RiskTier.CERTAIN:
                    tier = RiskTier.HIGH
                    reason += "; absence is from data only (the target was not probed)"
        evidence = [*evidence, f"baseline: {group.calls} call(s) observed at this site"]
        site = group.site()
        out.risks.append(PredictedRisk(
            id=_risk_id(group.symbol, site), tier=tier, symbol=group.symbol, package="python",
            site=site, basis="trace", reason=reason, fact=delta, facts=[delta],
            evidence=evidence, scope="python", reached_at="call", calls=group.calls,
            warnings=[CallWarning(category=c, message=m, count=n)
                      for (c, m), n in sorted(group.warnings.items())]))


def merge_python_risks(dependency: PredictionReport | None,
                       python: PredictionReport) -> PredictionReport:
    """One report for a plan: the dependency risks (None: no baseline) plus the python ones."""
    if dependency is None:
        merged = PredictionReport(status="partial", notes=[
            "no baseline capture: dependency risks were not predicted (python risks need none)"])
    else:
        merged = dependency
    merged.risks.extend(python.risks)
    merged.deltas.extend(python.deltas)
    merged.guarded.extend(python.guarded)
    merged.pre_existing.extend(python.pre_existing)
    merged.notes.extend(python.notes)
    if python.status != "complete" and merged.status == "complete":
        merged.status = "partial"
    if dependency is not None and dependency.status == "unavailable" and python.risks:
        merged.status = "partial"
    return merged


# ── calibration: predicted vs observed ────────────────────────────────────────────────────


class CalibrationResult(StrEnum):
    HIT = "hit"
    PREVENTED = "prevented"          # fixed by the plan, and the rewrite kept the behaviour
    MISS = "miss"
    NOT_EXERCISED = "not_exercised"


class ObservedEntry(BaseModel):
    category: str
    comparison: str
    symbol: str
    new_symbol: str | None = None
    site: RiskSite


class CalibratedRisk(BaseModel):
    id: str
    tier: RiskTier
    symbol: str
    site: RiskSite
    fixed_by_plan: bool = False
    result: CalibrationResult
    observed: list[ObservedEntry] = Field(default_factory=list)
    note: str | None = None


class Calibration(BaseModel):
    schema_: str = Field(default=CALIBRATION_SCHEMA, alias="schema")
    source: Literal["plan", "baseline"] = "plan"   # where the predictions came from
    plan_id: str | None = None
    predicted: int = 0
    hit: int = 0
    prevented: int = 0
    miss: int = 0
    not_exercised: int = 0
    by_tier: dict[str, dict[str, int]] = Field(default_factory=dict)
    risks: list[CalibratedRisk] = Field(default_factory=list)
    unpredicted: list[ObservedEntry] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    model_config = ConfigDict(serialize_by_alias=True, validate_by_name=True,
                              validate_by_alias=True)


def _hits(entry: FactEntry) -> bool:
    if entry.category in (FactCategory.CHANGED, FactCategory.DISAPPEARED):
        return True
    return entry.category is FactCategory.REWRITTEN and entry.comparison is Comparison.CHANGED


def _compared(entry: FactEntry) -> bool:
    return entry.comparison in (Comparison.EQUAL, Comparison.CHANGED) or entry.category in (
        FactCategory.EQUAL, FactCategory.DRIFT)


def _same_symbol(risk: PredictedRisk, entry: FactEntry) -> bool:
    """The entry is about the risk's call, not another call on the same line."""
    if entry.category is FactCategory.REWRITTEN:
        return True
    return risk.symbol in (entry.symbol, entry.new_symbol)


def _entry_sites(entry: FactEntry) -> list:
    return [s for s in (entry.old_site, entry.new_site) if s is not None][:1]


def _same_site(risk: PredictedRisk, entry: FactEntry, site: Any) -> bool:
    rs = risk.site
    if rs.file != site.file or rs.func != site.func:
        return False
    if rs.line is None:                      # a static (function-level) site: same symbol
        leaf = "." + _leaf(risk.symbol)
        return any(isinstance(s, str) and (s.endswith(leaf) or leaf + "." in s)
                   for s in (entry.symbol, entry.new_symbol))
    if rs.text is not None and site.text is not None:
        return rs.text == site.text
    return rs.line == site.line


def _observed(entry: FactEntry) -> ObservedEntry:
    site = entry.old_site or entry.new_site
    return ObservedEntry(category=entry.category.value, comparison=entry.comparison.value,
                         symbol=entry.symbol, new_symbol=entry.new_symbol,
                         site=RiskSite(file=site.file, func=site.func, line=site.line,
                                       text=site.text))


def calibrate(
    risks: Iterable[PredictedRisk | Mapping[str, Any]],
    fdiff: FactDiff | None,
    *,
    source: Literal["plan", "baseline"] = "plan",
    plan_id: str | None = None,
) -> Calibration:
    """Score each predicted risk against the post-migration fact diff (module docstring)."""
    cal = Calibration(source=source, plan_id=plan_id)
    items = [r if isinstance(r, PredictedRisk) else PredictedRisk.model_validate(r)
             for r in risks or ()]
    entries = list(fdiff.entries) if fdiff is not None else []
    if fdiff is None:
        cal.notes.append("no post-migration fact diff: no prediction could be checked")
    covered: set[int] = set()
    for risk in items:
        on_line = [e for e in entries if any(_same_site(risk, e, s) for s in _entry_sites(e))]
        at_site = [e for e in on_line if risk.site.line is None or _same_symbol(risk, e)]
        covered.update(id(e) for e in at_site)
        if any(_hits(e) for e in at_site):
            result, note = CalibrationResult.HIT, None
            # The line's other calls that moved with it (a ``.to_dict()`` that never ran
            # because ``mean`` now raises) are consequences of the predicted break.
            covered.update(id(e) for e in on_line)
            at_site = on_line
        elif any(_compared(e) for e in at_site):
            if risk.fixed_by_plan:
                result = CalibrationResult.PREVENTED
                note = "fixed by the plan: the rewrite kept the behaviour at this site"
            else:
                result, note = CalibrationResult.MISS, None
        else:
            result = CalibrationResult.NOT_EXERCISED
            note = ("only uncomparable/unpaired entries at this site" if at_site else
                    "the post-migration capture did not compare this site")
        shown = [e for e in at_site if e.category is not FactCategory.EQUAL or e.notes]
        cal.risks.append(CalibratedRisk(
            id=risk.id, tier=risk.tier, symbol=risk.symbol, site=risk.site,
            fixed_by_plan=risk.fixed_by_plan, result=result,
            observed=[_observed(e) for e in shown[:10]], note=note))
        tier = cal.by_tier.setdefault(risk.tier.value, {r.value: 0 for r in CalibrationResult})
        tier[result.value] += 1
    cal.predicted = len(cal.risks)
    cal.hit = sum(1 for r in cal.risks if r.result is CalibrationResult.HIT)
    cal.prevented = sum(1 for r in cal.risks if r.result is CalibrationResult.PREVENTED)
    cal.miss = sum(1 for r in cal.risks if r.result is CalibrationResult.MISS)
    cal.not_exercised = cal.predicted - cal.hit - cal.prevented - cal.miss
    cal.unpredicted = [_observed(e) for e in entries if e.fails and id(e) not in covered]
    return cal


def baseline_predictions(baseline_trace: str | Path, root: str | Path,
                         changes: Iterable[Any] = ()) -> PredictionReport:
    """Predictions from the baseline capture alone (its runtime warnings; no delta facts):
    what ``verify`` checks when no plan stored predictions. ``changes``: an applied plan's
    file changes, for ``fixed_by_plan``."""
    try:
        baseline = load_facts(baseline_trace, root=root)
    except OSError as exc:
        return PredictionReport(status="unavailable",
                                notes=[f"cannot read the baseline capture: {exc}"])
    return predict_risks(baseline, (), changes=changes, root=root)
