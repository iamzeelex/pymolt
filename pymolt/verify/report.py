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
from pathlib import Path

from pydantic import BaseModel, Field

from pymolt.verify.contact_map import _IGNORE_DEPS, build_contact_map
from pymolt.verify.diff import build_boundary_diff

logger = logging.getLogger(__name__)


class SymbolStatus(BaseModel):
    target: str                  # dependency qualname, e.g. "flask.cli.with_appcontext"
    dep: str
    status: str                  # confirmed | blind | dynamic-only
    callers: list[str] = Field(default_factory=list)      # our functions (static)
    files: list[str] = Field(default_factory=list)
    observed_at: list[str] = Field(default_factory=list)  # file:line from the trace
    probed: bool = False         # re-invoked in the sandbox under the target version
    probe_status: str | None = None  # stable | changed | error | opaque-inputs


class ContractReport(BaseModel):
    root: str
    static_targets: int = 0
    observed_targets: int = 0
    confirmed: int = 0
    blind: int = 0
    dynamic_only: int = 0
    trust: float = 0.0           # confirmed / static_targets (0..1)
    probed: int = 0              # symbols re-invoked under the target version
    probe_changed: int = 0       # of those, how many changed behavior
    by_dep: dict[str, dict[str, int]] = Field(default_factory=dict)
    symbols: list[SymbolStatus] = Field(default_factory=list)
    diff: dict[str, int] | None = None   # BoundaryDiff.counts() when two traces given
    diff_clean: bool | None = None
    # True when a capture was taken against a different manifest/config than the
    # one on disk now: the verdict below still describes the old world. False
    # means checked-and-current; None means there was nothing to check against.
    baseline_stale: bool | None = None
    notes: list[str] = Field(default_factory=list)


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
) -> ContractReport:
    """Build the unified report.

    ``trace`` is the observed dynamic recording (current/new version) used for
    coverage. ``against`` is an older recording; when both are given, the version
    diff (``against`` -> ``trace``) is attached. ``probe_python`` (a target venv
    interpreter) re-invokes each reconstructible captured contact in the sandbox
    and records whether it stays stable or changes under that version.
    """
    root = Path(project_dir).resolve()
    report = ContractReport(root=str(root))

    cmap = build_contact_map(root, dependencies)
    report.notes.extend(cmap.notes)

    static: dict[str, dict] = {}
    for c in cmap.contacts:
        s = static.setdefault(c.target, {"dep": c.dep, "callers": set(), "files": set()})
        s["callers"].add(c.caller)
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
        symbols.append(SymbolStatus(
            target=target, dep=s["dep"], status=status,
            callers=sorted(s["callers"]), files=sorted(s["files"]),
            observed_at=observed.get(target, []),
        ))
    for target in sorted(observed_targets - static_targets):
        dep = target.split(".")[0]
        if dep in stdlib or dep in _IGNORE_DEPS:
            continue  # the trace boundary may include odd entries; keep deps only
        symbols.append(SymbolStatus(
            target=target, dep=dep, status="dynamic-only", observed_at=observed[target],
        ))

    symbols.sort(key=lambda x: (x.dep, {"blind": 0, "dynamic-only": 1, "confirmed": 2}[x.status], x.target))
    report.symbols = symbols

    status_counts = Counter(x.status for x in symbols)
    report.confirmed = status_counts["confirmed"]
    report.blind = status_counts["blind"]
    report.dynamic_only = status_counts["dynamic-only"]
    report.static_targets = len(static_targets)
    report.observed_targets = len(observed_targets)
    report.trust = (report.confirmed / report.static_targets) if report.static_targets else 0.0

    by_dep: dict[str, Counter] = {}
    for x in symbols:
        by_dep.setdefault(x.dep, Counter())[x.status] += 1
    report.by_dep = {
        dep: {"confirmed": c["confirmed"], "blind": c["blind"], "dynamic_only": c["dynamic-only"]}
        for dep, c in sorted(by_dep.items())
    }

    if trace and probe_python:
        from pymolt.verify.probe import probe_trace
        probes = probe_trace(trace, probe_python)
        for sym in report.symbols:
            pr = probes.get(sym.target)
            if pr is not None:
                sym.probe_status = pr.status
                sym.probed = pr.probed_contacts > 0
        report.probed = sum(1 for s in report.symbols if s.probed)
        report.probe_changed = sum(1 for s in report.symbols if s.probe_status == "changed")
        report.notes.append(f"sandbox-probed reconstructible contacts under {probe_python}")

    if trace and against:
        try:
            bdiff = build_boundary_diff(str(against), str(trace))
            report.diff = bdiff.counts()
            report.diff_clean = bdiff.is_clean()
        except (OSError, ValueError) as e:
            report.notes.append(f"version diff failed: {e}")

    return report
