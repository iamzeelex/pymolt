"""The unified contract report — what we KNOW about the boundary, honestly.

Merges the two axes we locked:

* **coverage of the contract** — the static contact map (the denominator: every
  dependency symbol our code *could* call) vs a dynamic trace (the numerator:
  what actually ran). Each symbol is ``confirmed`` (static ∧ observed),
  ``blind`` (static, never observed → a sandbox candidate), or ``dynamic-only``
  (observed but the static map missed it → static under-approximation, e.g. via
  ``getattr``/monkeypatching).
* **identity** — static paths are public import paths (``requests.Request``), the
  runtime records defining names (``sym`` = ``requests.models.Request.__init__``;
  v1 traces only ``q`` = ``<frame module>.<co_name>``). Static paths are resolved
  in the capture's interpreter (:mod:`pymolt.verify.identity_resolver`); a runtime
  name confirms a static path when it is the resolved symbol, a method of a
  resolved class, or a legacy name unique in the defining module. Unresolvable
  paths keep the exact-string rule and are counted as such.
* **trust** = confirmed / static over *behavioural* contacts (call, decorator,
  context-manager, protocol) in files the trace executed — existence contacts
  (attribute reads, bases, constants) are proven by an import, not by a trace,
  and code the workload never ran cannot be blamed on the trace. The tiers
  (exact-join → canonical → behavioural → executed) stay in ``trust_tiers`` so
  every step is auditable; ``all_*`` keeps the full-surface totals.
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

from pydantic import BaseModel, Field

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
from pymolt.verify.models import BoundaryDiff, VerificationVerdict
from pymolt.verify.predict import Calibration, calibrate, predict_risks

logger = logging.getLogger(__name__)

TRUST_BASIS = "behavioural contacts in executed files"
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
    probed: bool = False  # re-invoked in the sandbox under the target version
    probe_status: str | None = None  # stable | changed | error | opaque-inputs
    # Static identity, resolved in the capture's interpreter (static symbols only).
    sym: str | None = None  # canonical "<defining module>.<qualname>"
    identity_kind: str | None = None  # function | class | module | other
    identity_error: str | None = None  # why the path could not be resolved
    # behavioural | existence | interpreter (a behavioural contact that resolves to
    # builtins/stdlib code, e.g. ``type.__or__`` for ``A | B`` — not dependency code) |
    # returned-value (a chain through a call result) | absent-dependency (not installed)
    contact_class: str | None = None
    executed: bool | None = None  # a behavioural contact sits in a file the trace ran
    matched_by: str | None = None  # exact (v1 string join) | canonical
    observed_as: list[str] = Field(default_factory=list)  # runtime names that confirmed it


class TrustTier(BaseModel):
    """One step from the old exact-string join to the headline trust."""

    name: str  # exact | canonical | behavioural | executed
    basis: str
    confirmed: int = 0
    static: int = 0
    trust: float = 0.0


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
    # Headline: confirmed / static over ``trust_basis`` (behavioural contacts in
    # files the trace executed), within the evaluation scope above.
    trust: float = 0.0
    trust_basis: str = TRUST_BASIS
    trust_confirmed: int = 0
    trust_static: int = 0
    # The same scope, tier by tier: exact-join (the v1 string rule) → canonical
    # identity → behavioural contacts only → executed files only (= trust).
    exact_trust: float = 0.0
    canonical_trust: float = 0.0
    behavioural_trust: float = 0.0
    executed_trust: float = 0.0
    trust_tiers: list[TrustTier] = Field(default_factory=list)
    # Project files holding contacts that the trace executed; ``executed_scope``
    # is False when the trace carries no usable call sites (scope = all files).
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
    # Fact-level version diff (schema ``pymolt.fact-diff/1``): every non-equal fact with its
    # symbol, call site and old -> new outcome. ``fact_diff`` follows the impact filter;
    # ``all_fact_diff`` is always the whole surface.
    fact_diff: dict[str, Any] | None = None
    all_fact_diff: dict[str, Any] | None = None
    # Failing entries of the whole surface that the impact filter hid from ``fact_diff``:
    # behaviour changes outside the plan's predicted impacts (they still decide the verdict).
    unpredicted_changes: list[dict[str, Any]] = Field(default_factory=list)
    # Runtime symbols every one of whose facts compared equal on both sides (equal, DRIFT,
    # or REWRITTEN with an equal outcome) in the scoped fact diff. A dynamic-only symbol
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


@dataclass
class _Observation:
    """Every event recorded under one runtime name (``sym`` when present, else ``q``)."""

    name: str
    canonical: bool  # ``name`` came from a v2 ``sym``
    legacy: set[str] = field(default_factory=set)  # the v1 ``q`` spellings seen
    sites: dict[str, None] = field(default_factory=dict)  # ordered set of file:line


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


def _executed_files(
    raw_files: Iterable[str], root: Path, contact_files: Iterable[str]
) -> tuple[set[str], str | None]:
    """Contact files (as the contact map spells them) that the trace executed."""
    by_posix = {Path(f).as_posix(): f for f in contact_files}
    under_root: set[str] = set()
    outside: list[str] = []
    for raw in raw_files:
        if raw.startswith("<"):  # <string>, <frozen ...>: no file to attribute
            continue
        path = Path(raw)
        if not path.is_absolute():
            under_root.add(path.as_posix())
            continue
        rel = None
        for candidate in (path, Path(os.path.realpath(raw))):
            try:
                rel = candidate.relative_to(root).as_posix()
                break
            except ValueError:
                continue
        if rel is None:
            outside.append(raw.replace("\\", "/"))
        else:
            under_root.add(rel)
    executed = {by_posix[rel] for rel in under_root if rel in by_posix}
    if under_root or not outside:
        return executed, None
    # Every call site lies outside the project root: a container capture or a
    # moved checkout. Attribute by path suffix rather than claim nothing ran.
    for rel, original in by_posix.items():
        if any(raw.endswith("/" + rel) for raw in outside):
            executed.add(original)
    return executed, (
        "the trace's call sites lie outside the project root; executed files were "
        "matched by path suffix"
    )


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
    interpreter) re-invokes each reconstructible captured contact in the sandbox
    and records whether it stays stable or changes under that version.

    Static paths are resolved to canonical identities (one subprocess, cached
    under ``.pymolt/cache/``) in ``probe_python`` if given, else the interpreter
    the trace's metadata records, else pymolt's own — noted as such.
    ``resolve_identities=False`` keeps the exact-string join only.

    ``changed_api_paths`` narrows trust and verdict inputs to Axiom's changed
    surface while keeping ``all_*`` totals and every symbol visible. Items may
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
                "behavioural_files": set(),
            },
        )
        s["callers"].add(c.caller)
        s["kinds"].add(c.kind)
        if c.file:
            s["files"].add(c.file)
            if contact_class(c.kind) == "behavioural":
                s["behavioural_files"].add(c.file)

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
    executed_files: set[str] = set()
    executed_scope: bool | None = None
    if view.events:
        executed_files, scope_note = _executed_files(view.files, root, contact_files)
        if scope_note:
            report.notes.append(_container_path_note(scope_note, trace, root))
        executed_scope = bool(view.files - {f for f in view.files if f.startswith("<")})
        if not executed_scope:
            report.notes.append(
                "the trace records no call sites, so the executed-file scope is unknown: "
                "trust is computed over behavioural contacts in every file"
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
    symbols: list[SymbolStatus] = []
    for target, s in static.items():
        hits = matched[target]
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
            # trust denominator.
            if unobservable == "absent-dependency" and (
                report.identity_source == "host" or s["dep"] in observed_tops
            ):
                unobservable = None
            klass = unobservable or klass
        if (klass == "behavioural" and s["dep"].split(".")[0] in TEST_RUNNER_PACKAGES
                and not hits and not observed_tops & TEST_RUNNER_PACKAGES):
            # the capture does not trace the test runner (unless a target names one of the
            # family, which lifts the exclusion for all of it), so its contacts can never be
            # confirmed; counting them would lower trust for nothing
            klass = "test-runner"
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
                executed=(
                    bool(s["behavioural_files"] & executed_files) if executed_scope else None
                ),
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

    symbols.sort(
        key=lambda x: (x.dep, {"blind": 0, "dynamic-only": 1, "confirmed": 2}[x.status], x.target)
    )
    report.symbols = symbols

    all_counts = _counts(symbols)
    report.all_confirmed = all_counts["confirmed"]
    report.all_blind = all_counts["blind"]
    report.all_dynamic_only = all_counts["dynamic-only"]
    report.all_static_targets = len(static)
    report.all_observed_targets = len(view.observations)
    report.all_trust = _ratio(report.all_confirmed, report.all_static_targets)
    report.all_by_dep = _by_dep(symbols)

    report.impacted_symbols = [symbol for symbol in symbols if symbol.impacted]
    impacted_counts = _counts(report.impacted_symbols)
    report.impacted_confirmed = impacted_counts["confirmed"]
    report.impacted_blind = impacted_counts["blind"]
    report.impacted_dynamic_only = impacted_counts["dynamic-only"]
    report.impacted_static_targets = report.impacted_confirmed + report.impacted_blind
    report.impacted_observed_targets = report.impacted_confirmed + report.impacted_dynamic_only
    report.impacted_trust = _ratio(report.impacted_confirmed, report.impacted_static_targets)
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

    if impact_filter_active:
        report.confirmed = report.impacted_confirmed
        report.blind = report.impacted_blind
        report.dynamic_only = report.impacted_dynamic_only
        report.static_targets = report.impacted_static_targets
        report.observed_targets = report.impacted_observed_targets
        report.by_dep = report.impacted_by_dep
        if report.impacted_static_targets < report.all_static_targets:
            pkgs = sorted({sym.dep for sym in report.impacted_symbols if sym.dep})
            report.notes.append(
                f"static contacts narrowed to the plan's packages ({', '.join(pkgs) or 'none'}): "
                f"{report.impacted_static_targets} of {report.all_static_targets} kept; "
                "trust is computed over the narrowed set"
            )
    else:
        report.confirmed = report.all_confirmed
        report.blind = report.all_blind
        report.dynamic_only = report.all_dynamic_only
        report.static_targets = report.all_static_targets
        report.observed_targets = report.all_observed_targets
        report.by_dep = report.all_by_dep

    # ── trust tiers over the evaluation scope ────────────────────────────────
    scope = [
        s
        for s in (report.impacted_symbols if impact_filter_active else symbols)
        if s.status != "dynamic-only"
    ]
    behavioural_scope = [s for s in scope if s.contact_class == "behavioural"]
    for klass, why in (
        ("returned-value", "chain through a function's return value (e.g. typer.prompt().strip): "
                           "statically unnameable, so neither BLIND nor confirmable"),
        ("absent-dependency", "target a package that is not installed in the capture's "
                              "interpreter, so no trace there can exercise them"),
        ("test-runner", "belong to the test runner, which captures do not trace"),
    ):
        excluded = [s.target for s in scope if s.contact_class == klass]
        if excluded:
            sample = ", ".join(excluded[:3]) + (", …" if len(excluded) > 3 else "")
            report.notes.append(
                f"{len(excluded)} behavioural contact(s) {why} ({sample}); left out of trust"
            )
    interpreter = [s.target for s in scope if s.contact_class == "interpreter"]
    if interpreter:
        sample = ", ".join(interpreter[:3]) + (", …" if len(interpreter) > 3 else "")
        report.notes.append(
            f"{len(interpreter)} behavioural contact(s) resolve to interpreter code, not "
            f"dependency code ({sample}); they are left out of trust"
        )
    executed_basis = TRUST_BASIS
    if executed_scope:
        executed_scope_symbols = [s for s in behavioural_scope if s.executed]
    else:
        executed_scope_symbols = behavioural_scope
        if trace:
            executed_basis = "behavioural contacts (the trace records no call sites)"

    def tier(name: str, basis: str, members: list[SymbolStatus], exact: bool = False) -> TrustTier:
        if exact:
            confirmed = sum(1 for s in members if s.target in view.q_names)
        else:
            confirmed = sum(1 for s in members if s.status == "confirmed")
        return TrustTier(
            name=name,
            basis=basis,
            confirmed=confirmed,
            static=len(members),
            trust=_ratio(confirmed, len(members)),
        )

    report.trust_tiers = [
        tier("exact", "exact string join of static path and runtime q (v1 rule)", scope, True),
        tier("canonical", "canonical identity, every static contact", scope),
        tier("behavioural", "canonical identity, behavioural contacts", behavioural_scope),
        tier("executed", executed_basis, executed_scope_symbols),
    ]
    exact_tier, canonical_tier, behavioural_tier, executed_tier = report.trust_tiers
    report.exact_trust = exact_tier.trust
    report.canonical_trust = canonical_tier.trust
    report.behavioural_trust = behavioural_tier.trust
    report.executed_trust = executed_tier.trust
    report.trust = executed_tier.trust
    report.trust_basis = executed_tier.basis
    report.trust_confirmed = executed_tier.confirmed
    report.trust_static = executed_tier.static

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
        if impact_filter_active:
            report.probed = report.impacted_probed
            report.probe_changed = report.impacted_probe_changed
        else:
            report.probed = report.all_probed
            report.probe_changed = report.all_probe_changed
        report.notes.append(f"sandbox-probed reconstructible contacts under {probe_python}")

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
                    from pymolt.verify.replicate import apply_noise_profile

                    apply_noise_profile(fdiff, dict(noise_profile[0]), noise_profile[1])
                _attach_fact_diff(report, fdiff, impact_filter_active, impact_paths)
                if predictions is not None:
                    report.calibration = _calibration(predictions, old_facts, fdiff, root)
        except (OSError, ValueError) as e:
            report.notes.append(f"version diff failed: {e}")

    return report


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
                 root: Path) -> Calibration:
    """Score the predictions; ``risks: None`` means "predict from the baseline side now"
    (its runtime warnings, no delta facts), with ``changes`` for fixed-by-plan."""
    risks = predictions.get("risks")
    notes = list(predictions.get("notes") or ())
    if risks is None:
        made = predict_risks(old_facts, (), changes=predictions.get("changes") or (),
                             root=root)
        risks = made.risks
        notes.extend(made.notes)
    cal = calibrate(risks, fdiff, source=predictions.get("source") or "plan",
                    plan_id=predictions.get("plan_id"))
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
    canonical symbol the static side resolved it to.
    """
    report.all_fact_diff = fdiff.to_json_dict()
    report.all_diff = fdiff.to_boundary_diff().counts()
    report.all_diff_clean = fdiff.is_clean()
    if not impact_filter_active:
        report.fact_diff = report.all_fact_diff
        report.fact_equal_symbols = _compared_equal_symbols(fdiff)
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
    report.fact_equal_symbols = _compared_equal_symbols(scoped)
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
