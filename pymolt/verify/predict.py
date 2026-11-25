"""Predicted risks: what the migration will probably break, per call site, before it runs.

HOST side (pydantic). Joins the **baseline facts** (symbol, call site, the input with its
``defaulted`` parameters, outcomes, runtime warnings) with the **delta facts** the local delta
engine computed for the plan's migrations (``pymolt.delta/1``: REMOVED / MOVED / SIGNATURE /
DEFAULT / DEPRECATED per symbol and parameter) and rates every call site:

=========  ==============================================================================
certain    a name the site writes is REMOVED, or MOVED so that the name itself is gone: a
           name on its statement (the static contact map; its function's, for a call that
           lands elsewhere, like a generator's body), or the attribute it calls on its
           receiver, whose class is still there in the new version
high       a parameter the site left at its default has a DEFAULT change between concrete
           values, or one resolved at call time (None / a sentinel) that the site's own
           baseline warning names; or the site emitted a deprecation-family warning
medium     a SIGNATURE change touching a parameter the site passes (or a new required one),
           or a DEFAULT change to or from None / a sentinel that no warning at the site names
           (a call-time default: the behaviour may well not change)
low        DEPRECATED in the old version without a recorded warning at the site, a MOVED
           name the site does not write, a class-level SIGNATURE change, any change on a
           symbol reached only statically (BLIND: the baseline never ran it), or an
           ``unverified`` existence fact (below)
=========  ==============================================================================

A dependency site is rated by what it writes, not by where the old version defined the
callee: the baseline records ``df.groupby(...)`` as ``NDFrame.groupby``, and pandas moving
``groupby`` off ``NDFrame`` breaks nothing while ``DataFrame.groupby`` resolves. A REMOVED or
MOVED fact (or a deprecation that only accessing the name warns about) is about a name; it
decides a site only when the site writes that name. About another name (the internal one the
old version defined the call under), it says nothing when the site's own spelling was probed
and still resolves, and otherwise gives a LOW risk marked ``unverified``: what is unknown, and
the spelling to check (``spelling``). Object facts (signature, default, deprecation of the
object) apply through any spelling of the object, but only those naming the site's own
spelling once that was probed.

One risk per (site, symbol) at its strongest tier; ``fact`` is the delta fact that decided
it (None when the warning alone did), ``facts`` every delta fact that matched, ``evidence``
the reasons in words. ``fixed_by_plan`` says a planned codemod rewrites the site: the line
changes and the symbol's name leaves it, or the import that binds the name the site reads now
binds another target (an import move; the site's line stays).

``calibrate`` scores the predictions against the post-migration fact diff, using the
entries at the risk's site for the risk's own symbol (a line can hold several calls; a
REWRITTEN entry counts whatever its symbol, since position pairing ignores names): a risk is
a **hit** when such an entry CHANGED, DISAPPEARED, or was REWRITTEN with a different
outcome; **prevented** when the plan fixed the site and the rewrite kept the behaviour (the
prediction is not disproved: the plan acted on it); a **miss** when the site was compared
and did not change; **not exercised** when the post-migration capture never compared it.
The other calls on a hit risk's line count as its consequences; failing entries no risk
covers are listed as **unpredicted**.

Each risk's ``id`` names it without a line number (``risk_id``): symbol, file, function,
the site's line text and ``site.nth``, its ordinal among the report's sites that share those,
so an edit that moves lines keeps the ids. The plan persists the ids it predicted; a plan
saved before (``schema: pymolt.risks/1``) keeps its line-based ones.

Never fabricates: without delta facts the prediction rests on the baseline warnings alone
and says so (``status``/``notes``); a trace that predates warning capture is noted too.
"""

from __future__ import annotations

import ast
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

RISKS_SCHEMA = "pymolt.risks/2"     # /2: ids without line numbers (``risk_id``)
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
    line: int | None = None                  # where it is: never part of the risk's id
    text: str | None = None
    # The site's ordinal among the report's sites with the same symbol, file, func and text
    # (in line order, dependency sites first): it tells two equal call lines apart in the id.
    nth: int = 0

    def label(self) -> str:
        return f"{self.file}:{self.line if self.line is not None else '?'}"


class PredictedRisk(BaseModel):
    id: str                                  # ``risk_id``: symbol + site, no line number
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
    # The plan step that addresses the risk (a pymolt step) or leaves it to an agent or a
    # person (a manual step); None outside a plan, or in a plan made before steps.
    step_id: str | None = None
    # The name this site writes for the call (a name on its statement, or
    # ``<receiver's class>.<attribute>``), when known: the one a REMOVED / MOVED fact decided
    # on, or for an ``unverified`` risk the one to check in the target environment.
    spelling: str | None = None
    # The deciding REMOVED / MOVED fact is about a name the site does not write (the one the old
    # version defined the call under), and the site's own spelling was not found resolving:
    # whether the call breaks is unknown. Never above LOW; its step claims no successor.
    unverified: bool = False


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


class _Rebind(BaseModel):
    """A module-level name the plan's import rewrite binds to another target. ``gone``: what it
    no longer names (``pandas.util.testing.assert_frame_equal``); ``uses``: every place the old
    file reads that import binding, as ``(first line, last line, function)`` of the expression
    the read heads (``assert_frame_equal(a, b)``), the import statement itself included.
    ``function`` is the innermost ``def``/``class`` name, or the module's leaf at module
    level, as static contacts name their caller."""

    name: str
    gone: list[str]
    block: _Block
    uses: list[tuple[int, int, str]] = Field(default_factory=list)


def _module_leaf(path: str) -> str:
    p = Path(path)
    return p.parent.name if p.stem == "__init__" else p.stem


def _import_targets(node: Any, name: str) -> set[str]:
    """What an ``import`` / ``from ... import`` statement binds ``name`` to (absolute imports
    only: a relative one names the project's own code)."""
    import libcst as cst
    from libcst.helpers import get_full_name_for_node

    out: set[str] = set()
    if isinstance(node, cst.ImportFrom):
        if node.relative or node.module is None or isinstance(node.names, cst.ImportStar):
            return out
        module = get_full_name_for_node(node.module)
        for alias in node.names:
            bound = alias.asname.name.value if alias.asname else get_full_name_for_node(
                alias.name)
            if bound == name and module:
                out.add(f"{module}.{get_full_name_for_node(alias.name)}")
    elif isinstance(node, cst.Import):
        for alias in node.names:
            dotted = get_full_name_for_node(alias.name) or ""
            if alias.asname is not None:
                if alias.asname.name.value == name:
                    out.add(dotted)
            elif dotted.split(".")[0] == name:
                out.add(name)
    return out


def _import_bindings(source: str) -> tuple[Any, dict[str, list[Any]]] | None:
    """``(metadata wrapper, {name: [Assignment]})`` of the module scope's names bound only
    by imports. A name also bound by anything else is left out: which binding a read sees
    is then decided at run time. None when the source does not parse."""
    import libcst as cst
    from libcst.metadata import (
        Assignment,
        GlobalScope,
        MetadataWrapper,
        ParentNodeProvider,
        PositionProvider,
        ScopeProvider,
    )

    try:
        wrapper = MetadataWrapper(cst.parse_module(source))
        scopes = wrapper.resolve(ScopeProvider)
        wrapper.resolve(ParentNodeProvider)
        wrapper.resolve(PositionProvider)
    except Exception:  # noqa: BLE001 - an unparseable file credits nothing
        return None
    module_scope = next((s for s in scopes.values() if isinstance(s, GlobalScope)), None)
    if module_scope is None:
        return None
    bindings: dict[str, list[Any]] = defaultdict(list)
    other: set[str] = set()
    for assignment in module_scope.assignments:
        if isinstance(assignment, Assignment) and isinstance(
                assignment.node, cst.Import | cst.ImportFrom):
            bindings[assignment.name].append(assignment)
        else:
            other.add(assignment.name)
    return wrapper, {n: a for n, a in bindings.items() if n not in other}


def _rebinds_for(path: str, old: str, new: str, block_for) -> list[_Rebind]:
    """The names a planned change of one file binds to new import targets (see ``_Rebind``)."""
    import libcst as cst
    from libcst.metadata import ParentNodeProvider, PositionProvider

    before, after = _import_bindings(old), _import_bindings(new)
    if before is None or after is None:
        return []
    wrapper, old_bound = before
    _new_wrapper, new_bound = after
    parents = wrapper.resolve(ParentNodeProvider)
    positions = wrapper.resolve(PositionProvider)
    module_leaf = _module_leaf(path)

    def targets(assignments) -> set[str]:
        return {t for a in assignments for t in _import_targets(a.node, a.name)}

    def statement(node):
        while node is not None and not isinstance(node, cst.SimpleStatementLine):
            node = parents.get(node)
        return node

    def owner(node) -> str:
        while node is not None:
            node = parents.get(node)
            if isinstance(node, cst.FunctionDef | cst.ClassDef):
                return node.name.value
        return module_leaf

    def head(node):
        """The outermost expression ``node`` heads: ``x`` in ``x.y.z(...)``."""
        while True:
            parent = parents.get(node)
            if isinstance(parent, cst.Attribute) and parent.value is node:
                node = parent
            elif isinstance(parent, cst.Call) and parent.func is node:
                node = parent
            else:
                return node

    out: list[_Rebind] = []
    for name in sorted(old_bound.keys() & new_bound.keys()):
        gone = sorted(targets(old_bound[name]) - targets(new_bound[name]))
        if not gone:
            continue
        uses: list[tuple[int, int, str]] = []
        lines: set[int] = set()
        for assignment in old_bound[name]:
            stmt = statement(assignment.node)
            if stmt is not None:
                pos = positions[stmt]
                uses.append((pos.start.line, pos.end.line, owner(stmt)))
                lines.update(range(pos.start.line, pos.end.line + 1))
            for access in assignment.references:
                # A read every one of whose bindings is an import of this name.
                if not all(r in old_bound[name] for r in access.referents):
                    continue
                pos = positions.get(head(access.node))
                if pos is not None:
                    uses.append((pos.start.line, pos.end.line, owner(access.node)))
        block = block_for(lines)
        if block is not None:
            out.append(_Rebind(name=name, gone=gone, block=block, uses=sorted(set(uses))))
    return out


def planned_rebinds(changes: Iterable[Any],
                    rewrites: Mapping[str, list[_Block]]) -> dict[str, list[_Rebind]]:
    """Per (project-relative) file of a plan's frozen changes, the module-level names whose
    import the plan rewrites to bind another target (``from a.b import f`` -> ``from a.c
    import f``). ``rewrites``: ``planned_rewrites(changes)``, whose blocks hold the imports."""
    out: dict[str, list[_Rebind]] = {}
    for change in changes or ():
        path = _attr(change, "path")
        old, new = _attr(change, "old_source"), _attr(change, "new_source")
        if not isinstance(path, str) or not isinstance(old, str) or not isinstance(new, str):
            continue
        file = Path(path).as_posix()
        blocks = rewrites.get(file) or ()

        def block_for(lines: set[int], blocks=blocks) -> _Block | None:
            return next((b for b in blocks if any(b.start <= ln <= b.end for ln in lines)),
                        None)

        found = _rebinds_for(file, old, new, block_for)
        if found:
            out[file] = found
    return out


def _risk_spellings(risk: Any) -> set[str]:
    """The names a risk's symbol is known by: its own, and that of each delta fact it matched
    (the public spelling an import names)."""
    names = {risk.symbol}
    for fact in [risk.fact, *(risk.facts or ())]:
        if isinstance(fact, Mapping) and isinstance(fact.get("symbol"), str):
            names.add(fact["symbol"])
    return names


def _rebind_fix_for(risk: Any, rebinds: Mapping[str, list[_Rebind]]) -> _Block | None:
    """The planned import rewrite that fixes this site without touching its line: the site
    reads a name whose import now binds another target, and its symbol was reached through
    the old one. Binding-aware: a traced site counts when an expression on its line reads that
    import binding (not a local of the same name); a static site, when its function does."""
    site = risk.site
    spellings = _risk_spellings(risk)
    for rebind in rebinds.get(site.file) or ():
        if not any(s == g or s.startswith(g + ".") for s in spellings for g in rebind.gone):
            continue
        if site.line is not None:
            if any(first <= site.line <= last for first, last, _ in rebind.uses):
                return rebind.block
        elif any(func == site.func for _, _, func in rebind.uses):
            return rebind.block
    return None


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
        # The classes of the receivers the calls were made on (``type(self)`` / ``cls``).
        self.receivers: set[str] = set()

    def add(self, fact: Fact) -> None:
        self.calls += fact.count
        self.passed |= _passed_params(fact.input)
        touched = _touched()
        recv = touched._receiver_type(fact) if touched is not None else None
        if recv is not None:
            self.receivers.add(recv)
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


_TOUCHED: list[Any] = []


def _touched():
    """``pymolt.delta.touched`` (the receiver readers), or None when the delta engine is not
    importable: the prediction then reads sites from the contact map alone."""
    if not _TOUCHED:
        try:
            from pymolt.delta import touched
        except ImportError:
            return None
        _TOUCHED.append(touched)
    return _TOUCHED[0]


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


def _names(fact: Mapping[str, Any]) -> set[str]:
    return {n for n in [fact.get("symbol"), *(fact.get("aliases") or ())]
            if isinstance(n, str) and n}


class _DeltaIndex:
    """Every non-UNCHANGED delta fact by each of its spellings, and which spellings the delta
    compared at all (``probed``) and found gone (``gone``: REMOVED / MOVED)."""

    def __init__(self, reports: Iterable[Mapping[str, Any]]):
        self.by_name: dict[str, list[tuple[str, dict]]] = defaultdict(list)
        self.probed: set[str] = set()
        self.gone: set[str] = set()
        for rep in reports:
            package = str(rep.get("package") or "")
            for fact in rep.get("facts") or ():
                if not isinstance(fact, Mapping) or fact.get("kind") is None:
                    continue
                names = _names(fact)
                self.probed |= names
                if fact.get("kind") in ("REMOVED", "MOVED"):
                    self.gone |= names
                if fact.get("kind") == "UNCHANGED":
                    continue
                fact = dict(fact)
                for name in names:
                    self.by_name[name].append((package, fact))

    def __bool__(self) -> bool:
        return bool(self.by_name)

    def resolves(self, name: str) -> bool:
        """The delta compared ``name`` and it still resolves in the new version."""
        return name in self.probed and name not in self.gone

    def match(self, symbol: str) -> list[tuple[str, dict, str]]:
        """``(package, fact, how)`` for every fact about ``symbol``: the symbol itself, the
        class of a constructor, or an enclosing object (class, module) that was removed. A
        name nested in a function (``f.<locals>.g``) has no enclosing object: the function
        that made it going away says nothing about the name the site reached it by."""
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
        if "<" not in symbol:
            for i in range(len(parts) - 1, 1, -1):
                add(self.by_name.get(".".join(parts[:i]), []), "enclosing", True)
        return out

    def about(self, names: Iterable[str]) -> list[tuple[str, dict, str]]:
        """``(package, fact, "spelling")`` for every fact naming one of ``names``."""
        out: list[tuple[str, dict, str]] = []
        seen: set[int] = set()
        for name in names:
            for package, fact in self.by_name.get(name, ()):
                if id(fact) not in seen:
                    seen.add(id(fact))
                    out.append((package, fact, "spelling"))
        return out


# ── what a site writes ────────────────────────────────────────────────────────────────────

_CTOR = ("__init__", "__new__")
_ACCESS_TAGS = ("getattr:", "access-warning:")


def _about_a_name(fact: Mapping[str, Any]) -> bool:
    """A fact about a name rather than the object it names: REMOVED, MOVED, or a deprecation
    that only accessing that name warns about (a module ``__getattr__``, an access warning,
    ``numpy.core`` -> ``numpy._core``)."""
    kind = fact.get("kind")
    if kind in ("REMOVED", "MOVED"):
        return True
    if kind != "DEPRECATED":
        return False
    tags = [str(e).removeprefix("deprecated-in-new:") for e in fact.get("evidence") or ()
            if not str(e).startswith("warning:")]
    return bool(tags) and all(t.startswith(_ACCESS_TAGS) for t in tags)


def _header_names(node: ast.AST) -> set[str]:
    """Identifiers a statement spells: names, attributes, imported names. A compound
    statement counts by its header only (its body holds statements of their own)."""
    if isinstance(node, ast.For | ast.AsyncFor):
        parts: list[Any] = [node.target, node.iter]
    elif isinstance(node, ast.While | ast.If):
        parts = [node.test]
    elif isinstance(node, ast.With | ast.AsyncWith):
        parts = list(node.items)
    elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        parts = [*node.decorator_list, node.args, *( [node.returns] if node.returns else [])]
    elif isinstance(node, ast.ClassDef):
        parts = [*node.decorator_list, *node.bases, *node.keywords]
    elif isinstance(node, ast.Try) or type(node).__name__ == "TryStar":
        parts = []
    elif isinstance(node, ast.Match):
        parts = [node.subject]
    else:
        parts = [node]
    out: set[str] = set()
    for part in parts:
        for n in ast.walk(part):
            if isinstance(n, ast.Attribute):
                out.add(n.attr)
            elif isinstance(n, ast.Name):
                out.add(n.id)
            elif isinstance(n, ast.alias):
                out.add((n.asname or n.name).split(".")[-1])
                out.add(n.name.split(".")[-1])
    return out


class _Statements:
    """The project's files, each parsed once: what the statement at a line spells."""

    def __init__(self, root: str | Path | None):
        self.root = Path(root) if root is not None else None
        self._trees: dict[str, list[ast.stmt] | None] = {}

    def _stmts(self, file: str) -> list[ast.stmt] | None:
        if file not in self._trees:
            found = None
            if self.root is not None and file and not Path(file).is_absolute():
                try:
                    tree = ast.parse((self.root / file).read_text(encoding="utf-8"))
                    found = [n for n in ast.walk(tree) if isinstance(n, ast.stmt)]
                except (OSError, SyntaxError, ValueError, UnicodeDecodeError):
                    found = None
            self._trees[file] = found
        return self._trees[file]

    def names(self, file: str, line: int | None, text: str | None) -> set[str] | None:
        """Identifiers the statement at ``line`` spells; from the site's line text when the
        file cannot be read; None when neither is known."""
        stmts = self._stmts(file) if line is not None else None
        if stmts:
            covering = [n for n in stmts
                        if n.lineno <= line <= (n.end_lineno or n.lineno)]  # type: ignore[operator]
            if covering:
                node = max(covering, key=lambda n: (n.lineno, -(n.end_lineno or n.lineno)))
                return _header_names(node)
        if text is None:
            return None
        return set(re.findall(r"[A-Za-z_]\w*", text))


class _StaticIndex:
    """The static contacts by file, with the statement lines each one is on, and by the
    function that makes them."""

    def __init__(self, contacts: Iterable[Any], root: str | Path | None):
        self.by_file: dict[str, list[tuple[int, int, str]]] = defaultdict(list)
        self.by_func: dict[tuple[str, str], set[str]] = defaultdict(set)
        for contact in contacts:
            target = _attr(contact, "target")
            if not isinstance(target, str) or not target:
                continue
            file = _relative(_attr(contact, "file"), root)
            caller = _attr(contact, "caller")
            if isinstance(caller, str) and caller:
                self.by_func[(file, caller.rsplit(".", 1)[-1])].add(target)
            spans = _attr(contact, "spans") or ()
            placed = False
            for span in spans:
                if isinstance(span, list | tuple) and len(span) == 3:
                    self.by_file[file].append((int(span[1]), int(span[2]), target))
                    placed = True
            line = _attr(contact, "line")
            if not placed and isinstance(line, int):
                self.by_file[file].append((line, line, target))

    def on(self, file: str, line: int | None) -> set[str]:
        """The dependency names the statement at ``file:line`` writes."""
        if line is None:
            return set()
        return {t for first, last, t in self.by_file.get(file, ()) if first <= line <= last}

    def in_function(self, file: str, func: str) -> set[str]:
        """The dependency names a function writes, on any of its statements."""
        return set(self.by_func.get((file, func), ()))


class _SiteNames:
    """What one baseline call site writes for its call.

    ``static``: the dependency names its statement writes (the contact map); ``own``: those
    that are this call (the same last name, or the class for a constructor) and the call's
    receiver spellings, ``<class>.<attribute>`` under the receiver's class and each public
    candidate of it; ``spelled``: the statement names the attribute the call was made by."""

    def __init__(self, group: _SiteGroup, statements: _Statements, statics: _StaticIndex,
                 receiver_of) -> None:
        touched = _touched()
        symbol = group.symbol
        leaf = symbol.rsplit(".", 1)[-1]
        classes = sorted(c for c in (receiver_of(r) for r in group.receivers) if c)
        self.static = statics.on(group.file, group.line)
        call = leaf
        if leaf in _CTOR:
            call = (classes[0] if classes else symbol.rsplit(".", 2)[0]).rsplit(".", 1)[-1]
        self.receiver: list[str] = []
        if "<" not in symbol and touched is not None:
            for cls in classes:
                if "<" not in cls:
                    self.receiver += [f"{c}.{leaf}" for c in touched.public_candidates(cls)]
        self.own = [t for t in sorted(self.static) if t.rsplit(".", 1)[-1] == call] \
            + self.receiver
        named = statements.names(group.file, group.line, group.text) or set()
        self.spelled = call in named or any(t.rsplit(".", 1)[-1] == call for t in self.static)
        # A call recorded at a statement that does not name it and has no receiver: a
        # generator's body runs where it is consumed (``max(parts)`` for ``parts =
        # nx.connected_component_subgraphs(G)``). The names its function writes elsewhere
        # are the ones that made the call.
        self.function: set[str] = set()
        if not self.spelled and not classes:
            self.function = {t for t in statics.in_function(group.file, group.func)
                             if t.rsplit(".", 1)[-1] == call}
        # A protocol method (``__setitem__``, ``__iter__``, ``__array_wrap__``): the statement
        # makes the call without naming it.
        self.protocol = leaf.startswith("__") and leaf.endswith("__") and leaf not in _CTOR


def _written(fact: Mapping[str, Any], site: _SiteNames) -> str | None:
    """The name of ``fact`` this site writes: one on its statement (or a module or class a
    name on it is read through: ``Cls`` of ``Cls.run``), or a receiver spelling of an attribute
    the statement names whose class still resolves in the new version (``vanished_at`` says
    the name alone went). None: the fact is about another name."""
    names = _names(fact)
    hit = sorted(n for n in names
                 if any(t == n or t.startswith(n + ".") for t in site.static | site.function))
    if hit:
        return hit[0]
    if not site.spelled:
        return None
    vanished = fact.get("vanished_at") if isinstance(fact.get("vanished_at"), Mapping) else {}
    for name in site.receiver:
        if name in names and vanished.get(name) == name:
            return name
    return None


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
            if param in _RECEIVERS:
                # ``update(*args, **kwds)`` -> ``update(self, other=(), /, **kwds)``: a bound
                # method call passes its receiver; ``self`` is never a parameter it misses.
                return None
            evidence.append(f"baseline: this site does not pass the new parameter {param}")
            return RiskTier.MEDIUM, f"a new required parameter {param} that this site does " \
                "not pass", evidence
        return None
    if kind == "DEPRECATED":
        if param:
            # A deprecated parameter concerns only the sites that pass it.
            if param not in group.passed:
                return None
            evidence.append(f"baseline: this site passes {param}")
            return (RiskTier.LOW, f"the parameter {param} is deprecated in the new version"
                    + (f"; use {fact['successor']}" if fact.get("successor") else ""), evidence)
        if fact.get("deprecated_in_old"):
            return (RiskTier.LOW, "deprecated in the old version, no warning recorded at this "
                    "site", evidence)
        return RiskTier.LOW, "deprecated in the new version", evidence
    return None


def risk_id(symbol: str, site: RiskSite) -> str:
    """``sha1("v2|" + symbol|file|func|site_text|nth)[:12]``: the risk named by its call and the
    text of its line (an anchor's ``site_text``), not the line number, so lines moving (a hand
    edit above, an earlier step) keep it. A static site (no line) has no text and ``nth`` 0."""
    raw = "|".join((symbol, site.file, site.func, site.text or "", str(site.nth)))
    return hashlib.sha1(("v2|" + raw).encode("utf-8")).hexdigest()[:12]


def assign_risk_ids(risks: Iterable[PredictedRisk]) -> None:
    """Set every risk's ``site.nth`` and ``id``. ``nth`` counts the sites that share symbol,
    file, func and text in line order, dependency risks before python ones, so the ids are
    unique within ``risks``, two equal call lines in one function keep theirs when lines
    shift, and a dependency risk's id never depends on the python risks beside it."""
    same: dict[tuple[str, str, str, str], list[PredictedRisk]] = defaultdict(list)
    for risk in risks:
        same[(risk.symbol, risk.site.file, risk.site.func, risk.site.text or "")].append(risk)
    for group in same.values():
        group.sort(key=lambda r: (r.scope != "dependency",
                                  -1 if r.site.line is None else r.site.line))
        for n, risk in enumerate(group):
            risk.site.nth = n
            risk.id = risk_id(risk.symbol, risk.site)


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

    contacts = list(static_contacts) if static_contacts is not None else None
    statements = _Statements(root)
    statics = _StaticIndex(contacts or (), root)
    receiver_of = _receiver_resolver(root)
    matched_facts: set[int] = set()
    for group in groups.values():
        best: tuple[RiskTier, str, dict | None, str | None, str | None] | None = None
        evidence: list[str] = []
        facts: list[dict] = []
        if group.warnings:
            evidence.extend(_warning_texts(group.warnings))
            (cat, msg), _n = sorted(group.warnings.items())[0]
            best = (RiskTier.HIGH, f"it emitted {cat} in the baseline: {msg}", None, None, None)
        site = _SiteNames(group, statements, statics, receiver_of) if index else None
        # The site's own spellings the delta found still resolving: a change on another
        # spelling of the same old object does not reach the site. (A spelling of its own
        # that is gone exhibits nothing, so then the object's facts apply as they are.)
        own_probed = {n for n in site.own if index.resolves(n)} if site is not None else set()
        internal: list[tuple[str, dict]] = []
        found = index.match(group.symbol) if index else []
        if site is not None:
            known = {id(f) for _p, f, _h in found}
            found += [item for item in index.about(site.own) if id(item[1]) not in known]
        for package, fact, how in found:
            written = None
            if _about_a_name(fact):
                written = _written(fact, site) if site is not None else None
                if written is None:
                    # Not this site's name: the statements that write it (if any) are rated
                    # from the contact map (``_add_static_risks``).
                    internal.append((package, fact))
                    continue
            elif own_probed and not _names(fact) & own_probed:
                continue            # a change on another spelling of the same old object
            matched_facts.add(id(fact))
            rated = _rate(group, package, fact, how)
            if rated is None:
                continue
            tier, reason, ev = rated
            if (fact.get("kind") == "DEPRECATED" and fact.get("deprecated_in_old")
                    and group.warnings):
                tier = RiskTier.HIGH            # the warning at this site confirms it
                reason = "deprecated in the old version, and it warned at this site"
            if fact.get("kind") == "MOVED" and written is not None:
                # The site writes the very spelling that vanished: the call fails as written
                # (the same object lives on elsewhere, so the fix is a rename).
                tier = RiskTier.CERTAIN
                reason = f"the spelling {written} no longer exists" + (
                    f"; the same object is at {fact['successor']}" if fact.get("successor")
                    else "")
            if written is not None and written != group.symbol:
                ev = [*ev, f"site: this statement writes {written}"]
            facts.append(fact)
            evidence.extend(e for e in ev if e not in evidence)
            if best is None or TIER_ORDER[tier] < TIER_ORDER[best[0]] or (
                    best[2] is None and TIER_ORDER[tier] == TIER_ORDER[best[0]]):
                best = (tier, reason, fact, package, written)
        unverified = False
        # A deprecation of another name (its access warns) is no question about this site.
        internal = [(p, f) for p, f in internal if f.get("kind") in ("REMOVED", "MOVED")]
        if internal and site is not None:
            resolving = [n for n in site.own if index.resolves(n)]
            for package, fact in internal:
                facts.append(fact)
                if resolving:
                    evidence.append(f"delta: {fact.get('symbol')} is gone in the new version, "
                                    f"but {resolving[0]}, what this site calls, still resolves")
            # Only a call the statement makes: one it names, or one made on a receiver it
            # handles (a protocol method, ``dict(row)`` calling ``row.keys``). A function the
            # statement neither names nor reaches through a receiver ran under it from compiled
            # code or a resumed generator (``mv.any()`` -> numpy's ``_methods._any``).
            if not resolving and (site.spelled or site.protocol or site.receiver):
                package, fact = internal[0]
                reason, check = _unverified_reason(group, fact, site, index)
                evidence.extend(e for e in [_fact_label(package, fact), reason]
                                if e not in evidence)
                if best is None:
                    best = (RiskTier.LOW, reason, fact, package, check)
                    unverified = True
        if best is None:
            continue
        tier, reason, fact, package, spelling = best
        if fact is not None and group.warnings:
            reason += "; it warned in the baseline"
        report.risks.append(PredictedRisk(
            id="", tier=tier, symbol=group.symbol,
            package=package or (_package_of(facts) if facts else None), site=group.site(),
            basis="trace", reason=reason, fact=fact, facts=facts, evidence=evidence,
            warnings=[CallWarning(category=c, message=m, count=n)
                      for (c, m), n in sorted(group.warnings.items())],
            calls=group.calls, spelling=spelling, unverified=unverified))

    if index and contacts is not None:
        _add_static_risks(report, index, matched_facts, contacts, root)
    assign_risk_ids(report.risks)

    rebinds = planned_rebinds(changes or (), rewrites) if rewrites else {}
    for risk in report.risks:
        block = _fix_for(risk.site, risk.symbol, rewrites) or _rebind_fix_for(risk, rebinds)
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


def _receiver_resolver(root: str | Path | None):
    """``class -> the dependency class it stands for``: itself, or for a project class its
    first dependency base (``touched.dependency_bases``; None without one). The project's
    classes are parsed once, and only when a receiver's package is in the project."""
    touched = _touched()
    cache: dict[str, str | None] = {}
    classes: dict[str, Any] | None = None

    here = Path(root) if root is not None else None

    def in_project(name: str) -> bool:
        top = name.split(".", 1)[0]
        return here is not None and ((here / top).is_dir() or (here / f"{top}.py").is_file())

    def resolve(name: str) -> str | None:
        nonlocal classes
        if name in cache:
            return cache[name]
        out: str | None = name
        if touched is not None and in_project(name):
            if classes is None:
                try:
                    classes, _ = touched.project_classes(here)
                except OSError:
                    classes = {}
            # A project name: its first dependency base, else no dependency class at all
            # (``Base = declarative_base()`` is a project name, not a class it defines).
            bases = [b for b in (touched.dependency_bases(name, classes)
                                 if name in classes else []) if not in_project(b)]
            out = bases[0] if bases else None
        cache[name] = out
        return out

    return resolve


def _unverified_reason(group: _SiteGroup, fact: Mapping[str, Any], site: _SiteNames,
                       index: _DeltaIndex) -> tuple[str, str | None]:
    """``(reason, spelling to check)`` of a site whose only existence fact is about a name it
    does not write. The spelling is given only where its resolving settles the question: the
    attribute under the receiver's recorded class, which the old version resolved, when the
    delta did not probe it. A protocol call, or a class gone too, settles nothing that way."""
    gone = str(fact.get("symbol") or group.symbol)
    leaf = group.symbol.rsplit(".", 1)[-1]
    probed_gone = [n for n in site.own if n in index.gone]
    if probed_gone and not site.spelled:
        return (f"{probed_gone[0]} is gone in the new version, but the statement does not call "
                f"`{leaf}` by name (Python or compiled code makes this call), so whether the "
                "statement breaks is unknown", None)
    if probed_gone:
        return (f"{probed_gone[0]} is gone in the new version together with its class; the "
                "receiver may be of another class there, so whether the call still resolves "
                "is unknown", None)
    what = f"the name the old version defined it under ({gone}) is gone in the new version"
    check = site.receiver[0] if site.receiver else None
    if check is not None and not site.protocol:
        return (f"{what}; {check}, the name this site calls, was not probed, so whether the "
                "call still resolves is unknown", check)
    return (f"{what}; the name this site calls it by was not probed, so whether the call "
            "still resolves is unknown", None)


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
            id="", tier=RiskTier.LOW, symbol=target,
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
            id="", tier=tier, symbol=spelling, package="python",
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
            id="", tier=tier, symbol=kind, package="python",
            site=risk_site, basis="static", reason=reason, scope="python",
            reached_at="import",
            evidence=[f"compile check on Python {finding.get('python') or target}"]))

    if runtime_facts:
        _python_runtime_risks(out, runtime_facts, index, target, target_version, root,
                              {(sp, f, ln) for sp, f, ln in seen})
    assign_risk_ids(out.risks)

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
                id="", tier=RiskTier.MEDIUM,
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


def _aliased_python_fact(index: dict[str, dict], group: Any,
                         recorded: set[tuple] | None = None) -> dict | None:
    """A python fact for a spelling the site wrote but the runtime did not record
    (``self.assertEquals(...)`` is recorded as ``unittest.case.TestCase.assertEqual``): the
    receiver's spelling plus an attribute the statement names. Only a fact about the name
    (REMOVED, MOVED, DEPRECATED): the recorded call's arguments are another callee's, so they
    say nothing about the parameters of the spelling the site wrote (uvloop's compiled
    ``ensure_future(coro, loop=self)`` recorded at ``await asyncio.sleep(0)`` never passed
    ``loop`` to ``sleep``). ``recorded``: the ``(symbol, file, line)`` of every call recorded
    at a site; a spelling recorded there itself is another call, not an alias."""
    parent, _, leaf = group.symbol.rpartition(".")
    if not parent or not group.text:
        return None
    parts = parent.split(".")
    parents = list(dict.fromkeys([parent, f"{parts[0]}.{parts[-1]}"]))
    for attr in dict.fromkeys(re.findall(r"\.([A-Za-z_]\w*)", group.text)):
        if attr == leaf:
            continue
        for p in parents:
            spelling = f"{p}.{attr}"
            if recorded is not None and (spelling, group.file, group.line) in recorded:
                continue
            fact = index.get(spelling)
            if fact is not None and fact.get("kind") in ("REMOVED", "MOVED", "DEPRECATED"):
                return fact
    return None


def _python_runtime_risks(out: PredictionReport, facts: Iterable[Fact], index: dict[str, dict],
                          target: str | None, target_version: Any, root: str | Path | None,
                          seen: set[tuple]) -> None:
    """Rate every observed call site of a stdlib symbol against the python delta.

    Only the calls the statement makes count: a callee it names (or a class it constructs).
    A call recorded at the line that the line did not write (made from compiled code, like
    uvloop's ``_set_running_loop``, or the body of a generator its ``for`` resumed, like
    ``dis._get_instructions_bytes``) is rated only as an alias of a name the statement does
    write, and then by facts about that name alone (``_aliased_python_fact``)."""
    groups: dict[tuple, _SiteGroup] = {}
    for fact in facts:
        file = _relative(fact.file, root)
        key = (fact.symbol, file, fact.func, fact.line)
        group = groups.get(key)
        if group is None:
            group = groups[key] = _SiteGroup(fact.symbol, file, fact.func, fact.line, fact.text)
            group.spelling_site = True
        group.add(fact)
    statements = _Statements(root)
    recorded = {(g.symbol, g.file, g.line) for g in groups.values()}
    for group in groups.values():
        if (group.symbol, group.file, group.line) in seen:
            continue                              # the static pass rated this spelling here
        delta, how = _python_fact(index, group.symbol)
        if delta is not None and delta.get("kind") != "UNCHANGED" and not _names_its_callee(
                group, statements):
            delta = None                          # not this statement's call
        if delta is None or delta.get("kind") == "UNCHANGED":
            delta = _aliased_python_fact(index, group, recorded)
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
            id="", tier=tier, symbol=group.symbol, package="python",
            site=site, basis="trace", reason=reason, fact=delta, facts=[delta],
            evidence=evidence, scope="python", reached_at="call", calls=group.calls,
            warnings=[CallWarning(category=c, message=m, count=n)
                      for (c, m), n in sorted(group.warnings.items())]))


def _names_its_callee(group: _SiteGroup, statements: _Statements) -> bool:
    """The statement names the call's callee: its last name, or for a constructor its class.
    A protocol method on a public class (``Version.__ge__`` for ``a >= b``) is the statement's
    own operation; one on a private class (``_AssertLogsContext.__enter__`` under ``with
    self.assertLogs(...)``) is the implementation of a name the statement does write. True when
    the statement is not known (no file to read, no line text): only evidence that the line did
    not write the call drops it."""
    parts = group.symbol.split(".")
    leaf = parts[-1]
    if leaf.startswith("__") and leaf.endswith("__") and leaf not in _CTOR:
        return not any(p.startswith("_") and not p.startswith("__") for p in parts)
    call = parts[-2] if len(parts) > 1 and leaf in _CTOR else leaf
    named = statements.names(group.file, group.line, group.text)
    return named is None or call in named


def merge_python_risks(dependency: PredictionReport | None,
                       python: PredictionReport) -> PredictionReport:
    """One report for a plan: the dependency risks (None: no baseline) plus the python ones."""
    if dependency is None:
        merged = PredictionReport(status="partial", notes=[
            "no baseline capture: dependency risks were not predicted (python risks need none)"])
    else:
        merged = dependency
    merged.risks.extend(python.risks)
    # Unique over both: a stdlib call both deltas name (``asyncio.Queue(loop=...)``) is a risk in
    # each scope at the same site, and the python one takes an ``nth`` after the dependency's.
    assign_risk_ids(merged.risks)
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
    PREVENTED = "prevented"          # fixed by a step, and the fix kept the behaviour
    MISS = "miss"
    NOT_EXERCISED = "not_exercised"
    ACCEPTED = "accepted"            # the change at the site was accepted by a person


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
    # The step the risk is linked to, and what verify found it did there (design 5.5):
    # ``fixed`` (its effect removed or replaced the risk's statement in the post-migration
    # code), ``recorded-not-fixing`` (in effect, but it did not), ``unfixed`` (not in effect).
    step_id: str | None = None
    fix: str | None = None


class Calibration(BaseModel):
    schema_: str = Field(default=CALIBRATION_SCHEMA, alias="schema")
    source: Literal["plan", "baseline"] = "plan"   # where the predictions came from
    plan_id: str | None = None
    predicted: int = 0
    hit: int = 0
    prevented: int = 0
    miss: int = 0
    not_exercised: int = 0
    accepted: int = 0
    by_tier: dict[str, dict[str, int]] = Field(default_factory=dict)
    # step id -> {hit, prevented, miss, not_exercised, accepted} of the risks linked to it.
    by_step: dict[str, dict[str, int]] = Field(default_factory=dict)
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
    links: Mapping[str, Any] | None = None,
) -> Calibration:
    """Score each predicted risk against the post-migration fact diff (module docstring).
    ``links``: risk id -> ``workorder.credit.StepLink``, the steps the risks are linked to and
    whether each fixed its risk's statement (design 5.5). With links, a risk is prevented only
    when its step fixed it; without them (no step context), ``fixed_by_plan`` decides, as
    before steps."""
    cal = Calibration(source=source, plan_id=plan_id)
    items = [r if isinstance(r, PredictedRisk) else PredictedRisk.model_validate(r)
             for r in risks or ()]
    entries = list(fdiff.entries) if fdiff is not None else []
    if fdiff is None:
        cal.notes.append("no post-migration fact diff: no prediction could be checked")
    covered: set[int] = set()
    for risk in items:
        link = links.get(risk.id) if links is not None else None
        fix = link.fix if link is not None else None
        fixed = (fix == "fixed") if links is not None else risk.fixed_by_plan
        on_line = [e for e in entries if any(_same_site(risk, e, s) for s in _entry_sites(e))]
        at_site = [e for e in on_line if risk.site.line is None or _same_symbol(risk, e)]
        covered.update(id(e) for e in at_site)
        if any(_hits(e) and not e.accepted for e in at_site):
            result, note = CalibrationResult.HIT, None
            # The line's other calls that moved with it (a ``.to_dict()`` that never ran
            # because ``mean`` now raises) are consequences of the predicted break.
            covered.update(id(e) for e in on_line)
            at_site = on_line
        elif any(e.accepted for e in at_site):
            result, note = CalibrationResult.ACCEPTED, "a person accepted the change here"
        elif any(_compared(e) for e in at_site):
            if fixed:
                result = CalibrationResult.PREVENTED
                note = ("fixed by the plan: the rewrite kept the behaviour at this site"
                        if links is None else
                        f"fixed by step {link.step_id}: it replaced the statement, and the "
                        "behaviour stayed the same")
            else:
                result, note = CalibrationResult.MISS, None
                if fix == "recorded-not-fixing":
                    note = (f"step {link.step_id} is in effect, but its change did not touch "
                            "this statement: unfixed")
        else:
            result = CalibrationResult.NOT_EXERCISED
            note = ("only uncomparable/unpaired entries at this site" if at_site else
                    "the post-migration capture did not compare this site")
        shown = [e for e in at_site if e.category is not FactCategory.EQUAL or e.notes]
        step_id = link.step_id if link is not None else risk.step_id
        cal.risks.append(CalibratedRisk(
            id=risk.id, tier=risk.tier, symbol=risk.symbol, site=risk.site,
            fixed_by_plan=risk.fixed_by_plan, result=result,
            observed=[_observed(e) for e in shown[:10]], note=note, step_id=step_id,
            fix=fix))
        tier = cal.by_tier.setdefault(risk.tier.value, {r.value: 0 for r in CalibrationResult
                                                         if r is not CalibrationResult.ACCEPTED})
        tier[result.value] = tier.get(result.value, 0) + 1
        if step_id:
            by = cal.by_step.setdefault(step_id, {r.value: 0 for r in CalibrationResult})
            by[result.value] += 1
    cal.predicted = len(cal.risks)
    cal.hit = sum(1 for r in cal.risks if r.result is CalibrationResult.HIT)
    cal.prevented = sum(1 for r in cal.risks if r.result is CalibrationResult.PREVENTED)
    cal.miss = sum(1 for r in cal.risks if r.result is CalibrationResult.MISS)
    cal.accepted = sum(1 for r in cal.risks if r.result is CalibrationResult.ACCEPTED)
    cal.not_exercised = cal.predicted - cal.hit - cal.prevented - cal.miss - cal.accepted
    cal.unpredicted = [_observed(e) for e in entries if e.fails and id(e) not in covered]
    return cal
