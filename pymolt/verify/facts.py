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
6. REWRITTEN by position: within a ``(file, func)`` whose line texts changed on both sides,
   leftover calls at the changed lines pair in first-seen order; outcomes compared, symbol and
   input ignored
7. leftovers: at a changed line -> REWRITTEN without a partner (unverifiable, inconclusive);
   otherwise DISAPPEARED / APPEARED

A pair's outcomes are compared at ``exact`` level when both sides are single, fully rendered
values; at ``type`` level (``outcome_type``) when either side carries a type-only marker
(``__opaque__``/``__cycle__``/``__shape__``/truncation/a value-less ``__structure__``), when a
side is nondeterministic (more than one distinct outcome and the sets are not identical) or when
``profile="shape"``. An aggregated record whose result collapsed to ``__nondeterministic__``
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
import io
import json
import logging
import os
import tokenize
import uuid
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, computed_field

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
FACT_DIFF_SCHEMA = "pymolt.fact-diff/1"
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
    TYPE = "type"
    KIND = "kind"   # from tallies: which outcome kinds (return / raise types) occurred


_CATEGORY_ORDER = {c: i for i, c in enumerate(FactCategory)}


class Outcome(BaseModel):
    """One distinct outcome of a fact: a normalized return value or a raised type name."""

    kind: Literal["return", "raise"]
    value: Any = None
    count: int = 1


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
    # Deprecation-family warnings its (sampled) calls issued, merged across occurrences.
    warnings: list[CallWarning] = Field(default_factory=list)

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

    @computed_field
    @property
    def fails(self) -> bool:
        if self.category in (FactCategory.CHANGED, FactCategory.DISAPPEARED,
                             FactCategory.APPEARED):
            return True
        return self.category is FactCategory.REWRITTEN and self.comparison is Comparison.CHANGED

    @property
    def inconclusive(self) -> bool:
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

    def counts(self) -> dict[str, int]:
        out = {c.value: 0 for c in FactCategory}
        for e in self.entries:
            out[e.category.value] += 1
        return out

    def by_category(self, category: FactCategory | str) -> list[FactEntry]:
        cat = FactCategory(category)
        return [e for e in self.entries if e.category is cat]

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
        entries = [
            e.model_dump(mode="json", exclude_none=True, exclude_defaults=False)
            for e in self.entries
            if include_equal or e.category is not FactCategory.EQUAL or e.notes
        ]
        return {
            "schema": FACT_DIFF_SCHEMA,
            "profile": self.profile.value,
            "verdict": self.verdict.value,
            "clean": self.is_clean(),
            "incomplete": self.incomplete,
            "counts": self.counts(),
            "unsampled": self.unsampled,
            "warnings": list(self.warnings),
            "old": self.old_summary.model_dump(mode="json"),
            "new": self.new_summary.model_dump(mode="json"),
            "entries": entries,
        }

    def to_boundary_diff(self) -> BoundaryDiff:
        """Project onto today's ``BoundaryDiff`` so existing renderers/counters keep working.

        CHANGED and failing REWRITTEN -> ``raise_changed`` when the raise side differs, else
        ``result_changed``; DISAPPEARED/APPEARED map 1:1; UNCOMPARABLE and unpaired/undecidable
        REWRITTEN -> ``skipped_opaque``. equal and DRIFT are dropped. Note BoundaryDiff treats
        ``appeared`` as non-blocking: ``FactDiff.verdict`` is the authority.
        """
        out = BoundaryDiff()
        for e in self.entries:
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


def iter_trace_records(path: str | Path, stats: _ReadStats | None = None) -> Iterator[dict]:
    """Yield raw records from a JSONL event stream or an aggregated ``{"records": [...]}``.

    Undecodable JSONL lines (e.g. the last line of a killed process) are skipped and counted
    in ``stats.invalid_lines``.
    """
    stats = stats if stats is not None else _ReadStats()
    p = Path(path)
    if is_facts_snapshot(p):
        yield from iter_snapshot_records(p)
        return
    if _is_blank(p):
        snap = find_facts_snapshot(p)
        if snap is not None:
            yield from iter_snapshot_records(snap)
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
                yield ev
            else:
                stats.invalid_lines += 1


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
    path: str | Path, kinds: Iterable[str] = ("header", "fact", "tally"),
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
            yield {**base, "t": "return", "result": o.get("result"), "count": count}
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


def iter_snapshot_records(path: str | Path) -> Iterator[dict]:
    """Every fact of a snapshot as counted events, in first-seen order."""
    for _kind, fact in iter_snapshot(path, kinds=("fact",)):
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

    def add_outcome(self, kind: str, payload: str, value: Any, n: int) -> None:
        key = (kind, payload)
        if key in self.outcomes:
            self.outcomes[key] += n
        elif len(self.outcomes) < MAX_OUTCOMES:
            self.outcomes[key] = n
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
                outs.append('{"t":"return","result":' + payload + f',"count":{n}}}')
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
        return '{"in":' + self.in_json + ',"outcomes":[' + ",".join(outs) + "]," + \
            _canon(head)[1:]


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
                 "incremental_settle", "started_at", "ended_at", "backend", "sample_rate",
                 "warnings_capture", "distributions")


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
    if f is None:
        f = facts[key] = _MergedFact(
            q=q, sym=sym, symq=obj.get("symq") if isinstance(obj.get("symq"), str) else None,
            own=obj.get("own") if isinstance(obj.get("own"), str) else None, where=where,
            in_json=in_json, widened=in_json == WIDENED_INPUT or bool(obj.get("widened")),
            first_ts=first if isinstance(first, int | float) else None,
            last_ts=last if isinstance(last, int | float) else None)
    else:
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
            f.add_outcome("return", _canon(o.get("result")), o.get("result"), n)
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


def collect_site_text(trace_path: str | Path, root: str | Path) -> dict[str, str]:
    """Read the source line of every distinct ``where.file:line`` in a trace.

    Run at capture time, before the project's code changes. Returns
    ``{"<file as recorded>:<line>": "<stripped line, at most 240 chars>"}`` for files under
    ``root`` only (relative recorded paths resolve against ``root``). Unreadable files, lines
    past EOF and undecodable bytes are skipped / replaced, never raised.
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
    pending = path.with_name(f".{path.name}.{uuid.uuid4().hex}.pending")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with pending.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
        os.replace(pending, path)
    except OSError:
        try:
            pending.unlink()
        except OSError:
            pass
        return None
    return str(path)


def record_site_text(trace_path: str | Path, root: str | Path) -> int:
    """``collect_site_text`` + ``store_site_text``; returns the number of lines recorded."""
    site_text = collect_site_text(trace_path, root)
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


def fold_facts(
    records: Iterable[Mapping[str, Any]],
    *,
    root: str | Path | None = None,
    site_text: Mapping[str, str] | None = None,
    source: str | None = None,
) -> FactSet:
    """Fold v1/v2 JSONL events and/or aggregated MemorySink records into a FactSet.

    Facts are kept at line granularity (file, func, line, input); ``FactSet.keyed()`` and
    ``diff_facts`` merge lines that share a line text.
    """
    root_s = os.path.abspath(str(root)) if root is not None else None
    site_text = site_text or {}
    rel_cache: dict[str, str] = {}
    acc: dict[tuple, _Acc] = {}
    fs = FactSet(source=source, root=root_s, site_text_entries=len(site_text))
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
                rel = rel_cache[abs_file] = _relativize(abs_file, root_s)
        symbol = ev["sym"] or ev["q"]
        ikey = _canon(ev["input"])
        ikey = pool.setdefault("i" + ikey, ikey)
        key = (symbol, rel, func, line, ikey)
        a = acc.get(key)
        if a is None:
            text = site_text.get(f"{abs_file}:{line}") if abs_file and line is not None else None
            wkey = (abs_file, where.get("line"), where.get("func"), len(where))
            shared_where = where_pool.get(wkey) if where else None
            if where and shared_where is None:
                shared_where = where_pool[wkey] = dict(where)
            a = acc[key] = _Acc(symbol=symbol, file=rel, func=func, line=line, text=text,
                                input=pool.setdefault("v" + ikey, ev["input"]),
                                first_seen=index, where=shared_where)
        a.qualnames.add(ev["q"])
        if ev["symq"] and a.symq is None:
            a.symq = ev["symq"]
        if ev["defaulted"] is not None:
            if a.defaulted is None:
                a.defaulted = set()
            a.defaulted.add(ev["defaulted"])
        okey = _canon([ev["kind"], ev["value"]])
        slot = a.outcomes.get(okey)
        if slot is None:
            okey = pool.setdefault("o" + okey, okey)
            a.outcomes[okey] = [ev["kind"], pool.setdefault("w" + okey, ev["value"]),
                                ev["count"]]
        else:
            slot[2] += ev["count"]
        a.count += ev["count"]
        if ev["warnings"]:
            a.warnings = merge_warnings(a.warnings if a.warnings is not None else {},
                                        ev["warnings"])
    pool.clear()
    where_pool.clear()
    fs.schema_versions = sorted(versions)
    ordered = sorted(acc.values(), key=lambda a: a.first_seen)
    acc.clear()
    fs.facts = [_fact_from_acc(a) for a in ordered]
    return fs


def load_facts(
    trace_path: str | Path,
    *,
    root: str | Path | None = None,
    site_text: Mapping[str, str] | None = None,
) -> FactSet:
    """Load a trace (JSONL, aggregated JSON, or a facts snapshot) and its sidecar
    ``site_text``, then fold.

    A facts snapshot is read when it is the path given, or when the JSONL is absent or empty
    and ``<trace>.facts.json`` (``find_facts_snapshot``) exists; otherwise the JSONL is folded
    (a facts capture's JSONL holds the snapshot's projection). Whenever a snapshot belongs to
    the trace, its quality counters (evicted, widened, outcomes past the cap) and tallies
    annotate the FactSet."""
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
    records = iter_snapshot_records(snap) if jsonl is None and snap is not None else \
        iter_trace_records(trace_path, stats)
    fs = fold_facts(records, root=root, site_text=site_text, source=str(trace_path))
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
    _add_sidecar_tally_warnings(fs, meta, site_text)
    return fs


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
        file=_relativize(file, fs.root),
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
                rel = rel_cache[file] = _relativize(file, fs.root)
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
    if kind == "raise":
        value = _raise_name(value)
    v = raw.get("v")
    version = v if isinstance(v, int) and not isinstance(v, bool) else (2 if sym else 1)
    where = raw.get("where") if isinstance(raw.get("where"), dict) else {}
    warns = raw.get("warnings")
    return {"q": q, "sym": sym, "symq": raw.get("symq") if isinstance(raw.get("symq"), str)
            else None, "input": inputs, "defaulted": defaulted, "kind": kind, "value": value,
            "count": count, "where": where, "v": version, "aggregated": aggregated,
            "warnings": warns if isinstance(warns, list) and warns else None}


UNTYPED_RAISE = "<unknown>"  # setprofile below 3.12: a raise whose type it cannot see
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
        outcomes=[Outcome.model_construct(_OUTCOME_FIELDS_SET, kind=k, value=v, count=c)
                  for _, (k, v, c) in outcomes],
        count=a.count, where=a.where, first_seen=a.first_seen,
        warnings=_call_warnings(a.warnings),
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
            else:
                outcomes[k] = [o.kind, o.value, o.count]
        if f.warnings:
            merge_warnings(warns, [w.model_dump() for w in f.warnings])
    return head.model_copy(update={
        "qualnames": sorted(qualnames),
        "symq": next((f.symq for f in ordered if f.symq), None),
        "lines": sorted(lines),
        "defaulted": None if defaulted is None else [list(d) for d in sorted(defaulted)],
        "outcomes": [Outcome(kind=k, value=v, count=c)
                     for _, (k, v, c) in sorted(outcomes.items())],
        "count": sum(f.count for f in ordered),
        "warnings": _call_warnings(warns),
    })


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
    if "dataclass" in a and "dataclass" in b:
        fa, fb = a.get("fields", {}), b.get("fields", {})
        return (a["dataclass"] == b["dataclass"] and fa.keys() == fb.keys()
                and all(types_match(fa[k], fb[k]) for k in fa))
    return False


def _named_match(named: dict, other: Any) -> bool:
    name = named.get("opaque", named.get("cycle"))
    if isinstance(other, dict) and ("opaque" in other or "cycle" in other):
        other_name = other.get("opaque", other.get("cycle"))
    else:
        other_name = _type_label(other)
    return other_name == name or (name, other_name) in _LABEL_ALIASES


def _type_label(t: Any) -> str | None:
    """Top-level type name of a ``value_type`` tree."""
    if isinstance(t, str):
        return t
    if isinstance(t, dict):
        for k in ("structure", "dataclass", "opaque", "cycle", "enum"):
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


def compare_outcomes(
    old: list[Outcome], new: list[Outcome], profile: DiffProfile | str = DiffProfile.EXACT,
) -> OutcomeComparison:
    """Compare two outcome distributions (the support, not the counts)."""
    profile = DiffProfile(profile)
    if any(_is_unrecorded(o) for o in (*old, *new)):
        return OutcomeComparison(Comparison.UNCOMPARABLE, None, ["nondeterministic-unrecorded"])
    o_keys = {_canon([o.kind, o.value]) for o in old}
    n_keys = {_canon([o.kind, o.value]) for o in new}
    nondet = len(o_keys) > 1 or len(n_keys) > 1
    notes = ["nondeterministic"] if nondet else []
    # A raise recorded without its type (setprofile below 3.12) can only be compared as "a
    # raise": against a typed raise on the other side that is a type-level comparison.
    untyped = o_keys != n_keys and any(_is_untyped_raise(o) for o in (*old, *new))
    if untyped:
        notes.append("exception type unknown on one side")
    markers = untyped or any(_partial(o) for o in (*old, *new))
    if profile is DiffProfile.EXACT:
        if o_keys == n_keys:
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
    return a == b or UNTYPED_RAISE in (a, b)


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
    if _type_only(a) or _type_only(b):
        return types_match(value_type(a), value_type(b))
    if not (isinstance(a, dict) and isinstance(b, dict)):
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


# ── diff ──────────────────────────────────────────────────────────────────────────────────


@dataclass(eq=False)
class _V:
    """A fact in the diff's view: merged per the file's site mode, with its matching keys."""

    fact: Fact
    site_value: str | int | None
    input_key: str
    core_key: str = ""
    rewritten_site: bool = False

    @property
    def first_seen(self) -> int:
        return self.fact.first_seen


def diff_facts(
    old: FactSet, new: FactSet, *, profile: DiffProfile | str = DiffProfile.EXACT,
) -> FactDiff:
    """Compare two FactSets (baseline -> post-migration). See the module docstring."""
    prof = DiffProfile(profile)
    old_facts, new_facts, c_level_excluded = _exclude_c_level(old, new)
    old_facts, new_facts = _collapse_widened(old_facts, new_facts)
    if prof is DiffProfile.SHAPE:
        old_facts = [f.model_copy(update={"input": input_type(f.input)}) for f in old_facts]
        new_facts = [f.model_copy(update={"input": input_type(f.input)}) for f in new_facts]

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

    def site(v: _V) -> tuple:
        return (v.fact.file, v.fact.func, v.site_value)

    entries: list[FactEntry] = []
    old_left, new_left = list(ov), list(nv)
    passes: list[Callable[[_V], Any]] = [
        lambda v: (v.fact.symbol, site(v), v.input_key),                # 1 exact key
        lambda v: (v.fact.symbol, site(v), v.core_key),                 # 2 modulo defaults
        lambda v: (v.fact.symbol, v.fact.file, v.fact.func, v.core_key),  # 3 same function
        lambda v: (site(v), v.core_key),                                # 4 identity changed
        lambda v: (v.fact.symbol, v.core_key),                          # 5 site moved
    ]
    for key_fn in passes:
        pairs, old_left, new_left = _pair_groups(old_left, new_left, key_fn)
        for o, n in pairs:
            # A text-mode call whose line text changed inside the same function: REWRITTEN.
            same_call = (modes.get(o.fact.file) == "text"
                         and (o.fact.file, o.fact.func) == (n.fact.file, n.fact.func)
                         and o.site_value != n.site_value)
            entries.append(_pair_entry(o, n, prof, modes,
                                       rewritten="same-call" if same_call else None))

    # 6 Positional REWRITTEN pairing inside edited functions.
    by_func_o: dict[tuple, list[_V]] = defaultdict(list)
    by_func_n: dict[tuple, list[_V]] = defaultdict(list)
    for v in old_left:
        if v.rewritten_site:
            by_func_o[(v.fact.file, v.fact.func)].append(v)
    for v in new_left:
        if v.rewritten_site:
            by_func_n[(v.fact.file, v.fact.func)].append(v)
    paired: set[int] = set()
    for fkey, olds in by_func_o.items():
        news = by_func_n.get(fkey, [])
        olds.sort(key=lambda v: v.first_seen)
        news.sort(key=lambda v: v.first_seen)
        for o, n in zip(olds, news):
            entries.append(_pair_entry(o, n, prof, modes, rewritten="positional"))
            paired.update((id(o), id(n)))
    old_left = [v for v in old_left if id(v) not in paired]
    new_left = [v for v in new_left if id(v) not in paired]

    for v in old_left:
        entries.append(_single_entry(v, old_side=True))
    for v in new_left:
        entries.append(_single_entry(v, old_side=False))

    unsampled = _apply_tallies(entries, old, new, ov, nv, modes)
    _soften_evicted(entries, old, new)
    entries.sort(key=_entry_sort_key)
    warnings = _warnings(old, new)
    if unsampled:
        warnings.append(
            f"unsampled:{unsampled}: value facts seen on one side only, at call sites both "
            "sides' tallies show were called but not fully sampled; not compared")
    if c_level_excluded:
        warnings.append(
            f"c-level-excluded:{c_level_excluded}: C-level raise facts were not compared — "
            f"one side's backend ({_families(old)} vs {_families(new)}) does not record them")
    return FactDiff(profile=prof, entries=entries, old_summary=old.summary(),
                    new_summary=new.summary(), warnings=warnings, unsampled=unsampled)


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
                "input": dict(_WIDENED),
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


def diff_traces(
    old_path: str | Path,
    new_path: str | Path,
    *,
    root: str | Path | None = None,
    old_root: str | Path | None = None,
    new_root: str | Path | None = None,
    profile: DiffProfile | str = DiffProfile.EXACT,
) -> FactDiff:
    """Load two traces (each with its own sidecar site_text) and diff them.

    ``old_root``/``new_root`` default to ``root``; pass them separately when the two captures
    ran from different checkouts (or a container and the host)."""
    old = load_facts(old_path, root=old_root if old_root is not None else root)
    new = load_facts(new_path, root=new_root if new_root is not None else root)
    return diff_facts(old, new, profile=profile)


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
        groups[(f.symbol, f.file, f.func, sv, _canon(f.input))].append(f)
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


def _pair_entry(o: _V, n: _V, profile: DiffProfile, modes: dict[str, str], *,
                rewritten: str | None) -> FactEntry:
    """Classify one old/new pair. ``rewritten`` is ``"same-call"`` / ``"positional"`` for a
    REWRITTEN pair (outcomes compared, symbol and input free to differ), else None."""
    of, nf = o.fact, n.fact
    cmp = compare_outcomes(of.outcomes, nf.outcomes, profile)
    drift = (of.defaulted is not None and nf.defaulted is not None
             and of.defaulted != nf.defaulted)
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
        if o.input_key != n.input_key:
            notes.append("matched-modulo-defaults")
        if of.symbol != nf.symbol:
            notes.append("identity-changed")
    if (of.file, of.func) != (nf.file, nf.func):
        notes.append("site-moved")
    elif not rewritten and o.site_value != n.site_value:
        notes.append("line-moved")  # line mode only: a text-mode move is REWRITTEN
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
    )


def _single_entry(v: _V, *, old_side: bool) -> FactEntry:
    f = v.fact
    if v.rewritten_site:
        category, notes = FactCategory.REWRITTEN, ["unpaired"]
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
