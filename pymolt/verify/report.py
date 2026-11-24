"""The unified contract report — what we KNOW about the boundary, honestly.

Merges the two axes we locked:

* **coverage of the contract** — the static contact map (the denominator: every
  dependency symbol our code *could* call) vs a dynamic trace (the numerator:
  what actually ran). Each symbol is ``confirmed`` (static ∧ observed),
  ``blind`` (static, never observed → a probe candidate), or ``dynamic-only``
  (observed but the static map missed it → static under-approximation, e.g. via
  ``getattr``/monkeypatching).
* **identity** — static paths are public import paths (``requests.Request``), the
  runtime records defining names (``sym`` = ``requests.models.Request.__init__``;
  v1 traces only ``q`` = ``<frame module>.<co_name>``). Static paths are resolved
  in the capture's interpreter (:mod:`pymolt.verify.identity_resolver`); a runtime
  name confirms a static path when it is the resolved symbol, a method of a
  resolved class, or a legacy name unique in the defining module. Unresolvable
  paths keep the exact-string rule and are counted as such.
* **coverage and recall**, over static *contacts* (a place your code touches a dependency:
  file, line, symbol, kind), not symbols. Each contact is ``recorded`` (the trace has a call of
  its symbol at its statement), ``ran-not-recorded`` (its line ran, the trace has nothing
  there: a tracer gap), ``not-run`` (its line never ran: a test gap) or ``unobservable`` (no
  trace can record it, see ``UNOBSERVABLE_CLASSES`` and existence contacts below). coverage =
  (recorded + ran-not-recorded) / observable contacts, the pytest-coverage analogue; recall =
  recorded / (recorded + ran-not-recorded), about 100% unless the tracer missed something.
  "Ran" comes from the capture's executed record (lines on 3.12+, functions below), else from
  the files the trace recorded call sites in; ``coverage_granularity`` says which.
  ``identity_tiers`` (exact-join vs canonical identity) keep the symbol join auditable.
* **version diff** (optional) — two traces (old vs new dependency version) folded
  into a :class:`BoundaryDiff`; behavior changes are only trustworthy over the
  area the dynamics covered.

This subsumes the old standalone ``gaps`` command: blind zones are now a section
here, not a separate tool.
"""

import json
import logging
import os
from collections import Counter
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, PrivateAttr

from pymolt.core.stdlib import is_stdlib
from pymolt.verify import identity_resolver
from pymolt.verify.comparators import ComparatorProfile, TraceComparator, compare_traces
from pymolt.verify.contact_map import _IGNORE_DEPS, Contact, build_contact_map, contact_class
from pymolt.verify.facts import (
    Comparison,
    DiffProfile,
    FactCategory,
    FactDiff,
    FactSet,
    OrderCause,
    diff_facts,
    load_facts,
)
from pymolt.verify.identity_resolver import ResolvedIdentity
from pymolt.verify.models import BoundaryDiff, InconclusiveCause, VerificationVerdict
from pymolt.verify.predict import Calibration, calibrate, predict_risks

logger = logging.getLogger(__name__)

# Per-contact statuses (``ContactStatus.status``).
RECORDED = "recorded"
RAN_NOT_RECORDED = "ran-not-recorded"
NOT_RUN = "not-run"
UNOBSERVABLE = "unobservable"
# Symbol classes (``SymbolStatus.contact_class``) whose contacts no trace can record: a chain
# through a call's return value, a dependency missing where the capture ran, interpreter code
# reached by a protocol (``type.__or__``). The test runner's symbols are not the project's
# dependency surface at all: they are left out of the report (``test_runner_symbols``).
UNOBSERVABLE_CLASSES = ("returned-value", "absent-dependency", "interpreter")
# How "ran" was known, finest first: the executed record's lines (3.12+) or functions (below
# 3.12, and 3.12 code objects too large for line events), the files the trace recorded call
# sites in (older captures), or nothing but the symbol (a trace without call sites).
GRANULARITIES = ("line", "function", "file", "symbol")
# Packages the injected runtime never treats as a boundary unless a target names them (it
# keeps its own copy in ``_backend.TEST_RUNNER_PACKAGES``; a test keeps the two equal).
TEST_RUNNER_PACKAGES = frozenset((
    "_pytest", "pytest", "pluggy", "py", "iniconfig", "_hypothesis_pytestplugin", "xdist"))


class SymbolStatus(BaseModel):
    target: str  # dependency qualname, e.g. "flask.cli.with_appcontext"
    dep: str
    status: str  # confirmed | blind | dynamic-only
    callers: list[str] = Field(default_factory=list)  # our functions (static)
    files: list[str] = Field(default_factory=list)
    observed_at: list[str] = Field(default_factory=list)  # file:line from the trace
    contact_kinds: list[str] = Field(default_factory=list)
    impacted: bool | None = None  # None when no impact set was supplied
    matched_impact_paths: list[str] = Field(default_factory=list)
    probed: bool = False  # re-invoked under the target version (``--probe-python``)
    probe_status: str | None = None  # stable | changed | error | opaque-inputs
    # Static identity, resolved in the capture's interpreter (static symbols only).
    sym: str | None = None  # canonical "<defining module>.<qualname>"
    identity_kind: str | None = None  # function | class | module | other
    identity_error: str | None = None  # why the path could not be resolved
    # behavioural | existence | interpreter (a behavioural contact that resolves to
    # builtins/stdlib code, e.g. ``type.__or__`` for ``A | B`` — not dependency code) |
    # returned-value (a chain through a call result) | absent-dependency (not installed)
    contact_class: str | None = None
    # exact (v1 string join) | canonical | alternative (never observed: at each of its
    # statements the other method of its operator was recorded, ``ContactStatus``)
    matched_by: str | None = None
    observed_as: list[str] = Field(default_factory=list)  # runtime names that confirmed it
    # Its contacts by status (``ContactStatus.status``), and for a BLIND symbol why: not-run
    # (none of its contacts ran), ran-not-recorded (one ran, the trace has nothing there) or
    # unobservable (no trace could record any of them).
    contact_status: dict[str, int] = Field(default_factory=dict)
    blind_reason: str | None = None


class ContactStatus(BaseModel):
    """One static contact: a place your code touches a dependency, and what the run says."""

    file: str | None = None
    line: int | None = None
    span: tuple[int, int] | None = None  # the statement's lines; a multi-line one counts whole
    target: str
    kind: str
    caller: str = ""
    status: str  # recorded | ran-not-recorded | not-run | unobservable
    granularity: str | None = None  # how "ran" was known (``GRANULARITIES``); None: unobservable
    # recorded: ``site`` (a call of its symbol at its statement), ``symbol`` (an existence
    # contact whose line ran, of a symbol the trace observed elsewhere) or ``alternative``
    # (one of an operator's two methods: the other was recorded at its statement)
    matched_by: str | None = None
    why: str | None = None  # unobservable: existence or one of ``UNOBSERVABLE_CLASSES``
    # An operator's method: its alternatives at this statement (``Contact.alternatives``).
    alternatives: list[str] = Field(default_factory=list)
    # The contact of the other method this one is counted with (one operator at one statement
    # counts once in coverage and recall); None: counted on its own.
    counted_with: str | None = None


class IdentityTier(BaseModel):
    """Static symbols one identity rule confirms: identity-resolution diagnostics."""

    name: str  # exact | canonical
    basis: str
    confirmed: int = 0
    static: int = 0
    share: float = 0.0


class ComparedRecordings(BaseModel):
    """The recordings a report compared, and which capture each one is.

    ``*_capture`` is ``"post-migration"`` or ``"baseline"`` when the recording is that
    capture's own, None when it is a file named on the command line that is no capture (or
    there is no recording on that side). Only a verdict whose ``after`` side is the
    post-migration capture is this migration's verdict."""

    after: str | None = None
    after_capture: str | None = None
    before: str | None = None
    before_capture: str | None = None

    @property
    def captures(self) -> bool:
        """Both sides are the two captures."""
        return self.after_capture == "post-migration" and self.before_capture == "baseline"


class ContractReport(BaseModel):
    # The fact diff and its accept context, kept for `verify --accept` (not saved).
    _fact_diff: Any = PrivateAttr(default=None)
    _accepts: Any = PrivateAttr(default=None)

    root: str
    static_targets: int = 0
    observed_targets: int = 0
    confirmed: int = 0
    blind: int = 0
    dynamic_only: int = 0
    # These headline fields are the whole recorded surface, the one the verdict judges, with
    # or without an impact filter: an applied plan's paths focus the report (``impacted_*``),
    # they do not shrink what PASS must cover.
    # Headline: coverage and recall over static contacts (``contacts``), the whole surface;
    # an operator's two methods at one statement count once (``ContactStatus.counted_with``),
    # in these counts too.
    # coverage = (recorded + ran-not-recorded) / observable contacts: how much of the
    # dependency surface the run reached. recall = recorded / (recorded + ran-not-recorded):
    # how much of what ran the tracer caught. None when the denominator is empty.
    # ``coverage_granularity`` is how "ran" was known for the contacts it decided (the
    # coarsest; ``granularity_counts`` has them all): line | function | file | symbol.
    coverage: float | None = None
    recall: float | None = None
    coverage_granularity: str | None = None
    granularity_counts: dict[str, int] = Field(default_factory=dict)
    contacts_total: int = 0
    contacts_recorded: int = 0
    contacts_ran_not_recorded: int = 0
    contacts_not_run: int = 0
    contacts_unobservable: int = 0
    unobservable_by_reason: dict[str, int] = Field(default_factory=dict)
    # Static symbols of the test runner (``TEST_RUNNER_PACKAGES``: ``pytest.fixture`` in a test
    # file), which captures do not trace unless a target names it: not the project's dependency
    # surface, so they are in no count, no table and no reason, only a counted note.
    test_runner_symbols: list[str] = Field(default_factory=list)
    test_runner_contacts: int = 0
    # The executed record covered every process for its whole run (False: a process had
    # none, or it stopped early; None: no record, the file rule decided).
    executed_complete: bool | None = None
    contacts: list[ContactStatus] = Field(default_factory=list)
    # BLIND symbols split by why (``SymbolStatus.blind_reason``).
    blind_not_run: int = 0
    blind_ran_not_recorded: int = 0
    blind_unobservable: int = 0
    # Identity diagnostics over the static symbols: the v1 exact-string join, then
    # canonical identity (which ``confirmed`` uses).
    identity_tiers: list[IdentityTier] = Field(default_factory=list)
    # Project files holding contacts where the trace recorded a call site; ``executed_scope``
    # is False when the trace carries no usable call sites.
    executed_files: list[str] = Field(default_factory=list)
    executed_scope: bool | None = None
    trace_schema: int | None = None  # 1 = runtime q only, 2 = canonical sym present
    identity_interpreter: str | None = None
    identity_source: str | None = None  # probe-python | trace-metadata | host
    identity_python: str | None = None
    identity_resolved: int = 0
    identity_unresolved: int = 0
    probed: int = 0  # symbols re-invoked under the target version
    probe_changed: int = 0  # of those, how many changed behavior
    by_dep: dict[str, dict[str, int]] = Field(default_factory=dict)
    symbols: list[SymbolStatus] = Field(default_factory=list)
    diff: dict[str, int] | None = None  # BoundaryDiff.counts() when two traces given
    diff_clean: bool | None = None
    # True when a capture was taken against a different manifest/config than the
    # one on disk now: the verdict below still describes the old world. False
    # means checked-and-current; None means there was nothing to check against.
    baseline_stale: bool | None = None
    # The current-side capture's source staleness (post-migration, or a lone baseline): the
    # files it recorded whose text differs on disk now, by capture. Its evidence describes
    # code that is no longer there. ``sources_unchecked``: a current-side capture that keeps
    # no source snapshot, so this cannot be checked (never read as fresh).
    stale_sources: dict[str, list[str]] = Field(default_factory=dict)
    sources_unchecked: list[str] = Field(default_factory=list)
    verdict: VerificationVerdict = VerificationVerdict.INCONCLUSIVE
    verdict_reasons: list[str] = Field(default_factory=list)
    # An INCONCLUSIVE verdict's reasons, each with its kind (needs_review / needs_evidence) and
    # what settles it, in ``verdict_reasons``' order (design decision 7). Empty otherwise.
    inconclusive_causes: list[InconclusiveCause] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    # What was compared (``build_contract_report_from_state``). None on a report built
    # without the captures' state, or saved before this existed.
    compared: ComparedRecordings | None = None

    # Full-surface totals: the same values as the headline fields above (kept for readers
    # of saved reports, which may predate the headline being the whole surface).
    all_static_targets: int = 0
    all_observed_targets: int = 0
    all_confirmed: int = 0
    all_blind: int = 0
    all_dynamic_only: int = 0
    all_probed: int = 0
    all_probe_changed: int = 0
    all_by_dep: dict[str, dict[str, int]] = Field(default_factory=dict)
    all_diff: dict[str, int] | None = None
    all_diff_clean: bool | None = None

    impact_filter_active: bool = False
    impact_paths: list[str] = Field(default_factory=list)
    impacted_static_targets: int = 0
    impacted_observed_targets: int = 0
    impacted_confirmed: int = 0
    impacted_blind: int = 0
    impacted_dynamic_only: int = 0
    impacted_coverage: float | None = None   # coverage / recall over the impacted symbols' contacts
    impacted_recall: float | None = None
    impacted_probed: int = 0
    impacted_probe_changed: int = 0
    impacted_by_dep: dict[str, dict[str, int]] = Field(default_factory=dict)
    impacted_symbols: list[SymbolStatus] = Field(default_factory=list)
    impacted_contacts: list[Contact] = Field(default_factory=list)
    impact_by_kind: dict[str, int] = Field(default_factory=dict)
    unmatched_impact_paths: list[str] = Field(default_factory=list)
    impact_diff_details: dict[str, list[dict[str, Any]]] | None = None
    # Fact-level version diff (schema ``pymolt.fact-diff/1``): every non-equal fact with its
    # symbol, call site and old -> new outcome. ``fact_diff`` follows the impact filter;
    # ``all_fact_diff`` is always the whole surface.
    fact_diff: dict[str, Any] | None = None
    all_fact_diff: dict[str, Any] | None = None
    # Failing entries of the whole surface that the impact filter hid from ``fact_diff``:
    # behaviour changes outside the plan's predicted impacts (they still decide the verdict).
    unpredicted_changes: list[dict[str, Any]] = Field(default_factory=list)
    # Runtime symbols every one of whose facts compared equal on both sides (equal, DRIFT,
    # or REWRITTEN with an equal outcome) in the whole fact diff. A dynamic-only symbol
    # in this set was observed AND verified, so it does not block PASS.
    fact_equal_symbols: list[str] = Field(default_factory=list)
    # Predicted vs observed (schema ``pymolt.calibration/1``): each predicted risk is a hit,
    # miss or not exercised against the whole-surface fact diff, plus the failing changes no
    # prediction covered. Evidence about the predictions, never a verdict input.
    calibration: Calibration | None = None


_IMPACT_PATH_FIELDS = (
    "path",
    "replacement_path",
    "old_qualname",
    "new_qualname",
    "from_path",
    "to_path",
)


def _impact_paths(*collections: Collection[object] | None) -> tuple[bool, list[str]]:
    """Normalize strings, API-change models, and their serialized dictionaries."""
    active = any(items is not None for items in collections)
    paths: set[str] = set()

    def add(value: object) -> None:
        if isinstance(value, str):
            if path := value.strip():
                paths.add(path)
            return
        if isinstance(value, Mapping):
            for field in _IMPACT_PATH_FIELDS:
                if field in value:
                    add(value[field])
            return
        if isinstance(value, (list, tuple, set, frozenset)):
            for item in value:
                add(item)
            return
        for field in _IMPACT_PATH_FIELDS:
            field_value = getattr(value, field, None)
            if field_value is not None:
                add(field_value)

    for items in collections:
        if items is None:
            continue
        if isinstance(items, (str, Mapping)):
            add(items)
        else:
            for item in items:
                add(item)
    return active, sorted(paths)


def _matches_impact(target: str, paths: Collection[str]) -> list[str]:
    """Match exact APIs and parent/child surfaces on dotted-name boundaries."""
    return [
        path
        for path in paths
        if target == path or target.startswith(path + ".") or path.startswith(target + ".")
    ]


def _counts(symbols: Collection[SymbolStatus]) -> Counter[str]:
    return Counter(symbol.status for symbol in symbols)


def _by_dep(symbols: Collection[SymbolStatus]) -> dict[str, dict[str, int]]:
    counts: dict[str, Counter[str]] = {}
    for symbol in symbols:
        counts.setdefault(symbol.dep, Counter())[symbol.status] += 1
    return {
        dep: {
            "confirmed": values["confirmed"],
            "blind": values["blind"],
            "dynamic_only": values["dynamic-only"],
        }
        for dep, values in sorted(counts.items())
    }


def _filter_diff(diff: BoundaryDiff, paths: Collection[str]) -> BoundaryDiff:
    def selected(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            record
            for record in records
            if _matches_impact(str(record.get("qualname") or record.get("q") or ""), paths)
        ]

    return BoundaryDiff(
        disappeared=selected(diff.disappeared),
        result_changed=selected(diff.result_changed),
        raise_changed=selected(diff.raise_changed),
        appeared=selected(diff.appeared),
        skipped_opaque=selected(diff.skipped_opaque),
    )


@dataclass
class _Observation:
    """Every event recorded under one runtime name (``sym`` when present, else ``q``)."""

    name: str
    canonical: bool  # ``name`` came from a v2 ``sym``
    legacy: set[str] = field(default_factory=set)  # the v1 ``q`` spellings seen
    sites: dict[str, None] = field(default_factory=dict)  # ordered set of file:line
    site_lines: set[tuple[str, int]] = field(default_factory=set)  # (raw file, line)


@dataclass
class _TraceView:
    observations: dict[str, _Observation] = field(default_factory=dict)
    q_names: set[str] = field(default_factory=set)  # the exact-join (v1) vocabulary
    files: set[str] = field(default_factory=set)  # raw ``where.file`` values
    events: int = 0
    v2_events: int = 0


def _load_observations(path: str | Path) -> _TraceView:
    view = _TraceView()
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError as e:
        logger.warning("Could not read trace %s: %s", path, e)
        return view
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        q = rec.get("q") if isinstance(rec.get("q"), str) and rec.get("q") else None
        sym = rec.get("sym") if isinstance(rec.get("sym"), str) and rec.get("sym") else None
        name = sym or q
        if not name:
            continue
        view.events += 1
        if sym:
            view.v2_events += 1
        obs = view.observations.get(name)
        if obs is None:
            obs = view.observations[name] = _Observation(name=name, canonical=sym is not None)
        elif sym:
            obs.canonical = True
        if q:
            obs.legacy.add(q)
            view.q_names.add(q)
        where = rec.get("where") if isinstance(rec.get("where"), dict) else {}
        file = where.get("file")
        if isinstance(file, str) and file:
            view.files.add(file)
        obs.sites.setdefault(f"{where.get('file', '?')}:{where.get('line', '?')}", None)
        line = where.get("line")
        if isinstance(file, str) and file and isinstance(line, int):
            obs.site_lines.add((file, line))
    return view


class _IdentityIndex:
    """Which static targets a runtime name confirms.

    * any name equal to the static path (the v1 exact-string rule);
    * a v1 ``q`` equal to a *legacy* name of the resolved object (unique in its
      defining module — ambiguous ``module.__init__`` never confirms a class);
    * a v2 ``sym`` equal to the resolved symbol or one of its aliases, or — for a
      class — any ``sym`` under it (``class_sym + "."``: a method or constructor).
    """

    def __init__(self, targets: Iterable[str], identities: Mapping[str, ResolvedIdentity]):
        self.exact = set(targets)
        self.by_name: dict[str, set[str]] = {}
        self.by_legacy: dict[str, set[str]] = {}
        self.by_class: dict[str, set[str]] = {}
        for target in self.exact:
            identity = identities.get(target)
            if identity is None or not identity.resolved:
                continue
            names = {identity.sym, *identity.aliases}
            for name in names:
                self.by_name.setdefault(name, set()).add(target)
                if identity.kind == "class":
                    self.by_class.setdefault(name, set()).add(target)
            for name in identity.legacy:
                self.by_legacy.setdefault(name, set()).add(target)

    def match(self, obs: _Observation) -> set[str]:
        hits: set[str] = set()
        legacy = obs.legacy if obs.canonical else obs.legacy | {obs.name}
        for name in legacy:
            if name in self.exact:
                hits.add(name)
            hits |= self.by_legacy.get(name, set())
        if obs.canonical:
            name = obs.name
            if name in self.exact:
                hits.add(name)
            hits |= self.by_name.get(name, set())
            prefix = name
            while "." in prefix:
                prefix = prefix.rsplit(".", 1)[0]
                hits |= self.by_class.get(prefix, set())
        return hits


def _map_files(
    raw_files: Iterable[str], root: Path, contact_files: Iterable[str]
) -> tuple[dict[str, str], str | None]:
    """Runtime file names (``where.file``, ``co_filename``) -> contact files, as the contact map
    spells them. A path under the project root maps by its relative path; when every path
    lies outside the root (a container capture, a moved checkout) they map by path suffix,
    the longest contact path first, rather than claim nothing ran."""
    by_posix = {Path(f).as_posix(): f for f in contact_files}
    mapping: dict[str, str] = {}
    under_root = False
    outside: list[str] = []
    for raw in raw_files:
        if raw.startswith("<"):  # <string>, <frozen ...>: no file to attribute
            continue
        path = Path(raw)
        rel = None
        if not path.is_absolute():
            rel = path.as_posix()
        else:
            for candidate in (path, Path(os.path.realpath(raw))):
                try:
                    rel = candidate.relative_to(root).as_posix()
                    break
                except ValueError:
                    continue
        if rel is None:
            outside.append(raw)
            continue
        under_root = True
        if rel in by_posix:
            mapping[raw] = by_posix[rel]
    if under_root or not outside:
        return mapping, None
    for raw in outside:
        parts = raw.replace("\\", "/").split("/")
        for i in range(1, len(parts)):      # longest suffix first
            hit = by_posix.get("/".join(parts[i:]))
            if hit is not None:
                mapping[raw] = hit
                break
    return mapping, (
        "the trace's call sites lie outside the project root; executed files were "
        "matched by path suffix"
    )


@dataclass
class _Executed:
    """The capture's executed record (``executed`` in each process's sidecar), merged."""

    granularity: str | None = None      # the finest any process recorded: line | function
    coarsest: str | None = None         # the coarsest (a not-run claim is this precise)
    complete: bool = True               # every record covers its process's whole run
    processes: int = 0                  # processes with a record
    without: int = 0                    # processes without one (older runtime, watch mode)
    lines: dict[str, set[int]] = field(default_factory=dict)       # raw file -> lines
    functions: dict[str, set[int]] = field(default_factory=dict)   # raw file -> lines


def _load_executed(trace: str | Path | None) -> _Executed | None:
    """The executed record of ``trace``'s sidecar (``<trace>.meta.json``): the merged capture's
    ``processes`` or, for one process's own sidecar, itself. None when no process has one."""
    if trace is None:
        return None
    try:
        meta = json.loads(Path(str(trace) + ".meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(meta, dict):
        return None
    procs = [p for p in (meta.get("processes") or ()) if isinstance(p, dict)] or [meta]
    from pymolt.verify._backend import decode_lines

    ex = _Executed()
    order = {"line": 0, "function": 1}
    for proc in procs:
        rec = proc.get("executed")
        if not isinstance(rec, dict) or rec.get("v") != 1 or rec.get("granularity") not in order:
            ex.without += 1
            continue
        ex.processes += 1
        gran = rec["granularity"]
        if ex.granularity is None or order[gran] < order[ex.granularity]:
            ex.granularity = gran
        if ex.coarsest is None or order[gran] > order[ex.coarsest]:
            ex.coarsest = gran
        if rec.get("complete") is not True:
            ex.complete = False
        for name, item in (rec.get("files") or {}).items():
            if not isinstance(item, dict):
                continue
            for key, into in (("lines", ex.lines), ("functions", ex.functions)):
                found = decode_lines(item.get(key) or "")
                if found:
                    into.setdefault(name, set()).update(found)
    if not ex.processes:
        return None
    if ex.without:
        ex.complete = False
    return ex


def _container_path_note(scope_note: str, trace: str | Path | None, root: Path) -> str:
    """Keep the path-suffix note only when container paths stayed foreign after mapping."""
    if trace is None:
        return scope_note
    try:
        facts = load_facts(trace, root=root)
    except Exception:  # noqa: BLE001 — the note is advisory; never fail the report over it
        return scope_note
    foreign = facts.paths_foreign > 0 or any("container paths not mapped" in n for n in facts.notes)
    if facts.paths_mapped and not foreign:
        return f"container paths mapped via {facts.path_map_source}"
    return scope_note


def _ratio(confirmed: int, static: int) -> float:
    return confirmed / static if static else 0.0


def _names_match_impact(names: Iterable[str], impact_names: Mapping[str, set[str]]) -> list[str]:
    """Impact paths matching any of ``names`` (static path, resolved sym) on dotted boundaries."""
    names = [name for name in names if name]
    return sorted(
        path
        for path, aliases in impact_names.items()
        if any(_matches_impact(name, aliases) for name in names)
    )


def build_contract_report(
    project_dir: str | Path,
    trace: str | Path | None = None,
    against: str | Path | None = None,
    probe_python: str | None = None,
    dependencies: set[str] | None = None,
    changed_api_paths: Collection[object] | None = None,
    impact_set: Collection[object] | None = None,
    comparator_profile: ComparatorProfile | str = ComparatorProfile.EXACT,
    custom_comparator: TraceComparator | str | None = None,
    resolve_identities: bool = True,
    predictions: Mapping[str, Any] | None = None,
    diff_context: Any = None,
    noise_profile: tuple[Mapping[str, Any], str] | None = None,
) -> ContractReport:
    """Build the unified report.

    ``noise_profile`` (``(profile, label)``, from a baseline replicate): the run-to-run noise
    the fact diff is judged with (``replicate.apply_noise_profile``).

    ``diff_context`` (a ``facts.DiffContext``): the snapshots, apply ledger, contact maps and
    successors that let the fact diff judge an edited site by who edited it.

    ``predictions`` (``{"risks", "source", "plan_id", "notes"}``) are the predicted risks to
    calibrate against the fact diff (``calibration``); only a fact diff can score them.

    ``trace`` is the observed dynamic recording (current/new version) used for
    coverage. ``against`` is an older recording; when both are given, the version
    diff (``against`` -> ``trace``) is attached. ``probe_python`` (a target venv
    interpreter) re-invokes each reconstructible captured contact in a subprocess of that interpreter
    and records whether it stays stable or changes under that version.

    Static paths are resolved to canonical identities (one subprocess, cached
    under ``.pymolt/cache/``) in ``probe_python`` if given, else the interpreter
    the trace's metadata records, else pymolt's own — noted as such.
    ``resolve_identities=False`` keeps the exact-string join only.

    ``changed_api_paths`` (an applied plan's impacts) focuses the report on the changed
    API paths: ``impacted_*``, ``fact_diff`` and ``diff`` hold the focus, while the headline
    coverage, recall and every verdict input stay the whole surface. Items may
    be dotted strings, serialized changes, or objects with ``path`` and optional
    ``replacement_path`` fields. ``impact_set`` is an equivalent alias useful
    to callers that already use that terminology; values from both are merged.
    Impact paths match a static symbol by its public path or its resolved symbol.
    """
    root = Path(project_dir).resolve()
    report = ContractReport(root=str(root))
    impact_filter_active, impact_paths = _impact_paths(changed_api_paths, impact_set)
    report.impact_filter_active = impact_filter_active
    report.impact_paths = impact_paths

    cmap = build_contact_map(root, dependencies)
    report.notes.extend(cmap.notes)

    static: dict[str, dict] = {}
    for c in cmap.contacts:
        s = static.setdefault(
            c.target,
            {
                "dep": c.dep,
                "callers": set(),
                "files": set(),
                "kinds": set(),
            },
        )
        s["callers"].add(c.caller)
        s["kinds"].add(c.kind)
        if c.file:
            s["files"].add(c.file)

    view = _load_observations(trace) if trace else _TraceView()
    if not trace:
        report.notes.append("no trace given — every static contact is BLIND (no dynamics yet)")
    elif view.events:
        report.trace_schema = 2 if view.v2_events else 1
        if not view.v2_events:
            report.notes.append(
                "trace schema v1 (runtime names are module.co_name, no canonical sym): a "
                "static path is confirmed by an exact match or by a name unique in its "
                "defining module — re-capture for full canonical matching"
            )

    # ── static identity ──────────────────────────────────────────────────────
    identities: dict[str, ResolvedIdentity] = {}
    if resolve_identities and static and (view.observations or impact_filter_active):
        choice = identity_resolver.select_interpreter(probe_python, trace)
        resolution = identity_resolver.resolve_identities(
            set(static) | set(impact_paths), root, choice
        )
        identities = resolution.results
        report.identity_interpreter = resolution.interpreter
        report.identity_source = resolution.interpreter_source
        report.identity_python = resolution.python
        report.notes.extend(resolution.notes)
        found = {t: identities.get(t, ResolvedIdentity(error="not resolved")) for t in static}
        unresolved = sorted(t for t, identity in found.items() if identity.error)
        valueish = sum(1 for identity in found.values() if not identity.error and not identity.sym)
        report.identity_resolved = sum(1 for identity in found.values() if identity.resolved)
        report.identity_unresolved = len(unresolved)
        python = f" (Python {resolution.python})" if resolution.python else ""
        note = (
            f"static identity: {report.identity_resolved}/{len(static)} static path(s) "
            f"resolved under {resolution.interpreter}{python}"
        )
        if valueish:
            note += f"; {valueish} are values without a code identity (constants, instances)"
        if unresolved:
            sample = ", ".join(unresolved[:5]) + (", …" if len(unresolved) > 5 else "")
            note += f"; {len(unresolved)} unresolvable ({sample})"
        if valueish or unresolved:
            note += " — those keep the exact-string match"
        report.notes.append(note)

    # ── static ↔ observed ────────────────────────────────────────────────────
    index = _IdentityIndex(static, identities)
    matched: dict[str, list[_Observation]] = {target: [] for target in static}
    unmatched: list[_Observation] = []
    for obs in view.observations.values():
        hits = index.match(obs)
        for target in hits:
            matched[target].append(obs)
        if not hits:
            unmatched.append(obs)

    contact_files = {f for s in static.values() for f in s["files"]}
    executed = _load_executed(trace)
    raw_files = set(view.files)
    if executed is not None:
        raw_files |= set(executed.lines) | set(executed.functions)
    file_map, scope_note = _map_files(raw_files, root, contact_files)
    if scope_note and (view.events or executed is not None):
        report.notes.append(_container_path_note(scope_note, trace, root))
    executed_files: set[str] = set()
    executed_scope: bool | None = None
    if view.events:
        executed_files = {file_map[f] for f in view.files if f in file_map}
        executed_scope = bool(view.files - {f for f in view.files if f.startswith("<")})
        if not executed_scope:
            report.notes.append(
                "the trace records no call sites: a contact counts as recorded when its "
                "symbol was observed anywhere, and whether it ran is unknown (granularity "
                "symbol)"
            )
    report.executed_files = sorted(executed_files)
    report.executed_scope = executed_scope

    impact_names: dict[str, set[str]] = {}
    for path in impact_paths:
        names = {path}
        identity = identities.get(path)
        if identity is not None and identity.resolved:
            names.add(identity.sym)
        impact_names[path] = names

    observed_tops = {name.split(".", 1)[0] for name in (*view.observations, *view.q_names)}
    # The test runner is not traced (the runtime keeps the same list), unless a target names
    # one of the family, which lifts the exclusion for all of it: its static symbols are not
    # the project's dependency surface, and no capture could confirm them.
    runner_untraced = not observed_tops & TEST_RUNNER_PACKAGES
    symbols: list[SymbolStatus] = []
    for target, s in static.items():
        hits = matched[target]
        if (runner_untraced and not hits
                and s["dep"].split(".")[0] in TEST_RUNNER_PACKAGES):
            report.test_runner_symbols.append(target)
            continue
        identity = identities.get(target)
        klass = (
            "behavioural"
            if any(contact_class(kind) == "behavioural" for kind in s["kinds"])
            else "existence"
        )
        if klass == "behavioural" and identity is not None and identity.resolved:
            if is_stdlib(identity.sym.split(".")[0]):
                klass = "interpreter"
        elif klass == "behavioural" and identity is not None and identity.error:
            unobservable = _unresolvable_class(identity.error, s["dep"])
            # "Not installed" is only believable when the resolver ran in the capture's
            # own interpreter AND the trace saw nothing from that package; under pymolt's
            # interpreter every dependency could look absent and silently empty the
            # coverage denominator.
            if unobservable == "absent-dependency" and (
                report.identity_source == "host" or s["dep"] in observed_tops
            ):
                unobservable = None
            klass = unobservable or klass
        names = [target]
        if identity is not None and identity.resolved:
            names.append(identity.sym)
        matched_paths = _names_match_impact(names, impact_names)
        sites: dict[str, None] = {}
        for obs in hits:
            sites.update(obs.sites)
        symbols.append(
            SymbolStatus(
                target=target,
                dep=s["dep"],
                status="confirmed" if hits else "blind",
                callers=sorted(s["callers"]),
                files=sorted(s["files"]),
                observed_at=list(sites),
                contact_kinds=sorted(s["kinds"]),
                impacted=bool(matched_paths) if impact_filter_active else None,
                matched_impact_paths=matched_paths,
                sym=identity.sym if identity is not None else None,
                identity_kind=identity.kind if identity is not None else None,
                identity_error=identity.error if identity is not None else None,
                contact_class=klass,
                matched_by=(
                    ("exact" if target in view.q_names else "canonical") if hits else None
                ),
                observed_as=sorted(obs.name for obs in hits)[:20],
            )
        )
    for obs in sorted(unmatched, key=lambda o: o.name):
        dep = obs.name.split(".")[0]
        if is_stdlib(dep) or dep in _IGNORE_DEPS:
            continue  # the trace boundary may include odd entries; keep deps only
        matched_paths = _names_match_impact([obs.name, *sorted(obs.legacy)], impact_names)
        symbols.append(
            SymbolStatus(
                target=obs.name,
                dep=dep,
                status="dynamic-only",
                observed_at=list(obs.sites),
                impacted=bool(matched_paths) if impact_filter_active else None,
                matched_impact_paths=matched_paths,
            )
        )

    # Every static contact's status. A symbol never observed whose every contact is an
    # operator method answered by the operator's other method at its statement is confirmed by
    # it: Python called that one instead.
    contacts, outside = _judge_contacts(
        cmap.contacts, symbols, matched, file_map, executed, executed_files,
        traced=bool(trace), sited=bool(executed_scope), events=view.events)
    answered = _confirm_by_alternatives(symbols, contacts)
    if answered:
        sample = ", ".join(answered[:3]) + (", …" if len(answered) > 3 else "")
        report.notes.append(
            f"{len(answered)} operator method(s) never ran: at each of their statements the "
            "operator's other method (forward or reflected, e.g. __gt__ for __lt__) was "
            f"recorded instead, which confirms them ({sample})")

    symbols.sort(
        key=lambda x: (x.dep, {"blind": 0, "dynamic-only": 1, "confirmed": 2}[x.status], x.target)
    )
    report.symbols = symbols

    all_counts = _counts(symbols)
    report.all_confirmed = all_counts["confirmed"]
    report.all_blind = all_counts["blind"]
    report.all_dynamic_only = all_counts["dynamic-only"]
    report.all_static_targets = len(static) - len(report.test_runner_symbols)
    if report.test_runner_symbols:
        runner = set(report.test_runner_symbols)
        report.test_runner_contacts = len({(c.file, line, c.target, c.kind)
                                           for c in cmap.contacts if c.target in runner
                                           for line, _first, _last in
                                           (c.spans or [(c.line, c.line, c.line)])})
        names = sorted(runner)
        sample = ", ".join(names[:3]) + (", …" if len(names) > 3 else "")
        report.notes.append(
            f"{report.test_runner_contacts} contact(s) of {len(names)} test-runner symbol(s) "
            f"({sample}) are left out of the verdict, coverage and recall: they belong to the "
            "test runner, which captures do not trace, not to the project's dependencies")
    report.all_observed_targets = len(view.observations)
    report.all_by_dep = _by_dep(symbols)

    report.impacted_symbols = [symbol for symbol in symbols if symbol.impacted]
    impacted_counts = _counts(report.impacted_symbols)
    report.impacted_confirmed = impacted_counts["confirmed"]
    report.impacted_blind = impacted_counts["blind"]
    report.impacted_dynamic_only = impacted_counts["dynamic-only"]
    report.impacted_static_targets = report.impacted_confirmed + report.impacted_blind
    report.impacted_observed_targets = report.impacted_confirmed + report.impacted_dynamic_only
    report.impacted_by_dep = _by_dep(report.impacted_symbols)
    sym_of = {s.target: s.sym for s in symbols if s.status != "dynamic-only"}
    report.impacted_contacts = [
        contact
        for contact in cmap.contacts
        if _names_match_impact([contact.target, sym_of.get(contact.target) or ""], impact_names)
    ]
    report.impact_by_kind = dict(
        sorted(Counter(contact.kind for contact in report.impacted_contacts).items())
    )
    matched_impact = {path for symbol in symbols for path in symbol.matched_impact_paths}
    report.unmatched_impact_paths = [path for path in impact_paths if path not in matched_impact]

    # The headline is the whole surface whatever the focus: an applied plan's paths say what
    # to look at first, not what PASS may leave unexercised.
    report.confirmed = report.all_confirmed
    report.blind = report.all_blind
    report.dynamic_only = report.all_dynamic_only
    report.static_targets = report.all_static_targets
    report.observed_targets = report.all_observed_targets
    report.by_dep = report.all_by_dep

    # ── coverage and recall over the whole surface ───────────────────────────
    _stamp_coverage(report, contacts, executed, traced=bool(trace),
                    sited=bool(executed_scope), events=view.events)
    if outside:
        report.notes.append(
            f"{outside} recorded contact(s) lie on lines the executed record does not list "
            "there: the record is incomplete for them")
    # identity joins only: a symbol confirmed by its operator's other method was not joined
    scope = [s for s in symbols if s.status != "dynamic-only" and s.matched_by != "alternative"]

    def tier(name: str, basis: str, confirmed: int) -> IdentityTier:
        return IdentityTier(name=name, basis=basis, confirmed=confirmed, static=len(scope),
                            share=_ratio(confirmed, len(scope)))

    report.identity_tiers = [
        tier("exact", "exact string join of static path and runtime q (v1 rule)",
             sum(1 for s in scope if s.target in view.q_names)),
        tier("canonical", "canonical identity (what confirmed uses)",
             sum(1 for s in scope if s.status == "confirmed")),
    ]

    if trace and probe_python:
        from pymolt.verify.probe import probe_trace

        probes = probe_trace(trace, probe_python)
        runtime_names = {
            target: [obs.name for obs in hits] + sorted({q for obs in hits for q in obs.legacy})
            for target, hits in matched.items()
        }
        for sym in report.symbols:
            pr = probes.get(sym.target)
            for name in runtime_names.get(sym.target, ()):
                if pr is not None:
                    break
                pr = probes.get(name)
            if pr is not None:
                sym.probe_status = pr.status
                sym.probed = pr.probed_contacts > 0
        report.all_probed = sum(1 for s in report.symbols if s.probed)
        report.all_probe_changed = sum(1 for s in report.symbols if s.probe_status == "changed")
        report.impacted_probed = sum(1 for s in report.impacted_symbols if s.probed)
        report.impacted_probe_changed = sum(
            1 for s in report.impacted_symbols if s.probe_status == "changed"
        )
        report.probed = report.all_probed
        report.probe_changed = report.all_probe_changed
        report.notes.append(f"probed reconstructible contacts under {probe_python}")

    if trace and against:
        try:
            profile = ComparatorProfile(comparator_profile)
            if profile is ComparatorProfile.CUSTOM:
                _attach_boundary_diff(
                    report, compare_traces(str(against), str(trace), profile=profile,
                                           custom=custom_comparator),
                    impact_filter_active, impact_paths,
                )
            else:
                old_facts = load_facts(against, root=root)
                new_facts = load_facts(trace, root=root)
                fdiff = diff_facts(
                    old_facts, new_facts,
                    profile=DiffProfile.SHAPE if profile is ComparatorProfile.SHAPE
                    else DiffProfile.EXACT,
                    context=diff_context,
                    order_causes=_order_causes(root, old_facts, new_facts, report.notes),
                )
                if noise_profile is not None:
                    from pymolt.verify.replicate import apply_noise_profile, timing_difference

                    apply_noise_profile(fdiff, dict(noise_profile[0]), noise_profile[1],
                                        timing=timing_difference(old_facts, new_facts))
                from pymolt.verify.accepts import apply_accepts, stamp_findings

                stamp_findings(fdiff)
                accepts = getattr(diff_context, "accepts", None)
                report.notes.extend(apply_accepts(fdiff, accepts))
                report._fact_diff, report._accepts = fdiff, accepts
                _attach_fact_diff(report, fdiff, impact_filter_active, impact_paths)
                if predictions is not None:
                    report.calibration = _calibration(predictions, old_facts, fdiff, root,
                                                      diff_context)
        except (OSError, ValueError) as e:
            report.notes.append(f"version diff failed: {e}")

    return report


_UNOBSERVABLE_NOTES = {
    "existence": "attribute reads, bases or constants of symbols no capture observed: reading "
                 "a name runs no dependency code there, so no call can record them",
    "returned-value": "chains through a function's return value (e.g. typer.prompt().strip): "
                      "statically unnameable, so neither BLIND nor confirmable",
    "absent-dependency": "target a package that is not installed in the capture's "
                         "interpreter, so no trace there can exercise them",
    "interpreter": "resolve to interpreter code, not dependency code",
}


def _judge_contacts(
    cmap_contacts: Iterable[Contact], symbols: Iterable[SymbolStatus],
    matched: Mapping[str, list[_Observation]], file_map: Mapping[str, str],
    executed: _Executed | None, executed_files: Collection[str], *, traced: bool, sited: bool,
    events: int,
) -> tuple[list[ContactStatus], int]:
    """Every static contact's status, and how many recorded contacts the executed record
    does not list as run (it is incomplete there).

    * a contact of a symbol in ``UNOBSERVABLE_CLASSES`` is ``unobservable`` (that class);
    * ``recorded``: the trace has a call of the contact's symbol (canonical identity) whose
      site lies on the contact's statement, in its file;
    * an existence contact (attribute read, base, constant) runs no dependency code at its
      site: of a symbol the trace observed elsewhere it is ``recorded`` (matched by symbol)
      when its line ran, ``not-run`` when it did not; of one never observed, ``unobservable``;
    * otherwise ``ran-not-recorded`` when its statement ran, else ``not-run``. "Ran": a line
      of the statement in the executed record (granularity line), a line of a code object
      that started (function), else a file the trace recorded a call site in (file);
    * an operator's method and its alternatives at the same statement (``_fold_alternatives``)
      count once: either recorded there makes the other ``recorded`` (matched by
      ``alternative``).
    """
    by_target = {s.target: s for s in symbols if s.status != "dynamic-only"}
    recorded_at: dict[str, dict[str, set[int]]] = {}
    for target, hits in matched.items():
        for obs in hits:
            for raw, line in obs.site_lines:
                rel = file_map.get(raw)
                if rel is not None:
                    recorded_at.setdefault(target, {}).setdefault(rel, set()).add(line)
    ex_lines: dict[str, set[int]] = {}
    ex_functions: dict[str, set[int]] = {}
    if executed is not None:
        for into, source in ((ex_lines, executed.lines), (ex_functions, executed.functions)):
            for raw, lines in source.items():
                rel = file_map.get(raw)
                if rel is not None:
                    into.setdefault(rel, set()).update(lines)

    def hit(lines: set[int] | None, first: int | None, last: int | None) -> bool:
        return bool(lines) and first is not None and any(
            n in lines for n in range(first, (last or first) + 1))

    def ran(file: str | None, first: int | None, last: int | None) -> tuple[bool | None, str]:
        if executed is not None and file is not None:
            if hit(ex_lines.get(file), first, last):
                return True, "line"
            if hit(ex_functions.get(file), first, last):
                return True, "function"
            if file in ex_lines:
                return False, "line"
            if file in ex_functions:
                return False, "function"
        if not sited:
            if executed is not None:
                return False, executed.coarsest or "line"
            return (None, "symbol") if events else (False, "file")
        if file in executed_files:
            return True, "file"     # the trace recorded calls here; no executed record says more
        return False, (executed.coarsest or "line") if executed is not None else "file"

    out: dict[tuple, ContactStatus] = {}
    outside = 0
    for c in cmap_contacts:
        sym = by_target.get(c.target)
        if sym is None:
            continue
        spans = c.spans or [(c.line, c.line, c.line)]
        for line, first, last in spans:
            key = (c.file, line, c.target, c.kind)
            if key in out:
                # One place, two contacts of one symbol (``x + y`` and ``x + 1`` on a line): a
                # contact of its own needs its own record, so it wins over an alternative.
                kept = out[key]
                kept.alternatives = (sorted({*kept.alternatives, *c.alternatives})
                                     if kept.alternatives and c.alternatives else [])
                continue
            status = ContactStatus(file=c.file, line=line, target=c.target, kind=c.kind,
                                   caller=c.caller, status=NOT_RUN,
                                   span=(first, last) if first is not None else None,
                                   alternatives=list(c.alternatives))
            out[key] = status
            klass = sym.contact_class
            if klass in UNOBSERVABLE_CLASSES:
                status.status, status.why = UNOBSERVABLE, klass
                continue
            if not traced:
                if contact_class(c.kind) == "existence":
                    status.status, status.why = UNOBSERVABLE, "existence"
                continue
            if sited and hit((recorded_at.get(c.target) or {}).get(c.file or ""), first, last):
                status.status, status.matched_by = RECORDED, "site"
                if (executed is not None and (c.file in ex_lines or c.file in ex_functions)
                        and not hit(ex_lines.get(c.file), first, last)
                        and not hit(ex_functions.get(c.file), first, last)):
                    outside += 1
                continue
            has_run, granularity = ran(c.file, first, last)
            if contact_class(c.kind) == "existence":
                if sym.status != "confirmed":
                    status.status, status.why = UNOBSERVABLE, "existence"
                    continue
                status.granularity = granularity
                if has_run is not False:
                    status.status, status.matched_by = RECORDED, "symbol"
                continue
            status.granularity = granularity
            if has_run is None:         # no call sites: the symbol is all there is
                if sym.status == "confirmed":
                    status.status, status.matched_by = RECORDED, "symbol"
            elif has_run:
                status.status = RAN_NOT_RECORDED
    _fold_alternatives(list(out.values()))
    return list(out.values()), outside


#: Contact statuses, best evidence first: one operator at one statement takes its best method's.
_RANK = {RECORDED: 3, RAN_NOT_RECORDED: 2, NOT_RUN: 1, UNOBSERVABLE: 0}


def _fold_alternatives(contacts: list[ContactStatus]) -> None:
    """One operator at one statement counts once: its forward method on one operand and the
    reflected one on the other (``Contact.alternatives``) are alternatives, since Python calls
    the reflected one only when the forward one returns NotImplemented.

    A contact with alternatives present at its statement (same file and span) is counted with
    the best of them when that one ranks higher (``_RANK``, then a contact of its own, then a
    fixed order), and is ``recorded`` (matched by ``alternative``) when that one was recorded by
    its own evidence. A contact without alternatives keeps its own status and count. Decided
    from the statuses before any change, so the order of the contacts does not matter."""
    at: dict[tuple, list[ContactStatus]] = {}
    for c in contacts:
        if c.kind == "protocol":
            at.setdefault((c.file, c.span, c.target), []).append(c)

    def order(c: ContactStatus) -> tuple:
        return _RANK[c.status], not c.alternatives, c.target, c.line or 0

    decided: list[tuple[ContactStatus, ContactStatus]] = []
    for c in contacts:
        if not c.alternatives:
            continue
        partners = [p for target in c.alternatives for p in at.get((c.file, c.span, target), ())
                    if p is not c]
        if partners:
            best = max(partners, key=order)
            if order(best) > order(c):
                decided.append((c, best))
    recorded = {id(best) for _c, best in decided if best.status == RECORDED}
    for c, best in decided:
        c.counted_with = best.target
        if id(best) in recorded and c.status != RECORDED:
            c.status, c.matched_by, c.granularity, c.why = RECORDED, "alternative", None, None


def _confirm_by_alternatives(symbols: Iterable[SymbolStatus],
                             contacts: Iterable[ContactStatus]) -> list[str]:
    """Confirm each BLIND symbol whose every contact was recorded by its operator's other
    method (``_fold_alternatives``): it never ran because that one did. Returns their targets."""
    by_target: dict[str, list[ContactStatus]] = {}
    for c in contacts:
        by_target.setdefault(c.target, []).append(c)
    answered = []
    for sym in symbols:
        mine = by_target.get(sym.target)
        if sym.status == "blind" and mine and all(
                c.status == RECORDED and c.matched_by == "alternative" for c in mine):
            sym.status, sym.matched_by = "confirmed", "alternative"
            answered.append(sym.target)
    return sorted(answered)


def _coverage_ratios(contacts: Iterable[ContactStatus]) -> tuple[float | None, float | None]:
    counts = Counter(c.status for c in contacts if c.counted_with is None)
    reached = counts[RECORDED] + counts[RAN_NOT_RECORDED]
    observable = reached + counts[NOT_RUN]
    return (reached / observable if observable else None,
            counts[RECORDED] / reached if reached else None)


def _stamp_coverage(report: ContractReport, contacts: list[ContactStatus],
                    executed: _Executed | None, *, traced: bool, sited: bool,
                    events: int) -> None:
    """Coverage, recall, their granularity, the BLIND split and the notes, from the contacts.
    The counts are of contacts counted on their own: an operator's alternative counted with the
    other method at its statement is listed (``contacts``) but not counted again."""
    report.contacts = contacts
    counted = [c for c in contacts if c.counted_with is None]
    counts = Counter(c.status for c in counted)
    report.contacts_total = len(counted)
    report.contacts_recorded = counts[RECORDED]
    report.contacts_ran_not_recorded = counts[RAN_NOT_RECORDED]
    report.contacts_not_run = counts[NOT_RUN]
    report.contacts_unobservable = counts[UNOBSERVABLE]
    report.unobservable_by_reason = dict(sorted(Counter(
        c.why for c in counted if c.status == UNOBSERVABLE and c.why).items()))
    report.coverage, report.recall = _coverage_ratios(contacts)
    decided = Counter(c.granularity for c in counted if c.granularity)
    report.granularity_counts = {g: decided[g] for g in GRANULARITIES if decided[g]}
    if decided:
        report.coverage_granularity = max(decided, key=GRANULARITIES.index)
    elif executed is not None:
        report.coverage_granularity = executed.granularity
    elif traced:
        report.coverage_granularity = "file" if sited or not events else "symbol"
    report.executed_complete = executed.complete if executed is not None else None

    by_target: dict[str, list[ContactStatus]] = {}
    for c in contacts:
        by_target.setdefault(c.target, []).append(c)
    for sym in report.symbols:
        mine = by_target.get(sym.target)
        if not mine:
            continue
        sym.contact_status = dict(sorted(Counter(c.status for c in mine).items()))
        if sym.status == "blind":
            statuses = {c.status for c in mine}
            sym.blind_reason = (RAN_NOT_RECORDED if RAN_NOT_RECORDED in statuses
                                else NOT_RUN if NOT_RUN in statuses else UNOBSERVABLE)
    blind = Counter(s.blind_reason for s in report.symbols if s.status == "blind")
    report.blind_not_run = blind[NOT_RUN]
    report.blind_ran_not_recorded = blind[RAN_NOT_RECORDED]
    report.blind_unobservable = blind[UNOBSERVABLE]
    impacted = {s.target for s in report.impacted_symbols}
    if impacted:
        # an impacted contact counted with another method at its statement counts by that one
        stands_for = {(c.file, c.span, c.counted_with) for c in contacts
                      if c.target in impacted and c.counted_with}
        report.impacted_coverage, report.impacted_recall = _coverage_ratios(
            c for c in contacts
            if c.target in impacted or (c.file, c.span, c.target) in stands_for)

    for why, text in _UNOBSERVABLE_NOTES.items():
        members = sorted({c.target for c in counted if c.why == why})
        if members:
            n = sum(1 for c in counted if c.why == why)
            sample = ", ".join(members[:3]) + (", …" if len(members) > 3 else "")
            report.notes.append(f"{n} contact(s) {text} ({sample}): unobservable, left out of "
                                "coverage and recall")
    if not traced:
        return
    if executed is None:
        report.notes.append(
            "the capture has no executed-code record (an older runtime, a watch-mode capture, "
            "or PYMOLT_TRACE_EXECUTED=0): a contact counts as run when the trace recorded a "
            "call site in its file (granularity file)")
    else:
        note = (f"executed-code record: granularity {executed.granularity}, from "
                f"{executed.processes} process(es)")
        if executed.without:
            note += (f"; {executed.without} process(es) have none, so their files count by "
                     "the call sites the trace recorded there (granularity file)")
        if not executed.complete and not executed.without:
            note += ("; it stops before the end of the run (the discover -> watch switch, or a "
                     "cap): code first run after that reads as not run")
        report.notes.append(note)


def _capture_python(fs: FactSet) -> str | None:
    """The one Python X.Y a capture's processes ran (None: unknown or mixed)."""
    minors = {".".join(v.split(".")[:2]) for v in fs.python_versions if v}
    return minors.pop() if len(minors) == 1 else None


def _order_causes(root: Path, old_facts: FactSet, new_facts: FactSet,
                  notes: list[str]) -> list[OrderCause]:
    """Known causes of dict key order changes between the Pythons the two captures ACTUALLY
    ran (not what setup intended): the project's TypedDicts whose annotation order 3.9
    changed and that it iterates. An order finding is cited only on an exact match."""
    base, target = _capture_python(old_facts), _capture_python(new_facts)
    if not (base and target):
        return []
    try:
        from pymolt.pyversion.behavior import predicted_order_causes

        found = predicted_order_causes(root, base, target)
    except Exception as e:  # a detector failure must not cost the diff; say so
        notes.append(f"dict order causes not computed: {e}")
        return []
    return [OrderCause(keys=frozenset(c["keys"]), old=tuple(c["old"]), new=tuple(c["new"]),
                       cause=c["cause"], detail=c["detail"]) for c in found]


def _calibration(predictions: Mapping[str, Any], old_facts: FactSet, fdiff: FactDiff,
                 root: Path, diff_context=None) -> Calibration:
    """Score the predictions; ``risks: None`` means "predict from the baseline side now"
    (its runtime warnings, no delta facts), with ``changes`` for fixed-by-plan. With the two
    captures' sources (``diff_context``), a fix is credited by statement to the step a risk is
    linked to (``workorder.credit``)."""
    risks = predictions.get("risks")
    notes = list(predictions.get("notes") or ())
    if risks is None:
        made = predict_risks(old_facts, (), changes=predictions.get("changes") or (),
                             root=root)
        risks = made.risks
        notes.extend(made.notes)
    links = None
    if diff_context is not None and diff_context.old_sources is not None \
            and diff_context.new_sources is not None:
        try:
            from pymolt.verify.predict import PredictedRisk
            from pymolt.workorder.coverage import setup_pythons
            from pymolt.workorder.credit import step_links

            items = [r if isinstance(r, PredictedRisk) else PredictedRisk.model_validate(r)
                     for r in risks or ()]
            base, target = setup_pythons(root)
            links = step_links(root, items, old_sources=diff_context.old_sources,
                               new_sources=diff_context.new_sources, python_old=base,
                               python_new=target)
        except Exception as exc:  # noqa: BLE001 - calibration degrades to plan-time fixes
            notes.append(f"fixes not credited by step ({type(exc).__name__}): the plan's "
                         "fixed-by-plan decides")
            links = None
    cal = calibrate(risks, fdiff, source=predictions.get("source") or "plan",
                    plan_id=predictions.get("plan_id"), links=links)
    cal.notes[:0] = notes
    return cal


def _attach_boundary_diff(
    report: ContractReport, bdiff: BoundaryDiff, impact_filter_active: bool,
    impact_paths: list[str],
) -> None:
    """Legacy (custom comparator) path: a BoundaryDiff with counts only."""
    report.all_diff = bdiff.counts()
    report.all_diff_clean = bdiff.is_clean()
    if impact_filter_active:
        impact_diff = _filter_diff(bdiff, impact_paths)
        report.diff = impact_diff.counts()
        report.diff_clean = impact_diff.is_clean()
        report.impact_diff_details = {
            "disappeared": impact_diff.disappeared,
            "result_changed": impact_diff.result_changed,
            "raise_changed": impact_diff.raise_changed,
            "appeared": impact_diff.appeared,
            "skipped_opaque": impact_diff.skipped_opaque,
        }
    else:
        report.diff = report.all_diff
        report.diff_clean = report.all_diff_clean


def _attach_fact_diff(
    report: ContractReport, fdiff: FactDiff, impact_filter_active: bool,
    impact_paths: list[str],
) -> None:
    """Fact-level diff: verdict input plus every non-equal fact, named and located.

    ``diff``/``all_diff`` keep the BoundaryDiff count shape existing consumers read; the
    fact categories and entries live in ``fact_diff``/``all_fact_diff``. An impact filter
    keeps entries whose symbol is an impacted API — matched by public path or by the
    canonical symbol the static side resolved it to. ``fact_equal_symbols`` is always the
    whole diff: every dynamic-only symbol must be compared equal for PASS, in the plan or not.
    """
    report.all_fact_diff = fdiff.to_json_dict()
    report.all_diff = fdiff.to_boundary_diff().counts()
    report.all_diff_clean = fdiff.is_clean()
    report.fact_equal_symbols = _compared_equal_symbols(fdiff)
    if not impact_filter_active:
        report.fact_diff = report.all_fact_diff
        report.diff = report.all_diff
        report.diff_clean = report.all_diff_clean
        return
    impacted_syms = {s.sym for s in report.impacted_symbols if s.sym}

    def impacted(name: str | None) -> bool:
        if not name:
            return False
        if _matches_impact(name, impact_paths):
            return True
        return any(name == sym or name.startswith(sym + ".") for sym in impacted_syms)

    scoped = fdiff.model_copy(update={
        "entries": [e for e in fdiff.entries if impacted(e.symbol) or impacted(e.new_symbol)],
    })
    report.fact_diff = scoped.to_json_dict()
    kept = {id(e) for e in scoped.entries}
    from pymolt.verify.fact_report import cap_entry

    report.unpredicted_changes = [
        cap_entry(e.model_dump(mode="json", exclude_none=True))
        for e in fdiff.entries if id(e) not in kept and e.fails]
    bdiff = scoped.to_boundary_diff()
    report.diff = bdiff.counts()
    report.diff_clean = scoped.is_clean()
    report.impact_diff_details = {
        "disappeared": bdiff.disappeared,
        "result_changed": bdiff.result_changed,
        "raise_changed": bdiff.raise_changed,
        "appeared": bdiff.appeared,
        "skipped_opaque": bdiff.skipped_opaque,
    }


def _unresolvable_class(error: str, dep: str) -> str | None:
    """Why a behavioural static path cannot be resolved, when that reason makes it
    unobservable rather than BLIND.

    ``returned-value``: the contact map extends a dotted name through a call
    (``typer.prompt(...).strip()`` -> ``typer.prompt.strip``); the attribute lives on the
    function's return value, which the static side cannot name.
    ``absent-dependency``: the dependency itself is not importable where the capture ran
    (an optional integration guarded by ``try/except ImportError``).
    """
    if error.startswith("AttributeError: 'function' object has no attribute") or error.startswith(
        "AttributeError: 'builtin_function_or_method' object has no attribute"
    ):
        return "returned-value"
    if error.startswith("ModuleNotFoundError") and f"No module named '{dep}'" in error:
        return "absent-dependency"
    return None


def _compared_equal_symbols(fdiff: FactDiff) -> list[str]:
    """Symbols whose every fact was compared on both sides and found equal.

    Equal, DRIFT (only the library's defaults moved) and REWRITTEN-with-equal-outcome all
    count as compared-equal; any other entry for a symbol — changed, vanished, appeared,
    uncomparable, unpaired — disqualifies it.
    """
    settled: set[str] = set()
    unsettled: set[str] = set()
    for entry in fdiff.entries:
        names = {entry.symbol, *([entry.new_symbol] if entry.new_symbol else [])}
        equal = entry.comparison is Comparison.EQUAL and entry.category in (
            FactCategory.EQUAL, FactCategory.DRIFT, FactCategory.REWRITTEN,
        )
        (settled if equal else unsettled).update(names)
    return sorted(settled - unsettled)
