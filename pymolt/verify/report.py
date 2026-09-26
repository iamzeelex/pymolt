"""The unified contract report — what we KNOW about the boundary, honestly.

Merges the two axes we locked:

* **coverage of the contract** — the static contact map (the denominator: every
  dependency symbol our code *could* call) vs a dynamic trace (the numerator:
  what actually ran). Each symbol is ``confirmed`` (static ∧ observed),
  ``blind`` (static, never observed → a sandbox candidate), or ``dynamic-only``
  (observed but the static map missed it → static under-approximation, e.g. via
  ``getattr``/monkeypatching). ``trust`` = confirmed / static.
* **version diff** (optional) — two traces (old vs new dependency version) folded
  into a :class:`BoundaryDiff`; behavior changes are only trustworthy over the
  area the dynamics covered.

This subsumes the old standalone ``gaps`` command: blind zones are now a section
here, not a separate tool.
"""

import json
import logging
import sys
from collections import Counter
from collections.abc import Collection, Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from pymolt.verify.comparators import ComparatorProfile, TraceComparator, compare_traces
from pymolt.verify.contact_map import _IGNORE_DEPS, Contact, build_contact_map
from pymolt.verify.models import BoundaryDiff, VerificationVerdict

logger = logging.getLogger(__name__)


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
    probed: bool = False  # re-invoked in the sandbox under the target version
    probe_status: str | None = None  # stable | changed | error | opaque-inputs


class ContractReport(BaseModel):
    root: str
    static_targets: int = 0
    observed_targets: int = 0
    confirmed: int = 0
    blind: int = 0
    dynamic_only: int = 0
    # These legacy fields describe the evaluation scope. With no impact filter
    # that is the full surface; with one it is only the impacted surface, so the
    # existing verdict fold can consume focused values unchanged.
    trust: float = 0.0  # confirmed / static_targets (0..1)
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
    verdict: VerificationVerdict = VerificationVerdict.INCONCLUSIVE
    verdict_reasons: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    # Full-surface totals are always retained, even when the legacy fields above
    # are focused on an Axiom impact set.
    all_static_targets: int = 0
    all_observed_targets: int = 0
    all_confirmed: int = 0
    all_blind: int = 0
    all_dynamic_only: int = 0
    all_trust: float = 0.0
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
    impacted_trust: float = 0.0
    impacted_probed: int = 0
    impacted_probe_changed: int = 0
    impacted_by_dep: dict[str, dict[str, int]] = Field(default_factory=dict)
    impacted_symbols: list[SymbolStatus] = Field(default_factory=list)
    impacted_contacts: list[Contact] = Field(default_factory=list)
    impact_by_kind: dict[str, int] = Field(default_factory=dict)
    unmatched_impact_paths: list[str] = Field(default_factory=list)
    impact_diff_details: dict[str, list[dict[str, Any]]] | None = None


_IMPACT_PATH_FIELDS = (
    "path",
    "replacement_path",
    "old_qualname",
    "new_qualname",
    "from_path",
    "to_path",
)


def _impact_paths(*collections: Collection[object] | None) -> tuple[bool, list[str]]:
    """Normalize strings, Axiom change models, and their serialized dictionaries."""
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


def _load_trace(path: str | Path) -> dict[str, list[str]]:
    """Map each observed dependency qualname (``q``) to its ``file:line`` sites."""
    observed: dict[str, list[str]] = {}
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError as e:
        logger.warning("Could not read trace %s: %s", path, e)
        return observed
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        q = rec.get("q")
        if not q:
            continue
        where = rec.get("where") or {}
        loc = f"{where.get('file', '?')}:{where.get('line', '?')}"
        sites = observed.setdefault(q, [])
        if loc not in sites:
            sites.append(loc)
    return observed


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
) -> ContractReport:
    """Build the unified report.

    ``trace`` is the observed dynamic recording (current/new version) used for
    coverage. ``against`` is an older recording; when both are given, the version
    diff (``against`` -> ``trace``) is attached. ``probe_python`` (a target venv
    interpreter) re-invokes each reconstructible captured contact in the sandbox
    and records whether it stays stable or changes under that version.

    ``changed_api_paths`` narrows trust and verdict inputs to Axiom's changed
    surface while keeping ``all_*`` totals and every symbol visible. Items may
    be dotted strings, serialized changes, or objects with ``path`` and optional
    ``replacement_path`` fields. ``impact_set`` is an equivalent alias useful
    to callers that already use that terminology; values from both are merged.
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
            {"dep": c.dep, "callers": set(), "files": set(), "kinds": set()},
        )
        s["callers"].add(c.caller)
        s["kinds"].add(c.kind)
        if c.file:
            s["files"].add(c.file)

    observed = _load_trace(trace) if trace else {}
    if not trace:
        report.notes.append("no trace given — every static contact is BLIND (no dynamics yet)")

    static_targets = set(static)
    observed_targets = set(observed)
    stdlib = sys.stdlib_module_names

    symbols: list[SymbolStatus] = []
    for target, s in static.items():
        status = "confirmed" if target in observed else "blind"
        matched_paths = _matches_impact(target, impact_paths)
        symbols.append(
            SymbolStatus(
                target=target,
                dep=s["dep"],
                status=status,
                callers=sorted(s["callers"]),
                files=sorted(s["files"]),
                observed_at=observed.get(target, []),
                contact_kinds=sorted(s["kinds"]),
                impacted=bool(matched_paths) if impact_filter_active else None,
                matched_impact_paths=matched_paths,
            )
        )
    for target in sorted(observed_targets - static_targets):
        dep = target.split(".")[0]
        if dep in stdlib or dep in _IGNORE_DEPS:
            continue  # the trace boundary may include odd entries; keep deps only
        matched_paths = _matches_impact(target, impact_paths)
        symbols.append(
            SymbolStatus(
                target=target,
                dep=dep,
                status="dynamic-only",
                observed_at=observed[target],
                impacted=bool(matched_paths) if impact_filter_active else None,
                matched_impact_paths=matched_paths,
            )
        )

    symbols.sort(
        key=lambda x: (x.dep, {"blind": 0, "dynamic-only": 1, "confirmed": 2}[x.status], x.target)
    )
    report.symbols = symbols

    all_counts = _counts(symbols)
    report.all_confirmed = all_counts["confirmed"]
    report.all_blind = all_counts["blind"]
    report.all_dynamic_only = all_counts["dynamic-only"]
    report.all_static_targets = len(static_targets)
    report.all_observed_targets = len(observed_targets)
    report.all_trust = (
        report.all_confirmed / report.all_static_targets if report.all_static_targets else 0.0
    )
    report.all_by_dep = _by_dep(symbols)

    report.impacted_symbols = [symbol for symbol in symbols if symbol.impacted]
    impacted_counts = _counts(report.impacted_symbols)
    report.impacted_confirmed = impacted_counts["confirmed"]
    report.impacted_blind = impacted_counts["blind"]
    report.impacted_dynamic_only = impacted_counts["dynamic-only"]
    report.impacted_static_targets = report.impacted_confirmed + report.impacted_blind
    report.impacted_observed_targets = report.impacted_confirmed + report.impacted_dynamic_only
    report.impacted_trust = (
        report.impacted_confirmed / report.impacted_static_targets
        if report.impacted_static_targets
        else 0.0
    )
    report.impacted_by_dep = _by_dep(report.impacted_symbols)
    report.impacted_contacts = [
        contact for contact in cmap.contacts if _matches_impact(contact.target, impact_paths)
    ]
    report.impact_by_kind = dict(
        sorted(Counter(contact.kind for contact in report.impacted_contacts).items())
    )
    report.unmatched_impact_paths = [
        path
        for path in impact_paths
        if not any(_matches_impact(symbol.target, [path]) for symbol in symbols)
    ]

    if impact_filter_active:
        report.confirmed = report.impacted_confirmed
        report.blind = report.impacted_blind
        report.dynamic_only = report.impacted_dynamic_only
        report.static_targets = report.impacted_static_targets
        report.observed_targets = report.impacted_observed_targets
        report.trust = report.impacted_trust
        report.by_dep = report.impacted_by_dep
    else:
        report.confirmed = report.all_confirmed
        report.blind = report.all_blind
        report.dynamic_only = report.all_dynamic_only
        report.static_targets = report.all_static_targets
        report.observed_targets = report.all_observed_targets
        report.trust = report.all_trust
        report.by_dep = report.all_by_dep

    if trace and probe_python:
        from pymolt.verify.probe import probe_trace

        probes = probe_trace(trace, probe_python)
        for sym in report.symbols:
            pr = probes.get(sym.target)
            if pr is not None:
                sym.probe_status = pr.status
                sym.probed = pr.probed_contacts > 0
        report.all_probed = sum(1 for s in report.symbols if s.probed)
        report.all_probe_changed = sum(1 for s in report.symbols if s.probe_status == "changed")
        report.impacted_probed = sum(1 for s in report.impacted_symbols if s.probed)
        report.impacted_probe_changed = sum(
            1 for s in report.impacted_symbols if s.probe_status == "changed"
        )
        if impact_filter_active:
            report.probed = report.impacted_probed
            report.probe_changed = report.impacted_probe_changed
        else:
            report.probed = report.all_probed
            report.probe_changed = report.all_probe_changed
        report.notes.append(f"sandbox-probed reconstructible contacts under {probe_python}")

    if trace and against:
        try:
            bdiff = compare_traces(
                str(against),
                str(trace),
                profile=comparator_profile,
                custom=custom_comparator,
            )
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
        except (OSError, ValueError) as e:
            report.notes.append(f"version diff failed: {e}")

    return report
