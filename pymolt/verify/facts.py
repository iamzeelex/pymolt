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
"""
import io
import json
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

from pymolt.verify.models import BoundaryDiff, VerificationVerdict

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


_CATEGORY_ORDER = {c: i for i, c in enumerate(FactCategory)}


class Outcome(BaseModel):
    """One distinct outcome of a fact: a normalized return value or a raised type name."""

    kind: Literal["return", "raise"]
    value: Any = None
    count: int = 1


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

    @computed_field
    @property
    def verdict(self) -> VerificationVerdict:
        """FAIL on any failing entry; INCONCLUSIVE on undecidable entries or no evidence."""
        if not self.is_clean():
            return VerificationVerdict.FAIL
        if not self.entries or any(e.inconclusive for e in self.entries):
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
            "counts": self.counts(),
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
                reason = ("rewritten-unpaired" if e.comparison is Comparison.MISSING
                          else "nondeterministic-unrecorded")
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
        key = (symbol, rel, func, line, _canon(ev["input"]))
        a = acc.get(key)
        if a is None:
            text = site_text.get(f"{abs_file}:{line}") if abs_file and line is not None else None
            a = acc[key] = _Acc(symbol=symbol, file=rel, func=func, line=line, text=text,
                                input=ev["input"], first_seen=index,
                                where=dict(where) if where else None)
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
            a.outcomes[okey] = [ev["kind"], ev["value"], ev["count"]]
        else:
            slot[2] += ev["count"]
        a.count += ev["count"]
    fs.schema_versions = sorted(versions)
    fs.facts = [_fact_from_acc(a) for a in sorted(acc.values(), key=lambda a: a.first_seen)]
    return fs


def load_facts(
    trace_path: str | Path,
    *,
    root: str | Path | None = None,
    site_text: Mapping[str, str] | None = None,
) -> FactSet:
    """Load a trace (JSONL or aggregated JSON) and its sidecar ``site_text``, then fold."""
    if site_text is None:
        site_text = load_site_text(trace_path)
    stats = _ReadStats()
    fs = fold_facts(iter_trace_records(trace_path, stats), root=root, site_text=site_text,
                    source=str(trace_path))
    fs.invalid_lines = stats.invalid_lines
    fs.aggregated = fs.aggregated or stats.aggregated
    return fs


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
        count = 1
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
    if kind == "raise" and not isinstance(value, str):
        value = str(value)
    v = raw.get("v")
    version = v if isinstance(v, int) and not isinstance(v, bool) else (2 if sym else 1)
    where = raw.get("where") if isinstance(raw.get("where"), dict) else {}
    return {"q": q, "sym": sym, "symq": raw.get("symq") if isinstance(raw.get("symq"), str)
            else None, "input": inputs, "defaulted": defaulted, "kind": kind, "value": value,
            "count": count, "where": where, "v": version, "aggregated": aggregated}


def _fact_from_acc(a: _Acc) -> Fact:
    outcomes = sorted(a.outcomes.items())
    return Fact(
        symbol=a.symbol, qualnames=sorted(a.qualnames), symq=a.symq, file=a.file, func=a.func,
        line=a.line, lines=[a.line] if a.line is not None else [], text=a.text, input=a.input,
        defaulted=None if a.defaulted is None else [list(d) for d in sorted(a.defaulted)],
        outcomes=[Outcome(kind=k, value=v, count=c) for _, (k, v, c) in outcomes],
        count=a.count, where=a.where, first_seen=a.first_seen,
    )


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
    return head.model_copy(update={
        "qualnames": sorted(qualnames),
        "symq": next((f.symq for f in ordered if f.symq), None),
        "lines": sorted(lines),
        "defaulted": None if defaulted is None else [list(d) for d in sorted(defaulted)],
        "outcomes": [Outcome(kind=k, value=v, count=c)
                     for _, (k, v, c) in sorted(outcomes.items())],
        "count": sum(f.count for f in ordered),
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
    markers = any(_partial(o) for o in (*old, *new))
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
        return a.value == b.value
    return _values_match(a.value, b.value)


def _outcome_types_match(a: dict, b: dict) -> bool:
    if _type_is_ambiguous(a) or _type_is_ambiguous(b):
        return _ambiguous_compatible(a, b)
    if ("raise" in a) != ("raise" in b):
        return False
    if "raise" in a:
        return a["raise"] == b["raise"]
    return types_match(a["return"], b["return"])


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
    old_facts, new_facts = old.facts, new.facts
    if prof is DiffProfile.SHAPE:
        old_facts = [f.model_copy(update={"input": input_type(f.input)}) for f in old_facts]
        new_facts = [f.model_copy(update={"input": input_type(f.input)}) for f in new_facts]

    modes = _site_modes(old_facts, new_facts)
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

    entries.sort(key=_entry_sort_key)
    return FactDiff(profile=prof, entries=entries, old_summary=old.summary(),
                    new_summary=new.summary(), warnings=_warnings(old, new))


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


def _site_modes(old_facts: list[Fact], new_facts: list[Fact]) -> dict[str, str]:
    """Per file: ``text`` when every fact in it on both sides has its line text, else ``line``
    (a site key must mean the same thing on both sides)."""
    modes: dict[str, str] = {}
    for f in (*old_facts, *new_facts):
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
    return FactEntry(
        category=category, comparison=cmp.comparison, level=cmp.level,
        symbol=of.symbol, new_symbol=nf.symbol if nf.symbol != of.symbol else None,
        old_site=_site_ref(of), new_site=_site_ref(nf),
        input=of.input, new_input=nf.input if n.input_key != o.input_key else None,
        old=_cap(of.outcomes, all_notes, "old"), new=_cap(nf.outcomes, all_notes, "new"),
        old_types=cmp.old_types, new_types=cmp.new_types,
        old_defaulted=of.defaulted, new_defaulted=nf.defaulted, drift=drift,
        old_count=of.count, new_count=nf.count, notes=all_notes,
        old_where=of.where, new_where=nf.where,
    )


def _single_entry(v: _V, *, old_side: bool) -> FactEntry:
    f = v.fact
    if v.rewritten_site:
        category, notes = FactCategory.REWRITTEN, ["unpaired"]
    else:
        category = FactCategory.DISAPPEARED if old_side else FactCategory.APPEARED
        notes = []
    outcomes = _cap(f.outcomes, notes, "old" if old_side else "new")
    return FactEntry(
        category=category, comparison=Comparison.MISSING, level=None, symbol=f.symbol,
        old_site=_site_ref(f) if old_side else None,
        new_site=None if old_side else _site_ref(f),
        input=f.input,
        old=outcomes if old_side else None, new=None if old_side else outcomes,
        old_defaulted=f.defaulted if old_side else None,
        new_defaulted=None if old_side else f.defaulted,
        old_count=f.count if old_side else 0, new_count=0 if old_side else f.count,
        notes=notes,
        old_where=f.where if old_side else None, new_where=None if old_side else f.where,
    )


def _site_ref(f: Fact) -> SiteRef:
    return SiteRef(file=f.file, func=f.func, line=f.line, text=f.text)


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
