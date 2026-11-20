"""Behavioural facts (fact model v2): fold boundary events into facts and diff them.

HOST side (>=3.12, pydantic). Never imported by the injected runtime.

Pipeline::

    load_facts(trace, root=...)            # v1/v2 JSONL or aggregated JSON + <trace>.meta.json
      -> FactSet                           # facts keyed by (symbol, site, input)
    diff_facts(old, new, profile=...)      # exact | shape
      -> FactDiff                          # one FactEntry per compared fact, categorised

A **fact** is "this dependency symbol, called from this site in our code with this input,
produced this outcome distribution". ``symbol`` = ``sym`` (v2 canonical identity) else ``q``;
``site`` = (file relative to the project root, func, line text) when the capture recorded the
line text (``site_text`` in the metadata sidecar), else (file, func, line number); ``input`` =
``in`` without ``defaulted``. Outcomes are ``("return", result)`` / ``("raise", type)`` with
counts.

Matching (``diff_facts``) runs in passes, strongest identity first; each fact pairs at most
once and only one-to-one (a group pairs only when both sides hold the same number of facts,
zipped in first-seen order):

1. exact key ``(symbol, site, input)``
2. same symbol and site, input equal modulo defaulted parameter names (library default changed,
   v1<->v2 inputs) -> DRIFT when the defaulted sets differ
3. same symbol, same ``(file, func)``, different line/text: REWRITTEN in text mode (the line
   changed, the call did not), "line-moved" in line mode (v1: code above shifted)
4. same site and input, different symbol ("identity-changed": the library moved/re-wrapped it)
5. same symbol and input, different function/file ("site-moved")
5c. same site, a *relocated* symbol (same top-level package and last name segment, e.g.
   ``sqlalchemy.engine.create_engine`` -> ``sqlalchemy.engine.create.create_engine``), any input
5b. same symbol and site, *different input* ("matched-modulo-input": a keyword renamed, an
   argument changed: the call is the same, what it was given is not); one-to-one groups only,
   outcomes compared. A binding failure (``"binding": true`` raise: the call died before the
   callee's frame started) pairs earlier, with the other side's normal call at the same
   symbol and site (or function), so "now raises TypeError while binding its arguments" is a
   CHANGED outcome, never a DISAPPEARED call. A compiled call (``in: {"__c_call__": true}``)
   carries no arguments: its input is constant, so it matches by symbol and site alone
6. REWRITTEN by position: within a ``(file, func)`` whose line texts changed on both sides,
   leftover calls at the changed lines pair in first-seen order; outcomes compared, symbol and
   input ignored
7q. (after 6) same file, function and input, a qualname-equivalent symbol
   (``pkg.mod.decorator`` before 3.11, ``pkg.mod.C.m.<locals>.decorator`` after; the interpreter
   also moves a decorator's call from the ``def`` line to the ``@`` line), both on definition
   lines of one decorated statement: ``attribution-moved``
7. leftovers: at a changed line -> REWRITTEN without a partner (unverifiable, inconclusive);
   one unedited ``(symbol, site)`` with leftovers on both sides that no pass paired one-to-one
   (every call a fresh input) -> one ``volatile-site`` group compared by type and outcome kind;
   otherwise DISAPPEARED / APPEARED

Before keying, each side's volatile positions are masked (see "noise canonicalisation"): its
environment identity (project root as the process saw it, Python version), readings of its
own clock, and values a curated stdlib entropy source returned (uuid4, random, datetime.now,
tempfile names). A pair whose inputs differ is EQUAL at ``masked`` level when its output differs
only as its input does. After pairing: a compiled call only the wider backend can see, a fact
missing on a side that dropped events, an implicit attribute read and the other half of a paired
constructor are not failures; and, when no replicate measured run-to-run noise in a workload
whose values vary between runs, a value difference at a call whose input is not fully observed
is a review item (``unmeasured noise``), never a pass.

A pair's outcomes are compared at ``exact`` level when both sides are single, fully rendered
values; at ``type`` level (``outcome_type``) when either side carries a type-only marker
(``__opaque__``/``__cycle__``/``__shape__``/truncation/a value-less ``__structure__``), when a
side is nondeterministic (more than one distinct outcome and the sets are not identical) or when
``profile="shape"``. Type names compare relocation-aware: the same top-level package and the
same last name segment (``sqlalchemy.ext.declarative.api.Base`` vs ``sqlalchemy.orm.decl_api.Base``)
is equal at type level with a "relocated" note (``__type__``, ``__opaque__``, ``__object__``).
``__object__`` renderings (``{"__object__": T, "attrs": {...}}``) compare attribute by attribute
at exact level when both sides are complete, by type name at type level when either is
truncated. An aggregated record whose result collapsed to ``__nondeterministic__``
lost its distribution and is UNCOMPARABLE (inconclusive, never a pass).

Facts snapshots (phase 2, ``FactSink``): a capture whose processes aggregated in-process
leaves ``<trace>.facts.json`` (``pymolt.facts-snapshot/1``, one fact per line) next to the
trace. ``merge_fact_snapshots`` folds per-process snapshots into it (counts summed, outcome
lists merged under the same caps) and ``project_snapshot`` writes its facts into the JSONL as
counted events (``"count": n``, ``"src": "facts"``), so every JSONL reader keeps working.
``load_facts`` reads the snapshot itself when it is given one, or when the JSONL next to it
is absent or empty; either way the snapshot's quality counters annotate the FactSet:
``evicted > 0`` makes the evidence incomplete (the diff is never PASS, and a vanished /
appeared fact the evicting side may have dropped is UNCOMPARABLE instead of failing), and
widened facts (``"in": {"__widened__": true}``) compare their whole (symbol, site) group at
type level on both sides.
"""
import ast
import datetime as _dt
import difflib
import io
import json
import logging
import math
import os
import re
import sys
import tokenize
import warnings
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, PrivateAttr, computed_field

from pymolt.adapters.python_runner import RunnerSpec, inspect_container, to_container, to_host
from pymolt.state_io import atomic_write_json
from pymolt.verify import dict_order as _order
from pymolt.verify import environment as _env
from pymolt.verify._sinks import (
    MAX_OTHER_TYPES,
    MAX_OUTCOMES,
    SNAPSHOT_SCHEMA,
    WIDENED_INPUT,
    merge_warnings,
    type_label,
    warning_list,
    widen_value,
    write_snapshot_file,
)
from pymolt.verify.models import BoundaryDiff, VerificationVerdict

logger = logging.getLogger(__name__)

SITE_TEXT_MAX = 240
FACT_DIFF_SCHEMA = "pymolt.fact-diff/2"  # /2: ``summary`` (findings) first, capped values
_ENTRY_OUTCOME_CAP = 20  # outcomes listed per entry side (a fact keeps all of them)

# Keys whose presence makes a rendering comparable by type only.
_TYPE_ONLY_KEYS = ("__opaque__", "__cycle__", "__shape__", "__nondeterministic__")
_SIMPLE_TAG_TYPES = {
    "__bytes__": "bytes",
    "__decimal__": "decimal.Decimal",
    "__uuid__": "uuid.UUID",
    "__timedelta__": "datetime.timedelta",
}
_LABEL_ALIASES = {("frozenset", "set"), ("set", "frozenset")}
_NUMERIC = ("int", "float")  # one kind at type level (PEP 484 numeric promotion)


# ── models ────────────────────────────────────────────────────────────────────────────────


@dataclass
class DiffContext:
    """What verify knows about the edit that separates the two captures (all optional).

    ``old_sources`` / ``new_sources``: project-relative path -> file text at each capture.
    ``edits``: the in-effect apply-ledger entries, each located by ``workorder.coverage``
    (``"located": {"old": [lines], "new": [lines]}``, 1-based) with its ``"file"``, ``"claim"``,
    ``"basis"``, ``"old_symbol"`` and ``"new_symbol"``; an entry handed over without
    ``located`` covers its ``"old_lines"``/``"new_lines"`` ranges ([a, b], inclusive). A line
    several entries cover takes the strictest claim. ``not_in_capture``: the steps in effect
    that the post-migration capture predates (``"<run>:<step>"``).
    ``old_contacts`` / ``new_contacts``: file -> line -> static dependency symbols.
    ``successors``: old symbol -> new symbol (the plan's deltas). ``type_relocations``: old
    type name -> new type name. Without a context the diff falls back to line-text rules and
    reads the post side's source from ``FactSet.root``."""

    old_sources: dict[str, str] | None = None
    new_sources: dict[str, str] | None = None
    edits: list[dict] | None = None
    old_contacts: dict[str, dict[int, set[str]]] | None = None
    new_contacts: dict[str, dict[int, set[str]]] | None = None
    successors: dict[str, str] | None = None
    type_relocations: dict[str, str] | None = None
    # Symbols a plan's predicted risks name: an environment difference never softens them.
    predicted_symbols: set[str] | None = None
    # What the report should say about how the context was built (a ledger left unused).
    notes: list[str] = field(default_factory=list)
    not_in_capture: list[str] = field(default_factory=list)
    # The accepts verify honours, and ``--strict`` (``verify.accepts.AcceptContext``).
    accepts: Any = None


class DiffProfile(StrEnum):
    EXACT = "exact"
    SHAPE = "shape"


class FactCategory(StrEnum):
    CHANGED = "changed"
    REWRITTEN = "rewritten"
    DISAPPEARED = "disappeared"
    APPEARED = "appeared"
    DRIFT = "drift"
    UNCOMPARABLE = "uncomparable"
    EQUAL = "equal"


class Comparison(StrEnum):
    EQUAL = "equal"
    CHANGED = "changed"
    UNCOMPARABLE = "uncomparable"  # nothing to compare by (distribution not recorded)
    MISSING = "missing"            # no partner on the other side


class ComparisonLevel(StrEnum):
    EXACT = "exact"
    MASKED = "masked"  # exact once volatile positions (environment, clock, source, input) masked
    TYPE = "type"
    KIND = "kind"   # from tallies: which outcome kinds (return / raise types) occurred


_CATEGORY_ORDER = {c: i for i, c in enumerate(FactCategory)}


class Outcome(BaseModel):
    """One distinct outcome of a fact: a normalized return value or a raised type name."""

    kind: Literal["return", "raise"]
    value: Any = None
    count: int = 1
    # When the calls that produced this outcome ran (epoch seconds), from the events' ts/dur;
    # None for a facts-snapshot capture. Not part of the outcome's identity, never reported.
    ts_min: float | None = Field(default=None, exclude=True)
    ts_max: float | None = Field(default=None, exclude=True)
    # Dict key insertion order of the value (``dict_order.strip``): ``{path: permutation}``,
    # ``"*"`` where it varied within the fact. Not part of the outcome's identity, never
    # reported as a value; None = every dict in sorted order, or order not recorded.
    order: dict[str, Any] | None = Field(default=None, exclude=True)


class CallWarning(BaseModel):
    """A deprecation-family warning a dependency call issued while it ran (runtime
    evidence of an announced API change). Evidence, never part of a fact's outcome."""

    category: str          # "FutureWarning", "sqlalchemy.exc.RemovedIn20Warning", ...
    message: str           # capped at 300 chars by the runtime
    count: int = 1


class SiteWarnings(BaseModel):
    """Warnings of calls at one (symbol, site) whose values were not sampled (a snapshot
    tally or a JSONL sidecar's ``tally_warnings``): a site's warnings are its facts' plus
    these, never counted twice."""

    symbol: str
    file: str
    func: str
    line: int | None = None
    text: str | None = None
    warnings: list[CallWarning] = Field(default_factory=list)


class Fact(BaseModel):
    """A dependency call from one site with one input, and every outcome it produced."""

    symbol: str
    qualnames: list[str] = Field(default_factory=list)  # runtime ``q`` names seen (v1 identity)
    symq: str | None = None          # "unwrapped" / "fallback" when ``sym`` was not exact
    file: str                        # relative to the project root when under it
    func: str
    line: int | None = None          # first-seen line
    lines: list[int] = Field(default_factory=list)
    text: str | None = None          # stripped source line from the capture's site_text
    input: Any = None                # ``in`` without ``defaulted``
    defaulted: list[list[str]] | None = None  # distinct defaulted sets; None = not reported
    outcomes: list[Outcome] = Field(default_factory=list)
    count: int = 0
    where: dict[str, Any] | None = None  # first-seen call site as recorded (absolute file)
    first_seen: int = 0              # event/record index: call order for positional pairing
    binding: bool = False            # the call raised while binding its arguments (never ran)
    attr_of: str | None = None       # module ``__getattr__`` (PEP 562): the original symbol
    # Deprecation-family warnings its (sampled) calls issued, merged across occurrences.
    warnings: list[CallWarning] = Field(default_factory=list)
    # Set by the diff's noise canonicalisation: which masks changed this fact's input or
    # outcomes ("environment", "clock", "source"), and the fact as recorded.
    _masks: frozenset = PrivateAttr(default=frozenset())
    _raw: Any = PrivateAttr(default=None)
    # When its calls ran (epoch seconds: the earliest start, the latest end), from the events'
    # ``ts``/``dur``; None for a facts-snapshot capture, whose facts carry no call time.
    ts_min: float | None = None
    ts_max: float | None = None
    # Dict key insertion order of the input (see ``Outcome.order``); not part of the key.
    input_order: dict[str, Any] | None = Field(default=None, exclude=True)

    @property
    def site(self) -> tuple[str, str, str | int | None]:
        """The spec's site: (file, func, line text) when known, else (file, func, line)."""
        return (self.file, self.func, self.text if self.text is not None else self.line)

    @property
    def nondeterministic(self) -> bool:
        return len(self.outcomes) > 1 or any(_is_unrecorded(o) for o in self.outcomes)


class FactSet(BaseModel):
    """All facts of one trace, plus what the trace could and could not tell us."""

    source: str | None = None
    root: str | None = None
    aggregated: bool = False         # MemorySink JSON: no distribution, no call order
    events: int = 0                  # events (or aggregated records) folded
    invalid_lines: int = 0
    schema_versions: list[int] = Field(default_factory=list)
    sym_events: int = 0              # events carrying a v2 ``sym``
    defaulted_events: int = 0        # events carrying ``in.defaulted``
    site_text_entries: int = 0       # size of the site_text map supplied
    facts: list[Fact] = Field(default_factory=list)
    # Facts snapshot quality (0 / None when the trace has no snapshot).
    snapshot: bool = False           # facts came from, or were annotated by, a snapshot
    calls: int | None = None         # completed boundary calls the runtime counted
    sampled: int | None = None       # of which folded into facts
    evicted: int = 0                 # calls whose facts were evicted at the fact cap
    evicted_facts: int = 0
    widened: int = 0                 # calls folded into widened (type-level) facts
    widened_facts: int = 0
    outcomes_other: int = 0          # calls past a fact's distinct-outcome cap
    # (symbol, file, func) with at least one call per the snapshot's tallies; None = unknown
    tally_sites: set[tuple[str, str, str]] | None = Field(default=None, exclude=True)
    # Backends that produced the trace (metadata ``backend`` / ``processes[].backend``).
    backends: list[str] = Field(default_factory=list)
    # Every call per (symbol, site), sampled or not (a snapshot's tallies; None = no tallies)
    site_tallies: list["SiteTally"] | None = Field(default=None, exclude=True)
    # The tallies counted every call: nothing declined by the sampler went untallied,
    # nothing was dropped, no tally overflowed. Then presence/absence of a call site and
    # its outcome kinds are facts, whatever the value sampling rate.
    tallies_complete: bool = False
    # Warnings of unsampled calls, per (symbol, site); facts carry the sampled calls' own.
    unsampled_warnings: list[SiteWarnings] = Field(default_factory=list)
    # The runtime's ``warnings_capture`` ("on" / "off"); None = a runtime that predates it,
    # so "no warnings on a fact" does not mean "none were issued".
    warnings_capture: str | None = None
    # Container call sites (``/work/<project>/...``) are mapped to host paths through the
    # capture's runner before they are made project-relative.
    runner: RunnerSpec | None = Field(default=None, exclude=True)
    path_map_source: str | None = None   # "slot" | "config" | "config-container" | "explicit"
    paths_mapped: int = 0                # distinct call-site files mapped container -> host
    paths_foreign: int = 0               # distinct absolute call-site files left outside the root
    notes: list[str] = Field(default_factory=list)
    # The capture's environment identity and loss (``annotate_capture_meta``): the Python
    # versions its processes ran, their run windows (epoch seconds), the project root as each
    # process saw it, and the events the runtime dropped (None = not reported).
    python_versions: list[str] = Field(default_factory=list, exclude=True)
    run_windows: list[tuple[float, float]] = Field(default_factory=list, exclude=True)
    env_roots: list[str] = Field(default_factory=list, exclude=True)
    host_capture: bool = Field(default=True, exclude=True)
    dropped_events: int | None = None
    # ``{normalized distribution name: version}`` the capture's processes loaded (one version
    # across processes; an ambiguous one left out), from the runtime's ``distributions``.
    package_versions: dict[str, str] = Field(default_factory=dict, exclude=True)
    # Every process declared the ``dict_order`` value format: an absent ``__order__`` means
    # the sorted order. False: order is not recorded, so it is never compared.
    dict_order: bool = Field(default=False, exclude=True)
    # The runtime environment record of each process (``boundary_tracer._environment``);
    # None when any process lacks one (unknown: every environment rule stays strict).
    environment: list[dict[str, Any]] | None = Field(default=None, exclude=True)
    # The compiled-call cap (runtime ``c_site_cap``): sites where a process recorded only the
    # first calls of a compiled callable and counted the rest. ``c_cap_reported`` processes
    # gave an account, ``c_cap_unreported`` did not (a runtime that predates it: unknown);
    # ``c_capped_complete``: every reported capped site is listed.
    c_capped: list["CappedSite"] = Field(default_factory=list, exclude=True)
    c_capped_complete: bool = Field(default=True, exclude=True)
    c_cap_reported: int = Field(default=0, exclude=True)
    c_cap_unreported: int = Field(default=0, exclude=True)
    c_muted: int = Field(default=0, exclude=True)   # compiled calls counted, not recorded

    def keyed(self) -> dict[tuple[str, tuple, str], Fact]:
        """Facts keyed by ``(symbol, site, canonical input)`` (merging line-level facts that
        share a line text)."""
        out: dict[tuple[str, tuple, str], Fact] = {}
        for f in self.facts:
            key = (f.symbol, f.site, _canon(f.input))
            out[key] = _merge_facts([out[key], f]) if key in out else f
        return out

    def summary(self) -> "TraceSummary":
        return TraceSummary(
            source=self.source, aggregated=self.aggregated, events=self.events,
            facts=len(self.facts), invalid_lines=self.invalid_lines,
            schema_versions=self.schema_versions, sym_events=self.sym_events,
            defaulted_events=self.defaulted_events,
            facts_with_site_text=sum(1 for f in self.facts if f.text is not None),
            snapshot=self.snapshot, calls=self.calls, evicted=self.evicted,
            widened=self.widened, outcomes_other=self.outcomes_other,
            tallies_complete=self.tallies_complete,
            dropped_events=getattr(self, "dropped_events", None),
            backends=list(self.backends),
        )


class TraceSummary(BaseModel):
    source: str | None = None
    aggregated: bool = False
    events: int = 0
    facts: int = 0
    invalid_lines: int = 0
    schema_versions: list[int] = Field(default_factory=list)
    sym_events: int = 0
    defaulted_events: int = 0
    facts_with_site_text: int = 0
    snapshot: bool = False
    calls: int | None = None
    evicted: int = 0
    widened: int = 0
    outcomes_other: int = 0
    tallies_complete: bool = False
    dropped_events: int | None = None   # events the runtime observed and lost (queue drops)
    backends: list[str] = Field(default_factory=list)


@dataclass
class SiteTally:
    """Every call one (symbol, site) received, sampled or not (a snapshot tally)."""

    symbol: str
    file: str
    func: str
    line: int | None
    text: str | None
    count: int
    returns: int
    raises: dict[str, int]
    where: dict[str, Any] | None = None


@dataclass
class CappedSite:
    """A (symbol, site) whose compiled calls past the runtime's per-site cap were counted, not
    recorded (one process's ``c_site_cap`` site)."""

    symbol: str
    file: str
    func: str
    line: int | None
    text: str | None
    recorded: int        # the cap: calls recorded there before counting started
    muted: int           # calls counted after it
    exact: bool          # False: the count is a lower bound (sys.monitoring stopped counting)


class SiteRef(BaseModel):
    file: str
    func: str
    line: int | None = None
    text: str | None = None

    def label(self) -> str:
        return f"{self.file}:{self.line if self.line is not None else '?'}"


class FactEntry(BaseModel):
    """One compared fact: what was called, where, with what, and old -> new outcome."""

    category: FactCategory
    comparison: Comparison
    level: ComparisonLevel | None = None   # None when nothing was compared
    symbol: str                            # old side's symbol (new side's for APPEARED)
    new_symbol: str | None = None          # set only when it differs from ``symbol``
    old_site: SiteRef | None = None
    new_site: SiteRef | None = None
    input: Any = None                      # old side's input (new side's for APPEARED)
    new_input: Any = None                  # set only when it differs from ``input``
    old: list[Outcome] | None = None
    new: list[Outcome] | None = None
    old_types: list[Any] | None = None     # outcome_type() per side, at type level only
    new_types: list[Any] | None = None
    old_defaulted: list[list[str]] | None = None
    new_defaulted: list[list[str]] | None = None
    drift: bool = False                    # defaulted parameter sets differ
    old_count: int = 0
    new_count: int = 0
    notes: list[str] = Field(default_factory=list)
    # Deprecation-family warnings each side's calls issued: evidence shown with the entry,
    # never part of its category (a warning is not an outcome).
    old_warnings: list[CallWarning] | None = None
    new_warnings: list[CallWarning] | None = None
    old_where: dict[str, Any] | None = Field(default=None, exclude=True)
    new_where: dict[str, Any] | None = Field(default=None, exclude=True)
    # Edit provenance (only with a ``DiffContext``): unedited / ledger (a ``preserve`` entry
    # covers the edited line) / intended (an ``intended`` entry does) / hand / downstream.
    provenance: str | None = None
    # The ledger step whose entry decides the edited line, and its run (None: no entry does).
    step_id: str | None = None
    step_run: str | None = None
    # A change a person attested (an expectation recorded with a person's attestation, or a
    # ``verify --accept``): ``{"how", "by", "at", "attestation", "step"}``. Counted as compared
    # equal by the verdict, and always reported.
    accepted: dict[str, Any] | None = None
    # A failing or review entry's finding id and its finding's outcome fingerprint
    # (``verify.accepts``): what ``verify --accept`` names and binds.
    finding: str | None = None
    fingerprint: str | None = None
    review: str | None = None       # a difference a person must look at: INCONCLUSIVE, not FAIL
    unverified: bool = False        # an edited site nothing could be paired with: counted only
    # Why an entry that is not a plain exact comparison came out as it did: ``<mechanism>:
    # <detail>`` (``volatile-site``, ``environment``, ``clock``, ``source``,
    # ``input-substitution``, ``attribution``, ``backend``, ``possibly-dropped``, ...). Reporting
    # groups by the mechanism; it never changes the verdict by itself.
    explained: str | None = None
    # Run-to-run noise measured by a replicate capture: the difference is real but the baseline
    # itself varies there the same way. Neither failing nor inconclusive.
    noise: str | None = None
    # A dict key order difference (``_judge_order``): ``{"side": "input"|"output", "path",
    # "keys", "old": first keys, "new": first keys}`` (+ ``"cause"`` when knowledge cites one).
    order: dict[str, Any] | None = None
    # The sorted key sets whose orders were compared and agreed, and the key sets of the order
    # differences this entry's order class stands for (replicate evidence, per key set).
    order_agreed: list[tuple[str, ...]] | None = Field(default=None, exclude=True)
    order_keysets: list[str] | None = Field(default=None, exclude=True)
    # The recorded values behind masked ones (None when no mask changed them): reporting shows
    # both. Kept in memory only (the JSON entries carry the masked, compared values).
    input_raw: Any = Field(default=None, exclude=True)
    new_input_raw: Any = Field(default=None, exclude=True)
    old_raw: list[Outcome] | None = Field(default=None, exclude=True)
    new_raw: list[Outcome] | None = Field(default=None, exclude=True)

    @computed_field
    @property
    def fails(self) -> bool:
        if self.review or self.unverified or self.noise or self.accepted:
            return False
        if self.category in (FactCategory.CHANGED, FactCategory.DISAPPEARED,
                             FactCategory.APPEARED):
            return True
        return self.category is FactCategory.REWRITTEN and self.comparison is Comparison.CHANGED

    @property
    def inconclusive(self) -> bool:
        if self.unverified or self.noise or self.accepted:
            return False
        if self.review:
            return True
        return self.comparison in (Comparison.UNCOMPARABLE, Comparison.MISSING) and (
            self.category in (FactCategory.UNCOMPARABLE, FactCategory.REWRITTEN))

    @property
    def site(self) -> SiteRef:
        site = self.old_site or self.new_site
        assert site is not None
        return site


class FactDiff(BaseModel):
    """Two FactSets compared; every compared fact is an entry (``equal`` ones included)."""

    profile: DiffProfile = DiffProfile.EXACT
    entries: list[FactEntry] = Field(default_factory=list)
    old_summary: TraceSummary = Field(default_factory=TraceSummary)
    new_summary: TraceSummary = Field(default_factory=TraceSummary)
    warnings: list[str] = Field(default_factory=list)
    # Value facts seen on one side only, at a call site both sides' complete tallies show
    # was called, by a side that did not sample every call there: not compared, not failing.
    unsampled: int = 0
    no_ledger: bool = False  # edits were judged as hand edits: no apply ledger was available
    # Run-to-run noise measured by a baseline replicate: what it covered (None: not measured).
    noise_measured: dict[str, Any] | None = None
    # Whether dict key order was compared, and why not (``"compared"`` | ``"not recorded
    # (old)"`` ...); the two captures' runtime environments compared (``environment.stamp``).
    dict_order: str | None = Field(default=None, exclude=True)
    environment: Any = Field(default=None, exclude=True)

    def edited_sites(self) -> dict[str, int]:
        """Edited sites by outcome: equal / representation-only / need review / unverified
        (plus ``failed`` for a ledger or hand edit that changed behaviour)."""
        out = {"sites": 0, "equal": 0, "representation_only": 0, "review": 0,
               "unverified": 0, "failed": 0, "accepted": 0}
        for e in self.entries:
            if e.provenance not in ("ledger", "intended", "hand", "downstream"):
                continue
            out["sites"] += 1
            if e.accepted:
                out["accepted"] += 1
            elif e.unverified:
                out["unverified"] += 1
            elif e.review:
                out["review"] += 1
            elif e.category is FactCategory.DRIFT and "representation changed" in e.notes:
                out["representation_only"] += 1
            elif e.fails:
                out["failed"] += 1
            else:
                out["equal"] += 1
        return out

    def edited_summary(self) -> str:
        """``N edited sites: a equal, b representation-only, c need review, d unverified`` (and
        ``e failed``, ``f accepted`` when there are any)."""
        e = self.edited_sites()
        line = (f"{e['sites']} edited sites: {e['equal']} equal, {e['representation_only']} "
                f"representation-only, {e['review']} need review, {e['unverified']} unverified")
        line += f", {e['failed']} failed" if e["failed"] else ""
        line += f", {e['accepted']} accepted" if e["accepted"] else ""
        return line + ("; no apply ledger: edits judged as hand edits" if self.no_ledger else "")

    def counts(self) -> dict[str, int]:
        out = {c.value: 0 for c in FactCategory}
        for e in self.entries:
            out[e.category.value] += 1
        return out

    def failures(self) -> list[FactEntry]:
        return [e for e in self.entries if e.fails]

    def is_clean(self) -> bool:
        """No CHANGED / DISAPPEARED / APPEARED, and no REWRITTEN with a different outcome."""
        return not any(e.fails for e in self.entries)

    @property
    def incomplete(self) -> bool:
        """A side's facts snapshot evicted facts at its cap: what was not kept cannot be
        shown equal, so the diff can fail but never pass."""
        return bool(self.old_summary.evicted or self.new_summary.evicted)

    @computed_field
    @property
    def verdict(self) -> VerificationVerdict:
        """FAIL on any failing entry; INCONCLUSIVE on undecidable entries, incomplete
        evidence (an evicting snapshot) or no evidence."""
        if not self.is_clean():
            return VerificationVerdict.FAIL
        if (not self.entries or self.incomplete
                or any(e.inconclusive for e in self.entries)):
            return VerificationVerdict.INCONCLUSIVE
        return VerificationVerdict.PASS

    def to_json_dict(self, *, include_equal: bool = False) -> dict[str, Any]:
        """Stable JSON form. Plain ``equal`` entries are omitted unless asked for; equal
        entries that carry notes (identity-changed, site-moved, ...) are always kept."""
        from pymolt.verify.fact_report import cap_entry, summarize

        entries = [
            cap_entry(e.model_dump(mode="json", exclude_none=True, exclude_defaults=False))
            for e in self.entries
            if include_equal or e.category is not FactCategory.EQUAL or e.notes
        ]
        return {
            "schema": FACT_DIFF_SCHEMA,
            "summary": summarize(self),
            "profile": self.profile.value,
            "verdict": self.verdict.value,
            "clean": self.is_clean(),
            "incomplete": self.incomplete,
            "counts": self.counts(),
            "unsampled": self.unsampled,
            "edited_sites": self.edited_sites(),
            "edited_summary": self.edited_summary(),
            "warnings": list(self.warnings),
            "old": self.old_summary.model_dump(mode="json"),
            "new": self.new_summary.model_dump(mode="json"),
            "noise_measured": self.noise_measured,
            "entries": entries,
        }

    def summary(self) -> dict[str, Any]:
        """Findings, not entries (see ``fact_report.summarize``)."""
        from pymolt.verify.fact_report import summarize

        return summarize(self)

    def to_boundary_diff(self) -> BoundaryDiff:
        """Project onto today's ``BoundaryDiff`` so existing renderers/counters keep working.

        CHANGED and failing REWRITTEN -> ``raise_changed`` when the raise side differs, else
        ``result_changed``; DISAPPEARED/APPEARED map 1:1; UNCOMPARABLE and unpaired/undecidable
        REWRITTEN -> ``skipped_opaque``. equal and DRIFT are dropped. Note BoundaryDiff treats
        ``appeared`` as non-blocking: ``FactDiff.verdict`` is the authority.
        """
        out = BoundaryDiff()
        for e in self.entries:
            if e.noise:
                continue  # measured run-to-run noise: not a boundary change
            if e.category is FactCategory.DISAPPEARED:
                out.disappeared.append(_legacy_record(e, old_side=True))
            elif e.category is FactCategory.APPEARED:
                out.appeared.append(_legacy_record(e, old_side=False))
            elif e.comparison is Comparison.CHANGED:
                rec = _legacy_record(e, old_side=True)
                old_raised, new_raised = _raised_of(e.old), _raised_of(e.new)
                if old_raised != new_raised:
                    out.raise_changed.append({**rec, "old_raised": old_raised,
                                              "new_raised": new_raised})
                else:
                    out.result_changed.append({**rec, "old": _result_of(e.old),
                                               "new": _result_of(e.new)})
            elif e.inconclusive:
                if any(n.startswith("possibly-evicted") for n in e.notes):
                    reason = "evicted"
                elif any(n.startswith("possibly-dropped") for n in e.notes):
                    reason = "dropped"
                elif any(n.startswith(C_SITE_CAPPED) for n in e.notes):
                    reason = "c-site-capped"
                elif any(n.startswith(CALL_SHAPE) for n in e.notes):
                    reason = "call-shape"
                elif e.comparison is Comparison.MISSING:
                    reason = "rewritten-unpaired"
                else:
                    reason = "nondeterministic-unrecorded"
                out.skipped_opaque.append({**_legacy_record(e, old_side=e.old is not None),
                                           "reason": reason})
        return out


# ── loading ───────────────────────────────────────────────────────────────────────────────


@dataclass
class _ReadStats:
    aggregated: bool = False
    invalid_lines: int = 0


def iter_trace_records(path: str | Path, stats: _ReadStats | None = None, *,
                       _mark: bool = False) -> Iterator[dict]:
    """Yield raw records from a JSONL event stream or an aggregated ``{"records": [...]}``.

    Undecodable JSONL lines (e.g. the last line of a killed process) are skipped and counted
    in ``stats.invalid_lines``.
    """
    stats = stats if stats is not None else _ReadStats()
    p = Path(path)
    if is_facts_snapshot(p):
        yield from iter_snapshot_records(p, _mark=_mark)
        return
    if _is_blank(p):
        snap = find_facts_snapshot(p)
        if snap is not None:
            yield from iter_snapshot_records(snap, _mark=_mark)
            return
    first = ""
    with p.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if line.strip():
                first = line.strip()
                break
    if not first:
        return
    head: Any = None
    try:
        head = json.loads(first)
    except json.JSONDecodeError:
        pass
    if not (isinstance(head, dict) and "t" in head):
        # Not a JSONL event first line: the aggregated (indented) JSON, if it parses.
        try:
            with p.open(encoding="utf-8") as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, UnicodeDecodeError):
            data = None
        if isinstance(data, dict) and isinstance(data.get("records"), list):
            stats.aggregated = True
            for rec in data["records"]:
                if isinstance(rec, dict):
                    yield rec
            return
    with p.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                stats.invalid_lines += 1
                continue
            if isinstance(ev, dict):
                if _mark:  # the load fast path: only a line naming ``__order__`` is walked
                    ev[_LINE_ORDER] = _order.ORDER_MARK in line
                yield ev
            else:
                stats.invalid_lines += 1


# Set on each record read from a line (JSONL event, snapshot fact): whether the line names
# ``__order__``. A record without it (aggregated JSON, a test's record) is always walked.
_LINE_ORDER = "\x00order"


def metadata_path(trace_path: str | Path) -> Path:
    return Path(str(trace_path) + ".meta.json")


# ── facts snapshots ───────────────────────────────────────────────────────────────────────


def snapshot_path(trace_path: str | Path) -> Path:
    """Where a capture keeps its merged facts snapshot: ``<trace>.facts.json``."""
    return Path(str(trace_path) + ".facts.json")


def find_facts_snapshot(trace_path: str | Path) -> Path | None:
    """The snapshot that belongs to a trace: the host's merged ``<trace>.facts.json``, else a
    runtime's own ``trace.jsonl`` -> ``trace.facts.json`` (an attach capture not yet
    collected). None when there is none."""
    p = Path(trace_path)
    candidates = [snapshot_path(p)]
    if p.name.endswith(".jsonl"):
        candidates.append(p.with_name(p.name[:-len(".jsonl")] + ".facts.json"))
    for c in candidates:
        if c.is_file() and is_facts_snapshot(c):
            return c
    return None


def snapshot_trace_path(snapshot: str | Path) -> Path:
    """The trace a snapshot stands in for (whose ``.meta.json`` holds its site text)."""
    p = Path(snapshot)
    name = p.name
    if name.endswith(".jsonl.facts.json"):
        return p.with_name(name[:-len(".facts.json")])
    if name.endswith(".facts.json"):
        return p.with_name(name[:-len(".facts.json")] + ".jsonl")
    return p


def is_facts_snapshot(path: str | Path) -> bool:
    try:
        with Path(path).open("rb") as fh:
            head = fh.read(256)
    except OSError:
        return False
    text = head.lstrip().decode("utf-8", errors="replace")
    return text.startswith('{"schema":"pymolt.facts-snapshot/') or (
        text.startswith("{") and SNAPSHOT_SCHEMA in text and '"schema"' in text)


def _is_blank(path: Path) -> bool:
    """Missing, or nothing but whitespace (a facts capture's JSONL before projection)."""
    try:
        with path.open("rb") as fh:
            while True:
                chunk = fh.read(65536)
                if not chunk:
                    return True
                if chunk.strip():
                    return False
    except OSError:
        return True


def iter_snapshot(
    path: str | Path, kinds: Iterable[str] = ("header", "fact", "tally"), *,
    _mark: bool = False,
) -> Iterator[tuple[str, dict]]:
    """Stream a facts snapshot as ``("header", {...})``, ``("fact", {...})``...,
    ``("tally", {...})``...; only the wanted ``kinds`` are parsed.

    Streams the line layout ``write_snapshot_file`` writes (one fact per line), so a large
    snapshot is never held in memory as a whole; any other layout (a hand-edited or
    re-indented file) falls back to ``json.load``. Undecodable lines are skipped."""
    want = set(kinds)
    p = Path(path)
    with p.open(encoding="utf-8", errors="replace") as fh:
        first = fh.readline().strip()
        second = fh.readline().strip()
        if not (first.startswith("{") and first.endswith(",") and second == '"facts":['):
            yield from _iter_snapshot_document(p, want)
            return
        if "header" in want:
            try:
                header = json.loads(first[:-1] + "}")
            except json.JSONDecodeError:
                header = {}
            yield "header", header if isinstance(header, dict) else {}
        section = "fact"
        for line in fh:
            line = line.strip()
            if not line:
                continue
            if line in ("],", "]", "]}"):
                continue
            if line == '"tallies":[':
                section = "tally"
                continue
            if section not in want:
                continue
            try:
                obj = json.loads(line[:-1] if line.endswith(",") else line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                if _mark and section == "fact":
                    obj[_LINE_ORDER] = _order.ORDER_MARK in line
                yield section, obj


def _iter_snapshot_document(p: Path, want: set[str]) -> Iterator[tuple[str, dict]]:
    try:
        with p.open(encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return
    if not isinstance(data, dict):
        return
    if "header" in want:
        yield "header", {k: v for k, v in data.items() if k not in ("facts", "tallies")}
    for key, kind in (("facts", "fact"), ("tallies", "tally")):
        if kind in want:
            for obj in data.get(key) or ():
                if isinstance(obj, dict):
                    yield kind, obj


def read_snapshot_header(path: str | Path) -> dict:
    """The header (counters, limits, process metadata) of a snapshot; ``{}`` if unreadable."""
    try:
        for _kind, header in iter_snapshot(path, kinds=("header",)):
            return header
    except OSError:
        pass
    return {}


def snapshot_fact_records(fact: Mapping[str, Any]) -> Iterator[dict]:
    """One snapshot fact as counted JSONL events (one per outcome, ``"count": n``).

    "other" outcomes become type-level returns (``{"__opaque__": type}``) / raises per kept
    type; calls whose type was not kept either (``lost``) become a
    ``{"__nondeterministic__": true}`` return: that part of the distribution is unknown, so
    the fact cannot compare (UNCOMPARABLE), never silently equal."""
    q = fact.get("q") if isinstance(fact.get("q"), str) and fact.get("q") else None
    sym = fact.get("sym") if isinstance(fact.get("sym"), str) and fact.get("sym") else None
    base: dict[str, Any] = {"v": 2, "q": q or sym or "?", "in": fact.get("in"), "src": "facts"}
    if _LINE_ORDER in fact:
        base[_LINE_ORDER] = fact[_LINE_ORDER]
    if sym:
        base["sym"] = sym
        if isinstance(fact.get("symq"), str):
            base["symq"] = fact["symq"]
    if isinstance(fact.get("own"), str):
        base["own"] = fact["own"]
    if isinstance(fact.get("where"), dict):
        base["where"] = fact["where"]
    if isinstance(fact.get("first_ts"), int | float):
        base["ts"] = fact["first_ts"]
    if isinstance(fact.get("in_order"), dict) and fact["in_order"]:
        base["in_order"] = fact["in_order"]   # the input's key order (the facts sink's side record)
    # The fact's warnings ride on its first projected event only (folding sums them).
    pending_warnings = fact.get("warnings") if isinstance(fact.get("warnings"), list) else None
    for rec in _snapshot_outcome_records(fact, base):
        if pending_warnings:
            rec["warnings"] = pending_warnings
            pending_warnings = None
        yield rec


def _snapshot_outcome_records(fact: Mapping[str, Any], base: dict[str, Any]) -> Iterator[dict]:
    for o in fact.get("outcomes") or ():
        if not isinstance(o, dict):
            continue
        count = _positive_int(o.get("count"))
        if not count:
            continue
        t = o.get("t")
        if t == "return":
            rec = {**base, "t": "return", "result": o.get("result"), "count": count}
            if isinstance(o.get("order"), dict) and o["order"]:
                rec["order"] = o["order"]
            yield rec
        elif t == "raise":
            yield {**base, "t": "raise", "raised": str(o.get("raised")), "count": count}
        elif t == "other":
            kept = 0
            for ty in o.get("types") or ():
                n = _positive_int(ty.get("count")) if isinstance(ty, dict) else 0
                if not n:
                    continue
                kept += n
                if ty.get("t") == "raise":
                    yield {**base, "t": "raise", "raised": str(ty.get("raised")), "count": n}
                else:
                    yield {**base, "t": "return", "count": n,
                           "result": {"__opaque__": str(ty.get("type") or "object")}}
            if count > kept:
                yield {**base, "t": "return", "result": {"__nondeterministic__": True},
                       "count": count - kept}


def iter_snapshot_records(path: str | Path, *, _mark: bool = False) -> Iterator[dict]:
    """Every fact of a snapshot as counted events, in first-seen order."""
    for _kind, fact in iter_snapshot(path, kinds=("fact",), _mark=_mark):
        yield from snapshot_fact_records(fact)


def project_snapshot(snapshot: str | Path, jsonl_path: str | Path) -> int:
    """Append a snapshot's facts to a JSONL trace as counted events; returns lines written.

    The projection is what keeps every JSONL reader (the report's observations, artifact
    validation, call-site text, the legacy comparator) working on a facts capture; it is
    bounded by the snapshot, not by the run."""
    written = 0
    with Path(jsonl_path).open("a", encoding="utf-8") as out:
        for rec in iter_snapshot_records(snapshot):
            out.write(json.dumps(rec, sort_keys=True, default=str) + "\n")
            written += 1
    return written


@dataclass
class _MergedFact:
    q: str | None
    sym: str | None
    symq: str | None
    own: str | None
    where: dict | None
    in_json: str
    widened: bool
    first_ts: float | None
    last_ts: float | None
    outcomes: dict[tuple[str, str], int] = field(default_factory=dict)
    other: int = 0
    other_types: dict[tuple[str, str], int] = field(default_factory=dict)
    other_lost: int = 0
    count: int = 0
    dur: list | None = None  # [n, sum, min, max]
    warnings: dict[tuple[str, str], int] | None = None
    # dict key order side records (merged across snapshots: a differing order is "*")
    in_order: dict[str, Any] | None = None
    out_orders: dict[tuple[str, str], dict[str, Any] | None] = field(default_factory=dict)

    def add_outcome(self, kind: str, payload: str, value: Any, n: int,
                    order: dict[str, Any] | None = None) -> None:
        key = (kind, payload)
        if key in self.outcomes:
            self.outcomes[key] += n
            if kind == "return":
                prev = self.out_orders.get(key)
                if prev != order:
                    self.out_orders[key] = _order.merge(prev, order)
        elif len(self.outcomes) < MAX_OUTCOMES:
            self.outcomes[key] = n
            if kind == "return":
                self.out_orders[key] = order
        else:
            self.add_other(kind, type_label(value) if kind == "return" else payload, n)

    def add_other(self, kind: str, label: str, n: int) -> None:
        self.other += n
        key = (kind, label)
        if key in self.other_types:
            self.other_types[key] += n
        elif len(self.other_types) < MAX_OTHER_TYPES:
            self.other_types[key] = n
        else:
            self.other_lost += n

    def to_json(self) -> str:
        outs = []
        for (kind, payload), n in self.outcomes.items():
            if kind == "return":
                order = self.out_orders.get((kind, payload))
                extra = f',"order":{_canon(order)}' if order else ""
                outs.append('{"t":"return","result":' + payload + f',"count":{n}{extra}}}')
            else:
                outs.append('{"t":"raise","raised":' + _canon(payload) + f',"count":{n}}}')
        if self.other:
            types = [{"t": "return", "type": label, "count": n} if kind == "return"
                     else {"t": "raise", "raised": label, "count": n}
                     for (kind, label), n in sorted(self.other_types.items())]
            outs.append(_canon({"t": "other", "count": self.other, "types": types,
                                "lost": self.other_lost}))
        head: dict[str, Any] = {"q": self.q, "sym": self.sym, "symq": self.symq,
                                "own": self.own, "where": self.where, "count": self.count,
                                "first_ts": self.first_ts, "last_ts": self.last_ts}
        if self.widened:
            head["widened"] = True
        if self.dur is not None:
            head["dur"] = dict(zip(("n", "sum", "min", "max"), self.dur, strict=True))
        if self.warnings:
            head["warnings"] = warning_list(self.warnings)
        if self.in_order:
            head["in_order"] = self.in_order
        return '{"in":' + self.in_json + ',"outcomes":[' + ",".join(outs) + "]," + \
            _canon(head)[1:]


def _side_order(value: Any) -> dict[str, Any] | None:
    """A side record's order map as read (``{path: permutation | "*"}``), else None."""
    if not isinstance(value, dict) or not value:
        return None
    out: dict[str, Any] = {}
    for path, perm in value.items():
        if not isinstance(path, str):
            continue
        if perm == _order.VARIES or (isinstance(perm, list) and all(
                isinstance(i, int) and not isinstance(i, bool) for i in perm)):
            out[path] = perm
        else:
            out[path] = _order.UNKNOWN
    return out or None


def _merge_dur(acc: list | None, d: Any) -> list | None:
    if not isinstance(d, dict) or not _positive_int(d.get("n")):
        return acc
    n, total = d["n"], d.get("sum") or 0.0
    lo, hi = d.get("min"), d.get("max")
    if acc is None:
        return [n, total, lo, hi]
    acc[0] += n
    acc[1] += total
    if lo is not None and (acc[2] is None or lo < acc[2]):
        acc[2] = lo
    if hi is not None and (acc[3] is None or hi > acc[3]):
        acc[3] = hi
    return acc


_PROCESS_KEYS = ("pid", "python", "executable", "mode", "switched_at", "switch_reason",
                 "discovered", "armed", "settled_at", "settle_reason", "handed_over",
                 "incremental_settle", "incremental_reopen", "reopened", "reopen_seconds",
                 "started_at", "ended_at", "backend", "sample_rate",
                 "warnings_capture", "distributions", "value_format", "environment")


def merge_fact_snapshots(
    paths: Iterable[str | Path], out_path: str | Path, *, max_facts: int | None = None,
) -> dict | None:
    """Merge per-process facts snapshots into one (streaming: one snapshot is read at a time
    and only the merged tables are held). Returns the merged header, or None when no path
    was a readable snapshot (nothing is written then).

    Facts with the same key merge (counts, outcome lists under ``MAX_OUTCOMES`` with the
    overflow kept as "other", duration summaries); tallies sum; numeric counters sum. The
    merged table is held to ``max_facts`` (default: ``PYMOLT_TRACE_MAX_FACTS`` or 50,000)
    by evicting the lowest-count facts, which is added to ``counters.evicted`` like a
    runtime eviction, so the result is incomplete exactly when an input was.
    """
    if max_facts is None:
        try:
            max_facts = max(1, int(os.environ.get("PYMOLT_TRACE_MAX_FACTS", "50000")))
        except ValueError:
            max_facts = 50000
    facts: dict[tuple, _MergedFact] = {}
    tallies: dict[tuple, dict] = {}
    counters: dict[str, int | float] = {}
    processes: list[dict] = []
    limits: dict[str, Any] = {}
    read = 0
    evicted = evicted_facts = 0
    for path in paths:
        if not is_facts_snapshot(path):
            continue
        read += 1
        try:
            for kind, obj in iter_snapshot(path):
                if kind == "header":
                    processes.append({k: obj.get(k) for k in _PROCESS_KEYS if k in obj}
                                     | {"counters": obj.get("counters") or {}})
                    for name, value in (obj.get("counters") or {}).items():
                        if isinstance(value, int | float) and not isinstance(value, bool):
                            counters[name] = counters.get(name, 0) + value
                    if isinstance(obj.get("limits"), dict):
                        limits = obj["limits"]
                elif kind == "fact":
                    _merge_fact(facts, obj)
                else:
                    _merge_tally(tallies, obj)
        except OSError as exc:
            logger.warning("could not read facts snapshot %s: %s", path, exc)
            continue
        if len(facts) > max_facts:
            victims = sorted(facts.items(), key=lambda kv: kv[1].count)[:len(facts) - max_facts]
            for key, f in victims:
                del facts[key]
                evicted += f.count
                evicted_facts += 1
    if not read:
        return None
    counters["evicted"] = int(counters.get("evicted", 0)) + evicted
    counters["evicted_facts"] = int(counters.get("evicted_facts", 0)) + evicted_facts
    counters["facts"] = len(facts)
    counters["tallies"] = len(tallies)
    starts = [p["started_at"] for p in processes if isinstance(p.get("started_at"), int | float)]
    ends = [p["ended_at"] for p in processes if isinstance(p.get("ended_at"), int | float)]
    header: dict[str, Any] = {
        "schema": SNAPSHOT_SCHEMA, "merged": True,
        "started_at": min(starts) if starts else None,
        "ended_at": max(ends) if ends else None,
        "counters": counters, "limits": limits, "processes": processes,
    }
    for key in ("python", "executable", "mode"):
        values = {p.get(key) for p in processes if p.get(key) is not None}
        if len(values) == 1:
            header[key] = values.pop()
        elif values:
            header[key] = "mixed" if key == "mode" else sorted(map(str, values))
    ordered = sorted(facts.values(), key=lambda f: (f.first_ts is None, f.first_ts or 0.0))
    write_snapshot_file(str(out_path), header, (f.to_json() for f in ordered),
                        (_canon(t) for t in tallies.values()))
    return header


def _merge_fact(facts: dict[tuple, _MergedFact], obj: dict) -> None:
    where = obj.get("where") if isinstance(obj.get("where"), dict) else None
    q = obj.get("q") if isinstance(obj.get("q"), str) else None
    sym = obj.get("sym") if isinstance(obj.get("sym"), str) else None
    in_json = _canon(obj.get("in"))
    w = where or {}
    key = (sym or q, w.get("file"), w.get("line"), w.get("func"), in_json)
    f = facts.get(key)
    first, last = obj.get("first_ts"), obj.get("last_ts")
    in_order = _side_order(obj.get("in_order"))
    if f is None:
        f = facts[key] = _MergedFact(
            q=q, sym=sym, symq=obj.get("symq") if isinstance(obj.get("symq"), str) else None,
            own=obj.get("own") if isinstance(obj.get("own"), str) else None, where=where,
            in_json=in_json, widened=in_json == WIDENED_INPUT or bool(obj.get("widened")),
            first_ts=first if isinstance(first, int | float) else None,
            last_ts=last if isinstance(last, int | float) else None, in_order=in_order)
    else:
        if in_order != f.in_order:
            f.in_order = _order.merge(f.in_order, in_order)
        if isinstance(first, int | float) and (f.first_ts is None or first < f.first_ts):
            f.first_ts = first
        if isinstance(last, int | float) and (f.last_ts is None or last > f.last_ts):
            f.last_ts = last
    for o in obj.get("outcomes") or ():
        if not isinstance(o, dict):
            continue
        n = _positive_int(o.get("count"))
        if not n:
            continue
        f.count += n
        t = o.get("t")
        if t == "return":
            f.add_outcome("return", _canon(o.get("result")), o.get("result"), n,
                          _side_order(o.get("order")))
        elif t == "raise":
            raised = str(o.get("raised"))
            f.add_outcome("raise", raised, raised, n)
        elif t == "other":
            kept = 0
            for ty in o.get("types") or ():
                c = _positive_int(ty.get("count")) if isinstance(ty, dict) else 0
                if not c:
                    continue
                kept += c
                if ty.get("t") == "raise":
                    f.add_other("raise", str(ty.get("raised")), c)
                else:
                    f.add_other("return", str(ty.get("type") or "object"), c)
            if n > kept:
                f.other += n - kept
                f.other_lost += n - kept
    f.dur = _merge_dur(f.dur, obj.get("dur"))
    if isinstance(obj.get("warnings"), list) and obj["warnings"]:
        f.warnings = merge_warnings(f.warnings if f.warnings is not None else {},
                                    obj["warnings"])


def _merge_tally(tallies: dict[tuple, dict], obj: dict) -> None:
    where = obj.get("where") if isinstance(obj.get("where"), dict) else None
    w = where or {}
    key = (obj.get("sym") or obj.get("q"), w.get("file"), w.get("line"), w.get("func"))
    t = tallies.get(key)
    if t is None:
        t = tallies[key] = {"q": obj.get("q"), "sym": obj.get("sym"), "where": where,
                            "count": 0, "returns": 0, "raises": {}}
    t["count"] += _positive_int(obj.get("count"))
    t["returns"] += _positive_int(obj.get("returns"))
    for name, n in (obj.get("raises") or {}).items():
        t["raises"][name] = t["raises"].get(name, 0) + _positive_int(n)
    if _positive_int(obj.get("raises_other")):
        t["raises_other"] = t.get("raises_other", 0) + obj["raises_other"]
    if isinstance(obj.get("warnings"), list) and obj["warnings"]:
        t["warnings"] = warning_list(merge_warnings(merge_warnings({}, t.get("warnings")),
                                                    obj["warnings"]))
    d = obj.get("dur")
    merged = _merge_dur(
        [t["dur"]["n"], t["dur"]["sum"], t["dur"]["min"], t["dur"]["max"]]
        if "dur" in t else None, d)
    if merged is not None:
        hist = t.get("dur", {}).get("hist")
        new_hist = d.get("hist") if isinstance(d, dict) else None
        if isinstance(new_hist, list):
            hist = ([a + b for a, b in zip(hist, new_hist, strict=False)]
                    if isinstance(hist, list) and len(hist) == len(new_hist) else
                    (list(new_hist) if hist is None else hist))
        t["dur"] = dict(zip(("n", "sum", "min", "max"), merged, strict=True))
        if hist is not None:
            t["dur"]["hist"] = hist


def _positive_int(v: Any) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else 0


def load_site_text(trace_path: str | Path) -> dict[str, str]:
    """``site_text`` from ``<trace>.meta.json`` (``{}`` when absent or malformed)."""
    try:
        with metadata_path(trace_path).open(encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    raw = payload.get("site_text") if isinstance(payload, dict) else None
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)}


def collect_site_text(trace_path: str | Path, root: str | Path,
                      runner: RunnerSpec | None = None) -> dict[str, str]:
    """Read the source line of every distinct ``where.file:line`` in a trace.

    Run at capture time, before the project's code changes. Returns
    ``{"<file as recorded>:<line>": "<stripped line, at most 240 chars>"}`` for files under
    ``root`` only (relative recorded paths resolve against ``root``). Unreadable files, lines
    past EOF and undecodable bytes are skipped / replaced, never raised. With ``runner`` a
    container path is read from its host counterpart (the key stays the recorded path).
    """
    root_real = os.path.realpath(str(root))
    wanted: dict[str, set[int]] = defaultdict(set)
    try:
        for rec in iter_trace_records(trace_path):
            where = rec.get("where")
            if not isinstance(where, dict):
                continue
            file, line = where.get("file"), where.get("line")
            if isinstance(file, str) and file and _is_lineno(line):
                wanted[file].add(line)
    except OSError:
        return {}
    out: dict[str, str] = {}
    for file, lines in wanted.items():
        path = file if os.path.isabs(file) else os.path.join(root_real, file)
        if runner is not None and os.path.isabs(file) and not _within(os.path.realpath(path),
                                                                     root_real):
            path = to_host(runner, file) or path
        real = os.path.realpath(path)
        if not _within(real, root_real):
            continue
        source = _read_source_lines(real)
        if source is None:
            continue
        for ln in sorted(lines):
            if ln <= len(source):
                out[f"{file}:{ln}"] = source[ln - 1].strip()[:SITE_TEXT_MAX]
    return out


def store_site_text(trace_path: str | Path, site_text: Mapping[str, str]) -> str | None:
    """Merge ``site_text`` into ``<trace>.meta.json`` (atomically; other keys kept).

    Call it after anything else that (re)writes the sidecar. Returns the sidecar path, or
    ``None`` when it could not be written.
    """
    path = metadata_path(trace_path)
    payload: dict[str, Any] = {}
    try:
        with path.open(encoding="utf-8") as fh:
            loaded = json.load(fh)
        if isinstance(loaded, dict):
            payload = loaded
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        pass
    merged = payload.get("site_text") if isinstance(payload.get("site_text"), dict) else {}
    merged = {**merged, **dict(site_text)}
    payload["site_text"] = merged
    try:
        atomic_write_json(path, payload, indent=2, sort_keys=True)
    except OSError:
        return None
    return str(path)


def record_site_text(trace_path: str | Path, root: str | Path,
                     runner: RunnerSpec | None = None) -> int:
    """``collect_site_text`` + ``store_site_text``; returns the number of lines recorded."""
    site_text = collect_site_text(trace_path, root, runner)
    if site_text:
        store_site_text(trace_path, site_text)
    return len(site_text)


# ── fold ──────────────────────────────────────────────────────────────────────────────────


@dataclass
class _Acc:
    symbol: str
    file: str
    func: str
    line: int | None
    text: str | None
    input: Any
    first_seen: int
    where: dict[str, Any] | None
    qualnames: set[str] = field(default_factory=set)
    symq: str | None = None
    defaulted: set[tuple[str, ...]] | None = None
    outcomes: dict[str, list] = field(default_factory=dict)  # canon -> [kind, value, count]
    count: int = 0
    warnings: dict[tuple[str, str], int] | None = None
    binding: bool = False
    attr_of: str | None = None
    ts_min: float | None = None
    ts_max: float | None = None
    input_order: dict[str, Any] | None = None
    seen_order: bool = False


_MAX_CALL_SECONDS = 3600.0
_EPOCH_MIN = 1e9  # 2001-09-09: a number below it, at any scale, is no clock reading


def _call_time(raw: Mapping[str, Any]) -> tuple[float, float] | None:
    """(start, end) of one event's call from its ``ts`` (written at return) and ``dur``."""
    ts, dur = raw.get("ts"), raw.get("dur")
    if isinstance(ts, bool) or not isinstance(ts, int | float) or not ts > 0:
        return None
    try:
        end = float(ts)
        # a duration beyond an hour is a broken start (a frame the backend lost), not a call
        start = end - float(dur) if isinstance(dur, int | float) and not isinstance(
            dur, bool) and 0 <= dur <= _MAX_CALL_SECONDS else end
    except (OverflowError, ValueError):
        return None
    return start, end


def fold_facts(
    records: Iterable[Mapping[str, Any]],
    *,
    root: str | Path | None = None,
    site_text: Mapping[str, str] | None = None,
    source: str | None = None,
    runner: RunnerSpec | None = None,
    runner_source: str | None = None,
) -> FactSet:
    """Fold v1/v2 JSONL events and/or aggregated MemorySink records into a FactSet.

    Facts are kept at line granularity (file, func, line, input); ``FactSet.keyed()`` and
    ``diff_facts`` merge lines that share a line text.
    """
    root_s = os.path.abspath(str(root)) if root is not None else None
    site_text = site_text or {}
    rel_cache: dict[str, str] = {}
    acc: dict[tuple, _Acc] = {}
    fs = FactSet(source=source, root=root_s, site_text_entries=len(site_text),
                 runner=runner, path_map_source=runner_source if runner else None)
    versions: set[int] = set()
    # Identical inputs / outcomes / call sites recur across facts (one result shared by many
    # inputs, one input at many sites): keep ONE object per canonical form. Decoded JSON is
    # ~5 KB per fact otherwise, which made a capped snapshot fold in >500 MB.
    pool: dict[str, Any] = {}
    where_pool: dict[tuple, dict] = {}
    for index, raw in enumerate(records):
        ev = _normalize_record(raw)
        if ev is None:
            continue
        fs.events += 1
        if ev["aggregated"]:
            fs.aggregated = True
        versions.add(ev["v"])
        if ev["sym"]:
            fs.sym_events += 1
        if ev["defaulted"] is not None:
            fs.defaulted_events += 1
        where = ev["where"]
        abs_file = where.get("file") if isinstance(where.get("file"), str) else None
        line = where.get("line") if _is_lineno(where.get("line")) else None
        func = where.get("func") if isinstance(where.get("func"), str) else "?"
        if abs_file is None:
            rel = "?"
        else:
            rel = rel_cache.get(abs_file)
            if rel is None:
                rel = rel_cache[abs_file] = _site_path(fs, abs_file)
        symbol = ev["sym"] or ev["q"]
        attr_of = None
        accessed = _module_getattr_name(symbol, ev["input"])
        if accessed is not None:  # ``mod.__getattr__(name)`` is the attribute access ``mod.name``
            attr_of, symbol = symbol, f"{symbol[:-len('__getattr__')]}{accessed}"
        ikey = _canon(ev["input"])
        ikey = pool.setdefault("i" + ikey, ikey)
        key = (symbol, rel, func, line, ikey, ev["binding"])
        a = acc.get(key)
        if a is None:
            text = site_text.get(f"{abs_file}:{line}") if abs_file and line is not None else None
            wkey = (abs_file, where.get("line"), where.get("func"), len(where))
            shared_where = where_pool.get(wkey) if where else None
            if where and shared_where is None:
                shared_where = where_pool[wkey] = dict(where)
            a = acc[key] = _Acc(symbol=symbol, file=rel, func=func, line=line, text=text,
                                input=pool.setdefault("v" + ikey, ev["input"]),
                                first_seen=index, where=shared_where,
                                binding=ev["binding"], attr_of=attr_of)
        a.qualnames.add(ev["q"])
        if a.seen_order:
            if ev["input_order"] != a.input_order:
                a.input_order = _order.merge(a.input_order, ev["input_order"])
        else:
            a.input_order, a.seen_order = ev["input_order"], True
        if ev["symq"] and a.symq is None:
            a.symq = ev["symq"]
        if ev["defaulted"] is not None:
            if a.defaulted is None:
                a.defaulted = set()
            a.defaulted.add(ev["defaulted"])
        okey = _canon([ev["kind"], ev["value"]])
        slot = a.outcomes.get(okey)
        when = _call_time(raw)
        if slot is None:
            okey = pool.setdefault("o" + okey, okey)
            slot = a.outcomes[okey] = [ev["kind"], pool.setdefault("w" + okey, ev["value"]),
                                       ev["count"], None, None, ev["order"]]
        else:
            slot[2] += ev["count"]
            if ev["order"] != slot[5]:
                slot[5] = _order.merge(slot[5], ev["order"])
        if when is not None:
            slot[3] = when[0] if slot[3] is None else min(slot[3], when[0])
            slot[4] = when[1] if slot[4] is None else max(slot[4], when[1])
        a.count += ev["count"]
        if when is not None:
            a.ts_min = when[0] if a.ts_min is None else min(a.ts_min, when[0])
            a.ts_max = when[1] if a.ts_max is None else max(a.ts_max, when[1])
        if ev["warnings"]:
            a.warnings = merge_warnings(a.warnings if a.warnings is not None else {},
                                        ev["warnings"])
    pool.clear()
    where_pool.clear()
    fs.schema_versions = sorted(versions)
    ordered = sorted(acc.values(), key=lambda a: a.first_seen)
    acc.clear()
    fs.facts = [_fact_from_acc(a) for a in ordered]
    _note_foreign_paths(fs)
    return fs


def load_facts(
    trace_path: str | Path,
    *,
    root: str | Path | None = None,
    site_text: Mapping[str, str] | None = None,
    runner: "RunnerSpec | None | Literal['auto']" = "auto",
) -> FactSet:
    """Load a trace (JSONL, aggregated JSON, or a facts snapshot) and its sidecar
    ``site_text``, then fold.

    A facts snapshot is read when it is the path given, or when the JSONL is absent or empty
    and ``<trace>.facts.json`` (``find_facts_snapshot``) exists; otherwise the JSONL is folded
    (a facts capture's JSONL holds the snapshot's projection). Whenever a snapshot belongs to
    the trace, its quality counters (evicted, widened, outcomes past the cap) and tallies
    annotate the FactSet.

    ``runner``: the container the trace was captured in. ``"auto"`` (default) finds it from
    the project's state (the capture slot's runner, else ``EnvConfig.runner``); ``None`` maps
    nothing."""
    p = Path(trace_path)
    if is_facts_snapshot(p):
        snap: Path | None = p
        jsonl: Path | None = None
        base = snapshot_trace_path(p)
    else:
        snap, jsonl, base = find_facts_snapshot(p), p, p
        if snap is not None and _is_blank(p):
            jsonl = None
    if site_text is None:
        site_text = load_site_text(base)
    stats = _ReadStats()
    records = iter_snapshot_records(snap, _mark=True) if jsonl is None and snap is not None \
        else iter_trace_records(trace_path, stats, _mark=True)
    runner_source = "explicit" if isinstance(runner, RunnerSpec) else None
    if runner == "auto":
        runner, runner_source = runner_for_trace(base, root)
    fs = fold_facts(records, root=root, site_text=site_text, source=str(trace_path),
                    runner=runner, runner_source=runner_source)
    fs.invalid_lines = stats.invalid_lines
    fs.aggregated = fs.aggregated or stats.aggregated
    meta = _read_json(metadata_path(base))
    backends = _backends_of(meta)
    header: dict = {}
    if snap is not None:
        _annotate_from_snapshot(fs, snap, site_text)
        header = read_snapshot_header(snap)
        if not backends:
            backends = _backends_of(header)
    fs.backends = backends
    fs.warnings_capture = _warnings_capture_of(meta, header)
    annotate_capture_meta(fs, meta)
    if header and not _capture_procs(meta):
        annotate_value_format(fs, header)  # a snapshot without its sidecar: its own header
    _add_sidecar_tally_warnings(fs, meta, site_text)
    _annotate_c_cap(fs, meta, site_text)
    return fs


def annotate_capture_meta(fs: FactSet, meta: Mapping[str, Any]) -> FactSet:
    """The capture's environment identity and event loss, from its metadata sidecar: Python
    versions and run windows per process, the project root as the traced processes saw it (the
    container path for a container capture, plus the host root), and the dropped events."""
    procs = [p for p in (meta.get("processes") or ()) if isinstance(p, Mapping)] or [meta]
    fs.python_versions = sorted({str(p["python"]) for p in procs if p.get("python")})
    windows = []
    for p in procs:
        start, end = p.get("started_at"), p.get("ended_at")
        if isinstance(start, int | float) and isinstance(end, int | float) and end >= start:
            windows.append((float(start), float(end)))
    fs.run_windows = windows
    roots = [fs.root] if fs.root else []
    if fs.runner is not None and fs.root:
        seen = to_container(fs.runner, fs.root)
        if seen:
            roots.append(seen)
    fs.env_roots = sorted({r.rstrip("/") for r in roots if r and r.rstrip("/")},
                          key=lambda r: -len(r))
    fs.host_capture = not fs.paths_mapped
    dropped = None
    for counters in [p.get("counters") for p in procs] or [meta.get("counters")]:
        lost = _queue_drops(counters)
        if lost is not None:
            dropped = (dropped or 0) + lost
    fs.dropped_events = dropped
    seen: dict[str, set[str]] = {}
    for p in procs:
        dists = p.get("distributions")
        if isinstance(dists, Mapping):
            for name, version in dists.items():
                if isinstance(name, str) and isinstance(version, str) and version:
                    seen.setdefault(_dist_name(name), set()).add(version)
    fs.package_versions = {n: next(iter(v)) for n, v in seen.items() if len(v) == 1}
    annotate_value_format(fs, meta)
    return fs


def _capture_procs(meta: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [p for p in (meta.get("processes") or ()) if isinstance(p, Mapping)] or (
        [meta] if meta else [])


def annotate_value_format(fs: FactSet, meta: Mapping[str, Any]) -> bool:
    """What every process of the capture declared about its values and where it ran: the
    ``dict_order`` value format (order is compared only when both captures declare it) and
    the ``environment`` record (unknown unless every process has one). Returns whether the
    metadata said anything about either."""
    procs = _capture_procs(meta)
    fmts = [p.get("value_format") for p in procs]
    fs.dict_order = bool(procs) and all(
        isinstance(f, Mapping) and isinstance(f.get("dict_order"), int)
        and not isinstance(f.get("dict_order"), bool) and f["dict_order"] >= 1 for f in fmts)
    envs = [p.get("environment") for p in procs]
    fs.environment = [dict(e) for e in envs] if procs and all(
        isinstance(e, Mapping) for e in envs) else None
    return any(f is not None for f in fmts) or any(e is not None for e in envs)


def _dist_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


_VERSION_TOKEN = re.compile(r"\d+(?:\.\d+)+(?:[.\-+]?[A-Za-z0-9]+)*")


def version_masks(old: FactSet, new: FactSet) -> tuple[dict[str, str], dict[str, str]]:
    """Per side, ``{version string: placeholder}`` for each distribution loaded at a different
    version on the two sides (``⟨ver:requests⟩`` for 2.28.1 on one side, 2.31.0 on the other):
    a User-Agent or ``__version__`` is environment identity, not behaviour. Only versions
    shaped like one with at least five characters (``2.28.1``, not ``1.0``), and never a
    string that names two such distributions on one side."""
    changed = sorted(n for n in old.package_versions.keys() & new.package_versions.keys()
                     if old.package_versions[n] != new.package_versions[n])
    out: list[dict[str, str]] = []
    for fs in (old, new):
        by_version: dict[str, set[str]] = defaultdict(set)
        for n in changed:
            v = fs.package_versions[n]
            if len(v) >= 5 and _VERSION_TOKEN.fullmatch(v):
                by_version[v].add(n)
        out.append({v: f"⟨ver:{next(iter(ns))}⟩" for v, ns in by_version.items() if len(ns) == 1})
    return out[0], out[1]


_QUEUE_DROPS = ("backpressure_dropped", "shutdown_dropped", "write_failures")


def _queue_drops(counters: Any) -> int | None:
    """Events the runtime observed and then lost (queue back-pressure, shutdown, failed
    writes). Never ``sampling_dropped``: a call the sampler declined by design is not lost
    evidence, its tallies still count it."""
    if not isinstance(counters, Mapping):
        return None
    parts = [counters.get(k) for k in _QUEUE_DROPS]
    if any(isinstance(n, int) for n in parts):
        return sum(n for n in parts if isinstance(n, int))
    total, sampled = counters.get("dropped_events"), counters.get("sampling_dropped")
    if isinstance(total, int):
        return max(0, total - (sampled if isinstance(sampled, int) else 0))
    return None


def _warnings_capture_of(*sources: Mapping[str, Any]) -> str | None:
    """"on" / "off" / "mixed" from a sidecar or snapshot header (its processes included);
    None when no runtime said (a runtime that predates warning capture)."""
    seen: set[str] = set()
    for meta in sources:
        for item in (meta, *(meta.get("processes") or ())):
            if isinstance(item, dict) and isinstance(item.get("warnings_capture"), str):
                seen.add(item["warnings_capture"])
    if not seen:
        return None
    return seen.pop() if len(seen) == 1 else "mixed"


def _site_warnings(fs: FactSet, item: Mapping[str, Any],
                   site_text: Mapping[str, str] | None) -> SiteWarnings | None:
    warns = item.get("warnings")
    where = item.get("where") if isinstance(item.get("where"), dict) else {}
    file = where.get("file")
    if not isinstance(warns, list) or not warns or not isinstance(file, str):
        return None
    line = where.get("line") if _is_lineno(where.get("line")) else None
    return SiteWarnings(
        symbol=item.get("sym") or item.get("q") or "?",
        file=_site_path(fs, file),
        func=where.get("func") if isinstance(where.get("func"), str) else "?",
        line=line,
        text=(site_text or {}).get(f"{file}:{line}") if line is not None else None,
        warnings=_call_warnings(merge_warnings({}, warns)),
    )


def _add_sidecar_tally_warnings(fs: FactSet, meta: Mapping[str, Any],
                                site_text: Mapping[str, str] | None) -> None:
    """JSONL captures: unsampled calls' warnings from ``tally_warnings`` (per process)."""
    for item in (meta, *(meta.get("processes") or ())):
        if not isinstance(item, dict):
            continue
        for tw in item.get("tally_warnings") or ():
            if isinstance(tw, dict):
                sw = _site_warnings(fs, tw, site_text)
                if sw is not None:
                    fs.unsampled_warnings.append(sw)


def _annotate_c_cap(fs: FactSet, meta: Mapping[str, Any],
                    site_text: Mapping[str, str] | None) -> None:
    """The compiled-call cap's account of each process (``c_site_cap``): the sites where only
    the first calls were recorded, how many were counted after them, and whether the list is
    complete. A process without one ran a runtime that predates the account."""
    for proc in _capture_procs(meta):
        cap = proc.get("c_site_cap")
        if not isinstance(cap, Mapping):
            fs.c_cap_unreported += 1
            continue
        fs.c_cap_reported += 1
        fs.c_muted += _positive_int(cap.get("muted"))
        if _positive_int(cap.get("sites_omitted")) or _positive_int(cap.get("unsited")):
            fs.c_capped_complete = False
        recorded = _positive_int(cap.get("max")) or _LEGACY_C_SITE_MAX
        for site in cap.get("sites") or ():
            if not isinstance(site, Mapping) or not isinstance(site.get("file"), str):
                continue
            file, line = site["file"], site.get("line") if _is_lineno(site.get("line")) else None
            fs.c_capped.append(CappedSite(
                symbol=str(site.get("sym") or "?"), file=_site_path(fs, file, count=False),
                func=site.get("func") if isinstance(site.get("func"), str) else "?", line=line,
                text=(site_text or {}).get(f"{file}:{line}") if line is not None else None,
                recorded=recorded, muted=_positive_int(site.get("muted")),
                exact=site.get("exact") is not False))


def _read_json(path: Path) -> dict:
    try:
        with path.open(encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _backends_of(meta: Mapping[str, Any]) -> list[str]:
    """Backend names in a trace sidecar or snapshot header (``a,b`` lists included)."""
    names: list[str] = []

    def add(value: Any) -> None:
        if isinstance(value, str):
            for name in value.split(","):
                name = name.strip()
                if name and name not in names:
                    names.append(name)

    add(meta.get("backend"))
    for proc in meta.get("processes") or ():
        if isinstance(proc, dict):
            add(proc.get("backend"))
    return names


# Backends that see C-implemented dependency calls (``sys.setprofile``'s c_call events) and
# record their raises as ``{"c_level": true}`` facts; sys.monitoring and wrappers do not.
_C_LEVEL_BACKENDS = frozenset({"setprofile", "hybrid"})


def _annotate_from_snapshot(fs: FactSet, snap: Path,
                            site_text: Mapping[str, str] | None = None) -> None:
    header = read_snapshot_header(snap)
    counters = header.get("counters") if isinstance(header.get("counters"), dict) else {}

    def num(name: str) -> int:
        return _positive_int(counters.get(name))

    fs.snapshot = True
    fs.calls = num("calls") if "calls" in counters else None
    fs.sampled = num("sampled") if "sampled" in counters else None
    fs.evicted = num("evicted")
    fs.evicted_facts = num("evicted_facts")
    fs.widened = num("widened")
    fs.widened_facts = max(num("widened_facts"),
                           sum(1 for f in fs.facts if _is_widened_input(f.input)))
    fs.outcomes_other = num("outcomes_other")
    if num("tally_overflow"):
        fs.tally_sites = None  # the tallies are incomplete: no absence can be proven
        return
    site_text = site_text or {}
    sites: set[tuple[str, str, str]] = set()
    tallies: list[SiteTally] = []
    rel_cache: dict[str, str] = {}
    try:
        for _kind, t in iter_snapshot(snap, kinds=("tally",)):
            where = t.get("where") if isinstance(t.get("where"), dict) else {}
            file = where.get("file")
            count = _positive_int(t.get("count"))
            if not isinstance(file, str) or not count:
                continue
            rel = rel_cache.get(file)
            if rel is None:
                rel = rel_cache[file] = _site_path(fs, file)
            if t.get("warnings"):
                sw = _site_warnings(fs, t, site_text)
                if sw is not None:
                    fs.unsampled_warnings.append(sw)
            symbol = t.get("sym") or t.get("q") or "?"
            func = where.get("func") if isinstance(where.get("func"), str) else "?"
            line = where.get("line") if _is_lineno(where.get("line")) else None
            sites.add((symbol, rel, func))
            raises: dict[str, int] = {}
            for name, n in (t.get("raises") or {}).items():
                name = _raise_name(name)
                raises[name] = raises.get(name, 0) + _positive_int(n)
            if _positive_int(t.get("raises_other")):
                raises["<other>"] = raises.get("<other>", 0) + t["raises_other"]
            tallies.append(SiteTally(
                symbol=symbol, file=rel, func=func, line=line,
                text=site_text.get(f"{file}:{line}") if line is not None else None,
                count=count, returns=_positive_int(t.get("returns")), raises=raises,
                where=where))
    except OSError:
        fs.tally_sites = None
        return
    fs.tally_sites = sites
    fs.site_tallies = tallies
    fs.tallies_complete = (
        "tallied" in counters and "calls" in counters
        and not num("sampling_dropped") and not num("dropped_events")
        and not num("write_failures")
    )


def _constructor_inputs(inputs: dict[str, Any]) -> dict[str, Any]:
    """``__init__``'s receiver as ``{"__opaque__": <type>}``: the object under construction.

    Its state is what the constructor produces, not an input, and backends render it at
    different moments (setprofile / monitoring before the body runs: an empty or opaque
    object; wrap after the call: the finished one). Left as recorded, the same
    ``pd.Series(a)`` call keyed differently per backend and showed as DISAPPEARED +
    APPEARED instead of comparing."""
    bound = inputs.get("bound")
    if isinstance(bound, dict) and "self" in bound:
        return {**inputs, "bound": {**bound, "self": _receiver_marker(bound["self"])}}
    args = inputs.get("args")
    if isinstance(args, list) and args:
        return {**inputs, "args": [_receiver_marker(args[0]), *args[1:]]}
    return inputs


def _receiver_marker(value: Any) -> dict[str, str]:
    if isinstance(value, dict):
        for key in ("__opaque__", "__structure__", "__shape__", "__cycle__"):
            if isinstance(value.get(key), str):
                return {"__opaque__": value[key]}
    return {"__opaque__": type(value).__name__ if value is not None else "?"}


def _module_getattr_name(symbol: str, inputs: Any) -> str | None:
    """The attribute name of a module-level ``__getattr__(name)`` call (no receiver), else None."""
    if not symbol.endswith(".__getattr__") or not isinstance(inputs, dict):
        return None
    bound = inputs.get("bound")
    if not isinstance(bound, dict) or "self" in bound or "cls" in bound:
        return None
    name = bound.get("name")
    if not isinstance(name, str) or not name.isidentifier():
        return None
    # A class's ``__getattr__`` has a receiver; here the owner must look like a module path.
    owner = symbol[:-len(".__getattr__")].split(".")
    return name if owner and not owner[-1][:1].isupper() else None


def _objects_to_opaque(value: Any) -> Any:
    """``{"__object__": T, ...}`` -> ``{"__opaque__": T}`` anywhere in an input: a structural
    object is output evidence; keyed as an input it would make every call a new fact."""
    if isinstance(value, dict):
        if isinstance(value.get("__object__"), str):
            return {"__opaque__": value["__object__"]}
        return {k: _objects_to_opaque(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_objects_to_opaque(v) for v in value]
    return value


def _normalize_record(raw: Mapping[str, Any]) -> dict[str, Any] | None:
    """One JSONL event or one aggregated record -> a uniform event dict (None = not a call)."""
    if not isinstance(raw, Mapping):
        return None
    aggregated = False
    if "t" in raw:
        t = raw.get("t")
        if t not in ("return", "raise"):
            return None
        q = raw.get("q")
        inputs = raw.get("in")
        kind = t
        value = raw.get("result") if t == "return" else raw.get("raised")
        count = _positive_int(raw.get("count")) or 1  # projected snapshot facts carry counts
    elif "qualname" in raw:
        aggregated = True
        q = raw.get("qualname")
        inputs = raw.get("inputs")
        result, raised = raw.get("result"), raw.get("raised")
        if _is_nondet_marker(result):
            kind, value = "return", {"__nondeterministic__": True}  # distribution lost
        elif raised is not None:
            kind, value = "raise", raised
        else:
            kind, value = "return", result
        count = raw.get("count") if isinstance(raw.get("count"), int) else 1
        count = max(1, count)
    else:
        return None
    if not isinstance(q, str) or not q:
        q = "?"
    sym = raw.get("sym") if isinstance(raw.get("sym"), str) and raw.get("sym") else None
    defaulted = None
    if isinstance(inputs, dict):
        if isinstance(inputs.get("defaulted"), list):
            defaulted = tuple(sorted({str(n) for n in inputs["defaulted"]}))
        inputs = {k: v for k, v in inputs.items() if k != "defaulted"}
        if (sym or q).endswith(".__init__"):
            inputs = _constructor_inputs(inputs)
    inputs = _objects_to_opaque(inputs)
    if isinstance(inputs, dict) and inputs.get("__c_call__"):
        inputs = {"__c_call__": True}  # a compiled call: no observable arguments (arg0 varies)
    input_order = order = None
    if raw.get(_LINE_ORDER) is not False:   # a line naming ``__order__``, or not from a line
        inputs, input_order = _order.strip(inputs)
        if kind == "return":
            value, order = _order.strip(value)
    # the facts sink keeps the order beside the stripped value (a snapshot's side record)
    side_in = _side_order(raw.get("in_order"))
    if side_in:
        input_order = {**(input_order or {}), **side_in}
    side_out = _side_order(raw.get("order")) if kind == "return" else None
    if side_out:
        order = {**(order or {}), **side_out}
    if kind == "raise":
        value = _raise_name(value)
    v = raw.get("v")
    version = v if isinstance(v, int) and not isinstance(v, bool) else (2 if sym else 1)
    where = raw.get("where") if isinstance(raw.get("where"), dict) else {}
    warns = raw.get("warnings")
    return {"q": q, "sym": sym, "symq": raw.get("symq") if isinstance(raw.get("symq"), str)
            else None, "input": inputs, "defaulted": defaulted, "kind": kind, "value": value,
            "count": count, "where": where, "v": version, "aggregated": aggregated,
            "warnings": warns if isinstance(warns, list) and warns else None,
            "binding": kind == "raise" and raw.get("binding") is True,
            "input_order": input_order, "order": order}


UNTYPED_RAISE = "<unknown>"  # setprofile below 3.12: a raise handled without a call
# One shared "fields set" per model for constructed instances (every field is set): pydantic
# otherwise keeps a fresh set per instance, ~0.7 KB per fact. Assigning an attribute adds
# an already-present name, so sharing it is safe.
_FACT_FIELDS_SET = set(Fact.model_fields)
_OUTCOME_FIELDS_SET = set(Outcome.model_fields)
_WARNING_FIELDS_SET = set(CallWarning.model_fields)


def _raise_name(value: Any) -> str:
    """One spelling per exception type: ``builtins.TypeError`` (3.12+ monitoring) and
    ``TypeError`` (setprofile / wrappers) are the same type."""
    name = value if isinstance(value, str) else str(value)
    return name[len("builtins."):] if name.startswith("builtins.") else name


def _fact_from_acc(a: _Acc) -> Fact:
    # ``model_construct``: the fold built these values itself, and validation would copy
    # every list (~1.6 KB per fact, the bulk of a capped snapshot's fold).
    outcomes = sorted(a.outcomes.items())
    return Fact.model_construct(
        _FACT_FIELDS_SET, symbol=a.symbol, qualnames=sorted(a.qualnames), symq=a.symq,
        file=a.file, func=a.func,
        line=a.line, lines=[a.line] if a.line is not None else [], text=a.text, input=a.input,
        defaulted=None if a.defaulted is None else [list(d) for d in sorted(a.defaulted)],
        outcomes=[Outcome.model_construct(_OUTCOME_FIELDS_SET, kind=k, value=v, count=c,
                                          ts_min=t0, ts_max=t1, order=od)
                  for _, (k, v, c, t0, t1, od) in outcomes],
        count=a.count, where=a.where, first_seen=a.first_seen,
        warnings=_call_warnings(a.warnings), binding=a.binding, attr_of=a.attr_of,
        ts_min=a.ts_min, ts_max=a.ts_max, input_order=a.input_order,
    )


def _call_warnings(acc: Mapping[tuple[str, str], int] | None) -> list[CallWarning]:
    if not acc:
        return []
    return [CallWarning.model_construct(_WARNING_FIELDS_SET, **w) for w in warning_list(acc)]


def warnings_acc(warnings: Iterable[CallWarning] | None) -> dict[tuple[str, str], int]:
    """``CallWarning``s as a ``(category, message) -> count`` accumulator (to merge more)."""
    return merge_warnings({}, [w.model_dump() for w in warnings or ()])


def _merge_facts(facts: list[Fact]) -> Fact:
    """Merge facts that share a (coarser) key: outcome counts add, first-seen wins the rest."""
    if len(facts) == 1:
        return facts[0]
    ordered = sorted(facts, key=lambda f: f.first_seen)
    head = ordered[0]
    outcomes: dict[str, list] = {}
    defaulted: set[tuple[str, ...]] | None = None
    qualnames: set[str] = set()
    lines: set[int] = set()
    warns: dict[tuple[str, str], int] = {}
    for f in ordered:
        qualnames.update(f.qualnames)
        lines.update(f.lines)
        if f.defaulted is not None:
            defaulted = (defaulted or set()) | {tuple(d) for d in f.defaulted}
        for o in f.outcomes:
            k = _canon([o.kind, o.value])
            if k in outcomes:
                outcomes[k][2] += o.count
                outcomes[k][3] = _tmin(outcomes[k][3], o.ts_min)
                outcomes[k][4] = _tmax(outcomes[k][4], o.ts_max)
                if o.order != outcomes[k][5]:
                    outcomes[k][5] = _order.merge(outcomes[k][5], o.order)
            else:
                outcomes[k] = [o.kind, o.value, o.count, o.ts_min, o.ts_max, o.order]
        if f.warnings:
            merge_warnings(warns, [w.model_dump() for w in f.warnings])
    merged = head.model_copy(update={
        "qualnames": sorted(qualnames),
        "symq": next((f.symq for f in ordered if f.symq), None),
        "lines": sorted(lines),
        "defaulted": None if defaulted is None else [list(d) for d in sorted(defaulted)],
        "outcomes": [Outcome(kind=k, value=v, count=c, ts_min=t0, ts_max=t1, order=od)
                     for _, (k, v, c, t0, t1, od) in sorted(outcomes.items())],
        "input_order": _order.merge_all([f.input_order for f in ordered]),
        "count": sum(f.count for f in ordered),
        "warnings": _call_warnings(warns),
        "ts_min": min((f.ts_min for f in ordered if f.ts_min is not None), default=None),
        "ts_max": max((f.ts_max for f in ordered if f.ts_max is not None), default=None),
    })
    masks = frozenset().union(*(_masks_of(f) for f in ordered))
    if masks:
        _set_masks(merged, masks, _raw_of(head))
    return merged


def _tmin(a: float | None, b: float | None) -> float | None:
    return b if a is None else a if b is None else min(a, b)


def _tmax(a: float | None, b: float | None) -> float | None:
    return b if a is None else a if b is None else max(a, b)


def _masks_of(f: Fact) -> frozenset:
    """The masks the noise canonicalisation applied (none for a fact built without private
    state: ``model_construct``, an unpickled older model)."""
    private = getattr(f, "__pydantic_private__", None)
    return private.get("_masks", frozenset()) if private else frozenset()


def _raw_of(f: Fact) -> Fact:
    private = getattr(f, "__pydantic_private__", None)
    return (private.get("_raw") if private else None) or f


def _set_masks(f: Fact, masks: frozenset, raw: Fact) -> None:
    if getattr(f, "__pydantic_private__", None) is None:
        object.__setattr__(f, "__pydantic_private__", {})
    f.__pydantic_private__["_masks"] = masks
    f.__pydantic_private__["_raw"] = raw


# ── type reduction ────────────────────────────────────────────────────────────────────────


def value_type(value: Any) -> Any:
    """Reduce a normalized value to its type/structure, deterministically.

    * scalars -> their type name, as the runtime's ``typename()`` spells it (``NoneType``,
      ``bool``, ``int``, ``float``, ``str``); ``{"__shape__": T}`` -> ``T`` (privacy=shape
      renderings meet value renderings); ``__bytes__``/``__decimal__``/``__uuid__``/
      ``__timedelta__`` -> ``bytes``/``decimal.Decimal``/``uuid.UUID``/``datetime.timedelta``;
      ``__datetime__`` -> ``datetime.datetime``/``.date``/``.time`` from the ISO form
    * ``__list__``/``__set__`` -> ``{"list"|"set": [distinct element types]}`` (length-free);
      ``__tuple__`` -> ``{"tuple": [element types]}`` (arity kept);
      ``__dict__`` -> ``{"dict": {key: value type}}`` (field names kept, like ``contract``)
    * ``__structure__`` -> the rendering without ``values`` (i.e. the privacy=shape form:
      type, shape, dtype(s), columns, status_code); ``__dataclass__`` -> type + field types
    * ``__enum__`` -> ``{"enum": class}``; ``__type__``/``__callable__`` kept (structural)
    * ``__opaque__`` -> ``{"opaque": T}`` (``float-nonfinite`` -> ``float``; an
      ``ambiguous:*`` label -> ``{"ambiguous": label}``); ``__cycle__`` -> ``{"cycle": T}``;
      ``__nondeterministic__`` -> ``{"nondeterministic": True}``
    """
    if value is None:
        return "NoneType"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):
        return {"list": _type_union(value)}
    if not isinstance(value, dict):
        return type(value).__name__
    if "__opaque__" in value:
        label = str(value["__opaque__"])
        if label == "float-nonfinite":
            return "float"
        if label.startswith("ambiguous"):
            return {"ambiguous": label}
        return {"opaque": label}
    if "__shape__" in value:
        return str(value["__shape__"])
    if "__cycle__" in value:
        return {"cycle": str(value["__cycle__"])}
    if "__nondeterministic__" in value:
        return {"nondeterministic": True}
    for tag, name in _SIMPLE_TAG_TYPES.items():
        if tag in value:
            return name
    if "__datetime__" in value:
        iso = str(value["__datetime__"])
        if "T" in iso:
            return "datetime.datetime"
        return "datetime.time" if ":" in iso else "datetime.date"
    if "__enum__" in value:
        return {"enum": str(value["__enum__"]).rpartition(".")[0]}
    if "__type__" in value:
        return {"type": value["__type__"]}
    if "__object__" in value:
        out = {"object": str(value["__object__"])}
        if not value.get("__truncated__"):  # a truncated object lost attributes: name only
            out["attrs"] = {str(n): value_type(v) for n, v in _attr_items(value)}
        return out
    if "__callable__" in value:
        return {"callable": value["__callable__"]}
    if "__list__" in value:
        return {"list": _type_union(value["__list__"])}
    if "__set__" in value:
        return {"set": _type_union(value["__set__"])}
    if "__tuple__" in value:
        return {"tuple": [value_type(e) for e in value["__tuple__"]]}
    if "__dict__" in value:
        return {"dict": {_key_label(k): value_type(v) for k, v in _pairs(value["__dict__"])}}
    if "__dataclass__" in value:
        return {"dataclass": value["__dataclass__"],
                "fields": {str(n): value_type(v) for n, v in _pairs(value.get("fields"))}}
    if "__structure__" in value:
        out = {"structure": value["__structure__"]}
        out.update({k: v for k, v in value.items() if k not in ("__structure__", "values")})
        return out
    return {k: value_type(v) for k, v in sorted(value.items())}


def outcome_type(outcome: Outcome) -> dict[str, Any]:
    """``{"raise": T}`` stays as is (already a type); ``{"return": value_type(result)}``."""
    if outcome.kind == "raise":
        return {"raise": outcome.value}
    return {"return": value_type(outcome.value)}


def input_type(inputs: Any) -> Any:
    """``value_type`` over an ``in`` envelope, keeping the bound parameter names."""
    if not isinstance(inputs, dict):
        return value_type(inputs)
    out: dict[str, Any] = {}
    for k, v in inputs.items():
        if k == "bound" and isinstance(v, dict):
            out[k] = {n: value_type(x) for n, x in v.items()}
        elif k == "unbound":
            out[k] = v
        else:
            out[k] = value_type(v)
    return out


def types_match(a: Any, b: Any) -> bool:
    """Type-level equality of two ``value_type`` trees.

    Beyond structural equality: ``int`` and ``float`` are one numeric kind (PEP 484 numeric
    promotion; ``bool`` stays distinct), ``opaque``/``cycle`` match any type of the same name,
    and an empty ``list``/``set`` element union matches any element type."""
    if _canon(a) == _canon(b):
        return True
    if a in _NUMERIC and b in _NUMERIC:
        return True
    if _is_unobserved_type(a) or _is_unobserved_type(b):
        return True  # an unobserved C return stands for any return value
    if isinstance(a, dict) and ("nondeterministic" in a or "ambiguous" in a):
        return True
    if isinstance(b, dict) and ("nondeterministic" in b or "ambiguous" in b):
        return True
    if isinstance(a, dict) and ("opaque" in a or "cycle" in a):
        return _named_match(a, b)
    if isinstance(b, dict) and ("opaque" in b or "cycle" in b):
        return _named_match(b, a)
    if not (isinstance(a, dict) and isinstance(b, dict)):
        return False
    for kind in ("list", "set"):
        if kind in a and kind in b:
            xs, ys = a[kind], b[kind]
            return not xs or not ys or _cover(xs, ys, types_match)
    if "tuple" in a and "tuple" in b:
        return len(a["tuple"]) == len(b["tuple"]) and all(
            types_match(x, y) for x, y in zip(a["tuple"], b["tuple"]))
    if "dict" in a and "dict" in b:
        da, db = a["dict"], b["dict"]
        return da.keys() == db.keys() and all(types_match(da[k], db[k]) for k in da)
    if "type" in a and "type" in b:
        return _same_type_name(a["type"], b["type"])
    if "callable" in a and "callable" in b:
        return _same_closure(a["callable"], b["callable"])
    if "object" in a and "object" in b:
        if not _same_type_name(a["object"], b["object"]):
            return False
        if "attrs" not in a or "attrs" not in b:
            return True
        if _type_key(a["object"]) in _HANDLES.get() or _type_key(b["object"]) in _HANDLES.get():
            return True  # a handle: its state is observed through the calls made on it
        # attributes present on both sides; one-sided attributes are additive, not a change
        return all(types_match(a["attrs"][k], b["attrs"][k])
                   for k in a["attrs"].keys() & b["attrs"].keys())
    if "dataclass" in a and "dataclass" in b:
        fa, fb = a.get("fields", {}), b.get("fields", {})
        return (a["dataclass"] == b["dataclass"] and fa.keys() == fb.keys()
                and all(types_match(fa[k], fb[k]) for k in fa))
    return False


UNOBSERVED_RETURN = "unobserved:c-return"


def _is_unobserved_type(t: Any) -> bool:
    return isinstance(t, dict) and t.get("opaque") == UNOBSERVED_RETURN


def _is_unobserved(o: Outcome) -> bool:
    """A compiled call's return the backend could not see (``__c_call__`` events): a marker,
    not data."""
    return (o.kind == "return" and isinstance(o.value, dict)
            and o.value.get("__opaque__") == UNOBSERVED_RETURN)


def _named_match(named: dict, other: Any) -> bool:
    name = named.get("opaque", named.get("cycle"))
    if isinstance(other, dict) and ("opaque" in other or "cycle" in other):
        other_name = other.get("opaque", other.get("cycle"))
    else:
        other_name = _type_label(other)
    return other_name == name or (name, other_name) in _LABEL_ALIASES or (
        isinstance(other_name, str) and _same_type_name(name, other_name))


# Relocations noticed while comparing one pair of outcome distributions (see compare_outcomes).
@dataclass
class _Evidence:
    relocated: list[tuple[str, str]] = field(default_factory=list)
    drift: list[str] = field(default_factory=list)   # additive / absent-vs-empty attribute notes
    type_only: bool = False                         # something compared by type name only
    closures: list[tuple[str, str]] = field(default_factory=list)


_EVIDENCE: ContextVar[_Evidence | None] = ContextVar("pymolt_evidence", default=None)
# (top-level package, last name segment) of every type that is the receiver of an operation-like
# traced call on either side: a *handle*, whose state is observed through those calls.
_HANDLES: ContextVar[frozenset] = ContextVar("pymolt_handles", default=frozenset())
UNOBSERVABLE_NOTE = "the replacement on this line was not observable"
HANDLE_NOTE = "state of a handle differs; calls on it compare separately"
_SENTINEL_RE = re.compile(
    r"unset|missing|not_?set|no_?default|undefined|nothing|sentinel|no_?value|not_?given")


def _type_key(name: Any) -> tuple[str, str] | None:
    if not isinstance(name, str) or not name:
        return None
    return (name.split(".")[0], name.rsplit(".", 1)[-1])


def _receiver_type(inputs: Any) -> str | None:
    bound = inputs.get("bound") if isinstance(inputs, dict) else None
    recv = bound.get("self") if isinstance(bound, dict) else None
    if isinstance(recv, dict):
        for key in ("__opaque__", "__structure__", "__object__", "__type__"):
            if isinstance(recv.get(key), str):
                return recv[key]
    return None


def _is_operation(f: Fact) -> bool:
    """A call that binds something beyond its receiver, or has defaulted parameters: more than
    a property read (``Result.output`` binds only ``self`` and defaults nothing)."""
    inputs = f.input
    if not isinstance(inputs, dict):
        return False
    bound = inputs.get("bound")
    if isinstance(bound, dict) and any(k not in ("self", "cls") for k in bound):
        return True
    if any(d for d in (f.defaulted or ())):
        return True
    return any(inputs.get(k) not in (None, [], {}, {"__tuple__": []}, {"__dict__": []})
               for k in ("args", "varargs", "kwargs"))


def _handle_types(*sides: list[Fact]) -> frozenset:
    out = set()
    for facts in sides:
        for f in facts:
            if _is_operation(f):
                key = _type_key(_receiver_type(f.input))
                if key:
                    out.add(key)
    return frozenset(out)


def _is_sentinel(v: Any) -> bool:
    if not isinstance(v, dict):
        return False
    for tag in ("__enum__", "__opaque__", "__object__"):
        name = v.get(tag)
        if isinstance(name, str):
            parts = name.lower().replace("-", "_").split(".")
            tail = parts[-1].strip("_")
            return bool(_SENTINEL_RE.search("".join(parts[-2:]).replace("__", "_"))
                        or tail == "default")
    return False


def _closure_leaf(name: Any) -> str | None:
    if isinstance(name, str) and "<locals>." in name:
        return name.rsplit("<locals>.", 1)[1]
    return None


def _same_type_name(a: Any, b: Any) -> bool:
    """Equal type names, or one class moved within its package: the same top-level package and
    the same last name segment (``sqlalchemy.ext.declarative.api.Base`` ->
    ``sqlalchemy.orm.decl_api.Base``). A relocation is recorded for the comparison's note."""
    if a == b:
        return True
    if isinstance(a, str) and isinstance(b, str) and _reloc_equiv(a, b):
        ev = _EVIDENCE.get()
        if ev is not None and (a, b) not in ev.relocated:
            ev.relocated.append((a, b))
        return True
    if not (isinstance(a, str) and isinstance(b, str)) or "." not in a or "." not in b:
        return False
    if a.split(".")[0] != b.split(".")[0] or a.rsplit(".", 1)[1] != b.rsplit(".", 1)[1]:
        return False
    ev = _EVIDENCE.get()
    if ev is not None and (a, b) not in ev.relocated:
        ev.relocated.append((a, b))
    return True


_EMPTY_TAGS = ("__list__", "__tuple__", "__set__", "__dict__", "__bytes__")


def _is_empty_value(v: Any) -> bool:
    if v in ("", [], {}):
        return v is not None
    return isinstance(v, dict) and len(v) == 1 and any(
        k in v and v[k] in ("", []) for k in _EMPTY_TAGS)


def _attrs_match(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    """Attributes present on both sides compare (None vs an empty value of the same family and
    an attribute on one side only are drift, not a change); a nested object compares by type."""
    ev = _EVIDENCE.get()
    xs, ys = dict(_attr_items(a)), dict(_attr_items(b))
    for name in sorted(xs.keys() ^ ys.keys()):
        if ev is not None:
            ev.drift.append(f"attribute {'removed' if name in xs else 'added'}: {name}")
    return all(_attr_value_match(name, xs[name], ys[name]) for name in xs.keys() & ys.keys())


def _attr_value_match(name: str, x: Any, y: Any) -> bool:
    ev = _EVIDENCE.get()
    if _canon(x) == _canon(y):
        return True
    if (x is None and _is_empty_value(y)) or (y is None and _is_empty_value(x)):
        if ev is not None:
            ev.drift.append(f"absent vs empty: {name}")
        return True
    if _is_sentinel(x) != _is_sentinel(y):
        if ev is not None:
            ev.drift.append(f"sentinel resolved: {name}")
        return True
    if isinstance(x, list) and isinstance(y, list):
        return len(x) == len(y) and all(_attr_value_match(name, p, q) for p, q in zip(x, y))
    if isinstance(x, dict) and isinstance(y, dict):
        if "__object__" in x and "__object__" in y:
            if ev is not None:
                ev.type_only = True
            return _same_type_name(x["__object__"], y["__object__"])
        for tag in ("__list__", "__tuple__", "__set__"):
            if tag in x and tag in y and isinstance(x[tag], list) and isinstance(y[tag], list):
                return len(x[tag]) == len(y[tag]) and all(
                    _attr_value_match(name, p, q) for p, q in zip(x[tag], y[tag]))
    return _values_match(x, y)


def _same_closure(a: Any, b: Any) -> bool:
    """Two ``<locals>`` closures with the same leaf name and different enclosing functions
    (a decorator factory renamed): equal at type level, with a note."""
    la, lb = _closure_leaf(a), _closure_leaf(b)
    if la is None or la != lb:
        return False
    ev = _EVIDENCE.get()
    if ev is not None:
        ev.type_only = True
        ev.closures.append((a, b))
    return True


def _attr_items(value: Mapping[str, Any]) -> list[tuple[str, Any]]:
    attrs = value.get("attrs")
    if not isinstance(attrs, dict):
        return []
    return [(str(k), attrs[k]) for k in sorted(attrs, key=str)]


def _type_label(t: Any) -> str | None:
    """Top-level type name of a ``value_type`` tree."""
    if isinstance(t, str):
        return t
    if isinstance(t, dict):
        # ``object``: a structural rendering names its type like an opaque one does, so an
        # ``__opaque__`` T on one side (3.8 renders static types opaque) meets an ``__object__``
        # T on the other (3.12 renders them as heap types) at type level.
        for k in ("structure", "dataclass", "opaque", "cycle", "enum", "object"):
            if k in t:
                return str(t[k])
        for k in ("list", "tuple", "set", "dict"):
            if k in t:
                return k
        if "type" in t:
            return "type"
        if "callable" in t:
            return "function"
    return None


def _type_union(elems: Any) -> list:
    if not isinstance(elems, list):
        return []
    seen = {_canon(value_type(e)): value_type(e) for e in elems}
    return [seen[k] for k in sorted(seen)]


def _key_label(k: Any) -> str:
    return k if isinstance(k, str) else _canon(value_type(k))


def _pairs(items: Any) -> list[tuple[Any, Any]]:
    if not isinstance(items, list):
        return []
    return [(p[0], p[1]) for p in items if isinstance(p, list | tuple) and len(p) == 2]


# ── outcome comparison ────────────────────────────────────────────────────────────────────


@dataclass
class OutcomeComparison:
    """How two outcome distributions compared (``old_types``/``new_types`` at type level)."""

    comparison: Comparison
    level: ComparisonLevel | None
    notes: list[str] = field(default_factory=list)
    old_types: list[Any] | None = None
    new_types: list[Any] | None = None
    drift: bool = False  # equal, but an attribute was added/removed or went absent <-> empty


def compare_outcomes(
    old: list[Outcome], new: list[Outcome], profile: DiffProfile | str = DiffProfile.EXACT,
) -> OutcomeComparison:
    """Compare two outcome distributions (the support, not the counts). Equal only because a
    class moved within its package is equal at type level, with a ``relocated`` note."""
    ev = _Evidence()
    token = _EVIDENCE.set(ev)
    try:
        cmp = _compare_outcomes(old, new, DiffProfile(profile))
    finally:
        _EVIDENCE.reset(token)
    if cmp.comparison is Comparison.EQUAL and ev.drift:
        cmp.drift = True
        cmp.notes.extend(dict.fromkeys(ev.drift))
    if (ev.relocated or ev.type_only) and cmp.comparison is Comparison.EQUAL:
        cmp.notes.extend(f"relocated: {a} -> {b}" for a, b in ev.relocated)
        cmp.notes.extend(f"closure: {a} -> {b}" for a, b in ev.closures)
        if cmp.level is not ComparisonLevel.TYPE:
            cmp.level = ComparisonLevel.TYPE
            cmp.old_types, cmp.new_types = _distinct_types(old), _distinct_types(new)
    return cmp


def _compare_outcomes(
    old: list[Outcome], new: list[Outcome], profile: DiffProfile,
) -> OutcomeComparison:
    if any(_is_unrecorded(o) for o in (*old, *new)):
        return OutcomeComparison(Comparison.UNCOMPARABLE, None, ["nondeterministic-unrecorded"])
    o_keys = {_canon([o.kind, o.value]) for o in old}
    n_keys = {_canon([o.kind, o.value]) for o in new}
    nondet = len(o_keys) > 1 or len(n_keys) > 1
    notes = ["nondeterministic"] if nondet else []
    if any(_is_unobserved(o) for o in (*old, *new)):
        notes.append("c-return unobserved")
    # A raise recorded without its type (setprofile below 3.12, see _backend) can only be
    # compared as "a raise": against a typed raise on the other side that is a type-level
    # comparison.
    untyped = o_keys != n_keys and any(_is_untyped_raise(o) for o in (*old, *new))
    if untyped:
        notes.append("exception type unknown on one side")
    markers = untyped or any(_partial(o) for o in (*old, *new))
    if profile is DiffProfile.EXACT:
        if o_keys == n_keys or (nondet and _clock_sets_match(old, new)):
            level = ComparisonLevel.TYPE if markers else ComparisonLevel.EXACT
            return _with_types(Comparison.EQUAL, level, notes, old, new)
        if not nondet:
            a, b = old[0], new[0]
            ambiguous = _is_ambiguous(a) or _is_ambiguous(b)
            same = _outcomes_match(a, b)
            level = ComparisonLevel.TYPE if markers or ambiguous else ComparisonLevel.EXACT
            return _with_types(Comparison.EQUAL if same else Comparison.CHANGED, level, notes,
                               old, new)
    old_t = _distinct_types(old)
    new_t = _distinct_types(new)
    same = ({_canon(t) for t in old_t} == {_canon(t) for t in new_t}
            or _cover(old_t, new_t, _outcome_types_match))
    return _with_types(Comparison.EQUAL if same else Comparison.CHANGED, ComparisonLevel.TYPE,
                       notes, old, new)


def _with_types(comparison: Comparison, level: ComparisonLevel, notes: list[str],
                old: list[Outcome], new: list[Outcome]) -> OutcomeComparison:
    cmp = OutcomeComparison(comparison, level, notes)
    if level is ComparisonLevel.TYPE:
        cmp.old_types = _distinct_types(old)
        cmp.new_types = _distinct_types(new)
    return cmp


def _distinct_types(outcomes: list[Outcome]) -> list[Any]:
    seen = {}
    for o in outcomes:
        t = outcome_type(o)
        seen.setdefault(_canon(t), t)
    return [seen[k] for k in sorted(seen)]


def _outcomes_match(a: Outcome, b: Outcome) -> bool:
    if _is_unobserved(a) or _is_unobserved(b):
        return a.kind == "return" and b.kind == "return"
    if _is_ambiguous(a) or _is_ambiguous(b):
        return _ambiguous_compatible(outcome_type(a), outcome_type(b))
    if a.kind != b.kind:
        return False
    if a.kind == "raise":
        return _raises_match(a.value, b.value)
    return _values_match(a.value, b.value)


def _outcome_types_match(a: dict, b: dict) -> bool:
    if _type_is_ambiguous(a) or _type_is_ambiguous(b):
        return _ambiguous_compatible(a, b)
    if ("raise" in a) != ("raise" in b):
        return False
    if "raise" in a:
        return _raises_match(a["raise"], b["raise"])
    return types_match(a["return"], b["return"])


def _raises_match(a: Any, b: Any) -> bool:
    """Same exception type; an untyped raise matches any raise (raise vs return still
    differs, and two different known types still differ)."""
    a, b = _raise_name(a), _raise_name(b)
    return a == b or UNTYPED_RAISE in (a, b) or _reloc_equiv(a, b)


def _is_untyped_raise(o: Outcome) -> bool:
    return o.kind == "raise" and _raise_name(o.value) == UNTYPED_RAISE


def _ambiguous_compatible(a: dict, b: dict) -> bool:
    """``ambiguous:return-or-raise`` (<=3.7 runtimes) is compatible with ``return None`` and
    with any raise — exactly the two things it could have been."""
    if _type_is_ambiguous(a) and _type_is_ambiguous(b):
        return True
    other = b if _type_is_ambiguous(a) else a
    return "raise" in other or other.get("return") == "NoneType"


def _type_is_ambiguous(t: dict) -> bool:
    r = t.get("return")
    return isinstance(r, dict) and "ambiguous" in r


def _is_ambiguous(o: Outcome) -> bool:
    return (o.kind == "return" and isinstance(o.value, dict)
            and str(o.value.get("__opaque__", "")).startswith("ambiguous"))


def _values_match(a: Any, b: Any) -> bool:
    """Exact equality (type-strict: ``1 != 1.0 != True``), falling back to type equality only
    where either side holds a type-only rendering."""
    if _canon(a) == _canon(b):
        return True
    if isinstance(a, str) and isinstance(b, str) and "⟨clock:" in a:
        return clock_values_match(a, b)
    if _type_only(a) or _type_only(b):
        return types_match(value_type(a), value_type(b))
    if not (isinstance(a, dict) and isinstance(b, dict)):
        return False
    if "__type__" in a and "__type__" in b:
        return _same_type_name(a["__type__"], b["__type__"])
    if "__callable__" in a and "__callable__" in b:
        return _same_closure(a["__callable__"], b["__callable__"])
    if "__object__" in a and "__object__" in b:
        if a.get("__truncated__") or b.get("__truncated__"):  # truncated object: type level
            return types_match(value_type(a), value_type(b))
        if not _same_type_name(a["__object__"], b["__object__"]):
            return False
        if _attrs_match(a, b):
            return True
        handles = _HANDLES.get()
        if _type_key(a["__object__"]) in handles or _type_key(b["__object__"]) in handles:
            ev = _EVIDENCE.get()
            if ev is not None:
                ev.drift.append(HANDLE_NOTE)
            return True
        return False
    for tag in ("__list__", "__tuple__", "__set__"):
        if tag in a and tag in b:
            xs, ys = a[tag], b[tag]
            if not (isinstance(xs, list) and isinstance(ys, list)):
                return False
            truncated = a.get("__truncated__") or b.get("__truncated__")
            if len(xs) != len(ys) and not truncated:
                return False
            return all(_values_match(x, y) for x, y in zip(xs, ys))
    if "__dict__" in a and "__dict__" in b:
        xs, ys = _pairs(a["__dict__"]), _pairs(b["__dict__"])
        return len(xs) == len(ys) and all(
            _values_match(ka, kb) and _values_match(va, vb)
            for (ka, va), (kb, vb) in zip(xs, ys))
    if "__dataclass__" in a and "__dataclass__" in b:
        xs, ys = _pairs(a.get("fields")), _pairs(b.get("fields"))
        return (a["__dataclass__"] == b["__dataclass__"] and len(xs) == len(ys)
                and all(na == nb and _values_match(va, vb)
                        for (na, va), (nb, vb) in zip(xs, ys)))
    if "__structure__" in a and "__structure__" in b:
        if "values" in a and "values" in b:
            rest_a = {k: v for k, v in a.items() if k != "values"}
            rest_b = {k: v for k, v in b.items() if k != "values"}
            return _canon(rest_a) == _canon(rest_b) and _values_match(a["values"], b["values"])
        return types_match(value_type(a), value_type(b))
    return False


def _type_only(v: Any) -> bool:
    return isinstance(v, dict) and any(k in v for k in _TYPE_ONLY_KEYS)


def _partial(o: Outcome) -> bool:
    """True when an outcome's rendering is incomplete anywhere (compare-by-type territory)."""
    return o.kind == "return" and _has_partial(o.value)


def _has_partial(v: Any) -> bool:
    if isinstance(v, dict):
        if any(k in v for k in _TYPE_ONLY_KEYS) or v.get("__truncated__"):
            return True
        if "__structure__" in v and "values" not in v:
            return True
        return any(_has_partial(x) for x in v.values())
    if isinstance(v, list):
        return any(_has_partial(x) for x in v)
    return False


def _is_unrecorded(o: Outcome) -> bool:
    """An aggregated record collapsed to ``__nondeterministic__``: its outcomes are unknown."""
    return o.kind == "return" and _is_nondet_marker(o.value)


def _is_nondet_marker(v: Any) -> bool:
    return isinstance(v, dict) and "__nondeterministic__" in v


def _cover(xs: Iterable, ys: Iterable, match: Callable[[Any, Any], bool]) -> bool:
    xs, ys = list(xs), list(ys)
    return (all(any(match(x, y) for y in ys) for x in xs)
            and all(any(match(x, y) for x in xs) for y in ys))


# ── Round 3: edit provenance (line-mapped diff, ledger, anchors, representation) ───────────

_TYPE_RELOC: ContextVar[Mapping[str, str]] = ContextVar("pymolt_type_reloc", default={})
_NOISE_PREFIXES = ("importlib._bootstrap",)
_CLASS_HOOKS = ("__init_subclass__", "__set_name__", "__prepare__")
_APPEAR_HOOKS = ("__getattribute__",)  # a new implicit protocol hook is not a call anyone made
def _COMPOUND(node: ast.stmt) -> bool:  # noqa: N802
    return isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.For,
                             ast.AsyncFor, ast.While, ast.If, ast.With, ast.AsyncWith, ast.Try,
                             ast.Match, getattr(ast, "TryStar", ast.Try)))


_SIMPLE_STMTS = (ast.Return, ast.Assign, ast.AnnAssign, ast.Expr, ast.AugAssign)


def _normalized(entry: Mapping) -> Mapping:
    """The entry with a schema-1 claim (``same-object``, ...) read as ``preserve`` plus that
    ``basis`` (design 2.5): the ladder knows only ``preserve`` and ``intended``."""
    from pymolt.apply_ledger import CLAIMS, normalize_claim

    if entry.get("claim") not in CLAIMS:
        return entry
    claim, basis = normalize_claim(entry.get("claim"))
    return {**entry, "claim": claim, "basis": entry.get("basis") or basis}


def _vouches(entry: Mapping) -> bool:
    """A ledger entry whose claim makes its edit strict: ``preserve``."""
    return entry.get("claim") == "preserve"


def _attested(entry: Mapping) -> bool:
    """The entry's ``intended`` claim carries a person's attestation. ``attested`` is set by
    the coverage computation from the out-of-tree attestation log (``pymolt.attest``), never
    read from the ledger."""
    return entry.get("claim") == "intended" and entry.get("attested") is True


def _claim_rank(entry: Mapping) -> int:
    """How strictly an entry's claim judges its lines, for a line several entries cover (the
    strictest wins; design 5.1): ``preserve`` 3, ``intended`` unattested 2, no claim (a hand
    edit) 1, ``intended`` with a person-attested expectation 0."""
    if _vouches(entry):
        return 3
    if entry.get("claim") == "intended":
        return 0 if _attested(entry) else 2
    return 1


def _entry_lines(entry: Mapping, side: int) -> Iterable[int]:
    """The lines an entry covers on one side: where ``workorder.coverage`` located it
    (``located``), or, for an entry handed over directly, the range it carries."""
    located = entry.get("located")
    if isinstance(located, Mapping):
        return located.get("old" if side == 0 else "new") or ()
    rng = entry.get("old_lines" if side == 0 else "new_lines")
    if isinstance(rng, list | tuple) and len(rng) == 2:
        return range(rng[0], rng[1] + 1)
    return ()


def _reloc_equiv(a: str, b: str) -> bool:
    """``a`` and ``b`` are an old/new spelling pair from the plan's type relocations (full
    names or their last segments)."""
    for old_t, new_t in _TYPE_RELOC.get().items():
        names = {old_t, new_t, old_t.rsplit(".", 1)[-1], new_t.rsplit(".", 1)[-1]}
        if a != b and a in names and b in names:
            return True
    return False


class _Provenance:
    """Which side's lines were edited (line-mapped diff of the sources), which edits are
    pymolt's own (the ledger) and what the static contact maps say."""

    def __init__(self, ctx: DiffContext | None, new_root: str | None) -> None:
        self.ctx = ctx
        self.new_root = new_root
        # No ledger (edits None) means nobody vouches for the edit: judge it as a hand edit.
        self.ledger_known = False
        self.no_ledger = False
        self.edits = [_normalized(e) for e in (ctx.edits if ctx else None) or []]
        self.symbol_map: dict[str, str] = {}
        if ctx is not None:
            self.symbol_map.update(ctx.successors or {})
            for e in self.edits:
                if e.get("old_symbol") and e.get("new_symbol"):
                    self.symbol_map[e["old_symbol"]] = e["new_symbol"]
        self.maps: dict[str, tuple[dict[int, tuple[str, int | None]],
                                   dict[int, tuple[str, int | None]]]] = {}
        old_src = (ctx.old_sources if ctx else None) or {}
        new_src = (ctx.new_sources if ctx else None) or {}
        for file in old_src.keys() & new_src.keys():
            self.maps[file] = _line_map(old_src[file], new_src[file])
        self.sources = (old_src, new_src)
        # Hand-edit judging needs line-mapped sources or a positive ledger; with neither (no
        # snapshots at all) the round-2 line-text rules apply, strictly.
        self.ledger_known = ctx is not None and (bool(self.maps) or ctx.edits is not None)
        self.no_ledger = self.ledger_known and ctx is not None and ctx.edits is None
        self._stmts: dict[tuple[int, str], dict[int, tuple] | None] = {}
        self._cover: dict[tuple[str, int], dict[int, Mapping]] | None = None

    def status(self, file: str, line: int | None, side: int) -> str | None:
        """``same`` / ``edited`` / ``gone`` (deleted or inserted) / None (no line map)."""
        m = self.maps.get(file)
        if m is None or line is None:
            return None
        st = m[side].get(line, ("gone", None))[0]
        if st == "same":  # a statement is edited if any of its lines is
            span = self.stmt_span(side, file, line)
            if span and any(m[side].get(ln, ("gone", None))[0] != "same"
                            for ln in range(span[0], span[1] + 1)):
                return "edited"
        if st == "same":  # a decorator's call is attributed to the ``def`` line by some versions
            for dl in self._decorator_lines(side, file).get(line, ()):
                if m[side].get(dl, ("gone", None))[0] != "same":
                    return "edited"
        if st == "same" and self.header_edited(side, file, line):
            return "edited"  # a definition's decorator, ``def`` or ``class`` line was edited
        return st

    def _definition(self, side: int, file: str, line: int | None) -> tuple | None:
        """``(span, header, node)`` of the definition a call on ``line`` belongs to: a
        ``class`` statement or a decorated ``def``, when ``line`` is one of its header lines
        (decorators, the ``def``/``class`` line and its continuation). ``span`` is (first,
        last) line of the whole definition, ``header`` (first, last) of its header."""
        if line is None:
            return None
        key = (side + 10, file)
        if key not in self._stmts:
            table: dict[int, tuple] = {}
            text = self.sources[side].get(file)
            tree = None
            if text is not None:
                try:
                    tree = _parse_source(text)
                except (SyntaxError, ValueError):
                    tree = None
            for node in ast.walk(tree) if tree is not None else ():
                decorated = isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) \
                    and node.decorator_list
                if isinstance(node, ast.ClassDef) or decorated:
                    first = min([node.lineno, *(d.lineno for d in node.decorator_list)])
                    body = node.body[0].lineno if node.body else node.lineno
                    header = (first, max(node.lineno, body - 1))
                    info = ((first, node.end_lineno or node.lineno), header, node)
                    for ln in range(header[0], header[1] + 1):
                        table.setdefault(ln, info)
            self._stmts[key] = table if tree is not None else None  # type: ignore[assignment]
        table = self._stmts[key]
        return table.get(line) if table else None  # type: ignore[union-attr]

    def definition_span(self, side: int, file: str, line: int | None) -> tuple[int, int] | None:
        """(first, last) line of the definition a call on ``line`` (one of its header lines)
        belongs to: a ``class`` statement or a decorated ``def``."""
        d = self._definition(side, file, line)
        return d[0] if d else None

    def _edited(self, side: int, file: str, first: int, last: int) -> bool:
        m = self.maps.get(file)
        return m is not None and any(m[side].get(ln, ("gone", None))[0] != "same"
                                     for ln in range(first, last + 1))

    def header_edited(self, side: int, file: str, line: int | None) -> bool:
        """A call on a definition's header whose decorator, ``def`` or ``class`` lines were
        edited. An edit inside the body alone does not make the header's calls edited: a
        decorator receives the same function, a base class the same class name; only a call
        that receives the class namespace can see a body edit (``namespace_edits``)."""
        d = self._definition(side, file, line)
        return d is not None and self._edited(side, file, *d[1])

    def body_bound_names(self, side: int, file: str, line: int | None) -> set[str] | None:
        """For a call on a ``class`` header: the names its body binds on edited lines
        (assignment targets of an edited statement; a nested ``def``/``class`` whose decorator
        or ``def``/``class`` lines were edited). None when ``line`` is no class header."""
        d = self._definition(side, file, line)
        if d is None or not isinstance(d[2], ast.ClassDef):
            return None
        names: set[str] = set()

        def visit(stmts: list[ast.stmt]) -> None:
            for st in stmts:
                if isinstance(st, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                    first = min([st.lineno, *(x.lineno for x in st.decorator_list)])
                    body = st.body[0].lineno if st.body else st.lineno
                    if self._edited(side, file, first, max(st.lineno, body - 1)):
                        names.add(st.name)
                    continue
                if isinstance(st, ast.Assign | ast.AnnAssign | ast.AugAssign):
                    if self._edited(side, file, st.lineno, st.end_lineno or st.lineno):
                        targets = st.targets if isinstance(st, ast.Assign) else [st.target]
                        for t in targets:
                            names.update(x.id for x in ast.walk(t) if isinstance(x, ast.Name))
                    continue
                for field_name in ("body", "orelse", "finalbody", "handlers"):
                    inner = getattr(st, field_name, None)
                    if isinstance(inner, list):
                        visit([x for x in inner if isinstance(x, ast.stmt)]
                              + [y for h in inner if isinstance(h, ast.ExceptHandler)
                                 for y in h.body])

        visit(d[2].body)
        return names

    def edited_span_lines(self, side: int, file: str, line: int | None) -> list[int]:
        """The edited lines inside the definition span ``line`` (a header line) belongs to
        (empty when none, or without source snapshots)."""
        m = self.maps.get(file)
        span = self.definition_span(side, file, line) if m is not None else None
        if span is None:
            return []
        return [ln for ln in range(span[0], span[1] + 1)
                if m[side].get(ln, ("gone", None))[0] != "same"]

    def covering_site(self, file: str, line: int | None, side: int) -> Mapping | None:
        """The entry whose claim judges a call (design 5.1): the one deciding its own line;
        else, when every edited line of the definition span it belongs to is covered, the least
        strict of theirs (the call sees each of those edits); None: a hand edit."""
        entry = self.covering(file, line, side)
        if entry is not None:
            return entry
        edited = self.edited_span_lines(side, file, line)
        found = [self.covering(file, ln, side) for ln in edited]
        if not found or any(e is None for e in found):
            return None
        return min(found, key=_claim_rank)

    def resolves_to_project(self, file: str, line: int | None, symbol: str) -> bool:
        """On the new side, the callee at ``line`` whose leaf is the symbol's leaf is rooted in a
        name that resolves to project code: a ``def`` / ``class`` in the file, an import from a
        project module, or an assignment from one of those. Local variables, parameters,
        dependency imports and anything unknown are not."""
        text = self.sources[1].get(file)
        if text is None or line is None:
            return False
        try:
            tree = _parse_source(text)
        except (SyntaxError, ValueError):
            return False
        span = self.stmt_span(1, file, line) or (line, line)
        leaf = _symbol_leaf(symbol)
        roots: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and span[0] <= node.lineno <= span[1]:
                fn = node.func
                name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
                if name == leaf:
                    while isinstance(fn, ast.Attribute):
                        fn = fn.value
                    if isinstance(fn, ast.Name):
                        roots.append(fn.id)
        if not roots:
            return False
        scope = _enclosing_function(tree, line)
        return all(self._project_name(tree, scope, r, 0) for r in roots)

    def _is_project_module(self, module: str, level: int) -> bool:
        if level:
            return True  # a relative import stays inside the project
        rel = module.replace(".", "/")
        new_src = self.sources[1]
        if any(k in new_src for k in (rel + ".py", rel + "/__init__.py")):
            return True
        root = self.new_root
        return bool(root) and (os.path.exists(os.path.join(root, rel + ".py"))
                               or os.path.exists(os.path.join(root, rel, "__init__.py")))

    def _project_name(self, tree: ast.AST, scope: ast.AST | None, name: str, depth: int) -> bool:
        if depth > 3:
            return False
        if scope is not None and name in _param_names(scope):
            return False
        for body_owner in (scope, tree):
            if body_owner is None:
                continue
            for node in ast.walk(body_owner):
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) \
                        and node.name == name and node is not scope:
                    return True
                if isinstance(node, ast.ImportFrom):
                    for a in node.names:
                        if (a.asname or a.name) == name:
                            return self._is_project_module(node.module or "", node.level)
                if isinstance(node, ast.Import):
                    for a in node.names:
                        if (a.asname or a.name.split(".")[0]) == name:
                            return self._is_project_module(a.name, 0)
                if isinstance(node, ast.Assign) and any(
                        isinstance(t, ast.Name) and t.id == name for t in node.targets):
                    v = node.value  # an alias of project code; a call's result is a runtime value
                    while isinstance(v, ast.Attribute):
                        v = v.value
                    if isinstance(v, ast.Name):
                        return v.id != name and self._project_name(tree, scope, v.id, depth + 1)
                    return False
        return False

    def _simple_stmts(self, side: int, file: str) -> list[ast.stmt]:
        key = (side + 8, file)
        if key not in self._stmts:
            text = self.sources[side].get(file)
            out: list[ast.stmt] = []
            if text is not None:
                try:
                    tree = _parse_source(text)
                except (SyntaxError, ValueError):
                    tree = None
                out = [n for n in ast.walk(tree) if isinstance(n, ast.stmt)
                       and not _COMPOUND(n)] if tree is not None else []
                out.sort(key=lambda n: n.lineno)
            self._stmts[key] = out  # type: ignore[assignment]
        return self._stmts[key]  # type: ignore[return-value]

    def naming_status(self, side: int, file: str, line: int | None, symbol: str) -> str | None:
        """For a fact whose callee leaf is not named on its own statement (a generator body runs
        when first consumed, so the fact lands on the consumer): the edit status of the nearest
        preceding statement in the same function that does name it. None when its own statement
        names it, it is a dunder, or nothing names it."""
        leaf = _symbol_leaf(symbol)
        if line is None or (leaf.startswith("__") and leaf.endswith("__")):
            return None
        stmts = self._simple_stmts(side, file)
        own = [n for n in stmts if n.lineno <= line <= (n.end_lineno or n.lineno)]
        if not own or any(_names_leaf(n, leaf) for n in own):
            return None
        start = min(n.lineno for n in own)
        text = self.sources[side].get(file)
        scope = _enclosing_function(_parse_source(text), line) if text else None
        lo, hi = (scope.lineno, scope.end_lineno) if scope is not None else (0, 10**9)
        prior = [n for n in stmts if lo <= n.lineno < start and (n.end_lineno or 0) <= hi
                 and _names_leaf(n, leaf)]
        if not prior:
            return None
        named = prior[-1]
        m = self.maps.get(file)
        if m is None:
            return None
        edited = any(m[side].get(ln, ("gone", None))[0] != "same"
                     for ln in range(named.lineno, (named.end_lineno or named.lineno) + 1))
        return "edited" if edited else "same"

    def attribute_read_evidence(self, side: int, file: str, line: int | None,
                                symbol: str) -> bool:
        """The statement's AST shows the leaf is read, not called, or called with arguments the
        fact did not bind (``G.edges(data=True)``: the getter, then a call on its value)."""
        leaf = _symbol_leaf(symbol)
        for n in self._simple_stmts(side, file):
            if line is None or not (n.lineno <= line <= (n.end_lineno or n.lineno)):
                continue
            called: set[int] = set()
            for node in ast.walk(n):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                        and node.func.attr == leaf:
                    called.add(id(node.func))
                    if node.args or node.keywords:
                        return True
            for node in ast.walk(n):
                if isinstance(node, ast.Attribute) and node.attr == leaf \
                        and id(node) not in called:
                    return True
        return False

    def stmt_span(self, side: int, file: str, line: int | None) -> tuple[int, int] | None:
        """The innermost enclosing simple statement's (first, last) line, from the snapshot."""
        if line is None:
            return None
        key = (side + 6, file)
        if key not in self._stmts:
            spans: dict[int, tuple[int, int]] = {}
            text = self.sources[side].get(file)
            tree = None
            if text is not None:
                try:
                    tree = _parse_source(text)
                except (SyntaxError, ValueError):
                    tree = None
            for node in ast.walk(tree) if tree is not None else ():
                if isinstance(node, ast.stmt) and not _COMPOUND(node):
                    end = node.end_lineno or node.lineno
                    for ln in range(node.lineno, end + 1):
                        spans[ln] = (node.lineno, end)
            self._stmts[key] = spans if tree is not None else None
        table = self._stmts[key]
        return table.get(line) if table else None

    def _decorator_lines(self, side: int, file: str) -> dict[int, list[int]]:
        key = (side + 4, file)
        if key not in self._stmts:
            out: dict[int, list[int]] = {}
            text = self.sources[side].get(file)
            if text is not None:
                try:
                    tree = _parse_source(text)
                except (SyntaxError, ValueError):
                    tree = None
                for node in ast.walk(tree) if tree is not None else ():
                    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) \
                            and node.decorator_list:
                        out[node.lineno] = [d.lineno for d in node.decorator_list]
            self._stmts[key] = out
        return self._stmts[key]  # type: ignore[return-value]

    def mapped_line(self, file: str, line: int | None, side: int) -> int | None:
        m = self.maps.get(file)
        if m is None or line is None:
            return None
        return m[side].get(line, ("gone", None))[1]

    def covering(self, file: str, line: int | None, side: int) -> Mapping | None:
        """The ledger entry that decides ``line`` (side 0: old, 1: new): of the entries that
        cover it, the one with the strictest claim (``_claim_rank``); None: no entry does."""
        if line is None:
            return None
        if self._cover is None:
            self._cover = {}
            for e in self.edits:
                for s in (0, 1):
                    table = self._cover.setdefault((e.get("file"), s), {})
                    for ln in _entry_lines(e, s):
                        current = table.get(ln)
                        if current is None or _claim_rank(e) > _claim_rank(current):
                            table[ln] = e
        return (self._cover.get((file, side)) or {}).get(line)

    def stmt_info(self, side: int, v: "_V") -> tuple | None:
        """``(kind, target, call leaf)`` of the one simple statement at the fact's line."""
        f = v.fact
        table = self._stmt_table(side, f.file)
        if table is not None and f.line in table:
            return table[f.line]
        return _parse_stmt_text(f.text)

    def in_class_body(self, side: int, file: str, line: int | None) -> bool:
        """The line sits directly in a class body (not inside a method): a class-creation site."""
        key = (side + 2, file)
        if key not in self._stmts:
            text = self.sources[side].get(file)
            lines: dict[int, tuple] | None = None
            if text is not None:
                try:
                    tree = _parse_source(text)
                except (SyntaxError, ValueError):
                    tree = None
                if tree is not None:
                    lines = {}
                    for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
                        lines[cls.lineno] = ()  # the ``class`` statement itself
                        for stmt in cls.body:
                            if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
                                continue
                            for ln in range(stmt.lineno, (stmt.end_lineno or stmt.lineno) + 1):
                                lines[ln] = ()
            self._stmts[key] = lines
        table = self._stmts[key]
        return table is not None and line in table

    def _stmt_table(self, side: int, file: str) -> dict[int, tuple] | None:
        key = (side, file)
        if key not in self._stmts:
            text = self.sources[side].get(file)
            table = None
            if text is not None:
                try:
                    tree = _parse_source(text)
                except (SyntaxError, ValueError):
                    tree = None
                if tree is not None:
                    table = {}
                    for node in ast.walk(tree):
                        if isinstance(node, _SIMPLE_STMTS):
                            info = _stmt_of(node)
                            for ln in range(node.lineno, (node.end_lineno or node.lineno) + 1):
                                table[ln] = info
            self._stmts[key] = table
        return self._stmts[key]


def _parse_source(text: str) -> ast.AST:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return ast.parse(text)


def _names_leaf(node: ast.AST, leaf: str) -> bool:
    for n in ast.walk(node):
        if isinstance(n, ast.Attribute) and n.attr == leaf:
            return True
        if isinstance(n, ast.Name) and n.id == leaf:
            return True
        if isinstance(n, ast.alias) and (n.asname or n.name).split(".")[-1] == leaf:
            return True
    return False


def _param_names(fn: ast.AST) -> set[str]:
    args = getattr(fn, "args", None)
    if args is None:
        return set()
    names = {a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)}
    names.update(a.arg for a in (args.vararg, args.kwarg) if a is not None)
    return names


def _enclosing_function(tree: ast.AST, line: int) -> ast.AST | None:
    best = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and \
                node.lineno <= line <= (node.end_lineno or node.lineno):
            if best is None or node.lineno >= best.lineno:
                best = node
    return best


def _line_map(old_text: str, new_text: str) -> tuple[dict[int, tuple[str, int | None]],
                                                     dict[int, tuple[str, int | None]]]:
    """difflib over stripped lines: per side, line -> (``same``, other line) | (``edited``,
    None) inside a replace hunk | (``gone``, None) for a deleted / inserted line."""
    a = [ln.strip() for ln in old_text.splitlines()]
    b = [ln.strip() for ln in new_text.splitlines()]
    om: dict[int, tuple[str, int | None]] = {}
    nm: dict[int, tuple[str, int | None]] = {}
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                om[i1 + k + 1] = ("same", j1 + k + 1)
                nm[j1 + k + 1] = ("same", i1 + k + 1)
        else:
            state = "edited" if tag == "replace" else "gone"
            for i in range(i1, i2):
                om[i + 1] = (state, None)
            for j in range(j1, j2):
                nm[j + 1] = (state, None)
    return om, nm


def _stmt_of(node: ast.AST) -> tuple | None:
    value = getattr(node, "value", None)
    if isinstance(node, ast.Return):
        kind, target = "return", ""
    elif isinstance(node, ast.Assign) and len(node.targets) == 1:
        kind, target = "assign", ast.dump(node.targets[0])
    elif isinstance(node, ast.AnnAssign):
        kind, target = "assign", ast.dump(node.target)
    else:
        return None
    if not isinstance(value, ast.Call):
        return None
    fn = value.func
    leaf = fn.attr if isinstance(fn, ast.Attribute) else fn.id if isinstance(fn, ast.Name) else None
    return (kind, target, leaf) if leaf else None


def _parse_stmt_text(text: str | None) -> tuple | None:
    if not text:
        return None
    for tail in ("", ")", "))", ")))"):
        try:
            tree = ast.parse(text + tail)
        except SyntaxError:
            continue
        if len(tree.body) == 1:
            return _stmt_of(tree.body[0])
    return None


def _wrapper_like(symbol: str) -> bool:
    return ".<locals>." in symbol or symbol.startswith("decorator.") or ".decorator." in symbol


def _symbol_leaf(symbol: str) -> str:
    parts = symbol.split(".")
    if parts[-1] in ("__init__", "__new__") and len(parts) > 1:
        return parts[-2]
    return parts[-1]


def _scalars(inputs: Any) -> list[Any]:
    bound = inputs.get("bound") if isinstance(inputs, dict) else None
    if not isinstance(bound, dict):
        return []
    return [v for k, v in bound.items()
            if k not in ("self", "cls") and isinstance(v, str | int | float | bool)]


def _anchored_pairs(old_left: list["_V"], new_left: list["_V"], prov: _Provenance,
                    ) -> list[tuple["_V", "_V"]]:
    """Pair edited-line leftovers inside one (file, function) by an *anchor*: the same symbol, a
    ledger / plan symbol mapping, or a one-statement edit (both ``return <call>`` / the same
    ``target = <call>``) whose outermost call matches the symbol. Scalar argument values
    choose within a candidate set; ambiguity stays unpaired."""
    olds = [v for v in old_left if v.rewritten_site]
    news = [v for v in new_left if v.rewritten_site]
    cross = True
    by_func: dict[tuple, tuple[list, list]] = defaultdict(lambda: ([], []))
    for v in olds:
        by_func[(v.fact.file, v.fact.func)][0].append(v)
    for v in news:
        by_func[(v.fact.file, v.fact.func)][1].append(v)

    def anchored(o: _V, n: _V) -> bool:
        if o.fact.symbol == n.fact.symbol:
            return True
        if not cross:
            return False
        if prov.symbol_map.get(o.fact.symbol) == n.fact.symbol:
            return True
        so, sn = prov.stmt_info(0, o), prov.stmt_info(1, n)
        return (so is not None and sn is not None and so[:2] == sn[:2]
                and so[2] == _symbol_leaf(o.fact.symbol)
                and sn[2] == _symbol_leaf(n.fact.symbol))

    pairs: list[tuple[_V, _V]] = []
    for os_, ns in by_func.values():
        if not os_ or not ns:
            continue
        edges = {(id(o), id(n)) for o in os_ for n in ns if anchored(o, n)}
        # connected components over the anchor edges
        comp: dict[int, int] = {}
        nodes = {id(v): v for v in (*os_, *ns)}
        parent = {k: k for k in nodes}

        def find(x: int, parent: dict[int, int] = parent) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for a, b in edges:
            parent[find(a)] = find(b)
        for k in nodes:
            comp[k] = find(k)
        groups: dict[int, tuple[list, list]] = defaultdict(lambda: ([], []))
        for o in os_:
            groups[comp[id(o)]][0].append(o)
        for n in ns:
            groups[comp[id(n)]][1].append(n)
        for go, gn in groups.values():
            if not go or not gn:
                continue
            go.sort(key=lambda v: v.first_seen)
            gn.sort(key=lambda v: v.first_seen)
            if len(go) == 1 and len(gn) == 1:
                if (id(go[0]), id(gn[0])) in edges:
                    pairs.append((go[0], gn[0]))
                continue
            chosen: set[int] = set()
            best: dict[int, tuple[int, _V] | None] = {}
            for o in go:
                so = _scalars(o.fact.input)
                scored = sorted(((sum(1 for x in so if x in _scalars(n.fact.input)), n)
                                 for n in gn if (id(o), id(n)) in edges),
                                key=lambda t: -t[0])
                best[id(o)] = (scored[0][1] if scored and scored[0][0] > 0
                               and (len(scored) == 1 or scored[1][0] < scored[0][0]) else None)
            for o in go:
                n = best[id(o)]
                if n is None or id(n) in chosen:
                    continue
                rivals = [x for x in go if best[id(x)] is n]
                if len(rivals) == 1:
                    pairs.append((o, n))
                    chosen.update((id(o), id(n)))
            rest_o = [o for o in go if id(o) not in chosen]
            rest_n = [n for n in gn if id(n) not in chosen]
            if len(rest_o) == 1 and len(rest_n) == 1 and (id(rest_o[0]), id(rest_n[0])) in edges:
                pairs.append((rest_o[0], rest_n[0]))
    return pairs


# representation-only: the same values in a different container or type family

def _content(v: Any) -> Any:
    """A canonical, container-free rendering of a value's content; ``_UNKNOWN`` when any part
    is opaque, cyclic or truncated (no digest, so no representation-only claim)."""
    if isinstance(v, dict):
        if any(k in v for k in _TYPE_ONLY_KEYS) or v.get("__truncated__"):
            return _UNKNOWN
        if "__structure__" in v:
            return _content(v["values"]) if "values" in v else _UNKNOWN
        for tag in ("__list__", "__tuple__"):
            if tag in v and isinstance(v[tag], list):
                return _seq([_content(x) for x in v[tag]])
        if "__set__" in v and isinstance(v["__set__"], list):
            return _seq(sorted((_content(x) for x in v["__set__"]), key=_canon), True)
        if "__object__" in v and isinstance(v.get("attrs"), dict):
            return _seq([_content(x) for x in v["attrs"].values()])
        if "__dict__" in v:
            return ("dict", [(_content(k), _content(x)) for k, x in _pairs(v["__dict__"])])
        return _UNKNOWN if any(k.startswith("__") for k in v) else v
    if isinstance(v, list):
        return _seq([_content(x) for x in v])
    return v


_UNKNOWN = object()


def _seq(items: list, unordered: bool = False) -> Any:
    if any(i is _UNKNOWN for i in items):
        return _UNKNOWN
    return ("unordered", items) if unordered else items


def _items(c: Any) -> list | None:
    if isinstance(c, tuple) and c and c[0] == "unordered":
        return sorted(c[1], key=_canon)
    return c if isinstance(c, list) else None


def _representation_only(old: list[Outcome], new: list[Outcome]) -> bool:
    if len(old) != 1 or len(new) != 1 or old[0].kind != "return" or new[0].kind != "return":
        return False
    a, b = _content(old[0].value), _content(new[0].value)
    if a is _UNKNOWN or b is _UNKNOWN:
        return False
    ia, ib = _items(a), _items(b)
    if ia is not None and ib is not None:
        same = [_canon(x) for x in ia] == [_canon(x) for x in ib]
        unordered = (isinstance(a, tuple)) or (isinstance(b, tuple))
        if not same and unordered:
            same = sorted(_canon(x) for x in ia) == sorted(_canon(x) for x in ib)
        return same and _canon(old[0].value) != _canon(new[0].value)
    return False


def _value_labels(v: Any) -> set[str]:
    out: set[str] = set()
    if isinstance(v, dict):
        for tag in ("__opaque__", "__structure__", "__object__"):
            if isinstance(v.get(tag), str):
                out.add(v[tag])
    return out


def _input_labels(inputs: Any) -> set[str]:
    bound = inputs.get("bound") if isinstance(inputs, dict) else None
    out: set[str] = set()
    if isinstance(bound, dict):
        for x in bound.values():
            out |= _value_labels(x)
    return out


def _is_noise(symbol: str) -> str | None:
    if symbol.startswith(_NOISE_PREFIXES):
        return "import machinery"
    if symbol.rsplit(".", 1)[-1] in _CLASS_HOOKS:
        return "implicit class-creation hook"
    return None


# ── noise canonicalisation: volatile positions masked before comparing ───────────────────
#
# Comparison is layered: the outcome kind is always strict; the structure (types, keys,
# attribute names, input signature, the format of strings) is always compared; values are
# compared only where they are not volatile. A position is volatile when its value is
#   * a value a recorded entropy source returned on that side (``source``: uuid4, random,
#     datetime.now, tempfile names, ...), or a reading of the capture's own clock (``clock``),
#     or the capture's environment identity (``environment``: project root, Python version):
#     masked per side, before keying, with placeholders that keep the format;
#   * explained by the pair's own input difference (``input-substitution``): masked per pair.
# Placeholders: ``⟨src:<alphabet>:<shape>⟩``, ``⟨clock:<shape>⟩``, ``⟨root⟩``, ``⟨python⟩``
# are unindexed (stable under concurrency: two runs interleave greenlets differently, so a
# per-side numbering by first appearance would differ); ``⟨inK⟩`` is indexed locally to one
# pair. A token shorter than 8 characters only matches as a whole token (``9095`` never masks
# a piece of ``2.5949095``); shorter than 4 it is never a token.

# Curated stdlib entropy sources (generic data, not per library). kind: "uuid" (its UUIDs),
# "datetime" (its datetimes), "tempname" (file and directory names), "value" (the returned
# value itself), "pick" (picks from its input: the fact is volatile, its values are not
# tokens). ``seedable``: a recorded seed makes it deterministic on that side.
_SOURCES: dict[str, tuple[str, bool]] = {
    "uuid.uuid1": ("uuid", False), "uuid.uuid4": ("uuid", False),
    "os.urandom": ("value", False), "os.getrandom": ("value", False),
    "datetime.datetime.now": ("datetime", False), "datetime.datetime.utcnow": ("datetime", False),
    "datetime.datetime.today": ("datetime", False), "datetime.date.today": ("datetime", False),
    **{f"time.{n}{ns}": ("value", False) for n in (
        "time", "monotonic", "perf_counter", "process_time", "thread_time") for ns in ("", "_ns")},
    "tempfile.mkstemp": ("tempname", False), "tempfile.mkdtemp": ("tempname", False),
    "tempfile.mktemp": ("tempname", False), "tempfile.NamedTemporaryFile": ("tempname", False),
    "tempfile.TemporaryDirectory": ("tempname", False),
    "tempfile.TemporaryDirectory.__enter__": ("tempname", False),
    "tempfile._RandomNameSequence.__next__": ("tempname", False),
}
_RANDOM_VALUES = frozenset({
    "random", "uniform", "gauss", "normalvariate", "lognormvariate", "expovariate",
    "vonmisesvariate", "gammavariate", "betavariate", "paretovariate", "weibullvariate",
    "triangular", "getrandbits", "randbytes", "binomialvariate", "randbelow", "randbits"})
_RANDOM_PICKS = frozenset({"choice", "choices", "sample", "shuffle", "randint", "randrange"})
_RANDOM_PREFIXES = (("random.SystemRandom.", False), ("random.Random.", True), ("random.", True),
                    ("secrets.SystemRandom.", False), ("secrets.", False))
_SEED_CALLS = ("random.seed", "random.Random.seed", "random.Random.__init__")
_TAG_NAME_KEYS = frozenset({
    "__opaque__", "__object__", "__type__", "__callable__", "__enum__", "__structure__",
    "__cycle__", "__shape__", "__dataclass__", "__nondeterministic__", "__widened__",
    "__c_call__"})
_WORD = re.compile(r"[A-Za-z0-9_]+")
_ALNUM = re.compile(r"[A-Za-z0-9]+")  # where a token may start: after any separator, ``_`` too
_NUMBER_TOKEN = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")
_ISO_CLOCK = re.compile(
    r"(?<!\d)(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(\.\d{1,9})?"
    r"(Z|[+-]\d{2}:?\d{2})?(?!\d)")
_EPOCH_CLOCK = re.compile(r"(?<![\d.])(\d{10}|\d{13}|\d{16}|\d{19})(\.\d+)?(?!\d)")
_EPOCH_SCALES = {10: (1.0, "s"), 13: (1e3, "ms"), 16: (1e6, "us"), 19: (1e9, "ns")}
_FOUR_DIGITS = re.compile(r"\d{4}")  # every timestamp and epoch reading holds a run of four
_CLOCK_HORIZON = 30 * 86400.0  # a value farther than this from its call is no clock value
_INPUT_WINDOW_MAX = 10.0  # a call window wider than this (calls over a suite) masks no clock


def source_kind(symbol: str) -> tuple[str, bool] | None:
    """``(kind, seedable)`` of a curated stdlib entropy source, or None."""
    if symbol in _SOURCES:
        return _SOURCES[symbol]
    for prefix, seedable in _RANDOM_PREFIXES:
        if symbol.startswith(prefix):
            name = symbol[len(prefix):]
            if "." in name:
                continue
            if name in _RANDOM_VALUES or name.startswith("token_"):
                return ("value", seedable)
            if name in _RANDOM_PICKS:
                return ("pick", seedable)
    return None


def _seeded(facts: list[Fact]) -> bool:
    """A seed recorded on this side (``random.seed(42)``, ``Random(42)``) makes its random
    module deterministic: then its values are compared like any other."""
    for f in facts:
        if f.symbol in _SEED_CALLS:
            bound = f.input.get("bound") if isinstance(f.input, dict) else None
            if isinstance(bound, dict) and any(
                    k in bound and bound[k] is not None for k in ("a", "x")):
                return True
    return False


def _token_placeholder(token: str) -> str:
    """``⟨src:<alphabet>:<shape>⟩``: letters' case class and the word-run lengths with every
    separator kept, so a uuid that lost its hyphens or changed case still differs."""
    if _NUMBER_TOKEN.fullmatch(token):
        return "⟨src:number⟩"
    letters = [c for c in token if c.isalpha()]
    upper, lower = any(c.isupper() for c in letters), any(c.islower() for c in letters)
    alphabet = "mixed" if upper and lower else ("upper" if upper else "lower")
    shape = _WORD.sub(lambda m: f"x{len(m.group())}", token)
    return f"⟨src:{alphabet}:{shape}⟩"


def _walk_leaves(value: Any, fn: Callable[[Any], Any],
                 bytes_fn: Callable[[str], str] | None = None) -> Any:
    """Rebuild ``value`` with ``fn`` applied to every scalar leaf (str, int, float), leaving
    type names (``__opaque__``, ``__object__``, ...) and booleans/None untouched. ``bytes_fn``,
    when given, rewrites the hex of a ``__bytes__`` rendering instead of ``fn``."""
    if isinstance(value, str):
        return fn(value)
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int | float):
        return fn(value)
    if isinstance(value, list):
        return [_walk_leaves(x, fn, bytes_fn) for x in value]
    if isinstance(value, dict):
        if bytes_fn is not None and isinstance(value.get("__bytes__"), str):
            return {**value, "__bytes__": bytes_fn(value["__bytes__"])}
        return {k: (v if k in _TAG_NAME_KEYS else _walk_leaves(v, fn, bytes_fn))
                for k, v in value.items()}
    return value


def _source_values(kind: str, value: Any) -> tuple[list[str], list[float]]:
    """The tokens one return value of a source contributes: (strings, numbers)."""
    strings: list[str] = []
    numbers: list[float] = []

    def visit(v: Any, attr: str | None = None) -> None:
        if isinstance(v, dict):
            if kind == "uuid" and isinstance(v.get("__uuid__"), str):
                strings.extend((v["__uuid__"], v["__uuid__"].replace("-", "")))
            elif kind == "datetime" and isinstance(v.get("__datetime__"), str):
                strings.extend((v["__datetime__"], v["__datetime__"].replace("T", " ")))
            elif kind == "value" and isinstance(v.get("__bytes__"), str):
                strings.append(v["__bytes__"])
            for k, x in v.items():
                if k not in _TAG_NAME_KEYS:
                    visit(x, k)
            attrs = v.get("attrs")
            if kind == "tempname" and isinstance(attrs, dict) \
                    and isinstance(attrs.get("name"), str):
                strings.append(attrs["name"])
        elif isinstance(v, list):
            for x in v:
                visit(x, attr)
        elif kind == "tempname" and isinstance(v, str) and attr in (None, "__tuple__", "name"):
            strings.append(v)
            base = v.rsplit("/", 1)[-1]
            if len(base) >= 8 and base != v:
                strings.append(base)
        elif kind == "value":
            if isinstance(v, str):
                strings.append(v)
            elif isinstance(v, int | float) and not isinstance(v, bool):
                numbers.append(v)
                strings.append(repr(v))

    visit(value)
    return [s for s in strings if len(s) >= 4], [
        n for n in numbers if len(repr(n)) >= 4]


@dataclass
class _Masker:
    """One side's masks: its environment identity, run windows and entropy-source tokens."""

    roots: list[str]
    pythons: list[str]
    host_capture: bool
    tokens: dict[str, str] = field(default_factory=dict)       # token -> placeholder
    numbers: dict[float, str] = field(default_factory=dict)    # numeric token -> placeholder
    counts: Counter = field(default_factory=Counter)
    examples: dict[str, str] = field(default_factory=dict)
    # The call window of the fact being masked (set per fact): a clock reading is only a
    # reading of *that call's* clock within a few seconds of it.
    window: tuple[float, float] | None = None
    untimed: int = 0  # facts with no call time: their clock readings are not masked
    # the run values a source or clock mask actually replaced: the seeds of propagation
    replaced: set = field(default_factory=set)
    # Raw first: clock-like texts (an epoch-range number's repr, a timestamp's text) recorded
    # identically on BOTH sides are no clock readings (``clock_candidates``): a 1.79 GB file
    # size equal on both runs stays 1794824000.
    stable: frozenset = frozenset()
    # a differing distribution's version string on this side -> ``⟨ver:<name>⟩``
    versions: dict[str, str] = field(default_factory=dict)
    # this side's OS name, kernel release, machine and tmpdir -> ``⟨env:<aspect>⟩``, for the
    # aspects that differ between the two captures' recorded environments
    env_tokens: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._roots = re.compile("|".join(
            rf"(?<![\w.\-/]){re.escape(r)}(?![\w.\-])" for r in self.roots)) if self.roots else None
        pys = [re.escape(v) for v in self.pythons]
        self._python = re.compile(rf"(?<![\d.])(?:{'|'.join(pys)})(?![\d.])") if pys else None
        self._long: dict[str, list[str]] = defaultdict(list)    # 8-char prefix -> tokens
        self._short: dict[str, str] = {}
        # a token's UTF-8 hex inside a ``__bytes__`` rendering (msgpack bytes carrying an id):
        # 16-hex-digit prefix -> (hex, token)
        self._hex: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for tok in sorted(self.tokens, key=len, reverse=True):
            if len(tok) >= 8:
                self._long[tok[:8]].append(tok)
                h = tok.encode("utf-8", errors="replace").hex()
                self._hex[h[:16]].append((h, tok))
            else:
                self._short[tok] = self.tokens[tok]
        self._versions = re.compile("|".join(
            rf"(?<![\w.]){re.escape(v)}(?![\w.]|-\w)"
            for v in sorted(self.versions, key=len, reverse=True))) if self.versions else None
        # tmpdir: path-like, masked like the project root ("environment"); the OS name, kernel
        # release and machine: "platform", which explains nothing by itself (_judge_platform)
        self._env = {}
        for mech, toks in (
                ("environment", [t for t, ph in self.env_tokens.items() if ph == "⟨env:tmpdir⟩"]),
                ("platform", [t for t, ph in self.env_tokens.items() if ph != "⟨env:tmpdir⟩"])):
            if toks:
                self._env[mech] = re.compile("|".join(
                    rf"(?<![\w.\-/]){re.escape(t)}(?![\w.\-])"
                    for t in sorted(toks, key=len, reverse=True)))
        # every start of a token's hex prefix, overlapping ones included (scanned in C)
        self._hex_rx = re.compile("(?=(" + "|".join(re.escape(p) for p in self._hex) + "))") \
            if self._hex else None

    def _note(self, mechanism: str, example: str) -> None:
        self.counts[mechanism] += 1
        self.examples.setdefault(mechanism, example)
        if mechanism in ("source", "clock"):
            self.replaced.add(example)

    def bytes_hex(self, h: str) -> str:
        """Source tokens hex-encoded inside a ``__bytes__`` rendering, at byte boundaries."""
        if self._hex_rx is None:
            return h
        out: list[str] = []
        last = 0
        for m in self._hex_rx.finditer(h):
            i = m.start()
            if i % 2 or i < last:  # byte boundaries only, never inside a replaced token
                continue
            hit = next((x for x in self._hex.get(m.group(1), ()) if h.startswith(x[0], i)),
                       None)
            if hit is None:
                continue
            out.append(h[last:i])
            out.append(self.tokens[hit[1]].replace("⟨src:", "⟨src:hex:"))
            self._note("source", hit[1])
            last = i + len(hit[0])
        if not out:
            return h
        out.append(h[last:])
        return "".join(out)

    def leaf(self, v: Any) -> Any:
        if isinstance(v, str):
            return self._string(v)
        if isinstance(v, int | float) and not isinstance(v, bool):
            if v in self.numbers:
                self._note("source", repr(v))
                return self.numbers[v]
            clock = self._epoch(v, "int" if isinstance(v, int) else "float")
            if clock is not None:
                self._note("clock", repr(v))
                return clock
        return v

    def _string(self, s: str) -> str:
        if self.tokens:
            s = self._mask_tokens(s)
        if self.window is not None:
            s = self._mask_clock(s)
        if self._roots is not None and self._roots.search(s):
            self._note("environment", self._roots.search(s).group())
            s = self._roots.sub("⟨root⟩", s)
        if self._python is not None and self._python.search(s):
            self._note("environment", self._python.search(s).group())
            s = self._python.sub("⟨python⟩", s)
        if self._versions is not None and self._versions.search(s):
            self._note("environment", self._versions.search(s).group())
            s = self._versions.sub(lambda m: self.versions[m.group()], s)
        for mech, rx in self._env.items():
            hit = rx.search(s)
            if hit is not None:
                self._note(mech, hit.group())
                s = rx.sub(lambda m: self.env_tokens[m.group()], s)
        return s

    def _mask_tokens(self, s: str) -> str:
        whole = self.tokens.get(s)
        if whole is not None:
            self._note("source", s)
            return whole
        out: list[str] = []
        i = last = 0
        for m in _ALNUM.finditer(s):
            i = m.start()
            if i < last:
                continue
            hit = None
            for tok in self._long.get(s[i:i + 8], ()):
                if s.startswith(tok, i):
                    hit = tok
                    break
            if hit is None and m.group() in self._short:
                hit = m.group()
            if hit is None:
                continue
            out.append(s[last:i])
            out.append(self.tokens[hit])
            self._note("source", hit)
            last = i + len(hit)
        if not out:
            return s
        out.append(s[last:])
        return "".join(out)

    def _offset(self, t: float) -> tuple[int, int] | None:
        """A clock value as the INTERVAL of its possible offsets from its own call, in whole
        seconds: ``[floor(t - end), ceil(t - start)]`` over the window of the calls that
        produced it (the value was computed at some moment inside that window). None when it
        is no clock value here: no call time, a window wider than 10 s (calls spread over a
        suite), or farther than 30 days from the call. Two masked values compare equal when
        their intervals overlap (``clock_values_match``); a change smaller than the call's own
        duration is therefore not visible."""
        w = self.window
        if w is None or w[1] - w[0] > _INPUT_WINDOW_MAX:
            return None
        if not (w[0] - _CLOCK_HORIZON <= t <= w[1] + _CLOCK_HORIZON):
            return None
        return int(math.floor(t - w[1])), int(math.ceil(t - w[0]))

    def _epoch(self, v: float, kind: str) -> str | None:
        try:
            t = float(v)
        except (OverflowError, ValueError):
            return None  # an int too large for a float is no clock reading
        if not t > 0 or t == float("inf") or t < _EPOCH_MIN:
            return None
        if self.window is None or (self.stable and repr(v) in self.stable):
            return None
        for scale, unit in ((1.0, "s"), (1e3, "ms"), (1e6, "us"), (1e9, "ns")):
            if t / scale >= _EPOCH_MIN:
                n = self._offset(t / scale)
                if n is not None:
                    return f"⟨clock:{kind}:epoch-{unit}@{_interval(n)}⟩"
        return None

    def _mask_clock(self, s: str) -> str:
        def iso(m: re.Match) -> str:
            if m.group() in self.stable:
                return m.group()
            y, mo, d, h, mi, sec, frac, tz = m.groups()
            try:
                micro = int((frac or ".0")[1:].ljust(6, "0")[:6])
                naive = _dt.datetime(int(y), int(mo), int(d), int(h), int(mi), int(sec), micro)
                if tz:
                    offset = 0 if tz == "Z" else (1 if tz[0] == "+" else -1) * (
                        int(tz[1:3]) * 3600 + int(tz[-2:]) * 60)
                    instants = [naive.replace(tzinfo=_dt.UTC).timestamp() - offset]
                else:
                    instants = [naive.replace(tzinfo=_dt.UTC).timestamp()]
                    if self.host_capture:  # naive local time of this host
                        instants.append(naive.timestamp())
            except (ValueError, OverflowError, OSError):
                return m.group()
            offsets = [n for n in (self._offset(t) for t in instants) if n is not None]
            if not offsets:
                return m.group()
            self._note("clock", m.group())
            n = min(offsets, key=lambda iv: min(abs(iv[0]), abs(iv[1])))  # UTC or local
            return "⟨clock:" + re.sub(r"\d", "9", m.group()) + f"@{_interval(n)}⟩"

        def epoch(m: re.Match) -> str:
            digits, frac = m.groups()
            scale, unit = _EPOCH_SCALES[len(digits)]
            t = int(digits) / scale
            if t < _EPOCH_MIN or m.group() in self.stable:
                return m.group()
            n = self._offset(t)
            if n is None:
                return m.group()
            self._note("clock", m.group())
            return f"⟨clock:epoch-{unit}{'.f' if frac else ''}@{_interval(n)}⟩"

        if _FOUR_DIGITS.search(s):
            s = _ISO_CLOCK.sub(iso, s)
            s = _EPOCH_CLOCK.sub(epoch, s)
        return s


def _interval(iv: tuple[int, int]) -> str:
    return f"[{iv[0]:+d},{iv[1]:+d}]s"


_CLOCK_INTERVAL = re.compile(r"@\[([+-]\d+),([+-]\d+)\]s⟩")


def clock_values_match(a: str, b: str) -> bool:
    """Two strings equal once their clock placeholders are compared as intervals: the same
    text and formats around them, and each pair of intervals overlapping."""
    ia, ib = _CLOCK_INTERVAL.findall(a), _CLOCK_INTERVAL.findall(b)
    if not ia or len(ia) != len(ib):
        return False
    if _CLOCK_INTERVAL.sub("@⟩", a) != _CLOCK_INTERVAL.sub("@⟩", b):
        return False
    return all(int(x0) <= int(y1) and int(y0) <= int(x1) for (x0, x1), (y0, y1) in zip(ia, ib))


def _clock_parts(value: Any) -> tuple[str, list[tuple[int, int]]] | None:
    """A value with clock placeholders as (its template: the value with every interval
    blanked, canonical; the intervals in order), or None when it holds no clock value."""
    ivs: list[tuple[int, int]] = []

    def strip(v: Any) -> Any:
        if isinstance(v, str):
            if "⟨clock:" not in v:
                return v
            ivs.extend((int(a), int(b)) for a, b in _CLOCK_INTERVAL.findall(v))
            return _CLOCK_INTERVAL.sub("@⟩", v)
        if isinstance(v, dict):
            return {k: strip(x) for k, x in v.items()}
        if isinstance(v, list):
            return [strip(x) for x in v]
        return v

    t = strip(value)
    return (_canon(t), ivs) if ivs else None


def _with_intervals(value: Any, ivs: list[tuple[int, int]]) -> Any:
    it = iter(ivs)

    def put(v: Any) -> Any:
        if isinstance(v, str):
            return _CLOCK_INTERVAL.sub(lambda _m: f"@{_interval(next(it))}⟩", v) \
                if "⟨clock:" in v else v
        if isinstance(v, dict):
            return {k: put(x) for k, x in v.items()}
        if isinstance(v, list):
            return [put(x) for x in v]
        return v

    return put(value)


def _overlap(a: list[tuple[int, int]], b: list[tuple[int, int]]) -> bool:
    return len(a) == len(b) and all(x0 <= y1 and y0 <= x1 for (x0, x1), (y0, y1) in zip(a, b))


def _merge_clock_intervals(merged: dict[str, list]) -> dict[str, list]:
    """Outcomes of one fact that differ only in their clock intervals are one outcome: the
    hull of their intervals (six calls that each returned now + 120 are one ``[+119,+120]``)."""
    out: dict[str, list] = {}
    for key, slot in merged.items():
        parts = _clock_parts(slot[1]) if slot[0] == "return" else None
        if parts is None:
            out[key] = slot
            continue
        tkey = _canon([slot[0], parts[0]])
        prev = out.get(tkey)
        prev_parts = _clock_parts(prev[1]) if prev is not None else None
        if prev_parts is None or len(prev_parts[1]) != len(parts[1]):
            out[tkey if prev is None else key] = slot
            continue
        hull = [(min(a0, b0), max(a1, b1)) for (a0, a1), (b0, b1) in zip(prev_parts[1], parts[1])]
        out[tkey] = [prev[0], _with_intervals(prev[1], hull), prev[2] + slot[2],
                     _tmin(prev[3], slot[3]), _tmax(prev[4], slot[4]),
                     prev[5] if prev[5] == slot[5] else _order.merge(prev[5], slot[5])]
    return out


def _clock_sets_match(old: list[Outcome], new: list[Outcome]) -> bool:
    """Two outcome sets equal once clock intervals compare by overlap: the same templates on
    both sides, each pair of intervals overlapping."""
    def by_template(outs: list[Outcome]) -> dict[str, list[tuple[int, int]]] | None:
        d: dict[str, list[tuple[int, int]]] = {}
        for o in outs:
            parts = _clock_parts(o.value) if o.kind == "return" else None
            key = _canon([o.kind, parts[0] if parts else o.value])
            if key in d:
                return None  # two outcomes of one template: no one-to-one reading
            d[key] = parts[1] if parts else []
        return d

    a, b = by_template(old), by_template(new)
    return a is not None and b is not None and a.keys() == b.keys() \
        and all(_overlap(a[k], b[k]) for k in a)


def clock_candidates(facts: list[Fact]) -> set[str]:
    """Every clock-like text the facts record where the masker would look (an epoch-range
    number's repr; a timestamp's or an epoch's digits inside a string)."""
    out: set[str] = set()
    stack: list[Any] = []
    for f in facts:
        stack.append(f.input)
        stack.extend(o.value for o in f.outcomes if o.kind == "return")
    while stack:
        v = stack.pop()
        if isinstance(v, str):
            if len(v) >= 10 and _FOUR_DIGITS.search(v):
                out.update(m.group() for m in _ISO_CLOCK.finditer(v))
                for m in _EPOCH_CLOCK.finditer(v):
                    if int(m.group(1)) / _EPOCH_SCALES[len(m.group(1))][0] >= _EPOCH_MIN:
                        out.add(m.group())
        elif isinstance(v, bool) or v is None:
            continue
        elif isinstance(v, int | float):
            if v >= _EPOCH_MIN:
                out.add(repr(v))
        elif isinstance(v, list):
            stack.extend(v)
        elif isinstance(v, dict):
            if isinstance(v.get("__bytes__"), str):
                continue
            stack.extend(x for k, x in v.items() if k not in _TAG_NAME_KEYS)
    return out


def _masker_for(fs: FactSet, facts: list[Fact], stable: frozenset = frozenset(),
                versions: dict[str, str] | None = None,
                env_tokens: dict[str, str] | None = None) -> _Masker:
    seeded = _seeded(facts)
    tokens: dict[str, str] = {}
    numbers: dict[float, str] = {}
    for f in facts:
        src = source_kind(f.symbol)
        if src is None or src[0] == "pick" or (src[1] and seeded):
            continue
        for o in f.outcomes:
            if o.kind != "return":
                continue
            strings, nums = _source_values(src[0], o.value)
            for tok in strings:
                tokens.setdefault(tok, _token_placeholder(tok))
            for n in nums:
                numbers.setdefault(n, "⟨src:number⟩")
    return _Masker(roots=list(fs.env_roots), pythons=list(fs.python_versions),
                   host_capture=fs.host_capture, tokens=tokens, numbers=numbers, stable=stable,
                   versions=versions or {}, env_tokens=env_tokens or {})


def _canonicalise(facts: list[Fact], masker: _Masker) -> list[Fact]:
    """Facts with their volatile positions masked (see the section comment). A fact a mask
    changed keeps the recorded one in ``_raw`` and the mechanisms in ``_masks``; outcomes that
    became identical merge (48 uuid4 values are one masked outcome)."""
    timed = any(f.ts_min is not None for f in facts)
    if not (masker.roots or masker.pythons or timed or masker.tokens or masker.numbers
            or masker.versions or masker.env_tokens):
        masker.untimed = len(facts)
        return facts
    out: list[Fact] = []
    for f in facts:
        before = Counter(masker.counts)
        if f.ts_min is None or f.ts_max is None:
            masker.untimed += 1
        # An input clock value is a reading of this call's clock only when all its calls ran
        # within a few seconds of each other (a fact repeated across a suite is not).
        masker.window = (f.ts_min, f.ts_max) if f.ts_min is not None and f.ts_max is not None \
            and f.ts_max - f.ts_min <= _INPUT_WINDOW_MAX else None
        inp = _walk_leaves(f.input, masker.leaf, masker.bytes_hex)
        merged: dict[str, list] = {}
        for o in f.outcomes:
            # an output clock value: within seconds of the calls that produced THIS outcome
            masker.window = (o.ts_min, o.ts_max) if o.ts_min is not None \
                and o.ts_max is not None else None
            v = _walk_leaves(o.value, masker.leaf, masker.bytes_hex) if o.kind == "return" \
                else o.value
            k = _canon([o.kind, v])
            if k in merged:
                merged[k][2] += o.count
                merged[k][3] = _tmin(merged[k][3], o.ts_min)
                merged[k][4] = _tmax(merged[k][4], o.ts_max)
                if o.order != merged[k][5]:
                    merged[k][5] = _order.merge(merged[k][5], o.order)
            else:
                merged[k] = [o.kind, v, o.count, o.ts_min, o.ts_max, o.order]
        if masker.counts["clock"] > before.get("clock", 0):
            merged = _merge_clock_intervals(merged)
        mechs = frozenset(m for m, n in masker.counts.items() if n > before.get(m, 0))
        if not mechs:
            out.append(f)
            continue
        c = f.model_copy(update={"input": inp, "outcomes": [
            Outcome(kind=k, value=v, count=n, ts_min=t0, ts_max=t1, order=od)
            for k, v, n, t0, t1, od in merged.values()]})
        _set_masks(c, mechs, f)
        out.append(c)
    return out


_SUB_TOKENS = re.compile(r"[A-Za-z0-9_]+|[^A-Za-z0-9_]")
_SUB_MIDDLE_CAP = 1024  # characters of the differing middle a token diff is run on


def _leaf_map(value: Any, path: tuple = (), out: dict | None = None) -> dict[tuple, Any]:
    out = {} if out is None else out
    if isinstance(value, dict):
        for k, v in value.items():
            _leaf_map(v, (*path, k), out)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _leaf_map(v, (*path, i), out)
    else:
        out[path] = value
    return out


def input_substitutions(old_input: Any, new_input: Any) -> list[tuple[str, str]] | None:
    """What the pair's input difference consists of: ``(old, new)`` text substitutions, from
    the input leaves that differ at the same path (whole leaves, plus the changed token runs
    inside strings up to 4 KB). None when the inputs differ in structure (another key set,
    another arity): that is not a value difference. Kept only when the map is consistent both
    ways (one old token never stands for two new ones)."""
    a, b = _leaf_map(old_input), _leaf_map(new_input)
    if a.keys() != b.keys():
        return None
    pairs: list[tuple[str, str]] = []
    for path, x in a.items():
        y = b[path]
        if _canon(x) == _canon(y):
            continue
        sx, sy = (x if isinstance(x, str) else json.dumps(x)), (y if isinstance(y, str) else
                                                                 json.dumps(y))
        pairs.append((sx, sy))
        if isinstance(x, str) and isinstance(y, str):
            pairs.extend(_token_substitutions(x, y))
    fwd: dict[str, set[str]] = defaultdict(set)
    back: dict[str, set[str]] = defaultdict(set)
    for x, y in pairs:
        fwd[x].add(y)
        back[y].add(x)
    out = [(x, next(iter(ys))) for x, ys in fwd.items()
           if len(ys) == 1 and len(back[next(iter(ys))]) == 1 and x != next(iter(ys))]
    return sorted(out, key=lambda p: -len(p[0]))


def _token_substitutions(x: str, y: str) -> list[tuple[str, str]]:
    """The changed token runs between two strings: the common prefix and suffix are trimmed
    first (linear), and the token diff runs on the middle only, up to 1 KB; past that only the
    whole leaf substitutes."""
    ta, tb = _SUB_TOKENS.findall(x), _SUB_TOKENS.findall(y)
    p = 0
    while p < len(ta) and p < len(tb) and ta[p] == tb[p]:
        p += 1
    q = 0
    while q < len(ta) - p and q < len(tb) - p and ta[-1 - q] == tb[-1 - q]:
        q += 1
    ma, mb = ta[p:len(ta) - q], tb[p:len(tb) - q]
    if not ma or not mb:
        return []
    if sum(map(len, ma)) > _SUB_MIDDLE_CAP or sum(map(len, mb)) > _SUB_MIDDLE_CAP:
        return []
    out = [("".join(ma), "".join(mb))]
    sm = difflib.SequenceMatcher(None, ma, mb, autojunk=False)
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "replace":
            out.append(("".join(ma[i1:i2]), "".join(mb[j1:j2])))
    return out


def _substitute(value: Any, tokens: list[tuple[int, str]]) -> Any:
    """Mask one side's substituted tokens as ``⟨inK⟩``: a whole leaf of any length; a token of
    8+ characters anywhere; 4-7 characters as a whole word only."""
    whole = {tok: k for k, tok in tokens}
    long_ = [(k, tok) for k, tok in tokens if len(tok) >= 8]
    short = [(k, re.compile(rf"(?<![A-Za-z0-9_.]){re.escape(tok)}(?![A-Za-z0-9_])"))
             for k, tok in tokens if 4 <= len(tok) < 8]

    def leaf(v: Any) -> Any:
        if not isinstance(v, str):
            key = json.dumps(v)
            return f"⟨in{whole[key]}⟩" if key in whole else v
        if v in whole:
            return f"⟨in{whole[v]}⟩"
        for k, tok in long_:
            if tok in v:
                v = v.replace(tok, f"⟨in{k}⟩")
        for k, rx in short:
            v = rx.sub(f"⟨in{k}⟩", v)
        return v

    return _walk_leaves(value, leaf)


@dataclass
class _NoiseState:
    """What the noise rules saw during one diff (the R10 trigger reads it)."""

    sources: frozenset = frozenset()     # source symbols volatile on both sides
    old: _Masker | None = None
    new: _Masker | None = None


# Set by ``diff_facts`` for one diff; None outside one (a ``_pair_entry`` called directly then
# works on a throwaway state and never mutates shared state).
_NOISE: ContextVar[_NoiseState | None] = ContextVar("pymolt_noise", default=None)


def _noise_state() -> _NoiseState:
    state = _NOISE.get()
    return state if state is not None else _NoiseState()


# ── diff ──────────────────────────────────────────────────────────────────────────────────


@dataclass(eq=False)
class _V:
    """A fact in the diff's view: merged per the file's site mode, with its matching keys."""

    fact: Fact
    site_value: str | int | None
    input_key: str
    core_key: str = ""
    rewritten_site: bool = False
    unobservable: bool = False  # edited line whose replacement left no recorded call
    stmt_key: Any = None        # (file, first line of its statement, in new-side numbering)
    noise: str | None = None    # why a vanished call is only a DRIFT note
    vanish: str | None = None   # why a vanished call on an unedited line is only a review item

    @property
    def first_seen(self) -> int:
        return self.fact.first_seen


def diff_facts(
    old: FactSet, new: FactSet, *, profile: DiffProfile | str = DiffProfile.EXACT,
    context: DiffContext | None = None, order_causes: Sequence["OrderCause"] = (),
) -> FactDiff:
    """Compare two FactSets (baseline -> post-migration). See the module docstring.

    ``order_causes``: known causes of dict key order changes (``OrderCause``); an order finding
    that matches one exactly is cited, and from 5 keys on the citation proves determinism."""
    token = _HANDLES.set(_handle_types(old.facts, new.facts))
    reloc = _TYPE_RELOC.set((context.type_relocations if context else None) or {})
    noise = _NOISE.set(_NoiseState())
    try:
        return _diff_facts(old, new, DiffProfile(profile), context, tuple(order_causes))
    finally:
        _NOISE.reset(noise)
        _TYPE_RELOC.reset(reloc)
        _HANDLES.reset(token)


def _diff_facts(old: FactSet, new: FactSet, prof: DiffProfile,
                context: DiffContext | None = None,
                order_causes: tuple["OrderCause", ...] = ()) -> FactDiff:
    old_facts, new_facts, c_level_excluded = _exclude_c_level(old, new)
    # Finalizers run at GC time, not at a call site the project controls.
    old_facts = [f for f in old_facts if not f.symbol.endswith(".__del__")]
    new_facts = [f for f in new_facts if not f.symbol.endswith(".__del__")]
    # A call site inside pymolt's own injected runtime is the tracer observing itself (a
    # tracer bug), never the project's call: not compared, counted.
    n_before = len(old_facts) + len(new_facts)
    old_facts = [f for f in old_facts if _TRACER_SITE not in f.file]
    new_facts = [f for f in new_facts if _TRACER_SITE not in f.file]
    tracer_self = n_before - len(old_facts) - len(new_facts)
    # Volatile positions (environment identity, clock readings, entropy-source values) are
    # masked per side before anything is keyed or compared.
    noise = _noise_state()
    vers = version_masks(old, new)
    # Raw first: a clock-like text recorded identically on both sides is no clock reading.
    stable = frozenset(clock_candidates(old_facts) & clock_candidates(new_facts))
    envcmp = _env.compare(old.environment, new.environment)
    noise.old = _masker_for(old, old_facts, stable, vers[0], _env.tokens(envcmp, 0))
    noise.new = _masker_for(new, new_facts, stable, vers[1], _env.tokens(envcmp, 1))
    seeded = _seeded(old_facts) or _seeded(new_facts)
    noise.sources = frozenset(
        f.symbol for f in (*old_facts, *new_facts)
        if (k := source_kind(f.symbol)) is not None and not (k[1] and seeded))
    old_facts = _canonicalise(old_facts, noise.old)
    new_facts = _canonicalise(new_facts, noise.new)
    old_facts, new_facts = _collapse_widened(old_facts, new_facts)
    if prof is DiffProfile.SHAPE:
        old_facts = [f.model_copy(update={"input": input_type(f.input), "input_order": None})
                     for f in old_facts]
        new_facts = [f.model_copy(update={"input": input_type(f.input), "input_order": None})
                     for f in new_facts]

    old_facts, new_facts = _normalize_scopes(old_facts), _normalize_scopes(new_facts)
    modes = _site_modes(old_facts, new_facts, old.site_tallies, new.site_tallies)
    ov = _views(old_facts, modes)
    nv = _views(new_facts, modes)

    defaulted_names: dict[str, set[str]] = defaultdict(set)
    for v in (*ov, *nv):
        for d in v.fact.defaulted or ():
            defaulted_names[v.fact.symbol].update(d)
    for v in (*ov, *nv):
        v.core_key = _canon(_core_input(v.fact.input, defaulted_names.get(v.fact.symbol, ())))

    _mark_rewritten_sites(ov, nv, modes)
    prov = _Provenance(context, new.root)
    for side, views in enumerate((ov, nv)):  # a line map beats the line-text heuristic
        for v in views:
            st = prov.status(v.fact.file, v.fact.line, side)
            if st is not None:
                v.rewritten_site = st != "same"
                if st == "same" and prov.naming_status(
                        side, v.fact.file, v.fact.line, v.fact.symbol) == "edited":
                    v.rewritten_site = True  # the statement that names the callee was edited
            span = prov.stmt_span(side, v.fact.file, v.fact.line)
            if span:  # key on the statement, in the new side's numbering
                start = span[0] if side == 1 else prov.mapped_line(v.fact.file, span[0], 0)
                v.stmt_key = (v.fact.file, start) if start is not None else None
            else:
                v.stmt_key = (v.fact.file, v.site_value)

    def site(v: _V) -> tuple:
        return (v.fact.file, v.fact.func, v.site_value)

    entries: list[FactEntry] = []
    pending: list[tuple[_V, _V, FactEntry]] = []
    old_left, new_left = list(ov), list(nv)
    passes: list[Callable[[_V], Any]] = [
        lambda v: (v.fact.symbol, site(v), v.input_key),                # 1 exact key
        lambda v: (v.fact.symbol, site(v), v.core_key),                 # 2 modulo defaults
        lambda v: (v.fact.symbol, v.fact.file, v.fact.func, v.core_key),  # 3 same function
        lambda v: (site(v), v.core_key),                                # 4 identity changed
        lambda v: (v.fact.symbol, v.core_key),                          # 5 site moved
    ]

    def pair_pass(pairs: list[tuple[_V, _V]], *, input_changed: bool = False) -> None:
        for o, n in pairs:
            # A text-mode call whose line text changed inside the same function: REWRITTEN.
            same_call = (modes.get(o.fact.file) == "text"
                         and (o.fact.file, o.fact.func) == (n.fact.file, n.fact.func)
                         and o.site_value != n.site_value)
            # Both lines unedited per the source snapshots: the interpreter attributed the same
            # call to another line of the same code (3.8 puts a ``with`` block's ``__exit__`` on
            # its last line, 3.12 on the ``with`` line). Not a rewrite.
            moved = same_call and bool(prov.maps) and not (o.rewritten_site or n.rewritten_site)
            e = _pair_entry(o, n, prof, modes, input_changed=input_changed,
                            rewritten="same-call" if same_call and not moved else None)
            if moved:
                e.notes.append("attribution-moved")
                e.explained = e.explained or (
                    "attribution: the same call on unedited code, attributed to another line "
                    f"({o.site_value!s:.40} -> {n.site_value!s:.40})")
            entries.append(e)
            pending.append((o, n, e))

    for i, key_fn in enumerate(passes):
        pairs, old_left, new_left = _pair_groups(old_left, new_left, key_fn)
        pair_pass(pairs)
        if i == 1:  # a binding failure pairs with the other side's normal call, any input
            for bkey in (lambda v: (v.fact.symbol, site(v)),
                         lambda v: (v.fact.symbol, v.fact.file, v.fact.func)):
                pairs, old_left, new_left = _pair_binding(old_left, new_left, bkey)
                pair_pass(pairs, input_changed=True)
            # a wrapped compiled call (real inputs) vs a ``__c_call__`` event: symbol and site
            pairs, old_left, new_left = _pair_binding(
                old_left, new_left, lambda v: (v.fact.symbol, site(v)), _is_c_call)
            pair_pass(pairs, input_changed=True)
    # 5b Same symbol, same site, different input (one-to-one): the call is the same. Not on a
    # sampled capture: there, differing inputs may just be different samples of one call.
    sampled = bool(old.site_tallies or new.site_tallies)
    if not sampled:
        pairs, old_left, new_left = _pair_groups(
            old_left, new_left, lambda v: (v.fact.symbol, site(v)))
        pair_pass(pairs, input_changed=True)
    # 5b' Same symbol, file and line text (line number in line mode), whatever the function:
    # a scope that differs by version (PEP 709 inlines comprehensions, genexpr/lambda frames).
    if not sampled:
        pairs, old_left, new_left = _pair_groups(
            old_left, new_left, lambda v: (v.fact.symbol, v.fact.file, v.site_value))
        pair_pass(pairs, input_changed=True)
    # 5s Same symbol, same *statement* (a chained multi-line call: 3.9 attributes every call to
    # the statement's first line, 3.12 to each call's own line).
    if not sampled and prov.maps:
        pairs, old_left, new_left = _pair_groups(
            old_left, new_left,
            lambda v: (v.fact.symbol, v.stmt_key if v.stmt_key else id(v)))
        pair_pass(pairs, input_changed=True)
    # 5c Same site, a *relocated* symbol (same package, same last name) and any input.
    if not sampled:
        pairs, old_left, new_left = _pair_groups(
            old_left, new_left,
            lambda v: (site(v), v.fact.symbol.split(".")[0], v.fact.symbol.rsplit(".", 1)[-1]))
        pair_pass(pairs, input_changed=True)

    # 6 Anchored pairing on edited lines (same symbol, ledger / plan symbol, one-statement
    # edit); never positional across different symbols.
    paired: set[int] = set()
    for o, n in _anchored_pairs(old_left, new_left, prov):
        e = _pair_entry(o, n, prof, modes, rewritten="anchored")
        entries.append(e)
        pending.append((o, n, e))
        paired.update((id(o), id(n)))
    old_left = [v for v in old_left if id(v) not in paired]
    new_left = [v for v in new_left if id(v) not in paired]

    # 7 One leftover each side at an unedited statement, one of them a decorator wrapper: the
    # same call named by its decorator's wrapper code; compared at type level.
    by_stmt: dict[Any, tuple[list[_V], list[_V]]] = defaultdict(lambda: ([], []))
    for v in old_left:
        if not v.rewritten_site and v.stmt_key:
            by_stmt[v.stmt_key][0].append(v)
    for v in new_left:
        if not v.rewritten_site and v.stmt_key:
            by_stmt[v.stmt_key][1].append(v)
    paired = set()
    for os_, ns in by_stmt.values():
        if len(os_) == 1 and len(ns) == 1 and (
                _wrapper_like(os_[0].fact.symbol) or _wrapper_like(ns[0].fact.symbol)):
            e = _pair_entry(os_[0], ns[0], DiffProfile.SHAPE, modes, rewritten=None,
                            input_changed=True)
            e.notes.append("wrapper-naming")
            entries.append(e)
            paired.update((id(os_[0]), id(ns[0])))
    old_left = [v for v in old_left if id(v) not in paired]
    new_left = [v for v in new_left if id(v) not in paired]

    # 7q (after the anchored passes) The same call spelled and placed differently by the
    # interpreter: a closure is ``pkg.mod.decorator`` before 3.11 (no ``co_qualname``) and
    # ``pkg.mod.C.m.<locals>.decorator`` after; a decorator's application is on the ``def`` line
    # in 3.8 and on the ``@`` line in 3.12. Same file and function, same input, both lines
    # definition lines (``def``/``class``/``@``) of one decorated statement, one-to-one; or
    # stacked decorators (one factory applied twice to one function: one ``def``-line fact
    # counted twice vs one fact per ``@`` line), merged per side and compared as one.
    pairs, old_left, new_left = _pair_qualname_equivalent(old_left, new_left, prov)
    for o, n, stacked in pairs:
        e = _pair_entry(o, n, prof, modes, rewritten=None)
        e.notes.append("attribution-moved")
        if stacked:
            e.notes.append("stacked-decorators")
        e.explained = e.explained or (
            (f"attribution: stacked decorators, {o.fact.count} application(s) of "
             f"{n.fact.symbol} on line(s) {', '.join(map(str, o.fact.lines))} -> "
             f"{', '.join(map(str, n.fact.lines))} of one decorated statement, which the "
             "interpreter attributes differently") if stacked else
            (f"attribution: {o.fact.symbol} -> {n.fact.symbol}, the interpreter names the "
             "closure and attributes its call differently"))
        entries.append(e)
        pending.append((o, n, e))

    # 8 Volatile sites: an unedited (symbol, site) called on both sides whose leftovers pair
    # neither by key nor one-to-one (every call carried a fresh input: a port, an id, a timing)
    # is judged as a site: kinds and types over the whole site, members paired by their input
    # with its volatile values masked, the rest strict. Not on a sampled capture (there the
    # tallies decide presence).
    gstats = _GroupStats()
    if not sampled:
        groups, old_left, new_left, gstats = _group_volatile(
            old_left, new_left, site, old_facts, new_facts,
            (bool(old.dropped_events), bool(new.dropped_events)))
        entries.extend(groups)

    _mark_unobservable(old_left, new)
    _mark_vanished(old_left, new_facts, prov, ov)
    for v in old_left:
        entries.append(_single_entry(v, old_side=True, prov=prov))
    for v in new_left:
        entries.append(_single_entry(v, old_side=False, prov=prov))
    _lift_namespace_edits(pending, prov)
    _judge_pairs(pending, old_left, new_left, prov)
    _apply_noise(entries, prov, {f.symbol.split('.')[0] for f in old_facts})
    backend_only = _soften_backend(entries, old, new)
    if sampled:
        changed_inputs = 0
    else:
        if not (gstats.seeds[0] or gstats.seeds[1]):  # no group ran: seeds from the maskers
            noise = _noise_state()
            for side, m in enumerate((noise.old, noise.new)):
                if m is not None:
                    gstats.seeds[side] = set(m.replaced)
                    idx: dict[str, list[str]] = defaultdict(list)
                    for tok in gstats.seeds[side]:
                        if len(tok) >= 8:
                            idx[tok[:8]].append(tok)
                    gstats.seed_index[side] = idx
        changed_inputs = _judge_input_changes(pending, old_left, new_left, gstats)
    order_stats = _judge_order(pending, old, new, prof, order_causes)
    protected = _protected_symbols(context)
    env_readings = _explain_env_readings(pending, envcmp, protected)
    platform_read, platform_reviews = _judge_platform(pending, envcmp, protected)
    env_readings += platform_read

    unsampled = _apply_tallies(entries, old, new, ov, nv, modes)
    _soften_dropped(entries, old, new)
    _soften_evicted(entries, old, new)
    _soften_capped(entries, old, new, ov, nv, modes)
    if not sampled:
        _judge_input_variants(entries, ov, nv, modes)
    _review_masked_input(entries)
    variation = _run_variation(gstats)
    gated = _gate_unmeasured(entries, variation) if variation else 0
    env_reviews = _judge_environment(entries, envcmp, protected) + platform_reviews
    entries.sort(key=_entry_sort_key)
    warnings = _warnings(old, new)
    if unsampled:
        warnings.append(
            f"unsampled:{unsampled}: value facts seen on one side only, at call sites both "
            "sides' tallies show were called but not fully sampled; not compared")
    if variation:
        warnings.append(
            f"run-variation: {variation}; no replicate measured run-to-run noise, so "
            f"{gated} value difference(s) at calls with equal but not fully observed inputs are "
            "review items, not failures (capture a baseline replicate to settle)")
    for side, masker in (("old", _noise_state().old), ("new", _noise_state().new)):
        if masker is not None and masker.untimed:
            warnings.append(
                f"clock-unmasked:{side}:{masker.untimed}: facts without a call time (a "
                "facts-snapshot capture records none), so clock readings in them are not "
                "masked")
    if changed_inputs:
        warnings.append(
            f"input-changed:{changed_inputs}: calls on unedited lines that paired although "
            "their input differs where nothing volatile explains it (an untraced upstream "
            "change, or an ephemeral port, looks the same); review items unless an outcome kind "
            "or type differs")
    if tracer_self:
        warnings.append(
            f"tracer-self:{tracer_self}: facts whose call site is inside pymolt's own injected "
            "runtime (the tracer observing itself) were not compared")
    if backend_only:
        warnings.append(
            f"backend-only:{backend_only}: compiled calls only one side's backend can observe "
            f"({_families(old)} vs {_families(new)}) are reported, not compared")
    if gstats.full or gstats.partial or gstats.unpaired:
        warnings.append(
            f"volatile-sites: {gstats.full} fully paired by their input with volatile values "
            f"masked, {gstats.partial} partly, {gstats.unpaired} not at all; calls that pair no "
            "other way are one entry per site and kind of result")
    if c_level_excluded:
        warnings.append(
            f"c-level-excluded:{c_level_excluded}: C-level raise facts were not compared — "
            f"one side's backend ({_families(old)} vs {_families(new)}) does not record them")
    if prov.maps or prov.ledger_known:
        summary = FactDiff(entries=entries, no_ledger=prov.no_ledger).edited_summary()
        warnings.append("edit-provenance: " + summary)
    if context is not None and not prov.maps:
        warnings.append(
            "definition-spans-unknown: no source snapshots, so an edit to a decorator or a class "
            "body does not make the calls on its definition line edited sites")
    if order_stats["output"] or order_stats["input"] or order_stats["strict"]:
        warnings.append(
            f"dict-order: {order_stats['output']} returned dict(s) and {order_stats['input']} "
            f"input dict(s) reaching an order-sensitive call changed key order only"
            + (f", {order_stats['strict']} input order change(s) with an output that differs "
               "beyond a reordering (strict)" if order_stats["strict"] else "")
            + (f"; {order_stats['cited']} cited by a known cause" if order_stats["cited"]
               else ""))
    if env_reviews or env_readings:
        warnings.append(
            f"environment-differs: the captures ran in different environments "
            f"({', '.join(envcmp.differs)}); {env_reviews} difference(s) at calls that read a "
            f"differing aspect are reviews, {env_readings} value(s) are each side's own "
            "reading")
    return FactDiff(profile=prof, entries=entries, no_ledger=prov.no_ledger,
                    old_summary=old.summary(),
                    new_summary=new.summary(), warnings=warnings, unsampled=unsampled,
                    dict_order=_order_status(old, new),
                    environment=_env.stamp(envcmp, old.environment, new.environment,
                                           reviews=env_reviews, explained=env_readings,
                                           inferred=_env_inference(old, new)))


def _site_key(symbol: str, file: str, func: str, line: int | None, text: str | None,
              modes: dict[str, str]) -> tuple:
    return (symbol, file, func, text if modes.get(file) == "text" else line)


def _tally_index(fs: FactSet, modes: dict[str, str]) -> dict[tuple, dict] | None:
    """Complete tallies per site key (lines sharing a text merge); None = not usable."""
    if not fs.tallies_complete or fs.site_tallies is None:
        return None
    idx: dict[tuple, dict] = {}
    for t in fs.site_tallies:
        k = _site_key(t.symbol, t.file, t.func, t.line, t.text, modes)
        agg = idx.get(k)
        if agg is None:
            agg = idx[k] = {"count": 0, "returns": 0, "raises": {}, "tally": t}
        agg["count"] += t.count
        agg["returns"] += t.returns
        for name, n in t.raises.items():
            agg["raises"][name] = agg["raises"].get(name, 0) + n
    return idx


def _apply_tallies(entries: list[FactEntry], old: FactSet, new: FactSet, ov: list[_V],
                   nv: list[_V], modes: dict[str, str]) -> int:
    """Decide what value sampling cannot, from complete tallies (every call counted).

    * A value fact seen on one side only, at a site the other side's tallies show was
      called: if that side sampled every call there, the input really is gone (kept,
      exact); otherwise it was not sampled — dropped from the entries and counted.
    * ... at a site the other side's tallies never saw: the call is gone (exact).
    * A site tallied on one side only and with no value facts there: DISAPPEARED/APPEARED.
    * A site tallied on both sides whose outcome kinds differ (returns on one side only, a
      raise type on one side only): CHANGED at level ``kind``, unless a value entry already
      fails there. Proportions are not compared.
    Returns the number of unsampled facts."""
    o_idx = _tally_index(old, modes)
    n_idx = _tally_index(new, modes)
    if o_idx is None and n_idx is None:
        return 0

    def sampled_calls(views: list[_V]) -> dict[tuple, int]:
        out: dict[tuple, int] = defaultdict(int)
        for v in views:
            f = v.fact
            out[(f.symbol, f.file, f.func, v.site_value)] += f.count
        return out

    o_sampled, n_sampled = sampled_calls(ov), sampled_calls(nv)
    unsampled = 0
    kept: list[FactEntry] = []
    covered_old: set[tuple] = set()
    covered_new: set[tuple] = set()
    failing: set[tuple] = set()
    for e in entries:
        ok = _entry_key(e, e.old_site, modes) if e.old_site else None
        nk = _entry_key(e, e.new_site, modes, new_side=True) if e.new_site else None
        if ok:
            covered_old.add(ok)
        if nk:
            covered_new.add(nk)
        missing_side = None
        if e.category is FactCategory.DISAPPEARED and n_idx is not None:
            missing_side, idx, done, key = "new", n_idx, n_sampled, ok
        elif e.category is FactCategory.APPEARED and o_idx is not None:
            missing_side, idx, done, key = "old", o_idx, o_sampled, nk
        if missing_side is not None:
            t = idx.get(key)
            if t is not None and done.get(key, 0) < t["count"]:
                unsampled += 1
                continue
            e.level = ComparisonLevel.EXACT
            e.notes.append("absent-from-tallies" if t is None else "site-fully-sampled")
        if e.fails:
            failing.update(k for k in (ok, nk) if k)
        kept.append(e)
    entries[:] = kept

    texts = _function_texts(old, new, ov, nv, modes)
    if n_idx is not None and o_idx is not None:
        for k in o_idx.keys() - n_idx.keys():
            if k not in covered_old and not _rewritten(k, texts, side=0, modes=modes):
                entries.append(_tally_entry(o_idx[k], None, FactCategory.DISAPPEARED))
        for k in n_idx.keys() - o_idx.keys():
            if k not in covered_new and not _rewritten(k, texts, side=1, modes=modes):
                entries.append(_tally_entry(None, n_idx[k], FactCategory.APPEARED))
        for k in o_idx.keys() & n_idx.keys():
            if k in failing:
                continue
            o, n = o_idx[k], n_idx[k]
            if (o["returns"] > 0) != (n["returns"] > 0) or not _raise_sets_match(
                    o["raises"], n["raises"]):
                entries.append(_tally_entry(o, n, FactCategory.CHANGED))
    return unsampled


def _entry_key(e: FactEntry, site: SiteRef, modes: dict[str, str], *,
               new_side: bool = False) -> tuple:
    symbol = (e.new_symbol or e.symbol) if new_side else e.symbol
    return _site_key(symbol, site.file, site.func, site.line, site.text, modes)


def _function_texts(old: FactSet, new: FactSet, ov: list[_V], nv: list[_V],
                    modes: dict[str, str]) -> list[dict[tuple, set]]:
    texts: list[dict[tuple, set]] = [defaultdict(set), defaultdict(set)]
    for side, (views, fs) in enumerate(((ov, old), (nv, new))):
        for v in views:
            if modes.get(v.fact.file) == "text":
                texts[side][(v.fact.file, v.fact.func)].add(v.site_value)
        for t in fs.site_tallies or ():
            if modes.get(t.file) == "text":
                texts[side][(t.file, t.func)].add(t.text)
    return texts


def _rewritten(k: tuple, texts: list[dict[tuple, set]], *, side: int,
               modes: dict[str, str]) -> bool:
    """A tally site whose text is gone from an edited function (REWRITTEN territory)."""
    _symbol, file, func, value = k
    if modes.get(file) != "text":
        return False
    mine, theirs = texts[side].get((file, func), set()), texts[1 - side].get((file, func), set())
    return value not in theirs and bool(theirs - mine)


def _raise_sets_match(a: Mapping[str, int], b: Mapping[str, int]) -> bool:
    xs, ys = [k for k, n in a.items() if n], [k for k, n in b.items() if n]
    if not xs and not ys:
        return True
    if not xs or not ys:
        return False
    return _cover(xs, ys, _raises_match)


def _kind_outcomes(t: dict) -> list[Outcome]:
    out = []
    if t["returns"]:
        out.append(Outcome(kind="return", value={"__opaque__": "<any return>"},
                           count=t["returns"]))
    for name, n in sorted(t["raises"].items()):
        if n:
            out.append(Outcome(kind="raise", value=name, count=n))
    return out


def _tally_site(t: SiteTally) -> SiteRef:
    return SiteRef(file=t.file, func=t.func, line=t.line, text=t.text)


def _tally_entry(o: dict | None, n: dict | None, category: FactCategory) -> FactEntry:
    """An entry decided from complete tallies alone (no value facts compared)."""
    ref = (o or n)["tally"]
    changed = category is FactCategory.CHANGED
    return FactEntry(
        category=category,
        comparison=Comparison.CHANGED if changed else Comparison.MISSING,
        level=ComparisonLevel.KIND if changed else ComparisonLevel.EXACT,
        symbol=ref.symbol,
        new_symbol=None,
        old_site=_tally_site(o["tally"]) if o else None,
        new_site=_tally_site(n["tally"]) if n else None,
        old=_kind_outcomes(o) if o else None, new=_kind_outcomes(n) if n else None,
        old_count=o["count"] if o else 0, new_count=n["count"] if n else 0,
        notes=["from-tallies"],
        old_where=o["tally"].where if o else None, new_where=n["tally"].where if n else None,
    )


def _is_c_level(f: Fact) -> bool:
    return isinstance(f.input, dict) and f.input.get("c_level") is True and len(f.input) == 1


def _families(fs: FactSet) -> str:
    return ",".join(fs.backends) or "unknown"


def _exclude_c_level(old: FactSet, new: FactSet) -> tuple[list[Fact], list[Fact], int]:
    """Drop ``{"c_level": true}`` facts from both sides when exactly one side's backends can
    record them (setprofile vs monitoring): their presence then says which backend ran, not
    how the dependency behaved. With the same family (or an unknown side) they compare."""
    if not (old.backends and new.backends):
        return old.facts, new.facts, 0
    o_c = bool(_C_LEVEL_BACKENDS.intersection(old.backends))
    n_c = bool(_C_LEVEL_BACKENDS.intersection(new.backends))
    if o_c == n_c:
        return old.facts, new.facts, 0
    o = [f for f in old.facts if not _is_c_level(f)]
    n = [f for f in new.facts if not _is_c_level(f)]
    return o, n, (len(old.facts) - len(o)) + (len(new.facts) - len(n))

_WIDENED = json.loads(WIDENED_INPUT)


def _is_widened_input(inputs: Any) -> bool:
    return isinstance(inputs, dict) and inputs.get("__widened__") is True


def _collapse_widened(old_facts: list[Fact], new_facts: list[Fact]) -> tuple[list, list]:
    """A (symbol, site) that a snapshot widened on either side is compared as one type-level
    fact on both sides: which inputs kept an exact fact depends on call order, so pairing
    the exact ones across runs would invent DISAPPEARED/APPEARED facts."""
    def group(f: Fact) -> tuple:
        return (f.symbol, f.file, f.func, f.text if f.text is not None else f.line)

    widened = {group(f) for f in (*old_facts, *new_facts) if _is_widened_input(f.input)}
    if not widened:
        return old_facts, new_facts

    def collapse(facts: list[Fact]) -> list[Fact]:
        out: list[Fact] = []
        groups: dict[tuple, list[Fact]] = defaultdict(list)
        for f in facts:
            if group(f) in widened:
                groups[group(f)].append(f)
            else:
                out.append(f)
        for members in groups.values():
            reduced = [m.model_copy(update={
                "input": dict(_WIDENED), "input_order": None,
                "outcomes": [o if o.kind == "raise" else
                             Outcome(kind="return", value=widen_value(o.value), count=o.count)
                             for o in m.outcomes],
            }) for m in members]
            out.append(_merge_facts(reduced))
        out.sort(key=lambda f: f.first_seen)
        return out

    return collapse(old_facts), collapse(new_facts)


def _soften_evicted(entries: list[FactEntry], old: FactSet, new: FactSet) -> None:
    """A fact missing on a side that evicted facts may have been evicted there, not have
    vanished: when that side's tallies show calls of the symbol in the same function (or the
    tallies are unknown), the entry is UNCOMPARABLE (inconclusive) instead of failing."""
    for e in entries:
        if "absent-from-tallies" in e.notes:
            continue  # complete tallies proved the call is gone: eviction cannot explain it
        if e.category is FactCategory.DISAPPEARED and new.evicted:
            side, fs = "new", new
        elif e.category is FactCategory.APPEARED and old.evicted:
            side, fs = "old", old
        else:
            continue
        site = e.site
        if fs.tally_sites is None or (e.symbol, site.file, site.func) in fs.tally_sites:
            e.category = FactCategory.UNCOMPARABLE
            e.notes.append(f"possibly-evicted:{side}")


_PSEUDO_SCOPES = ("<listcomp>", "<dictcomp>", "<setcomp>", "<genexpr>", "<lambda>")


def _normalize_scopes(facts: list[Fact]) -> list[Fact]:
    """Comprehension, generator and lambda frames belong to the function that contains them
    (3.12 inlines list/dict/set comprehensions, so the same code is ``<listcomp>`` on one side
    and the enclosing function on the other). A qualname ``f.<listcomp>`` becomes ``f``; a bare
    pseudo-scope takes the real function recorded at the same (file, line) on its own side."""
    real: dict[tuple[str, int], str] = {}
    for f in facts:
        if f.line is not None and not f.func.startswith("<") and "<" not in f.func:
            real.setdefault((f.file, f.line), f.func)
    out = []
    for f in facts:
        func = f.func
        for marker in _PSEUDO_SCOPES:
            if f".{marker}" in func:
                func = func.split(f".{marker}")[0]
        if func in _PSEUDO_SCOPES and f.line is not None:
            func = real.get((f.file, f.line), func)
        out.append(f if func == f.func else f.model_copy(update={"func": func}))
    return out


def _site_modes(old_facts: list[Fact], new_facts: list[Fact],
                *tallies: list[SiteTally] | None) -> dict[str, str]:
    """Per file: ``text`` when every fact (and tally) in it on both sides has its line text,
    else ``line`` (a site key must mean the same thing on both sides)."""
    modes: dict[str, str] = {}
    for f in (*old_facts, *new_facts, *(t for ts in tallies if ts for t in ts)):
        if f.text is None:
            modes[f.file] = "line"
        else:
            modes.setdefault(f.file, "text")
    return modes


def _views(facts: list[Fact], modes: dict[str, str]) -> list[_V]:
    groups: dict[tuple, list[Fact]] = defaultdict(list)
    for f in facts:
        sv = f.text if modes.get(f.file) == "text" else f.line
        groups[(f.symbol, f.file, f.func, sv, _canon(f.input), f.binding)].append(f)
    views = [_V(fact=_merge_facts(fs), site_value=key[3], input_key=key[4])
             for key, fs in groups.items()]
    views.sort(key=lambda v: v.first_seen)
    return views


def _core_input(inputs: Any, names: Iterable[str]) -> Any:
    """The input with any parameter that either side reported as defaulted removed."""
    names = set(names)
    if not names or not isinstance(inputs, dict) or not isinstance(inputs.get("bound"), dict):
        return inputs
    return {**inputs, "bound": {k: v for k, v in inputs["bound"].items() if k not in names}}


def _mark_rewritten_sites(ov: list[_V], nv: list[_V], modes: dict[str, str]) -> None:
    """A text-mode site is *rewritten* when its line text is absent from the same function on
    the other side AND that function has line texts absent on this side (it was edited)."""
    texts: list[dict[tuple, set]] = [defaultdict(set), defaultdict(set)]
    for side, views in enumerate((ov, nv)):
        for v in views:
            if modes.get(v.fact.file) == "text":
                texts[side][(v.fact.file, v.fact.func)].add(v.site_value)
    for side, views in enumerate((ov, nv)):
        mine, theirs = texts[side], texts[1 - side]
        for v in views:
            fkey = (v.fact.file, v.fact.func)
            if modes.get(v.fact.file) != "text" or v.site_value in theirs.get(fkey, ()):
                continue
            v.rewritten_site = bool(theirs.get(fkey, set()) - mine.get(fkey, set()))


def _pair_groups(
    old_left: list[_V], new_left: list[_V], key_fn: Callable[[_V], Any],
) -> tuple[list[tuple[_V, _V]], list[_V], list[_V]]:
    """Pair facts with equal keys one-to-one: a key group pairs only when both sides hold the
    same number of facts (zipped in first-seen order); ambiguous groups stay unpaired."""
    go: dict[Any, list[_V]] = defaultdict(list)
    gn: dict[Any, list[_V]] = defaultdict(list)
    for v in old_left:
        go[key_fn(v)].append(v)
    for v in new_left:
        gn[key_fn(v)].append(v)
    pairs: list[tuple[_V, _V]] = []
    paired: set[int] = set()
    for key, olds in go.items():
        news = gn.get(key)
        if not news or len(news) != len(olds):
            continue
        olds = sorted(olds, key=lambda v: v.first_seen)
        news = sorted(news, key=lambda v: v.first_seen)
        for o, n in zip(olds, news):
            pairs.append((o, n))
            paired.update((id(o), id(n)))
    return (pairs, [v for v in old_left if id(v) not in paired],
            [v for v in new_left if id(v) not in paired])


def _group_volatile(
    old_left: list[_V], new_left: list[_V], site_fn: Callable[[_V], tuple],
    old_facts: list[Fact], new_facts: list[Fact], dropped: tuple[bool, bool] = (False, False),
) -> tuple[list[FactEntry], list[_V], list[_V], "_GroupStats"]:
    """Leftovers at one unedited ``(symbol, site)`` with facts on both sides that no pass could
    pair (not one-to-one), with the same input signatures, are judged together.

    * Their outcome kinds and types are checked over the whole site (inputs widened, returns
      reduced to their type): a new raise or return type is one CHANGED entry and fails.
    * Each member's *masked key* is its input with its volatile leaves replaced (see
      ``_group_evidence``). Members whose masked key occurs on both sides pair by that key and
      compare their outcome sets by value once masked: equal -> one EQUAL entry at MASKED;
      different -> one review entry (inputs equal only once volatile values were masked, so
      the output may derive from them: only a replicate tells).
    * Members whose masked key occurs on one side only are not explained by volatility: one
      ``unpaired-site`` entry (strict; a review item when the unmeasured-noise gate is on). On
      a side that dropped events, a member whose key would be shared once a leaf shaped like
      the site's volatile values is taken as volatile may have lost its producer: one
      UNCOMPARABLE ``possibly-dropped-producer`` entry instead.
    A site whose members all pair by masked key is *fully evidenced*."""
    go: dict[tuple, list[_V]] = defaultdict(list)
    gn: dict[tuple, list[_V]] = defaultdict(list)
    for v in old_left:
        if not v.rewritten_site:
            go[(v.fact.symbol, site_fn(v))].append(v)
    for v in new_left:
        if not v.rewritten_site:
            gn[(v.fact.symbol, site_fn(v))].append(v)
    groups: list[tuple[list[_V], list[_V]]] = []
    for key, olds in go.items():
        news = gn.get(key)
        if not news or (len(olds) == 1 and len(news) == 1):
            continue
        if {_signature(v.fact) for v in olds} != {_signature(v.fact) for v in news}:
            continue  # the input signature changed: structure, not noise; stays strict
        groups.append((olds, news))
    stats = _GroupStats()
    judged = _group_evidence(groups, old_facts, new_facts, stats)
    entries: list[FactEntry] = []
    for (olds, news), j in zip(groups, judged, strict=True):
        entries.extend(_group_entries(olds, news, j, dropped, stats))
    grouped = {id(v) for olds, news in groups for v in (*olds, *news)}
    return (entries, [v for v in old_left if id(v) not in grouped],
            [v for v in new_left if id(v) not in grouped], stats)


@dataclass
class _GroupStats:
    full: int = 0        # every member paired by masked key
    partial: int = 0     # some did
    unpaired: int = 0    # none did
    triggers: list[str] = field(default_factory=list)  # full on source/clock masks, both sides
    # per side: seeds (replaced run values) with their 8-char index, and whole propagated
    # values (outputs of fully paired sites' members)
    seeds: list = field(default_factory=lambda: [set(), set()])
    seed_index: list = field(default_factory=lambda: [{}, {}])
    whole: list = field(default_factory=lambda: [set(), set()])


@dataclass
class _Judged:
    keys: list[list[str]]                   # per side, per member: its masked key
    volatile: list[list[dict]]              # per side, per member: volatile leaf paths
    mask_backed: bool = False               # every member on both sides has a source/clock mask


_FIXPOINT_ROUNDS = 4  # propagation hops: seed -> group -> group -> group (then stop)


def _volatile_leaves(v: _V, seeds: set[str], seed_index: dict[str, list[str]],
                     whole: set[str]) -> dict[tuple, str]:
    """The member's volatile input leaves, path -> why: ``mask`` (a source or clock mask
    replaced part of it), ``environment`` (only an environment mask did), ``seed`` (it holds a
    run value a mask replaced elsewhere on this side: equal, or containing one of 8+
    characters), ``propagated`` (it equals, as a whole, an output of a fully evidenced
    group's member)."""
    canon, raw = _leaf_map(v.fact.input), _leaf_map(_raw_of(v.fact).input)
    out: dict[tuple, str] = {}
    for path, c in canon.items():
        r = raw.get(path, c)
        if _canon(c) != _canon(r):
            out[path] = "mask" if isinstance(c, str) and ("⟨src:" in c or "⟨clock:" in c) \
                else "environment"
        elif isinstance(r, str) and (r in seeds or any(
                any(r.startswith(tok, m.start())
                    for tok in seed_index.get(r[m.start():m.start() + 8], ()))
                for m in _ALNUM.finditer(r))):
            out[path] = "seed"
        elif _canon(r) in whole:
            out[path] = "propagated"
    return out


def _vmask_key(v: _V, volatile: dict) -> str:
    leaves = _leaf_map(v.fact.input)
    return _canon(sorted((repr(p), "⟨v⟩" if p in volatile else _canon(x))
                         for p, x in leaves.items()))


def _group_evidence(groups: list[tuple[list[_V], list[_V]]], old_facts: list[Fact],
                    new_facts: list[Fact], stats: "_GroupStats | None" = None) -> list[_Judged]:
    """Each group's masked keys, after propagating volatility to a small fixpoint.

    Seeds, per side: the run values a source or clock mask actually replaced (matched inside a
    leaf too, from 8 characters). A group whose members ALL pair by masked key is fully
    evidenced, and its members' outputs then propagate as whole values (matched by equality
    only, never inside a leaf: ``application/json`` never becomes volatile). At most
    ``_FIXPOINT_ROUNDS`` rounds; a round that fully evidences nothing stops early."""
    noise = _noise_state()
    seeds = [set(m.replaced) if m is not None else set() for m in (noise.old, noise.new)]
    seed_index: list[dict[str, list[str]]] = [defaultdict(list), defaultdict(list)]
    for side in (0, 1):
        for tok in seeds[side]:
            if len(tok) >= 8:
                seed_index[side][tok[:8]].append(tok)
    whole: list[set[str]] = [set(), set()]
    judged: list[_Judged | None] = [None] * len(groups)
    full: set[int] = set()
    for _round in range(_FIXPOINT_ROUNDS):
        grew = False
        for i, (olds, news) in enumerate(groups):
            if i in full:
                continue
            keys, vols = [], []
            for side, members in ((0, olds), (1, news)):
                vl = [_volatile_leaves(v, seeds[side], seed_index[side], whole[side])
                      for v in members]
                vols.append(vl)
                keys.append([_vmask_key(v, x) for v, x in zip(members, vl, strict=True)])
            j = judged[i] = _Judged(keys=keys, volatile=vols)
            if set(keys[0]) != set(keys[1]):
                continue
            full.add(i)
            j.mask_backed = all(any(why in ("mask", "seed") for why in x.values())
                                for side_vols in vols for x in side_vols)
            for side, members in ((0, olds), (1, news)):
                for v in members:
                    for o in (*v.fact.outcomes, *_raw_of(v.fact).outcomes):
                        if o.kind == "return":
                            whole[side].update(_whole_values(o.value))
            grew = True
        if not grew:
            break
    if stats is not None:
        stats.seeds, stats.seed_index, stats.whole = seeds, seed_index, whole
    return [j for j in judged if j is not None]


_SCALAR_TAGS = ("__bytes__", "__uuid__", "__datetime__", "__decimal__")


def _whole_values(value: Any) -> set[str]:
    """An output as it can reach another call's input leaf, whole: a scalar of 8+ characters
    (so ``ok`` or ``1`` never propagate), or the scalar inside a one-scalar rendering
    (``{"__bytes__": hex}`` is seen as the hex leaf ``data.__bytes__``)."""
    if isinstance(value, dict) and len(value) == 1:
        tag, inner = next(iter(value.items()))
        if tag in _SCALAR_TAGS:
            value = inner
    if isinstance(value, str) and len(value) >= 8:
        return {_canon(value)}
    if isinstance(value, int | float) and not isinstance(value, bool) and len(repr(value)) >= 8:
        return {_canon(value)}
    return set()


def _shape(v: Any) -> str:
    return _token_placeholder(v) if isinstance(v, str) else type(v).__name__


def _producer_lost(member: int, side: int, j: _Judged, members: list[_V],
                   other_keys: set[str]) -> bool:
    """On a side that dropped events: would this member's key be shared if its leaves shaped
    like the site's volatile values (at the same paths) were volatile? Then the event that
    made them volatile may be among the lost ones."""
    shapes: dict[tuple, set[str]] = defaultdict(set)
    for k, (v, vol) in enumerate(zip(members, j.volatile[side], strict=True)):
        if k == member:
            continue
        raw = _leaf_map(_raw_of(v.fact).input)
        for path in vol:
            if path in raw:
                shapes[path].add(_shape(raw[path]))
    mine = members[member]
    raw = _leaf_map(_raw_of(mine.fact).input)
    extra = {p for p, x in raw.items() if p in shapes and p not in j.volatile[side][member]
             and _shape(x) in shapes[p]}
    if not extra:
        return False
    vol = {**j.volatile[side][member], **{p: "shape" for p in extra}}
    return _vmask_key(mine, vol) in other_keys


def _group_entries(olds: list[_V], news: list[_V], j: _Judged, dropped: tuple[bool, bool],
                   stats: _GroupStats) -> list[FactEntry]:
    sides = (olds, news)
    of = _merge_facts([_widened_member(v.fact) for v in olds])
    nf = _merge_facts([_widened_member(v.fact) for v in news])
    types = compare_outcomes(of.outcomes, nf.outcomes, DiffProfile.SHAPE)
    shared = set(j.keys[0]) & set(j.keys[1])
    n_shared = [sum(1 for k in j.keys[s] if k in shared) for s in (0, 1)]
    if not shared:
        stats.unpaired += 1
    elif n_shared == [len(olds), len(news)]:
        stats.full += 1
        if j.mask_backed:
            stats.triggers.append(f"{of.symbol} @ {_site_ref(olds[0].fact).label()}")
    else:
        stats.partial += 1
    label = f"{len(olds)}/{len(news)}"
    if types.comparison is not Comparison.EQUAL:
        # a new raise or return type anywhere at the site: one failing entry, whatever the keys
        category = FactCategory.UNCOMPARABLE \
            if types.comparison is Comparison.UNCOMPARABLE else FactCategory.CHANGED
        tag = "volatile-site" if shared else "unpaired-site"
        return [_site_entry(olds, news, category, types.comparison, ComparisonLevel.TYPE,
                            [f"{tag}:{label}", "type-changed", *types.notes],
                            f"{tag}: {label} facts; an outcome kind or type differs over the "
                            "site", types)]
    out: list[FactEntry] = []
    if shared:
        equal_m, diff_m, first_diff, first_equal = [[], []], [[], []], None, None
        for key in sorted(shared):
            ms = [[v for v, k in zip(sides[s], j.keys[s], strict=True) if k == key]
                  for s in (0, 1)]
            oo = _merge_facts([v.fact for v in ms[0]]).outcomes
            no = _merge_facts([v.fact for v in ms[1]]).outcomes
            # by value: the same set of (masked) outcomes on both sides; a set with several
            # values does not fall back to type level here
            cmp = compare_outcomes(oo, no, DiffProfile.EXACT)
            same = {_canon([o.kind, o.value]) for o in oo} == {_canon([o.kind, o.value])
                                                                for o in no}
            if not same and cmp.comparison is Comparison.EQUAL and (len(oo) > 1 or len(no) > 1):
                cmp = OutcomeComparison(Comparison.CHANGED, ComparisonLevel.MASKED,
                                        list(cmp.notes))
            bucket = equal_m if cmp.comparison is Comparison.EQUAL else diff_m
            for s in (0, 1):
                bucket[s].extend(ms[s])
            if bucket is diff_m and first_diff is None:
                first_diff = (ms, oo, no, cmp)
            if bucket is equal_m and first_equal is None:
                first_equal = (ms[0][0], ms[1][0], oo, no)
        if equal_m[0]:
            out.append(_site_entry(
                equal_m[0], equal_m[1], FactCategory.EQUAL, Comparison.EQUAL,
                ComparisonLevel.MASKED, [f"volatile-site:{len(equal_m[0])}/{len(equal_m[1])}"],
                f"volatile-site: {len(equal_m[0])} old / {len(equal_m[1])} new facts paired by "
                "their input with its volatile values masked; outcomes equal once masked",
                types, sample=first_equal))
        if diff_m[0]:
            ms, oo, no, cmp = first_diff
            e = _site_entry(
                diff_m[0], diff_m[1], FactCategory.CHANGED, Comparison.CHANGED,
                ComparisonLevel.MASKED,
                [f"volatile-site:{len(diff_m[0])}/{len(diff_m[1])}",
                 "matched-modulo-masked-input"],
                f"masked-input: {len(diff_m[0])} old / {len(diff_m[1])} new facts paired by "
                "their input with its volatile values masked; outcomes differ", types,
                sample=(ms[0][0], ms[1][0], oo, no))
            e.review = _MASKED_INPUT_REVIEW
            out.append(e)
    lost: list[list[_V]] = [[], []]
    strict: list[list[_V]] = [[], []]
    for s in (0, 1):
        other = set(j.keys[1 - s])
        for k, (v, key) in enumerate(zip(sides[s], j.keys[s], strict=True)):
            if key in shared:
                continue
            if dropped[s] and _producer_lost(k, s, j, sides[s], other):
                lost[s].append(v)
            else:
                strict[s].append(v)
    for s, name in ((0, "old"), (1, "new")):
        if lost[s]:
            e = _site_entry(lost[0] if s == 0 else [], lost[1] if s == 1 else [],
                            FactCategory.UNCOMPARABLE, Comparison.MISSING, None,
                            [f"possibly-dropped-producer:{name}"],
                            f"possibly-dropped: {len(lost[s])} {name} fact(s) whose input "
                            "would pair if a value shaped like this site's volatile values "
                            f"were one; the {name} capture dropped events", types)
            out.append(e)
    if strict[0] or strict[1]:
        e = _site_entry(strict[0], strict[1], FactCategory.CHANGED, Comparison.CHANGED,
                        ComparisonLevel.TYPE,
                        [f"unpaired-site:{len(strict[0])}/{len(strict[1])}", "types-equal"],
                        f"unpaired-site: {len(strict[0])} old / {len(strict[1])} new facts "
                        "whose inputs differ with no visible source of the variation", types,
                        sample=(strict[0][0] if strict[0] else None,
                                strict[1][0] if strict[1] else None, None, None))
        # outcome kinds and types agree over the site (a difference failed above): the
        # inputs reaching it changed, which is evidence of a change, not that the dependency's
        # behaviour did
        e.review = (f"{UNPAIRED_SITE}: {len(strict[0])} old / {len(strict[1])} new calls whose "
                    f"input changed with no visible cause; {_REPLICATE_HINT}")
        out.append(e)
    return out


_MASKED_INPUT_REVIEW = (
    "masked input: the value differs and the inputs matched only once volatile values were "
    "masked, so the output may derive from them; capture a baseline replicate to settle")
INPUT_CHANGED = "input changed"
UNPAIRED_SITE = "unpaired site"
_REPLICATE_HINT = "capture a baseline replicate to tell run noise from an upstream change"


def _site_entry(olds: list[_V], news: list[_V], category: FactCategory,
                comparison: Comparison, level: ComparisonLevel | None, notes: list[str],
                explained: str, types: OutcomeComparison,
                sample: tuple | None = None) -> FactEntry:
    """One entry standing for several facts of one site (counted, never dropped)."""
    members = olds or news
    ref = members[0].fact
    of = _merge_facts([_widened_member(v.fact) for v in olds]) if olds else None
    nf = _merge_facts([_widened_member(v.fact) for v in news]) if news else None
    so, sn, oo, no = sample or (None, None, None, None)
    old_out = oo if oo is not None else (of.outcomes if of else None)
    new_out = no if no is not None else (nf.outcomes if nf else None)
    notes = list(notes)
    return FactEntry.model_construct(
        _ENTRY_FIELDS_SET, category=category, comparison=comparison, level=level,
        symbol=ref.symbol, new_symbol=None,
        old_site=_site_ref(olds[0].fact) if olds else None,
        new_site=_site_ref(news[0].fact) if news else None,
        input=so.fact.input if so is not None else (dict(_WIDENED) if olds else
                                                    (sn.fact.input if sn else dict(_WIDENED))),
        new_input=sn.fact.input if (sn is not None and so is not None) else None,
        old=_cap(old_out, notes, "old") if old_out is not None else None,
        new=_cap(new_out, notes, "new") if new_out is not None else None,
        old_types=types.old_types or (_distinct_types(of.outcomes) if of else None),
        new_types=types.new_types or (_distinct_types(nf.outcomes) if nf else None),
        old_defaulted=of.defaulted if of else None, new_defaulted=nf.defaulted if nf else None,
        drift=False, old_count=sum(v.fact.count for v in olds),
        new_count=sum(v.fact.count for v in news), notes=notes,
        old_where=of.where if of else None, new_where=nf.where if nf else None,
        explained=explained,
        input_raw=_raw_of(so.fact).input if so is not None and _masks_of(so.fact) else None,
        new_input_raw=_raw_of(sn.fact).input if sn is not None and _masks_of(sn.fact) else None,
        old_raw=list(_raw_of(so.fact).outcomes) if so is not None and _masks_of(so.fact)
        else None,
        new_raw=list(_raw_of(sn.fact).outcomes) if sn is not None and _masks_of(sn.fact)
        else None,
    )


UNMEASURED_NOISE = "unmeasured noise"
_HIDDEN_KEYS = ("__opaque__", "__cycle__", "__shape__", "__nondeterministic__")


def _run_variation(stats: "_GroupStats") -> str:
    """The unmeasured-noise gate's trigger, with its basis (empty when off): at least one
    volatile site whose calls ALL pair by their input once source or clock values are masked,
    on both sides. Not a site evidenced only by propagation or by the drop allowance, and not a
    lone clock reading, substitution or entropy value."""
    sites = stats.triggers
    if not sites:
        return ""
    shown = ", ".join(sites[:5])
    more = f" (+{len(sites) - 5})" if len(sites) > 5 else ""
    return f"{len(sites)} volatile site(s) fully paired on masked run values: {shown}{more}"


def _hidden(v: Any) -> bool:
    if isinstance(v, dict):
        if any(k in v for k in _HIDDEN_KEYS) or v.get("__truncated__") \
                or v.get("__c_call__") is True:
            return True
        if "__structure__" in v and "values" not in v:
            return True
        return any(_hidden(x) for x in v.values())
    if isinstance(v, list):
        return any(_hidden(x) for x in v)
    return False


def input_hidden(inputs: Any) -> bool:
    """The call's input is not fully observed: a receiver or argument rendered by type only
    (opaque, a cycle, truncated, a value-less structure), or a compiled call whose arguments
    were not recorded. A call that binds nothing and has no receiver is fully observed: its
    input is the empty input (``cfg.default_timeout()`` returning 60 instead of 30 fails)."""
    return isinstance(inputs, dict) and _hidden(inputs)


def _kinds(outcomes: list[Outcome] | None) -> set[str]:
    return {"return" if o.kind == "return" else f"raise:{_raise_name(o.value)}"
            for o in outcomes or ()}


def _gate_unmeasured(entries: list[FactEntry], variation: str) -> int:
    """No replicate measured run-to-run noise, and this workload varies between runs: a value
    difference at an unedited pair with EQUAL inputs, where the input is not fully observed
    (a receiver or argument rendered by type only, a compiled call's unrecorded arguments), is
    not a controlled comparison, so it is a review item (INCONCLUSIVE), not a failure. Pairs
    whose inputs differ are the input-change and masked-input rules' business; outcome-kind
    and type changes always fail."""
    count = 0
    for e in entries:
        if "matched-modulo-input" in e.notes or "matched-modulo-masked-input" in e.notes:
            continue  # inputs differ: the input-change and masked-input rules judge those
        if _order_proved(e):
            continue  # a dict key order shown deterministic (_judge_order): not run noise
        if (e.category is not FactCategory.CHANGED or e.comparison is not Comparison.CHANGED
                or e.review or e.noise or e.unverified or e.provenance not in (None, "unedited")
                or e.level not in (ComparisonLevel.EXACT, ComparisonLevel.MASKED)
                or (e.explained or "").startswith(("volatile-site", "unpaired-site"))
                or "input-changed" in e.notes
                or not _kinds_match(e.old, e.new)
                or not _cover(_distinct_types(e.old or []), _distinct_types(e.new or []),
                              _outcome_types_match)):
            continue  # kind or structure changed: strict whatever the noise
        if not input_hidden(e.input):
            continue
        e.review = (f"{UNMEASURED_NOISE}: the value differs and its input is not fully "
                    f"observed, in a workload whose values vary between runs ({variation}); "
                    "capture a baseline replicate to settle")
        e.notes.append("unmeasured-noise")
        e.explained = "unmeasured-noise: its input is not fully observed"
        count += 1
    return count


_NOT_INPUT_CHANGE = ("wrapper-naming", "attribution-moved")


def _judge_input_changes(pending: list[tuple[_V, _V, FactEntry]], old_left: list[_V],
                         new_left: list[_V], stats: "_GroupStats") -> int:
    """A pair matched modulo input on an unedited line is the same call given another input.
    When every leaf where the inputs differ is volatile on both sides (masked, a seed, a
    propagated value) the run explains it. Otherwise the input itself changed for no visible
    reason, which is what an untraced upstream change looks like (``round()`` feeding
    ``charge(amount=...)``), and also what an ephemeral port looks like: evidence that
    something changed, not that the dependency did. Whatever the gate: a review item when the
    outcome kinds and types agree (the outcome comparison, and any substitution explanation,
    kept in the entry as evidence), a failure when they do not. Edited lines keep their
    provenance tiers; an input that carries values produced on edited lines is downstream of
    the edit and keeps that tier."""
    fresh_old, fresh_new = _fresh_labels(pending, old_left, new_left)
    count = 0
    for o, n, e in pending:
        if ("matched-modulo-input" not in e.notes or o.rewritten_site or n.rewritten_site
                or e.provenance not in (None, "unedited") or e.review or e.noise
                or any(x in e.notes for x in _NOT_INPUT_CHANGE)
                or any("while binding" in x for x in e.notes)):
            continue
        if _is_c_call(o) or _is_c_call(n):
            continue  # a compiled call's arguments were never recorded: nothing to compare
        if _input_labels(n.fact.input) & fresh_new or _input_labels(o.fact.input) & fresh_old:
            continue  # downstream of an edit
        changed, drift = _unexplained_input_leaves(o, n, stats)
        if changed is None:
            # the same argument values: only how the new version binds or names them drifted
            if drift:
                e.notes.append("binding-or-type-name-drift")
            if e.explained is None:
                e.explained = ("binding: the same argument values, bound or named differently "
                               "by the new version" + ("; a type name drifted" if drift else ""))
            continue
        if not changed:
            # every differing leaf is volatile: a masked-input pair (equal stays equal; a value
            # difference is a review; a kind or type difference fails)
            if "matched-modulo-masked-input" not in e.notes:
                e.notes.append("matched-modulo-masked-input")
            continue
        outcome = e.comparison.value
        kinds_ok = _kinds_match(e.old, e.new) and _cover(
            _distinct_types(e.old or []), _distinct_types(e.new or []), _outcome_types_match)
        e.category, e.comparison = FactCategory.CHANGED, Comparison.CHANGED
        e.notes.extend(["input-changed", f"outcomes-{outcome}"])
        shown = ", ".join(changed[:3]) + (f" (+{len(changed) - 3})" if len(changed) > 3 else "")
        prev = e.explained
        e.explained = (f"input-changed: the input differs where nothing volatile explains it "
                       f"({shown}); outcomes {outcome}" + (f"; {prev}" if prev else ""))
        if kinds_ok:
            e.review = (f"{INPUT_CHANGED}: the input reaching this call changed with no "
                        f"visible cause ({shown}); {_REPLICATE_HINT}")
        count += 1
    return count


INPUT_VARIANT = "input-variant"


def _judge_input_variants(entries: list[FactEntry], ov: list[_V], nv: list[_V],
                          modes: dict[str, str]) -> int:
    """An input seen on one side only, at a call both sides make: a call on an unedited line
    that has no partner, while the other side made the same call there with other inputs, with
    the same outcome kinds and types over the site and the same call signature. A poll that saw
    one more intermediate value, or a loop over a shorter list, looks like this: what reached
    the call changed, which is not proof that the dependency did. A review, like an input change
    (``input-variant``); a baseline replicate can show it to be noise but never confirms it,
    since an unpaired input is call volume made visible by grouping calls per input. A new or
    vanished outcome kind or type over the site, or a signature the other side never used
    there, still fails. Not on a sampled capture (the caller skips it). Returns how many."""
    by_site: tuple[dict[tuple, list[Fact]], dict[tuple, list[Fact]]] = (
        defaultdict(list), defaultdict(list))
    for side, views in enumerate((ov, nv)):
        for v in views:
            by_site[side][(v.fact.symbol, v.fact.file, v.fact.func, v.site_value)].append(v.fact)
    count = 0
    for e in entries:
        if e.category not in (FactCategory.DISAPPEARED, FactCategory.APPEARED) \
                or e.review or e.noise or e.unverified \
                or e.provenance not in (None, "unedited"):
            continue
        mine = 0 if e.category is FactCategory.DISAPPEARED else 1
        site = e.old_site if mine == 0 else e.new_site
        if site is None:
            continue
        key = (e.symbol, site.file, site.func,
               site.text if modes.get(site.file) == "text" else site.line)
        other, here = by_site[1 - mine].get(key), by_site[mine].get(key, [])
        if not other or not here:
            continue
        if _input_signature(e.input) not in {_signature(f) for f in other}:
            continue
        outs = ([o for f in here for o in f.outcomes], [o for f in other for o in f.outcomes])
        if not (_kinds_match(*outs) and _cover(_distinct_types(outs[0]),
                                               _distinct_types(outs[1]), _outcome_types_match)):
            continue
        nearest = min((sorted(input_leaf_differences(e.input, f.input)) for f in other),
                      key=len)
        shown = ", ".join(nearest[:3]) + (f" (+{len(nearest) - 3})" if len(nearest) > 3 else "")
        where = "old" if mine == 0 else "new"
        e.notes.extend(["input-changed", INPUT_VARIANT])
        e.explained = e.explained or (
            f"input-changed: an input seen on the {where} side only ({shown}), at a call both "
            "sides make with the same outcome kinds")
        e.review = (f"{INPUT_CHANGED}: an input reached this call on the {where} side only "
                    f"({shown}), while the call itself happens on both sides with the same "
                    f"outcome kinds; {_REPLICATE_HINT}")
        count += 1
    return count


def input_leaf_differences(a: Any, b: Any) -> set[str]:
    """Where two call inputs differ, as ``<argument>/<path>`` of their leaves (an unnamed
    positional value by its position): a leaf whose value differs, or that one input lacks.
    Type-only leaves compare by type name."""
    def leaves(inputs: Any) -> dict[str, str]:
        out: dict[str, str] = {}
        for i, (name, value) in enumerate(_argument_values(inputs)):
            arg = name if name is not None else f"#{i}"
            for path, _kind, leaf in _argument_leaves(value):
                out["/".join([arg, *map(str, path)])] = _canon(leaf)
        return out
    la, lb = leaves(a), leaves(b)
    return {k for k in la.keys() | lb.keys() if la.get(k) != lb.get(k)}


_TYPE_ONLY_TAGS = ("__opaque__", "__cycle__", "__shape__", "__type__", "__nondeterministic__")


def _argument_values(inputs: Any) -> list[tuple[str | None, Any]]:
    """A call's arguments whatever their binding, as ``(name, value)``: a bound parameter and
    a keyword argument by their name (one name whether bound or passed through ``**kwargs``),
    positional varargs unnamed. Wrapper parameters are dropped (a private name carrying a
    callable or a sentinel, as ``_argmap__wrapper``)."""
    if not isinstance(inputs, dict):
        return [(None, inputs)]
    out: list[tuple[str | None, Any]] = []
    bound = inputs.get("bound")
    if isinstance(bound, dict):
        for k, v in bound.items():
            if str(k).startswith("_") and (
                    (isinstance(v, dict) and "__callable__" in v) or _is_sentinel(v)):
                continue
            out.append((str(k), v))
    varargs = inputs.get("varargs")
    if isinstance(varargs, dict) and isinstance(varargs.get("__tuple__"), list):
        out.extend((None, x) for x in varargs["__tuple__"])
    elif isinstance(varargs, list):
        out.extend((None, x) for x in varargs)
    kwargs = inputs.get("kwargs")
    if isinstance(kwargs, dict) and isinstance(kwargs.get("__dict__"), list):
        out.extend((p[0] if isinstance(p[0], str) else None, p[1]) for p in kwargs["__dict__"]
                   if isinstance(p, list | tuple) and len(p) == 2)
    for k, v in inputs.items():
        if k not in ("bound", "varargs", "kwargs", "defaulted", "__c_call__"):
            out.append((None, v))
    return out


def _argument_leaves(value: Any, path: tuple = ()) -> Iterator[tuple[tuple, str, Any]]:
    """(path below the argument, ``value``|``type``, leaf): container contents flattened with
    their structure kept; a type-only rendering (opaque, a cycle, a value-less structure, a
    ``__type__``) is one ``type`` leaf, its type name."""
    if isinstance(value, dict):
        for tag in _TYPE_ONLY_TAGS:
            if tag in value:
                yield path, "type", value[tag]
                return
        if "__structure__" in value and "values" not in value:
            yield path, "type", value["__structure__"]
            return
        if "__object__" in value:
            yield (*path, "__object__"), "type", value["__object__"]
            if isinstance(value.get("attrs"), dict):
                for k, x in sorted(value["attrs"].items(), key=lambda kv: str(kv[0])):
                    yield from _argument_leaves(x, (*path, str(k)))
            return
        if "__dict__" in value and isinstance(value["__dict__"], list):
            for p in value["__dict__"]:
                if isinstance(p, list | tuple) and len(p) == 2:
                    yield from _argument_leaves(p[1], (*path, _canon(p[0])))
            return
        for k in sorted(value, key=str):
            yield from _argument_leaves(value[k], (*path, str(k)))
    elif isinstance(value, list):
        for i, x in enumerate(value):
            yield from _argument_leaves(x, (*path, i))
    else:
        yield path, "value", value


def _unexplained_input_leaves(o: _V, n: _V, stats: "_GroupStats",
                              ) -> tuple[list[str] | None, bool]:
    """Whether the pair's input VALUES differ, ignoring how they are bound.

    A parameter named on both sides (bound, or a ``**kwargs`` key) compares by its name: its
    value leaves against the other side's under the same name, so two swapped arguments
    (``f(timeout=5, retries=3)`` -> ``f(timeout=3, retries=5)``) differ. Parameters named on
    one side only and unnamed positional values compare as one multiset of value leaves (a
    renamed or re-bound parameter keeps its value); wrapper parameters are dropped. Type-only
    leaves compare by type name, and a type-name difference confined to them is drift, never
    an input change. Returns ``(None, drift)`` when nothing differs (only the binding or a
    type name moved); otherwise the leftover leaves that are not volatile on their side
    (masked, a seed, a propagated value): empty when the run explains every difference.
    Residual: a parameter dropped and another added with the same value (``f(b=1)`` ->
    ``f(d=1)``) reads as a rename."""
    sides = []
    for side, v in ((0, o), (1, n)):
        canon = _argument_values(v.fact.input)
        raw = _argument_values(_raw_of(v.fact).input)
        if len(raw) != len(canon):
            raw = canon
        items = []
        for (name, c), (_rname, r) in zip(canon, raw, strict=True):
            raw_leaves = list(_argument_leaves(r))
            canon_leaves = list(_argument_leaves(c))
            if len(raw_leaves) != len(canon_leaves):
                raw_leaves = canon_leaves
            items.extend((name, cp, kind, cl, rl) for (cp, kind, cl), (_rp, _rk, rl)
                         in zip(canon_leaves, raw_leaves, strict=True))
        sides.append(items)
    named = [{name for name, *_ in items if name is not None} for items in sides]
    common = named[0] & named[1]
    types = [sorted(_type_key(str(leaf)) or ("", str(leaf)) for _n, _p, kind, leaf, _r in items
                    if kind == "type") for items in sides]
    drift = types[0] != types[1] or [
        str(leaf) for _n, _p, k, leaf, _r in sides[0] if k == "type"] != [
        str(leaf) for _n, _p, k, leaf, _r in sides[1] if k == "type"]

    def key(name: str | None, p: tuple, leaf: Any) -> tuple:
        return (name if name in common else None, repr(p), _canon(leaf))

    counts = [Counter(key(name, p, leaf) for name, p, kind, leaf, _r in items if kind == "value")
              for items in sides]
    if counts[0] == counts[1]:
        return None, drift or _canon(o.fact.input) != _canon(n.fact.input)

    def volatile(side: int, leaf: Any, raw: Any) -> bool:
        if _canon(leaf) != _canon(raw):
            return True
        if isinstance(raw, str) and (raw in stats.seeds[side] or any(
                any(raw.startswith(tok, m.start())
                    for tok in stats.seed_index[side].get(raw[m.start():m.start() + 8], ()))
                for m in _ALNUM.finditer(raw))):
            return True
        return _canon(raw) in stats.whole[side]

    out: list[str] = []
    for side, label in ((0, "old"), (1, "new")):
        extra = counts[side] - counts[1 - side]
        for name, p, kind, leaf, raw in sides[side]:
            k = key(name, p, leaf)
            if kind != "value" or extra.get(k, 0) <= 0:
                continue
            extra[k] -= 1
            if volatile(side, leaf, raw):
                continue
            where = ".".join(str(x) for x in ((name,) if name in common else ()) + p) \
                or "<argument>"
            out.append(f"{label} {where}={json.dumps(raw)[:30]}")
    return out, drift


def _kinds_match(a: list[Outcome] | None, b: list[Outcome] | None) -> bool:
    """The same outcome kinds: returns on both sides or neither, and the same raise types,
    where an untyped raise (setprofile records C raises as ``<unknown>``) matches any."""
    ka, kb = _kinds(a), _kinds(b)
    if ("return" in ka) != ("return" in kb):
        return False
    ra = [k[len("raise:"):] for k in ka if k.startswith("raise:")]
    rb = [k[len("raise:"):] for k in kb if k.startswith("raise:")]
    if not ra and not rb:
        return True
    if not ra or not rb:
        return False
    return _cover(ra, rb, _raises_match)


# ── dict key order (value format ``dict_order``) ─────────────────────────────────────────

ORDER_CHANGED = "order changed"
INPUT_ORDER_CHANGED = "input order changed"
ORDER_REVIEWS = (ORDER_CHANGED, INPUT_ORDER_CHANGED)
ORDER_PROOF_KEYS = 5   # a cited cause proves determinism from this many keys (1/5! < 1/100)
_ORDER_HINT = ("a dict built from a set of str follows the hash seed; capture a baseline "
               "replicate (random hash seeds) to settle")


@dataclass(frozen=True)
class OrderCause:
    """A known cause of a dict key order change (from knowledge, e.g. Python 3.9 putting a
    TypedDict's base fields first): the key set and the exact old and new key sequences it
    predicts. An order finding is cited only on an exact match of all three."""

    keys: frozenset[str]
    old: tuple[str, ...]
    new: tuple[str, ...]
    cause: str      # the knowledge id
    detail: str     # human text


def _order_proved(e: FactEntry) -> bool:
    """An order failure whose determinism ``_judge_order`` showed (a cited knowledge cause of
    5+ keys, the same pinned hash seed): later review gates must not soften it."""
    return any(n == "order-cause-cited" or n.startswith("order-controlled") for n in e.notes)


def _order_status(old: FactSet, new: FactSet) -> str:
    missing = [side for side, fs in (("old", old), ("new", new)) if not fs.dict_order]
    return "compared" if not missing else f"not recorded ({', '.join(missing)})"


def _python_minor(versions: list[str]) -> str | None:
    minors = {".".join(v.split(".")[:2]) for v in versions if v}
    return minors.pop() if len(minors) == 1 else None


def order_controlled(old: FactSet, new: FactSet) -> bool:
    """Hash order is the same on both sides: the same pinned PYTHONHASHSEED and hash algorithm
    in every process, and the same Python X.Y (never across a Python move: str hashing and set
    iteration changed between minors)."""
    a, b = _env.hash_pinned(old.environment), _env.hash_pinned(new.environment)
    if a is None or a != b:
        return False
    pa, pb = _python_minor(old.python_versions), _python_minor(new.python_versions)
    return pa is not None and pa == pb


def _outcome_pairs(of: Fact, nf: Fact) -> list[tuple[Outcome, Outcome]]:
    """Returned outcomes matched one-to-one: the single outcome of each side, else by equal
    (masked) value."""
    if len(of.outcomes) == 1 and len(nf.outcomes) == 1:
        a, b = of.outcomes[0], nf.outcomes[0]
        return [(a, b)] if a.kind == b.kind == "return" else []
    by_new = {_canon(x.value): x for x in nf.outcomes if x.kind == "return"}
    return [(x, by_new[_canon(x.value)]) for x in of.outcomes
            if x.kind == "return" and _canon(x.value) in by_new]


def _order_explainable(of: Fact, nf: Fact) -> int | None:
    """The length of an output that is a byte (or character) permutation of the other side's,
    when that is the pair's whole output difference; else None."""
    for a_f, b_f in ((of, nf), (_raw_of(of), _raw_of(nf))):
        oa = {_canon([x.kind, x.value]): x for x in a_f.outcomes}
        ob = {_canon([x.kind, x.value]): x for x in b_f.outcomes}
        only_a = [x for k, x in oa.items() if k not in ob]
        only_b = [x for k, x in ob.items() if k not in oa]
        if len(only_a) == 1 and len(only_b) == 1 and only_a[0].kind == only_b[0].kind == "return":
            n = _order.is_order_explainable(only_a[0].value, only_b[0].value)
            if n is not None:
                return n
    return None


def _cite(diffs: list, causes: Sequence[OrderCause]) -> tuple[OrderCause, tuple] | None:
    for d in diffs:
        _path, old_seq, new_seq = d
        old_l = tuple(_order.key_label(k) for k in old_seq)
        new_l = tuple(_order.key_label(k) for k in new_seq)
        for c in causes:
            if frozenset(old_l) == c.keys and old_l == tuple(c.old) and new_l == tuple(c.new):
                return c, d
    return None


def _order_block(side: str, d: tuple, value: Any, cause: OrderCause | None) -> dict[str, Any]:
    out = {"side": side, **_order.describe(d[0], d[1], d[2], value)}
    if cause is not None:
        out["cause"] = f"{cause.detail} ({cause.cause})"
    return out


def _order_text(d: tuple, value: Any) -> str:
    return next(_order.iter_diff_labels([d], value))


def _judge_order(pending: list[tuple[_V, _V, FactEntry]], old: FactSet, new: FactSet,
                 prof: DiffProfile, causes: Sequence[OrderCause] = ()) -> Counter:
    """Dict key order, compared like a value where it is recorded on both sides (both captures
    declare the ``dict_order`` value format), on pairs compared at exact or masked level, at
    dict positions present on both sides with the same masked key set (k >= 2). Never at type
    or kind level and never in volatile-site groups (they are not pairs). The hash seed is a
    hidden input of every hash-ordered dict, so an unmeasured order difference is a review,
    never a failure, unless determinism is shown: a pinned identical hash seed (same Python
    X.Y), a replicate (``replicate.py``: ``equal@order``) or a knowledge cause that matches
    the key set and both sequences exactly (``OrderCause``, from 5 keys on).

    a. output order only: review ``order changed``;
    b. input order only, output the same: EQUAL, explained;
    c. input order only, the output's dicts reordered as the input's: EQUAL at MASKED;
    d. input order only, the output differs: a review ``input order changed`` when the output
       is a permutation of the other side's bytes or characters (the only difference an order
       can explain), else the strict failure stays (its text never says "same complete
       input"); a kind or type change fails as before;
    e. the input differs in content too: the existing rules decide, order is detail only.
    Order that varied within one run (one side's fact saw two orders) is not compared.

    Stated residual (case d): "order-explainable" looks at the output only, not at whether the
    reordered dict can reach it. A call whose input dict was reordered by hash order AND whose
    output changed into a permutation of itself for another reason (``"abcdef"`` ->
    ``"fedcba"``) is a review ``input order changed``: INCONCLUSIVE, never PASS, but not the
    FAIL it deserves until a replicate (``equal@order``) or a knowledge cause settles it."""
    stats: Counter = Counter()
    if prof is DiffProfile.SHAPE or not (old.dict_order and new.dict_order):
        return stats
    controlled = order_controlled(old, new)
    for o, n, e in pending:
        if e.level not in (ComparisonLevel.EXACT, ComparisonLevel.MASKED) or e.noise \
                or e.unverified or e.comparison not in (Comparison.EQUAL, Comparison.CHANGED):
            continue
        of, nf = o.fact, n.fact
        cin = _order.compare(of.input, of.input_order, nf.input, nf.input_order)
        cout = _order.Compared()
        out_value: dict[str, Any] = {}     # an output difference's path -> the old value
        for a, b in _outcome_pairs(of, nf):
            c = _order.compare(a.value, a.order, b.value, b.order)
            for d in c.diffs:
                out_value.setdefault(d[0], a.value)
            cout.extend(c)
        same_input = o.input_key == n.input_key and "matched-modulo-masked-input" not in e.notes
        if cin.varies or cout.varies:
            e.notes.append("order-varies-in-run")
            stats["varies"] += 1
        if cin.collisions or cout.collisions:
            e.notes.append("order-not-comparable: masked keys collide")
        if cin.agreed or cout.agreed:
            e.order_agreed = cin.agreed + cout.agreed
        if e.comparison is Comparison.EQUAL:
            if cout.diffs:
                follows = same_input and bool(cin.diffs) and all(
                    any(d[1] == x[1] and d[2] == x[2] for x in cin.diffs) for d in cout.diffs)
                if follows:                                                  # c
                    e.level = ComparisonLevel.MASKED
                    e.notes.append("order-follows-input")
                    e.explained = (f"order: the output's key order follows the input's "
                                   f"({_order_text(cin.diffs[0], of.input)})")
                    stats["follows"] += 1
                    continue
                d = cout.diffs[0]                                            # a
                cited = _cite(cout.diffs, causes)
                if cited is not None:
                    d = cited[1]
                value = out_value.get(d[0])
                e.order = _order_block("output", d, value, cited[0] if cited else None)
                e.order_keysets = sorted({_order.keyset_id(x[1]) for x in cout.diffs})
                if e.review:          # already a review for another reason: order is detail
                    e.notes.append("order-changed-detail")
                    stats["detail"] += 1
                    continue
                if e.category is not FactCategory.REWRITTEN:
                    e.category = FactCategory.CHANGED
                e.comparison = Comparison.CHANGED
                e.notes.append("order-changed")
                more = f" (+{len(cout.diffs) - 1} more)" if len(cout.diffs) > 1 else ""
                e.explained = f"order: same keys and values, key order {_order_text(d, value)}" \
                    + more + (f"; cause: {e.order['cause']}" if cited else "")
                if cited and len(d[1]) >= ORDER_PROOF_KEYS:
                    e.notes.append("order-cause-cited")
                    stats["cited"] += 1
                elif controlled:
                    e.notes.append("order-controlled: the same pinned hash seed on both sides")
                else:
                    e.review = (f"{ORDER_CHANGED}: same keys and values, key order "
                                f"{_order_text(d, value)}{more}; {_ORDER_HINT}")
                stats["output"] += 1
            elif cin.diffs:                                                  # b
                e.notes.append("input-order-differs")
                if e.explained is None:
                    e.explained = (f"order: input key order differs "
                                   f"{_order_text(cin.diffs[0], of.input)}; the output is the "
                                   "same")
                stats["input-neutral"] += 1
            elif cin.varies or cout.varies:
                if e.explained is None:
                    e.explained = "order: varies within one run"
            continue
        # CHANGED: the content differs
        if not cin.diffs:
            continue
        d = cin.diffs[0]
        unedited = e.provenance in (None, "unedited") and not (o.rewritten_site
                                                              or n.rewritten_site)
        kinds_ok = _kinds_match(e.old, e.new) and _cover(
            _distinct_types(e.old or []), _distinct_types(e.new or []), _outcome_types_match)
        cited = _cite(cin.diffs, causes) if same_input else None
        if cited is not None:
            d = cited[1]
        e.order = _order_block("input", d, of.input, cited[0] if cited else None)
        e.notes.append("input-order-differs")
        if not (same_input and unedited and kinds_ok) or e.review \
                or e.category is not FactCategory.CHANGED:
            stats["detail"] += 1                                             # e, or strict
            continue
        length = _order_explainable(of, nf)
        if length is None:                                                   # d, strict
            e.order["output"] = "differs beyond a reordering"
            stats["strict"] += 1
            continue
        e.order["output"] = f"a byte-permutation (len {length})"
        e.order_keysets = sorted({_order.keyset_id(x[1]) for x in cin.diffs})
        e.notes.append("input-order-changed")
        more = f" (+{len(cin.diffs) - 1} more)" if len(cin.diffs) > 1 else ""
        e.explained = (f"order: the input's dict key order changed (same keys and values) "
                       f"{_order_text(d, of.input)}{more}; the output is a byte-permutation "
                       f"(len {length})" + (f"; cause: {e.order['cause']}" if cited else ""))
        stats["input"] += 1
        if cited and len(d[1]) >= ORDER_PROOF_KEYS:
            e.notes.append("order-cause-cited")
            stats["cited"] += 1
        elif controlled:
            e.notes.append("order-controlled: the same pinned hash seed on both sides")
        else:
            e.review = (f"{INPUT_ORDER_CHANGED}: the dict key order reaching this call changed "
                        f"(same keys and values) {_order_text(d, of.input)}{more}; the output "
                        f"is a byte-permutation (len {length}); {_ORDER_HINT}")
    return stats


# ── runtime environment (``environment`` records) ───────────────────────────────────────

ENVIRONMENT_DIFFERS = "environment differs"
_READING_ASPECT = {"cpus": "cpus", "uid": "privilege", "euid": "privilege", "system": "os",
                   "machine": "machine", "release": "release", "rlimit": "rlimit"}


def _protected_symbols(context: DiffContext | None) -> set[str]:
    """Symbols the migration's own knowledge predicts a change at (a plan's delta facts and
    successors, its predicted risks): an environment difference never softens them."""
    if context is None:
        return set()
    out = set(context.predicted_symbols or ())
    for old, new in (context.successors or {}).items():
        out.update((old, new))
    return out


def _env_unedited(e: FactEntry) -> bool:
    return e.provenance in (None, "unedited") and not e.unverified


def _explain_env_readings(pending: list[tuple[_V, _V, FactEntry]], envcmp: Any,
                          protected: set[str]) -> int:
    """E2: a value at a curated reader that is each side's OWN recorded reading of an aspect
    that differs (``os.cpu_count()`` 8 vs 12 with cpus 8 vs 12) is EQUAL at masked level,
    explained. A value matching neither side's reading stays as it is."""
    if envcmp is None or envcmp.same:
        return 0
    count = 0
    for o, n, e in pending:
        if e.comparison is not Comparison.CHANGED or e.category is not FactCategory.CHANGED \
                or e.review or e.noise or not _env_unedited(e) or o.rewritten_site \
                or n.rewritten_site or e.symbol in protected or (e.new_symbol or "") in protected:
            continue
        kind = _env.reading_kind(e.symbol)
        aspect = _READING_ASPECT.get(kind or "")
        if aspect is None or not envcmp.differ(aspect):
            continue
        of, nf = o.fact, n.fact
        if len(of.outcomes) != 1 or len(nf.outcomes) != 1 \
                or of.outcomes[0].kind != "return" or nf.outcomes[0].kind != "return":
            continue
        if not (_env.is_own_reading(e.symbol, of.input, of.outcomes[0].value,
                                    envcmp.old_envs)
                and _env.is_own_reading(e.symbol, nf.input, nf.outcomes[0].value,
                                        envcmp.new_envs)):
            continue
        e.category, e.comparison, e.level = (FactCategory.EQUAL, Comparison.EQUAL,
                                             ComparisonLevel.MASKED)
        shown = envcmp.describe(envcmp.matching([aspect]))
        e.explained = f"environment: the value is this side's own {aspect} ({shown})"
        e.notes.append("environment-reading")
        count += 1
    return count


def _judge_platform(pending: list[tuple[_V, _V, FactEntry]], envcmp: Any,
                    protected: set[str]) -> tuple[int, int]:
    """A pair equal only once each side's OS name, kernel release or machine was masked
    (``platform`` masks, made only while those aspects differ) is no proof of equal behaviour:
    a call that returns the platform it ran on looks the same as one whose result changed to
    another platform's. It stays EQUAL only at a curated reader whose recorded values are each
    side's own reading (E2); elsewhere it is a review "environment differs" (a strict failure
    at a symbol the migration's knowledge names). Returns (explained, reviews)."""
    if envcmp is None or envcmp.same:
        return 0, 0
    explained = reviews = 0
    for o, n, e in pending:
        of, nf = o.fact, n.fact
        if "platform" not in (_masks_of(of) | _masks_of(nf)) or e.review or e.noise \
                or e.comparison is not Comparison.EQUAL:
            continue
        ro, rn = _raw_of(of), _raw_of(nf)
        if compare_outcomes(ro.outcomes, rn.outcomes).comparison is Comparison.EQUAL:
            continue  # only the input was masked: the outputs are equal as recorded
        aspects = envcmp.matching(["os", "release", "machine"])
        kind = _env.reading_kind(e.symbol)
        if kind in ("system", "machine", "release") and len(ro.outcomes) == 1 \
                and len(rn.outcomes) == 1 \
                and _env.is_own_reading(e.symbol, ro.input, ro.outcomes[0].value,
                                        envcmp.old_envs) \
                and _env.is_own_reading(e.symbol, rn.input, rn.outcomes[0].value,
                                        envcmp.new_envs):
            aspect = _READING_ASPECT[kind]
            e.level = ComparisonLevel.MASKED
            e.explained = (f"environment: the value is this side's own {aspect} "
                           f"({envcmp.describe(envcmp.matching([aspect]))})")
            e.notes.append("environment-reading")
            explained += 1
            continue
        if e.category in (FactCategory.EQUAL, FactCategory.DRIFT):
            e.category = FactCategory.CHANGED
        e.comparison = Comparison.CHANGED
        e.notes.append("platform-masked")
        e.explained = ("platform: equal only once each side's own "
                       f"{'/'.join(a for a in aspects)} was masked")
        if e.symbol in protected or (e.new_symbol or "") in protected \
                or not _env_unedited(e):
            continue  # strict: the migration's knowledge names it, or it is an edited site
        e.review = (f"{ENVIRONMENT_DIFFERS}: {envcmp.describe(aspects)}; the value differs only "
                    f"by each side's own {', '.join(aspects)}; capture both sides in the same "
                    "environment to settle")
        reviews += 1
    return explained, reviews


def _type_names(t: Any) -> set[str]:
    """Every type name a type tree holds (``object``/``opaque``/``type`` labels)."""
    out: set[str] = set()
    if isinstance(t, dict):
        for k, v in t.items():
            if isinstance(v, str) and k in ("object", "opaque", "type", "return", "raise",
                                            "structure", "dataclass"):
                out.add(v)
            else:
                out |= _type_names(v)
    elif isinstance(t, list):
        for x in t:
            out |= _type_names(x)
    return out


def _platform_tagged(e: FactEntry) -> bool:
    """The outcome types differ only by a platform-tagged name (``psutil._psosx.pmem`` vs
    ``psutil._pslinux.pmem``), one-sided attributes of such objects included."""
    old_t, new_t = _distinct_types(e.old or []), _distinct_types(e.new or [])
    a = {_env.platform_neutral(x) for x in _type_names(old_t)}
    b = {_env.platform_neutral(x) for x in _type_names(new_t)}
    raw_a, raw_b = _type_names(old_t), _type_names(new_t)
    return raw_a != raw_b and a == b and any("⟨os⟩" in x for x in a)


def _raise_types(outcomes: list[Outcome] | None) -> set[str]:
    return {_raise_name(o.value) for o in outcomes or () if o.kind == "raise"}


def _env_change_class(e: FactEntry) -> str | None:
    """The change classes an environment can cause (B's restriction): a presence change; a
    kind change whose raise types are all of the environment family (``<unknown>``, OSError
    and its kin, ValueError, NotImplementedError, AttributeError, ImportError); a
    platform-tagged type-name difference; a value change at equal types. None otherwise:
    TypeError, KeyError and other type changes are never softened."""
    if e.category in (FactCategory.APPEARED, FactCategory.DISAPPEARED):
        return "presence"
    if e.comparison is not Comparison.CHANGED:
        return None
    if not _kinds_match(e.old, e.new):
        raises = _raise_types(e.old) | _raise_types(e.new)
        return "kind" if raises and all(_env.env_raise(r) for r in raises) else None
    if _platform_tagged(e):
        return "type"
    if not _cover(_distinct_types(e.old or []), _distinct_types(e.new or []),
                  _outcome_types_match):
        return None
    return "value"


def _evidence_aspects(e: FactEntry, envcmp: Any) -> set[str]:
    """Aspects an entry shows it reads: a platform-tagged type name reads ``os``; a
    PermissionError raised only on the less privileged side reads ``privilege``."""
    out: set[str] = set()
    if _platform_tagged(e):
        out.add("os")
    po = "PermissionError" in _raise_types(e.old)
    pn = "PermissionError" in _raise_types(e.new)
    if po != pn and envcmp.differ("privilege"):
        new_less = _env.less_privileged(envcmp.new_envs, envcmp.old_envs)
        old_less = _env.less_privileged(envcmp.old_envs, envcmp.new_envs)
        if (pn and new_less) or (po and old_less):
            out.add("privilege")
    return out


def _judge_environment(entries: list[FactEntry], envcmp: Any, protected: set[str]) -> int:
    """E3/E4: a failing difference at an unedited call that reads an aspect of the environment
    that differs between the captures (curated reader, or evidence) is a review "environment
    differs", never noise and never a pass: only a same-environment capture settles it. Not
    for edited sites, for symbols the migration's knowledge names (``_protected_symbols``),
    for aspects that do not differ, for an unknown or same environment, or for a change class
    no environment causes (``_env_change_class``)."""
    if envcmp is None or envcmp.same:
        return 0
    count = 0
    for e in entries:
        if not e.fails or not _env_unedited(e):
            continue
        if e.symbol in protected or (e.new_symbol or "") in protected:
            continue
        aspects = _env.reader_aspects(e.symbol, e.input)
        if e.new_symbol:
            aspects |= _env.reader_aspects(e.new_symbol, e.new_input or e.input)
        aspects |= _evidence_aspects(e, envcmp)
        differing = envcmp.matching(sorted(aspects))
        if not differing or _env_change_class(e) is None:
            continue
        e.review = (f"{ENVIRONMENT_DIFFERS}: {envcmp.describe(differing)}; {e.symbol} reads "
                    "it; capture both sides in the same environment to settle")
        e.notes.append("environment-differs: " + ", ".join(differing))
        count += 1
    return count


def _env_inference(old: FactSet, new: FactSet) -> str | None:
    """What the call-site paths already say about where an unrecorded capture ran
    (informational only: it changes no verdict)."""
    o, n = bool(old.paths_mapped), bool(new.paths_mapped)
    if o == n:
        return None
    where = {True: "in a container", False: "on the host"}
    return (f"inferred from call-site paths: the baseline ran {where[o]}, the post-migration "
            f"capture {where[n]}")


def _review_masked_input(entries: list[FactEntry]) -> None:
    """A value difference at an unedited pair whose inputs were equal only once volatile values
    (source, clock) were masked is not a controlled comparison: the output may derive from the
    masked values (a timestamp packed into bytes, a session id hashed into a total). A review
    item, whatever the gate; outcome-kind and type changes stay failures."""
    for e in entries:
        if _order_proved(e):
            continue
        if ("matched-modulo-masked-input" not in e.notes or e.category is not FactCategory.CHANGED
                or e.comparison is not Comparison.CHANGED or e.review or e.noise
                or e.unverified or e.provenance not in (None, "unedited")
                or not _kinds_match(e.old, e.new)
                or not _cover(_distinct_types(e.old or []), _distinct_types(e.new or []),
                              _outcome_types_match)):
            continue
        e.review = _MASKED_INPUT_REVIEW
        if not (e.explained or "").startswith("masked-input"):
            e.explained = "masked-input: the inputs matched only once volatile values were masked"


_TRACER_SITE = "/pymolt_trace/"


def _qualname_equivalent(a: str, b: str) -> bool:
    """``pkg.mod.leaf`` (a closure named by ``co_name``, before 3.11) and
    ``pkg.mod.<qualname>.<locals>.leaf`` (by ``co_qualname``): the same function."""
    if a == b:
        return False
    short, long_ = sorted((a, b), key=len)
    if ".<locals>." not in long_ or "." not in short:
        return False
    ps, ls = short.rsplit(".", 1)
    pl, ll = long_.rsplit(".", 1)
    return ls == ll and (pl == ps or pl.startswith(ps + "."))


_DEF_LINE = re.compile(r"^\s*(?:@|def\s|async\s+def\s|class\s)")
_DECORATOR_SPAN = 10  # lines a decorator may sit above its def when no source says so


def _same_decorated_statement(o: _V, n: _V, prov: _Provenance) -> bool:
    """Both calls sit on definition lines (``def``/``class``/``@``) of ONE decorated statement:
    from the source snapshots when there are any (the def's decorator lines, in new-side
    numbering), else the new line is the old one or at most 10 lines above it (3.12 moves a
    decorator's call from the ``def`` line up to its ``@`` line)."""
    of, nf = o.fact, n.fact
    if not (of.text and nf.text and _DEF_LINE.match(of.text) and _DEF_LINE.match(nf.text)):
        return False
    if of.line is None or nf.line is None:
        return False
    if of.file in prov.maps:
        decorators = prov._decorator_lines(1, of.file)
        old_line = prov.mapped_line(of.file, of.line, 0)

        def stmt(line: int | None) -> int | None:
            if line is None:
                return None
            for def_line, decos in decorators.items():
                if line == def_line or line in decos:
                    return def_line
            return None

        a, b = stmt(old_line), stmt(nf.line)
        return a is not None and a == b
    return 0 <= of.line - nf.line <= _DECORATOR_SPAN


def _pair_qualname_equivalent(
    old_left: list[_V], new_left: list[_V], prov: _Provenance,
) -> tuple[list[tuple[_V, _V, bool]], list[_V], list[_V]]:
    """``(old, new, stacked)`` pairs: one-to-one, or (``stacked``) a side's several views of one
    decorated statement merged into one."""
    go: dict[tuple, list[_V]] = defaultdict(list)
    gn: dict[tuple, list[_V]] = defaultdict(list)
    for side, views in ((go, old_left), (gn, new_left)):
        for v in views:
            side[(v.fact.file, v.fact.func, v.core_key,
                  v.fact.symbol.rsplit(".", 1)[-1])].append(v)
    pairs: list[tuple[_V, _V, bool]] = []
    paired: set[int] = set()
    for key, olds in go.items():
        news = gn.get(key)
        if not news:
            continue
        if len(olds) == 1 and len(news) == 1:
            o, n = olds[0], news[0]
            if _qualname_equivalent(o.fact.symbol, n.fact.symbol) \
                    and _same_decorated_statement(o, n, prov):
                pairs.append((o, n, False))
                paired.update((id(o), id(n)))
            continue
        if _stacked_decorators(olds, news, prov):
            pairs.append((_merged_view(olds), _merged_view(news), True))
            paired.update(id(v) for v in (*olds, *news))
    return (pairs, [v for v in old_left if id(v) not in paired],
            [v for v in new_left if id(v) not in paired])


def _stacked_decorators(olds: list[_V], news: list[_V], prov: _Provenance) -> bool:
    """The same input given to one decorator callable several times on one decorated
    statement (``@app.route("/a")`` over ``@app.route("/b")``): 3.8-3.10 attribute every
    application to the ``def`` line, so one fact counts them all, while 3.11+ attribute each
    to its own ``@`` line. One callable per side (equal names, or a closure named both ways),
    every view on a definition line of the same statement, and as many calls on each side."""
    names_old = {v.fact.symbol for v in olds}
    names_new = {v.fact.symbol for v in news}
    if len(names_old) != 1 or len(names_new) != 1:
        return False
    a, b = next(iter(names_old)), next(iter(names_new))
    if a != b and not _qualname_equivalent(a, b):
        return False
    if sum(v.fact.count for v in olds) != sum(v.fact.count for v in news):
        return False
    return all(_same_decorated_statement(o, n, prov) for o in olds for n in news)


def _merged_view(views: list[_V]) -> _V:
    """One view of several of a side's views (outcome counts add); the first-seen view gives
    the site and the keys."""
    if len(views) == 1:
        return views[0]
    head = min(views, key=lambda v: v.first_seen)
    return _V(fact=_merge_facts([v.fact for v in views]), site_value=head.site_value,
              input_key=head.input_key, core_key=head.core_key,
              rewritten_site=any(v.rewritten_site for v in views), stmt_key=head.stmt_key)


_NARROW_C_BACKENDS = frozenset({"setprofile", "hybrid"})


def _c_call_visibility(fs: FactSet) -> str | None:
    """``narrow``: setprofile sees compiled calls of builtin functions only (no method
    descriptors, type calls or Cython methods); ``wide``: sys.monitoring sees every call."""
    if not fs.backends:
        return None
    if all(b in _NARROW_C_BACKENDS for b in fs.backends):
        return "narrow"
    if "monitoring" in fs.backends and not _NARROW_C_BACKENDS.intersection(fs.backends):
        return "wide"
    return None


BACKEND_UNOBSERVABLE = "backend-unobservable"


def _soften_backend(entries: list[FactEntry], old: FactSet, new: FactSet) -> int:
    """A compiled call (``__c_call__``) seen on one side only, when that side's backend sees a
    superset of the other's: the other side could not have observed it, so its absence there is
    no evidence and the comparison is impossible, not failed. Returning only: DRIFT
    (``backend``). Raising: UNCOMPARABLE (inconclusive), since a raise the other side could not
    record is neither a change nor noise. Never FAIL. The reverse direction stays strict (the
    wider backend would have seen it). Returns how many entries this explained."""
    o_vis, n_vis = _c_call_visibility(old), _c_call_visibility(new)
    if {o_vis, n_vis} != {"narrow", "wide"}:
        return 0
    wide_side = FactCategory.APPEARED if n_vis == "wide" else FactCategory.DISAPPEARED
    count = 0
    wide = _families(new if n_vis == "wide" else old)
    for e in entries:
        if e.category is not wide_side or not isinstance(e.input, dict) \
                or e.input.get("__c_call__") is not True:
            continue
        e.review, e.unverified = None, False
        if _returns_only(e.new if wide_side is FactCategory.APPEARED else e.old):
            e.category = FactCategory.DRIFT
            e.notes.append("noise: compiled call observable by one backend only")
            e.explained = f"backend: only {wide} observes this compiled call"
        else:
            e.category = FactCategory.UNCOMPARABLE
            e.notes.append(BACKEND_UNOBSERVABLE)
            e.explained = (f"backend: compiled call observable on one backend only ({wide}), and "
                           "it raises here: the other side could not record it")
        count += 1
    return count


def _only_missing(present: list[Outcome] | None, lacking: list[Outcome] | None) -> bool:
    """Every outcome type of the ``lacking`` side also occurs on the ``present`` side: the
    difference is outcomes missing on one side, nothing new there."""
    xs, ys = _distinct_types(present or []), _distinct_types(lacking or [])
    return all(any(_outcome_types_match(y, x) for x in xs) for y in ys)


def _soften_dropped(entries: list[FactEntry], old: FactSet, new: FactSet) -> None:
    """The mirror of ``_soften_evicted`` for a runtime that dropped events: a fact missing on a
    side that dropped events (the counter is global, so any fact there may be the lost one),
    or a volatile group whose only difference is outcomes missing on that side, is UNCOMPARABLE
    (``possibly-dropped:<side>``): inconclusive, never a pass, never a failure."""
    o_drop, n_drop = bool(old.dropped_events), bool(new.dropped_events)
    if not (o_drop or n_drop):
        return
    for e in entries:
        if "absent-from-tallies" in e.notes or "site-fully-sampled" in e.notes:
            continue  # complete tallies prove the call is gone: no drop explains it
        side = None
        if e.category is FactCategory.DISAPPEARED and n_drop:
            side = "new"
        elif e.category is FactCategory.APPEARED and o_drop:
            side = "old"
        elif e.category is FactCategory.CHANGED and "type-changed" in e.notes \
                and (e.explained or "").startswith("volatile-site"):
            if n_drop and _only_missing(e.old, e.new):
                side = "new"
            elif o_drop and _only_missing(e.new, e.old):
                side = "old"
        if side is None:
            continue
        lost = (new if side == "new" else old).dropped_events
        e.category = FactCategory.UNCOMPARABLE
        e.notes.append(f"possibly-dropped:{side}")
        e.explained = (f"possibly-dropped: the {side} capture dropped {lost} event(s); this "
                       "fact may be one of them")


C_SITE_CAPPED = "c-site-capped"
CALL_SHAPE = "call-shape"
# The per-site compiled-call cap of a runtime that predates the ``c_site_cap`` account (it
# recorded the first 2,000 calls of a compiled callable at a site and silently muted the rest).
_LEGACY_C_SITE_MAX = 2000


def _is_c_input(inputs: Any) -> bool:
    return isinstance(inputs, dict) and inputs.get("__c_call__") is True


def _capped_index(fs: FactSet, views: list[_V], modes: dict[str, str]
                  ) -> tuple[dict[tuple, list[CappedSite]], set[tuple]]:
    """Where this side recorded only the first calls of a compiled callable: the (symbol,
    site) keys its processes' accounts list. A one-process capture whose runtime predates the
    account (it muted without counting) is presumed capped at every compiled-call site that
    holds the old cap's worth of calls: for sys.monitoring, which capped a call instruction
    whatever it called, the whole site (a key without the symbol); for setprofile, which
    capped per symbol and line, that symbol there. Not inferred across processes: their
    recorded calls add up without any of them reaching the cap."""
    idx: dict[tuple, list[CappedSite]] = defaultdict(list)
    for c in fs.c_capped:
        idx[_site_key(c.symbol, c.file, c.func, c.line, c.text, modes)].append(c)
    inferred: set[tuple] = set()
    if fs.c_cap_unreported == 1 and not fs.c_cap_reported:
        counts: dict[tuple, int] = defaultdict(int)
        for v in views:
            if _is_c_input(v.fact.input) and not v.fact.binding:
                counts[(v.fact.symbol, v.fact.file, v.fact.func, v.site_value)] += v.fact.count
        whole_site = "monitoring" in fs.backends
        for k, n in counts.items():
            if n >= _LEGACY_C_SITE_MAX:
                if whole_site:
                    inferred.add(k[1:])
                else:
                    idx.setdefault(k, [])
    return dict(idx), inferred


def _is_capped(capped: tuple[dict[tuple, list[CappedSite]], set[tuple]], key: tuple) -> bool:
    return key in capped[0] or key[1:] in capped[1]


def _shape_index(views: list[_V]) -> tuple[dict[tuple, set[bool]], set[tuple]]:
    """Per site key, the call shapes recorded there (True: a compiled call, no inputs; False:
    a call with its inputs), binding failures aside; and the keys holding a binding failure
    (recorded with the compiled-call marker, though its callee is a Python function)."""
    shapes: dict[tuple, set[bool]] = defaultdict(set)
    binding: set[tuple] = set()
    for v in views:
        k = (v.fact.symbol, v.fact.file, v.fact.func, v.site_value)
        if v.fact.binding:
            binding.add(k)
        else:
            shapes[k].add(_is_c_input(v.fact.input))
    return shapes, binding


def _soften_capped(entries: list[FactEntry], old: FactSet, new: FactSet, ov: list[_V],
                   nv: list[_V], modes: dict[str, str]) -> int:
    """A fact missing on one side at a site that side did not record call by call is
    UNCOMPARABLE (inconclusive), never DISAPPEARED / APPEARED:

    * ``c-site-capped:<side>``: that side's runtime recorded only the first calls of the
      compiled callable there and counted the rest (``c_site_cap``), so the fact may be one of
      the calls it counted. A CHANGED entry whose only difference is outcomes missing on the
      capped side is softened the same way.
    * ``call-shape:<side>``: that side recorded the site's calls in the other shape: as
      compiled calls (``__c_call__``: no inputs, result unobserved; sys.monitoring sees a Cython
      function natively) where this side has them with their inputs (``CompiledWrap`` under
      setprofile), or the reverse. The compiled-call fact pairs with one of them; the others
      cannot pair by input, and their absence there is no evidence.

    Complete tallies that prove the call gone win. Returns how many entries this explained."""
    capped = (_capped_index(old, ov, modes), _capped_index(new, nv, modes))
    shapes = (_shape_index(ov), _shape_index(nv))
    count = 0
    for e in entries:
        if "absent-from-tallies" in e.notes or "site-fully-sampled" in e.notes:
            continue
        lacking: int | None = None
        if e.category is FactCategory.DISAPPEARED and e.old_site is not None:
            lacking, key = 1, _entry_key(e, e.old_site, modes)
        elif e.category is FactCategory.APPEARED and e.new_site is not None:
            lacking, key = 0, _entry_key(e, e.new_site, modes, new_side=True)
        elif e.category is FactCategory.CHANGED and e.old_site and e.new_site \
                and e.new_input is None and _is_c_input(e.input):
            # Only the same compiled call (its constant input) whose capped side lacks an
            # outcome the other side has: a pair whose inputs differ is a real change.
            for side, present, short, site in ((1, e.old, e.new, e.new_site),
                                               (0, e.new, e.old, e.old_site)):
                k = _entry_key(e, site, modes, new_side=side == 1)
                if _is_capped(capped[side], k) and _only_missing(present, short):
                    lacking, key = side, k
                    break
        if lacking is None:
            continue
        name, other = ("new", "old") if lacking == 1 else ("old", "new")
        notes: list[str] = []
        why: list[str] = []
        if _is_capped(capped[lacking], key):
            sites = capped[lacking][0].get(key)
            notes.append(f"{C_SITE_CAPPED}:{name}")
            if sites:
                muted = sum(c.muted for c in sites)
                bound = "" if all(c.exact for c in sites) else " at least"
                why.append(f"{C_SITE_CAPPED}: the {name} capture recorded the first "
                           f"{sites[0].recorded} calls of this compiled callable here and only "
                           f"counted the{bound} {muted} after them; this fact may be among those")
            else:
                why.append(f"{C_SITE_CAPPED}: the {name} capture recorded {_LEGACY_C_SITE_MAX} "
                           "or more compiled calls here with a runtime that silently stopped "
                           "recording at that cap; this fact may be among the unrecorded calls")
        if e.category is not FactCategory.CHANGED:
            mine_c = _is_c_input(e.input)
            other_shapes, other_binding = shapes[lacking]
            own_binding = shapes[1 - lacking][1]
            if (not mine_c) in other_shapes.get(key, ()) and not (mine_c and key in own_binding):
                notes.append(f"{CALL_SHAPE}:{name}")
                if mine_c:
                    how = (f"the {name} capture recorded this site's calls with their inputs, "
                           f"the {other} one as compiled calls (no inputs, result unobserved)")
                else:
                    how = (f"the {name} capture recorded this site's calls as compiled calls "
                           f"(no inputs, result unobserved), the {other} one with their inputs")
                why.append(f"{CALL_SHAPE}: {how}; the calls cannot be paired by input")
        if not notes:
            continue
        e.category = FactCategory.UNCOMPARABLE
        e.notes.extend(notes)
        e.explained = "; ".join(why)
        count += 1
    return count


def _signature(f: Fact) -> tuple:
    """The input's structure without its values: bound parameter names, positional arity, and
    keyword names (a compiled call's constant marker counts as its own signature)."""
    return _input_signature(f.input)


def _input_signature(inputs: Any) -> tuple:
    if not isinstance(inputs, dict):
        return ("<value>",)
    if inputs.get("__c_call__") is True:
        return ("<c_call>",)
    bound = inputs.get("bound")
    names = tuple(sorted(bound)) if isinstance(bound, dict) else ()
    kwargs = inputs.get("kwargs")
    keys = tuple(sorted(str(p[0]) for p in _pairs(kwargs.get("__dict__")))) \
        if isinstance(kwargs, dict) else ()
    varargs = inputs.get("varargs")
    arity = len(varargs["__tuple__"]) if isinstance(varargs, dict) and isinstance(
        varargs.get("__tuple__"), list) else 0
    return (names, keys, arity)


def _widened_member(f: Fact) -> Fact:
    return f.model_copy(update={
        "input": dict(_WIDENED), "input_order": None,
        "outcomes": [o if o.kind == "raise" else
                     Outcome(kind="return", value=widen_value(o.value), count=o.count)
                     for o in f.outcomes],
    })


def _is_c_call(v: _V) -> bool:
    return isinstance(v.fact.input, dict) and v.fact.input.get("__c_call__") is True


def _pair_binding(
    old_left: list[_V], new_left: list[_V], key_fn: Callable[[_V], Any],
    flag: Callable[[_V], bool] = lambda v: v.fact.binding,
) -> tuple[list[tuple[_V, _V]], list[_V], list[_V]]:
    """Pair a flagged fact (a binding failure; a ``__c_call__`` event) with the other side's
    *unflagged* call under the same key, in first-seen order, up to the shorter list (both
    directions: a call that now fails to bind, or one that used to)."""
    go: dict[Any, list[_V]] = defaultdict(list)
    gn: dict[Any, list[_V]] = defaultdict(list)
    for v in old_left:
        go[key_fn(v)].append(v)
    for v in new_left:
        gn[key_fn(v)].append(v)
    pairs: list[tuple[_V, _V]] = []
    paired: set[int] = set()
    for key, olds in go.items():
        news = gn.get(key)
        if not news:
            continue
        for old_binding in (False, True):
            os_ = sorted((v for v in olds if flag(v) is old_binding
                          and id(v) not in paired), key=lambda v: v.first_seen)
            ns = sorted((v for v in news if flag(v) is not old_binding
                         and id(v) not in paired), key=lambda v: v.first_seen)
            for o, n in zip(os_, ns):
                pairs.append((o, n))
                paired.update((id(o), id(n)))
    return (pairs, [v for v in old_left if id(v) not in paired],
            [v for v in new_left if id(v) not in paired])


def _pair_entry(o: _V, n: _V, profile: DiffProfile, modes: dict[str, str], *,
                rewritten: str | None, input_changed: bool = False) -> FactEntry:
    """Classify one old/new pair. ``rewritten`` is ``"same-call"`` / ``"positional"`` for a
    REWRITTEN pair (outcomes compared, symbol and input free to differ), else None."""
    of, nf = o.fact, n.fact
    cmp = compare_outcomes(of.outcomes, nf.outcomes, profile)
    explained = None
    if cmp.comparison is not Comparison.EQUAL and (_masks_of(of) or _masks_of(nf)):
        raw = _raw_equal(of, nf, profile)  # raw first: equal recorded outcomes are equal
        if raw is not None:
            cmp = raw
    if cmp.comparison is Comparison.CHANGED:
        cmp, explained = _explain_change(o, n, cmp, profile)
    elif cmp.comparison is Comparison.EQUAL and (_masks_of(of) or _masks_of(nf)):
        explained = _explain_masked(of, nf, cmp, profile)
    drift = (of.defaulted is not None and nf.defaulted is not None
             and of.defaulted != nf.defaulted) or cmp.drift
    if rewritten:
        category = FactCategory.REWRITTEN
    elif cmp.comparison is Comparison.UNCOMPARABLE:
        category = FactCategory.UNCOMPARABLE
    elif cmp.comparison is Comparison.CHANGED:
        category = FactCategory.CHANGED
    elif drift:
        category = FactCategory.DRIFT
    else:
        category = FactCategory.EQUAL
    notes = [rewritten] if rewritten else []
    if _is_widened_input(of.input) or _is_widened_input(nf.input):
        notes.append("widened")
    if rewritten != "positional":
        if input_changed and o.input_key != n.input_key:
            notes.append("matched-modulo-input")
        elif o.input_key != n.input_key:
            notes.append("matched-modulo-defaults")
        elif (_masks_of(of) or _masks_of(nf)) and \
                _canon(_raw_of(of).input) != _canon(_raw_of(nf).input):
            # equal inputs only once volatile values were masked: not the same input
            notes.append("matched-modulo-masked-input")
        if of.symbol != nf.symbol:
            notes.append("identity-changed")
    if (of.file, of.func) != (nf.file, nf.func):
        notes.append("site-moved")
    elif not rewritten and o.site_value != n.site_value:
        notes.append("line-moved")  # line mode only: a text-mode move is REWRITTEN
    if nf.binding != of.binding:
        raised = _binding_raise(nf if nf.binding else of)
        notes.append(f"now raises {raised} while binding its arguments" if nf.binding
                     else f"no longer raises {raised} while binding its arguments")
    all_notes = [*notes, *cmp.notes]
    return FactEntry.model_construct(  # trusted, typed values: no validation copies
        _ENTRY_FIELDS_SET, category=category, comparison=cmp.comparison, level=cmp.level,
        symbol=of.symbol, new_symbol=nf.symbol if nf.symbol != of.symbol else None,
        old_site=_site_ref(of), new_site=_site_ref(nf),
        input=of.input, new_input=nf.input if n.input_key != o.input_key else None,
        old=_cap(of.outcomes, all_notes, "old"), new=_cap(nf.outcomes, all_notes, "new"),
        old_types=cmp.old_types, new_types=cmp.new_types,
        old_defaulted=of.defaulted, new_defaulted=nf.defaulted, drift=drift,
        old_count=of.count, new_count=nf.count, notes=all_notes,
        old_where=of.where, new_where=nf.where,
        old_warnings=of.warnings or None, new_warnings=nf.warnings or None,
        explained=explained,
        **_raw_samples(of, nf),
    )


def _raw_equal(of: Fact, nf: Fact, profile: DiffProfile) -> OutcomeComparison | None:
    """The comparison of the recorded (unmasked) outcomes when it is EQUAL and the recorded
    facts stand for the same calls as the masked ones (a view that merged facts does not)."""
    rof, rnf = _raw_of(of), _raw_of(nf)
    if sum(x.count for x in rof.outcomes) != of.count \
            or sum(x.count for x in rnf.outcomes) != nf.count:
        return None
    raw = compare_outcomes(rof.outcomes, rnf.outcomes, profile)
    return raw if raw.comparison is Comparison.EQUAL else None


def _raw_samples(of: Fact, nf: Fact) -> dict[str, Any]:
    """The recorded input and outcomes behind masked ones, for reporting (empty when no mask
    changed either fact)."""
    out: dict[str, Any] = {}
    for side, f in (("old", of), ("new", nf)):
        if _masks_of(f):
            raw = _raw_of(f)
            out[f"{side}_raw"] = list(raw.outcomes)
            out["input_raw" if side == "old" else "new_input_raw"] = raw.input
    return out


def _explain_masked(of: Fact, nf: Fact, cmp: OutcomeComparison,
                    profile: DiffProfile) -> str | None:
    """Equal once masked: say so (level MASKED) when the recorded outcomes did differ."""
    raw_o, raw_n = _raw_of(of), _raw_of(nf)
    if compare_outcomes(raw_o.outcomes, raw_n.outcomes, profile).comparison \
            is Comparison.EQUAL and _canon(raw_o.input) == _canon(raw_n.input):
        return None
    if cmp.level is ComparisonLevel.EXACT:
        cmp.level = ComparisonLevel.MASKED
    mechs = sorted(_masks_of(of) | _masks_of(nf))
    noise = _noise_state()
    examples = []
    for side, masker in (("old", noise.old), ("new", noise.new)):
        for m in mechs:
            if masker is not None and m in masker.examples:
                examples.append(f"{side} {m} e.g. {masker.examples[m][:60]}")
    return f"{'+'.join(mechs)}: equal once volatile values are masked ({'; '.join(examples)})"


def _explain_change(o: _V, n: _V, cmp: OutcomeComparison,
                    profile: DiffProfile) -> tuple[OutcomeComparison, str | None]:
    """A value difference a stated rule explains: the callee is an entropy source (its own
    values are volatile: compared by structure), or the pair's own input difference accounts
    for every differing position (``input-substitution``, level MASKED). Anything else stays
    CHANGED."""
    of, nf = o.fact, n.fact
    noise = _noise_state()
    if of.symbol in noise.sources and nf.symbol in noise.sources:
        shape = compare_outcomes(of.outcomes, nf.outcomes, DiffProfile.SHAPE)
        if shape.comparison is Comparison.EQUAL:
            return shape, (f"source: {of.symbol} returns a run-dependent value; compared by "
                           "structure")
        return cmp, None
    if o.input_key == n.input_key or o.rewritten_site or n.rewritten_site:
        return cmp, None
    subs = input_substitutions(of.input, nf.input)
    if not subs:
        return cmp, None
    olds = [(k, a) for k, (a, _) in enumerate(subs)]
    news = [(k, b) for k, (_, b) in enumerate(subs)]
    mo = [Outcome(kind=x.kind, value=_substitute(x.value, olds) if x.kind == "return"
                  else x.value, count=x.count) for x in of.outcomes]
    mn = [Outcome(kind=x.kind, value=_substitute(x.value, news) if x.kind == "return"
                  else x.value, count=x.count) for x in nf.outcomes]
    masked = compare_outcomes(mo, mn, profile)
    if masked.comparison is not Comparison.EQUAL:
        return cmp, None
    if masked.level is ComparisonLevel.EXACT:
        masked.level = ComparisonLevel.MASKED
    shown = ", ".join(f"{a[:40]} -> {b[:40]}" for a, b in subs[:3])
    more = f" (+{len(subs) - 3})" if len(subs) > 3 else ""
    return masked, f"input-substitution: the output differs only as the input does: {shown}{more}"


def _binding_raise(f: Fact) -> str:
    return next((_raise_name(o.value) for o in f.outcomes if o.kind == "raise"), "TypeError")


def _mark_unobservable(old_left: list[_V], new: FactSet) -> None:
    """An old fact left unpaired whose source line no longer exists in the post side's file was
    edited (``resp.getheader(...)`` -> ``resp.headers.get(...)``): the replacement is a call
    nothing recorded (a stdlib method, a plain attribute), so the vanished call is not evidence
    of a behaviour change. Read the current source; unreadable files leave the fact as is."""
    if new.root is None:
        return
    cache: dict[str, set[str] | None] = {}
    for v in old_left:
        f = v.fact
        if v.rewritten_site or f.text is None or f.file == "?":
            continue
        path = f.file if os.path.isabs(f.file) else os.path.join(new.root, f.file)
        if path not in cache:
            lines = _read_source_lines(path)
            cache[path] = None if lines is None else {ln.strip()[:SITE_TEXT_MAX] for ln in lines}
        texts = cache[path]
        if texts is not None and f.text not in texts:
            v.rewritten_site = True
            v.unobservable = True


def _self_only(f: Fact) -> bool:
    bound = f.input.get("bound") if isinstance(f.input, dict) else None
    return (isinstance(bound, dict) and all(k in ("self", "cls") for k in bound)
            and not any(d for d in (f.defaulted or ())))


def _attribute_read(v: _V, f: Fact, prov: _Provenance, old_views: list[_V]) -> bool:
    """Positive evidence that a vanished self-only call was a property read: its value was used
    as a receiver on the same statement, or the statement's AST shows it is not the call."""
    returned = {lab for o in f.outcomes if o.kind == "return" for lab in _value_labels(o.value)}
    if returned and v.stmt_key:
        for other in old_views:
            if other is not v and other.stmt_key == v.stmt_key \
                    and _receiver_type(other.fact.input) in returned:
                return True
    return prov.attribute_read_evidence(0, f.file, f.line, f.symbol)


def _contact_matches(contact: str, symbol: str) -> bool:
    return contact == symbol or contact.rsplit(".", 1)[-1] == symbol.rsplit(".", 1)[-1]


def _mark_vanished(old_left: list[_V], new_facts: list[Fact], prov: _Provenance,
                   old_views: list[_V] | None = None) -> None:
    """An old call on an *unedited* line with no partner: a review item, not a failure, when the
    static contact maps say the line is no longer a dependency call, or when nothing in its
    function ran on the new side. Otherwise it stays DISAPPEARED (FAIL)."""
    ctx = prov.ctx
    if ctx is None:
        return
    ran = {(f.file, f.func) for f in new_facts}
    for v in old_left:
        f = v.fact
        if v.rewritten_site or prov.status(f.file, f.line, 0) != "same":
            continue
        if _self_only(f) and _attribute_read(v, f, prov, old_views or []):
            v.noise = "attribute read no longer goes through a traced getter"
            continue
        nl = prov.mapped_line(f.file, f.line, 0)
        if nl is not None and prov.resolves_to_project(f.file, nl, f.symbol):
            # The project changed what the name refers to. The contact map, when present, only
            # confirms: it never downgrades on its own (a removed dependency symbol is the
            # breaking change itself).
            now = (ctx.new_contacts.get(f.file, {}).get(nl, set())
                   if ctx.new_contacts is not None else set())
            if not any(_contact_matches(c, f.symbol) for c in now):
                v.vanish = "no longer a dependency call here"
                continue
        if (f.file, f.func) not in ran:
            v.vanish = "not reached: the enclosing function made no recorded call on the new side"


def _fresh_labels(pending: list[tuple[_V, _V, FactEntry]], old_left: list[_V],
                  new_left: list[_V]) -> tuple[set[str], set[str]]:
    """Type labels of values produced on edited lines on one side only (old, new): an input
    carrying one is downstream of an edit."""
    told: set[str] = set()
    tnew: set[str] = set()
    edited = [(o, 0) for o, _, _ in pending if o.rewritten_site]
    edited += [(n, 1) for _, n, _ in pending if n.rewritten_site]
    edited += [(v, 0) for v in old_left if v.rewritten_site]
    edited += [(v, 1) for v in new_left if v.rewritten_site]
    for v, side in edited:
        for out in v.fact.outcomes:
            if out.kind == "return":
                (told if side == 0 else tnew).update(_value_labels(out.value))
    return told - tnew, tnew - told


_NAMESPACE_CALLS = ("__new__", "__init__", "__init_subclass__")


def _lift_namespace_edits(pending: list[tuple[_V, _V, FactEntry]], prov: _Provenance) -> None:
    """A call that receives a class's namespace (a metaclass ``__new__``/``__init__``, an
    ``__init_subclass__``) on an unedited ``class`` line sees edits inside the class body.
    The pair is an edited site when its input differs and EVERY differing input leaf lies under
    a name the body binds on edited lines (on its side): the namespace carries the edit
    (``class Config:`` -> ``model_config = ...``). A difference anywhere else (the class
    name, an unedited attribute, an equal input with a different result) stays unedited."""
    if not prov.maps:
        return
    for o, n, _e in pending:
        if o.rewritten_site or n.rewritten_site or o.input_key == n.input_key:
            continue
        if o.fact.symbol.rsplit(".", 1)[-1] not in _NAMESPACE_CALLS \
                or n.fact.symbol.rsplit(".", 1)[-1] not in _NAMESPACE_CALLS:
            continue
        bound = [prov.body_bound_names(0, o.fact.file, o.fact.line),
                 prov.body_bound_names(1, n.fact.file, n.fact.line)]
        if bound[0] is None or bound[1] is None or not (bound[0] or bound[1]):
            continue
        differing = _differing_leaves(o.fact.input, n.fact.input)
        if differing and all(len(p) >= 1 and _leaf_name(p[0]) in bound[side]
                             for side, p in differing):
            o.rewritten_site = n.rewritten_site = True


def _differing_leaves(a: Any, b: Any) -> list[tuple[int, tuple]]:
    """``(side, path below the argument)`` of each input leaf one side holds and the other does
    not, compared as ``_unexplained_input_leaves`` does: a parameter named on both sides by its
    name, the rest as one multiset (a renamed parameter keeps its value); a type name at the
    same place on both sides is drift, not a difference."""
    sides = [[(name, p, kind, leaf) for name, value in _argument_values(x)
              for p, kind, leaf in _argument_leaves(value)] for x in (a, b)]
    named = [{it[0] for it in items if it[0] is not None} for items in sides]
    common = named[0] & named[1]
    typed = [{(it[0], repr(it[1])) for it in items if it[2] == "type" and it[0] in common}
             for items in sides]
    drifted = typed[0] & typed[1]

    def key(it: tuple) -> tuple:
        return (it[0] if it[0] in common else None, repr(it[1]), it[2], _canon(it[3]))

    kept = [[it for it in items if not (it[2] == "type" and (it[0], repr(it[1])) in drifted)]
            for items in sides]
    counts = [Counter(key(it) for it in items) for items in kept]
    out: list[tuple[int, tuple]] = []
    for side in (0, 1):
        extra = counts[side] - counts[1 - side]
        for it in kept[side]:
            k = key(it)
            if extra.get(k, 0) > 0:
                extra[k] -= 1
                out.append((side, it[1]))
    return out


def _leaf_name(step: Any) -> str:
    """A namespace key as a path step (``_argument_leaves`` keys a ``__dict__`` entry by its
    canonical JSON, an object's attribute by its name)."""
    if isinstance(step, str) and step.startswith('"'):
        try:
            v = json.loads(step)
        except ValueError:
            return step
        return v if isinstance(v, str) else step
    return str(step)


def _judge_pairs(pending: list[tuple[_V, _V, FactEntry]], old_left: list[_V],
                 new_left: list[_V], prov: _Provenance) -> None:
    """Judge each pair by edit provenance (only with a context whose ledger is known; without
    one a pair is compared strictly, as before)."""
    if prov.ctx is None:
        return
    fresh_old, fresh_new = _fresh_labels(pending, old_left, new_left)
    for o, n, e in pending:
        old_ed, new_ed = o.rewritten_site, n.rewritten_site
        tier = None
        entry = None
        if old_ed or new_ed:
            sides = [x for x in (prov.covering_site(o.fact.file, o.fact.line, 0),
                                 prov.covering_site(n.fact.file, n.fact.line, 1))
                     if x is not None]
            entry = max(sides, key=_claim_rank) if sides else None
            tier = _tier(entry) if entry is not None else (
                "hand" if prov.ledger_known else None)
        elif (prov.ledger_known and e.category is FactCategory.CHANGED
              and (_input_labels(n.fact.input) & fresh_new
                   or _input_labels(o.fact.input) & fresh_old)):
            tier = "downstream"
        if tier is None:
            continue
        e.provenance = tier
        if entry is not None and entry.get("step_id"):
            e.step_id = str(entry["step_id"])
            e.step_run = str(entry.get("run_id") or "") or None
        if e.category in (FactCategory.EQUAL, FactCategory.CHANGED, FactCategory.REWRITTEN) \
                and e.comparison is Comparison.EQUAL:
            e.category = FactCategory.REWRITTEN
        if tier == "ledger" or e.comparison is not Comparison.CHANGED:
            if tier == "ledger" and e.comparison is Comparison.CHANGED:
                e.category = FactCategory.REWRITTEN  # strict: the drop-in claim failed
                e.notes.append("pymolt's own edit changed behaviour"
                               if (entry or {}).get("basis") != "manual" else
                               "the recorded edit claimed preserve and changed behaviour")
            continue
        old_o, new_o = o.fact.outcomes, n.fact.outcomes
        if tier == "intended" and _judge_intended(e, entry or {}, old_o, new_o):
            continue
        if _representation_only(old_o, new_o):
            e.category, e.comparison, e.drift = FactCategory.DRIFT, Comparison.EQUAL, True
            e.notes.append("representation changed")
        elif all(x.kind == "return" for x in old_o) and any(x.kind == "raise" for x in new_o):
            e.category = FactCategory.REWRITTEN  # stays CHANGED: fails
            e.notes.append("now raises on an edited line")
        else:
            e.category, e.comparison = FactCategory.REWRITTEN, Comparison.UNCOMPARABLE
            e.review = "the value differs on a hand-edited line: review"
            e.notes.append(e.review)


def _tier(entry: Mapping) -> str:
    """The provenance an entry gives the lines it decides: ``ledger`` (``preserve``: strict),
    ``intended``, or ``hand`` (an entry with no claim vouches for nothing)."""
    if _vouches(entry):
        return "ledger"
    return "intended" if entry.get("claim") == "intended" else "hand"


def _leaf_type(name: Any) -> str:
    return str(name or "").rsplit(".", 1)[-1]


def _returned_type(value: Any) -> str:
    """The type name of a normalized returned value (a tagged structure, object or opaque
    value: its tag; a JSON scalar or container: its Python type)."""
    if isinstance(value, dict):
        for tag in ("__structure__", "__object__", "__opaque__"):
            if isinstance(value.get(tag), str):
                return value[tag]
        for tag, name in (("__list__", "list"), ("__tuple__", "tuple"), ("__set__", "set"),
                          ("__dict__", "dict"), ("__bytes__", "bytes")):
            if tag in value:
                return name
        return "dict"
    if value is None:
        return "NoneType"
    return type(value).__name__


def expectation_met(expect: str, old: list[Outcome], new: list[Outcome]) -> bool:
    """Whether a stated expectation holds of the new outcomes (design 5.2): ``representation``
    (only the representation changed), ``raise:<T>`` (every new outcome raises T) or
    ``return:<T>`` (every new outcome returns a T). Types compare by their last segment."""
    if expect == "representation":
        return _representation_only(old, new)
    kind, _sep, want = expect.partition(":")
    if kind not in ("raise", "return") or not want or not new:
        return False
    if kind == "raise":
        return all(o.kind == "raise" and _leaf_type(o.value) == _leaf_type(want) for o in new)
    return all(o.kind == "return" and _leaf_type(_returned_type(o.value)) == _leaf_type(want)
               for o in new)


#: The review of a change an unattested ``representation`` expectation cannot be checked
#: against: the values are not visible, only their types (design 5.1).
OPAQUE_REPRESENTATION = "representation unverifiable"
_OPAQUE_REPRESENTATION_REVIEW = (
    f"{OPAQUE_REPRESENTATION}: the step expects a change of representation only, and verify "
    "sees the type change but not the values; settle it by capturing with `--privacy values` "
    "where the values are recordable, by a baseline replicate, or by a person's "
    "`pymolt verify . --accept ID`")


def _content_unseen(e: FactEntry, old: list[Outcome], new: list[Outcome]) -> bool:
    """The difference lies only in returned content verify cannot see: both sides raise the
    same types (or none), both return, and the entry was compared by type only or a returned
    value on either side is opaque, truncated or cyclic (``_content`` is unknown). A change of
    outcome kind or of a raised type is visible, never unseen."""
    raised = [sorted(_canon(o.value) for o in side if o.kind == "raise") for side in (old, new)]
    returns = [[o for o in side if o.kind == "return"] for side in (old, new)]
    if raised[0] != raised[1] or not returns[0] or not returns[1]:
        return False
    if e.level in (ComparisonLevel.TYPE, ComparisonLevel.KIND):
        return True
    return any(_content(o.value) is _UNKNOWN for side in returns for o in side)


def _judge_intended(e: FactEntry, entry: Mapping, old: list[Outcome],
                    new: list[Outcome]) -> bool:
    """A changed outcome on a line an ``intended`` entry decides (design 5.1). With a
    person-attested expectation: ACCEPTED when it is met, FAIL otherwise. Unattested, with a
    stated ``representation`` expectation: FAIL when the change is visibly more than that
    (another value, a new raise); a review when only the type is visible (``_content_unseen``),
    as under the hand rule; DRIFT when only the representation changed (the hand rules, below).
    Otherwise the hand rules (returns False). An attested ``any`` is the hand rules too, until
    a person accepts the outcome."""
    expect = entry.get("expect") or "any"
    if _attested(entry) and expect != "any":
        if expectation_met(expect, old, new):
            e.category = FactCategory.REWRITTEN
            info = entry.get("attestation") if isinstance(entry.get("attestation"), Mapping) \
                else {}
            e.accepted = {"how": f"expectation {expect}", "by": info.get("user"),
                          "at": info.get("at"), "attestation": info.get("id"),
                          "step": entry.get("step_id")}
            e.notes.append(f"accepted: a person's attested expectation ({expect})")
            return True
        e.category = FactCategory.REWRITTEN           # stays CHANGED: fails
        e.notes.append(f"the attested expectation {expect} is not met")
        return True
    if expect == "representation" and not _representation_only(old, new):
        if _content_unseen(e, old, new):
            e.category, e.comparison = FactCategory.REWRITTEN, Comparison.UNCOMPARABLE
            e.review = _OPAQUE_REPRESENTATION_REVIEW
            e.notes.append(e.review)
            return True
        e.category = FactCategory.REWRITTEN           # stays CHANGED: fails
        e.notes.append("the change is more than its stated expectation (representation only)")
        return True
    return False


def _implicit_call_reason(e: FactEntry) -> str | None:
    """A call that nothing on its line names: the library's own internals or an implicit
    protocol (``==`` -> ``__eq__``) attributed to the project's line; or a name on the line that
    is read, not called (an attribute that became, or stopped being, a descriptor)."""
    site = e.old_site or e.new_site
    text = site.text if site is not None else None
    if not text:
        return None
    leaf = _symbol_leaf(e.symbol)
    parts = e.symbol.split(".")
    if parts[-1] == "__init__" and len(parts) > 1 and parts[-2][:1].islower():
        # a module-level ``__init__`` is a library-generated constructor of a project class
        return "library instrumentation of a project class's constructor"
    match = re.search(rf"(?<![\w]){re.escape(leaf)}\b\s*(\()?", text)
    if match is None:
        if _wrapper_like(e.symbol) or re.search(r"_\d+$", parts[-1]):
            return "decorator wrapper internals: not named on this line"
        if leaf.startswith("__") and leaf.endswith("__"):
            return "implicit protocol call: not named on this line"
        if e.category is FactCategory.APPEARED and _self_only_input(e.input) \
                and _returns_only(e.new):
            # A new property read by code that is not the project's (an io finalizer reading
            # ``closed``) attributed to the nearest project line.
            return "implicit attribute read: not named on this line"
        # Anything else not named on its line stays strict: an alias (``f = dep.func; f()``),
        # a self-only call that vanished (``db.Session.commit`` behind ``done()``).
        return None
    if not match.group(1) and not leaf.startswith("__init"):
        return "attribute access routed through a descriptor"
    return None


def _self_only_input(inputs: Any) -> bool:
    if not isinstance(inputs, dict):
        return False
    bound = inputs.get("bound")
    # a receiver and nothing else: an empty binding is a bare call (``f = dep.func; f()``)
    if not isinstance(bound, dict) or not bound or not set(bound) <= {"self", "cls"}:
        return False
    return all(inputs.get(k) in (None, [], {}, {"__tuple__": []}, {"__dict__": []})
               for k in ("args", "varargs", "kwargs")) and not inputs.get("defaulted")


def _returns_only(outcomes: list[Outcome] | None) -> bool:
    return bool(outcomes) and all(o.kind == "return" for o in outcomes)


def _constructor_halves(entries: list[FactEntry]) -> set[tuple]:
    """(type, file, line text) of every constructor half (``T.__new__``/``T.__init__``) that
    paired: its call site is known to construct T on both sides."""
    out = set()
    for e in entries:
        parts = e.symbol.rsplit(".", 1)
        if len(parts) == 2 and parts[1] in ("__new__", "__init__") and e.old_site \
                and e.new_site:
            out.add((parts[0], e.old_site.file, e.old_site.text or e.old_site.line))
            out.add((parts[0], e.new_site.file, e.new_site.text or e.new_site.line))
    return out


def _apply_noise(entries: list[FactEntry], prov: _Provenance,
                 old_packages: set[str]) -> None:
    """Import machinery and implicit class-creation hooks are never verdict drivers (a hook
    that appeared or vanished with a library version); a hook called on both sides whose
    outcome changed is judged like any call."""
    halves = _constructor_halves(entries) if prov.ctx is not None else set()
    for e in entries:
        if e.category in (FactCategory.EQUAL, FactCategory.DRIFT):
            continue
        reason = _is_noise(e.symbol)
        leaf = e.symbol.rsplit(".", 1)[-1]
        if reason == "implicit class-creation hook" and e.category is FactCategory.CHANGED:
            reason = None  # a hook called on both sides whose outcome changed is a regression
        if reason is None and e.category is FactCategory.APPEARED and leaf in _APPEAR_HOOKS:
            reason = "implicit protocol hook"
        if (reason is None and e.category is FactCategory.APPEARED
                and e.symbol.split(".")[0] in sys.stdlib_module_names
                and e.symbol.split(".")[0] not in old_packages):
            reason = "asymmetric instrumentation: stdlib package traced on the new side only"
        if reason is None and leaf == "__setitem__":
            site = e.old_site or e.new_site
            side = 0 if e.old_site else 1
            if site is not None and prov.in_class_body(side, site.file, site.line):
                reason = "class-body namespace write"
        if (reason is None and prov.ctx is not None
                and e.category in (FactCategory.DISAPPEARED, FactCategory.APPEARED)):
            reason = _implicit_call_reason(e)
            parts = e.symbol.rsplit(".", 1)
            if reason is None and e.category is FactCategory.APPEARED and len(parts) == 2 \
                    and parts[1] in ("__new__", "__init__") and _returns_only(e.new):
                site = e.site
                if (parts[0], site.file, site.text or site.line) in halves:
                    reason = (f"constructor protocol: the other half of {parts[0]}(...) paired "
                              "at this site")
        if reason is None:
            continue
        e.category, e.review, e.unverified = FactCategory.DRIFT, None, False
        e.notes.append(f"noise: {reason}")
        e.explained = e.explained or f"implicit: {reason}"


def _single_entry(v: _V, *, old_side: bool, prov: _Provenance | None = None) -> FactEntry:
    f = v.fact
    entry = _single_entry_base(v, old_side=old_side)
    if prov is None or prov.ctx is None or entry.category is FactCategory.DRIFT:
        return entry
    if v.noise:
        entry.category, entry.provenance = FactCategory.DRIFT, "unedited"
        entry.notes.append(v.noise)
    elif v.vanish:
        entry.category, entry.review, entry.provenance = (
            FactCategory.REWRITTEN, v.vanish, "unedited")
        entry.notes.append(v.vanish)
    elif v.rewritten_site and prov.ledger_known:
        side = 0 if old_side else 1
        covering = prov.covering_site(f.file, f.line, side)
        entry.category, entry.unverified = FactCategory.REWRITTEN, True
        entry.provenance = _tier(covering) if covering is not None else "hand"
        if covering is not None and covering.get("step_id"):
            entry.step_id = str(covering["step_id"])
            entry.step_run = str(covering.get("run_id") or "") or None
        entry.notes.append("unverified edited site")
    return entry


def _single_entry_base(v: _V, *, old_side: bool) -> FactEntry:
    f = v.fact
    if f.attr_of is not None and not v.rewritten_site:
        category = FactCategory.DRIFT
        notes = ["module-attribute-access", f"via {f.attr_of}"]
    elif v.rewritten_site:
        category, notes = FactCategory.REWRITTEN, ["unpaired"]
        if v.unobservable:
            notes.append(UNOBSERVABLE_NOTE)
    else:
        category = FactCategory.DISAPPEARED if old_side else FactCategory.APPEARED
        notes = []
    outcomes = _cap(f.outcomes, notes, "old" if old_side else "new")
    return FactEntry.model_construct(
        _ENTRY_FIELDS_SET, category=category, new_symbol=None, new_input=None,
        old_types=None, new_types=None, drift=False, comparison=Comparison.MISSING,
        level=None, symbol=f.symbol,
        old_site=_site_ref(f) if old_side else None,
        new_site=None if old_side else _site_ref(f),
        input=f.input,
        old=outcomes if old_side else None, new=None if old_side else outcomes,
        old_defaulted=f.defaulted if old_side else None,
        new_defaulted=None if old_side else f.defaulted,
        old_count=f.count if old_side else 0, new_count=0 if old_side else f.count,
        notes=notes,
        old_where=f.where if old_side else None, new_where=None if old_side else f.where,
        old_warnings=(f.warnings or None) if old_side else None,
        new_warnings=None if old_side else (f.warnings or None),
    )


def _site_ref(f: Fact) -> SiteRef:
    return SiteRef.model_construct(_SITE_FIELDS_SET, file=f.file, func=f.func, line=f.line,
                                   text=f.text)


_ENTRY_FIELDS_SET = set(FactEntry.model_fields)
_SITE_FIELDS_SET = set(SiteRef.model_fields)


def _cap(outcomes: list[Outcome], notes: list[str], side: str) -> list[Outcome]:
    if len(outcomes) <= _ENTRY_OUTCOME_CAP:
        return list(outcomes)
    notes.append(f"{side}-outcomes-truncated:{len(outcomes)}")
    return sorted(outcomes, key=lambda o: -o.count)[:_ENTRY_OUTCOME_CAP]


def _entry_sort_key(e: FactEntry) -> tuple:
    s = e.site
    return (_CATEGORY_ORDER[e.category], s.file, s.func, s.line if s.line is not None else -1,
            e.symbol, _canon(e.input))


def _warnings(old: FactSet, new: FactSet) -> list[str]:
    out: list[str] = []
    ov, nv = set(old.schema_versions), set(new.schema_versions)
    if ov and nv and (2 in ov) != (2 in nv):
        out.append("schema-mismatch: one trace is v1 (no sym/defaulted); decorated calls and "
                   "default-carrying inputs may not pair — recapture the older side")
    o_text = any(f.text is not None for f in old.facts)
    n_text = any(f.text is not None for f in new.facts)
    if o_text != n_text:
        side = "old" if not o_text else "new"
        out.append(f"site-text-missing:{side}: sites compared by line number, REWRITTEN "
                   "detection off")
    for side, fs in (("old", old), ("new", new)):
        if fs.aggregated:
            out.append(f"aggregated:{side}: outcome distributions and call order not recorded")
        if fs.invalid_lines:
            out.append(f"invalid-lines:{side}:{fs.invalid_lines}")
        if fs.evicted:
            out.append(f"evicted:{side}:{fs.evicted}: the facts snapshot reached its fact cap "
                       f"and dropped {fs.evicted_facts} fact(s); the evidence is incomplete "
                       "and cannot PASS")
        if fs.widened or fs.widened_facts:
            out.append(f"widened:{side}:{fs.widened}: call sites past the per-site input cap "
                       "are compared at type level only")
        if fs.outcomes_other:
            out.append(f"outcomes-capped:{side}:{fs.outcomes_other}: calls past a fact's "
                       "distinct-outcome cap are compared by type")
        if fs.c_muted:
            sites = len({(c.symbol, c.file, c.func, c.line) for c in fs.c_capped})
            out.append(f"c-site-cap:{side}:{fs.c_muted}: compiled call(s) at {sites} site(s) "
                       "past the runtime's per-site cap were counted, not recorded; facts "
                       "missing there are not comparable")
    return out


# ── legacy projection helpers ─────────────────────────────────────────────────────────────


def _legacy_record(e: FactEntry, *, old_side: bool) -> dict[str, Any]:
    site = (e.old_site if old_side else e.new_site) or e.site
    where = (e.old_where if old_side else e.new_where) or {
        "file": site.file, "line": site.line, "func": site.func}
    rec: dict[str, Any] = {
        "qualname": e.symbol if old_side or e.new_symbol is None else e.new_symbol,
        "inputs": e.input, "where": where, "category": e.category.value,
        "site_text": site.text, "level": e.level.value if e.level else None,
    }
    if e.new_symbol:
        rec["new_qualname"] = e.new_symbol
    return rec


def _raised_of(outcomes: list[Outcome] | None) -> Any:
    raised = sorted({o.value for o in outcomes or () if o.kind == "raise"})
    if not raised:
        return None
    return raised[0] if len(raised) == 1 else raised


def _result_of(outcomes: list[Outcome] | None) -> Any:
    values = [o.value for o in outcomes or () if o.kind == "return"]
    if not values:
        return None
    return values[0] if len(values) == 1 else values


# ── small utilities ───────────────────────────────────────────────────────────────────────


def _canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _is_lineno(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v > 0


def _within(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False


def _site_path(fs: FactSet, file: str, *, count: bool = True) -> str:
    """A call-site file as a project-relative path; a container path goes through the
    runner's mounts first (``/work/p/a.py`` -> ``<host>/a.py``). ``count``: add the mapping to
    ``paths_mapped`` (the call sites of facts, not those of a capture's accounts)."""
    if fs.runner is not None and os.path.isabs(file) and not _under_root(file, fs.root):
        host = to_host(fs.runner, file)
        if host is not None:
            if count:
                fs.paths_mapped += 1
            return _relativize(host, fs.root)
    return _relativize(file, fs.root)


def _under_root(file: str, root: str | None) -> bool:
    if root is None:
        return False
    return any(_within(f, b) for b in dict.fromkeys((root, os.path.realpath(root)))
               for f in dict.fromkeys((file, os.path.realpath(file))))


def _note_foreign_paths(fs: FactSet) -> None:
    """Say so when absolute call sites remain outside the project root."""
    if fs.root is None:
        return
    foreign = {f.file for f in fs.facts if os.path.isabs(f.file) or f.file.startswith("/")}
    fs.paths_foreign = len(foreign)
    if not foreign:
        return
    if fs.runner is None:
        fs.notes.append(
            f"container paths not mapped: {len(foreign)} call-site file(s) lie outside the "
            "project root and no runner is recorded for this capture (re-capture, or "
            "set the container with `pymolt setup`)")
    else:
        fs.notes.append(
            f"{len(foreign)} call-site file(s) are outside the project root and not under a "
            f"mount of {fs.runner.name} (dependency code, not project code)")


def runner_for_trace(trace_path: str | Path,
                     root: str | Path | None = None) -> tuple[RunnerSpec | None, str | None]:
    """The container a trace was captured in, and where that was recorded.

    The capture slot whose trace this is (``ContractSlot.runner``) > ``EnvConfig.runner`` >
    a legacy config that only names ``container_id`` (inspected with docker). ``(None, None)``
    when the project records no container. Never raises."""
    try:
        project = Path(root) if root is not None else _project_of(Path(trace_path))
        if project is None:
            return None, None
        state = _read_json(project / ".pymolt" / "contract_state.json")
        want = _canon_path(trace_path, project)
        if isinstance(state, dict):
            for key in ("baseline", "post_migration"):
                slot = state.get(key)
                if (isinstance(slot, dict) and isinstance(slot.get("trace_path"), str)
                        and _canon_path(slot["trace_path"], project) == want
                        and isinstance(slot.get("runner"), dict)):
                    return RunnerSpec.model_validate(slot["runner"]), "slot"
        config = _read_json(project / ".pymolt" / "env_config.json")
        if isinstance(config, dict):
            if isinstance(config.get("runner"), dict):
                return RunnerSpec.model_validate(config["runner"]), "config"
            cid = config.get("container_id")
            if isinstance(cid, str) and cid:
                spec = inspect_container(cid)
                if spec is not None:
                    return spec, "config-container"
    except Exception as exc:  # noqa: BLE001 - a missing mapping degrades to a note
        logger.warning("could not determine the capture's runner: %s", exc)
    return None, None


def _project_of(trace: Path) -> Path | None:
    for parent in trace.resolve().parents:
        if parent.name == ".pymolt":
            return parent.parent
    return None


def _canon_path(path: str | Path, project: Path) -> str:
    p = Path(path)
    p = p if p.is_absolute() else project / p
    return os.path.realpath(str(p))


def _relativize(file: str, root: str | None) -> str:
    if root is None:
        return Path(file).as_posix()
    candidates = [file]
    if os.path.isabs(file):
        candidates.append(os.path.realpath(file))
    for base in dict.fromkeys((root, os.path.realpath(root))):
        for f in candidates:
            if os.path.isabs(f) and _within(f, base):
                return Path(os.path.relpath(f, base)).as_posix()
    return Path(file).as_posix()


def _read_source_lines(path: str) -> list[str] | None:
    """Source lines with Python's own line numbering (PEP 263 cookie / BOM honoured)."""
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    try:
        encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
    except (SyntaxError, LookupError):
        encoding = "utf-8"
    try:
        text = data.decode(encoding, errors="replace")
    except LookupError:
        text = data.decode("utf-8", errors="replace")
    if text.startswith("﻿"):
        text = text[1:]
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
