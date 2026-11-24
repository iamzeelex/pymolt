"""UI-agnostic core for the assess phase.

``run_assess`` resolves the baseline and prospective-target dependency graphs,
builds the side-by-side comparison rows, and optionally the migration-risk
report — returning everything in an :class:`AssessResult`. The pure pieces
(``resolve_target_graph``/``build_comparison_rows``) are exposed so the CLI's
interactive override loop reuses them. Nothing here prompts or prints.
"""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from pymolt.core.graph import DependencyGraph
from pymolt.ingestion.detect import detect_sources, find_source
from pymolt.ingestion.orchestrator import orchestrate_ingestion
from pymolt.risk.models import RiskReport
from pymolt.state_io import atomic_write_text


class AssessResult(BaseModel):
    """Everything the assess phase produces — rendered by interfaces directly."""

    model_config = {"arbitrary_types_allowed": True}

    project_dir: str
    source_manifest: str
    source_fixation: str | None = None
    resolution_quality: str | None = None
    detected_python: str | None = None
    target_python: str | None = None
    target_resolved: bool = False
    target_error: str | None = None
    # The resolver's stderr tail (<= 40 lines, ANSI stripped) and the packages its
    # failure lines name. Only culprits get a "conflict" row; the rest are "unresolved".
    target_error_detail: str | None = None
    target_culprits: list[str] = Field(default_factory=list)
    # The legacy baseline could not be resolved (no legacy interpreter). The
    # target side is still resolved, but there is nothing honest to compare it to.
    baseline_unresolved: bool = False
    # Medallion trust tier for the baseline: gold | silver | bronze | none.
    baseline_tier: str = "none"
    target_manifest_path: str | None = None
    warnings: list[str] = Field(default_factory=list)
    manual_zone: list[Any] = Field(default_factory=list)
    rows: list[dict] = Field(default_factory=list)
    baseline_graph: DependencyGraph | None = None
    target_graph: DependencyGraph | None = None
    risk: RiskReport | None = None

    def counts_by_status(self) -> dict[str, int]:
        from collections import Counter

        return dict(Counter(r["status"] for r in self.rows))


# ── resolver failures ────────────────────────────────────────────────────────

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_DETAIL_LINES = 40
_REQ_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
# `name==1.0`, `name[extra]>=2`, `name @ url`: the name is what precedes the first operator.
_REQ_WITH_OP = re.compile(
    r"(?<![\w.-])([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?\s*(?:===|==|~=|!=|<=|>=|<|>|\s@\s)"
)
_FAILED_BUILD = re.compile(
    r"Failed to (?:download and build|build|download|fetch|clone)\s+`([^`]+)`")
_NOT_FOUND = re.compile(r"`([^`]+)` was not found in the package registry")
_NO_VERSIONS = re.compile(r"there are no versions of ([A-Za-z0-9][A-Za-z0-9._-]*)")
_NO_DIST = re.compile(r"No matching distribution found for ([A-Za-z0-9][A-Za-z0-9._-]*)")
_GIT_URL = re.compile(r"git\+(?:git|https?|ssh)://\S*?/([A-Za-z0-9._-]+?)(?:\.git)?(?:[@#\s`'\"]|$)")


def canonical_name(name: str) -> str:
    """PEP 503 normalisation, so ``Flask_Cors`` and ``flask-cors`` compare equal."""
    return re.sub(r"[-_.]+", "-", name).lower()


class TargetResolveError(str):
    """A one-line resolver failure summary that also carries the resolver's evidence.

    A ``str`` so every existing ``target_error`` consumer keeps working;
    ``detail`` is the stderr tail and ``culprits`` the packages its failure lines name.
    """

    detail: str | None
    culprits: list[str]

    def __new__(cls, summary: str, detail: str | None = None, culprits: list[str] | None = None):
        obj = super().__new__(cls, summary)
        obj.detail = detail
        obj.culprits = list(culprits or [])
        return obj


def _spec_name(spec: str) -> str | None:
    match = _REQ_NAME.match(spec.strip())
    return canonical_name(match.group(0)) if match else None


def parse_resolver_culprits(stderr: str) -> list[str]:
    """The package names a resolver's failure output points at, in order of appearance.

    Supported uv shapes: ``Failed to build `x==1```, ``Failed to download/fetch/clone
    `x```, ``Because x==1 depends on y>=2`` (incl. ``And because`` / ``Because only x<=1
    is available``, wrapped over lines), ```x` was not found in the package registry``,
    ``there are no versions of x``, ``No matching distribution found for x``, and
    ``git+git://host/org/x.git`` URLs. The bare ``No solution found`` header names none.
    """
    text = _ANSI.sub("", stderr or "")
    found: list[str] = []

    # Join wrapped continuation lines (and uv's tree glyphs) into one string.
    flat = re.sub(r"\s*\n[\s│├└─▶╰×]*", " ", text)
    positions: list[tuple[int, str | None]] = []
    for m in _FAILED_BUILD.finditer(flat):
        positions.append((m.start(), _spec_name(m.group(1))))
    for m in _NOT_FOUND.finditer(flat):
        positions.append((m.start(), _spec_name(m.group(1))))
    for m in _NO_VERSIONS.finditer(flat):
        positions.append((m.start(), canonical_name(m.group(1))))
    for m in _NO_DIST.finditer(flat):
        positions.append((m.start(), canonical_name(m.group(1))))
    for m in re.finditer(r"(?:[Bb]ecause|you require|Your project requires)\b", flat):
        end = flat.find(", we can conclude", m.end())
        clause = flat[m.end(): end if end != -1 else m.end() + 300]
        for req in _REQ_WITH_OP.finditer(clause):
            positions.append((m.start(), canonical_name(req.group(1))))
    for m in _GIT_URL.finditer(flat):
        positions.append((m.start(), canonical_name(m.group(1))))
    for _, name in sorted(positions, key=lambda item: item[0]):
        if name and name not in found:
            found.append(name)
    return found


def resolver_summary(stderr: str, fallback: str) -> str:
    """uv's first ``error:`` / ``×`` line, else its first non-blank line, else ``fallback``."""
    lines = [ln.strip() for ln in _ANSI.sub("", stderr or "").splitlines() if ln.strip()]
    for ln in lines:
        m = re.match(r"^(?:error:|×|ERROR:)\s*(.+)$", ln)
        if m:
            return m.group(1).rstrip(":").strip()
    return lines[0].rstrip(":") if lines else fallback


def _process_error(exc: BaseException | None):
    """The ``CalledProcessError`` at the root of ``exc``'s cause chain, if any."""
    import subprocess

    for _ in range(8):
        if exc is None:
            return None
        if isinstance(exc, subprocess.CalledProcessError):
            return exc
        exc = exc.__cause__ or exc.__context__
    return None


def describe_resolver_failure(exc: BaseException) -> TargetResolveError:
    """A resolver exception as a one-line summary plus its stderr tail and culprits."""
    proc = _process_error(exc)
    stderr = ""
    if proc is not None:
        raw = proc.stderr
        stderr = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else (raw or "")
    if not stderr.strip():
        return TargetResolveError(str(exc))
    clean = _ANSI.sub("", stderr).rstrip()
    tail = "\n".join(clean.splitlines()[-_DETAIL_LINES:])
    return TargetResolveError(
        resolver_summary(stderr, str(exc)), tail, parse_resolver_culprits(stderr),
    )


def manifest_fixation(source) -> str:
    """How firmly the *manifest* pins its versions (``pinned`` / ``intent`` / ...).

    Not the resolved graph's fixation: a resolve always yields pins, which says
    nothing about the file the engineer wrote (ranges only is still ``intent``).
    """
    fixation = source.fixation
    return getattr(fixation, "value", str(fixation))


def failure_evidence(target_error) -> tuple[str | None, list[str]]:
    """``(stderr tail, culprits)`` carried by a ``resolve_target_graph`` error, if any."""
    return getattr(target_error, "detail", None), list(getattr(target_error, "culprits", []))


def resolve_chosen_source(sources, manifest_name: str | None, project_dir=None):
    """Pick the source manifest by name or relative path, or default to the first lock/manifest.

    Raises ``ValueError`` when a named manifest is not found (the caller decides
    how to surface it).
    """
    if manifest_name:
        found = find_source(sources, manifest_name, project_dir or ".")
        if found is not None:
            return found
        discovered = [s.path.name for s in sources]
        raise ValueError(f"Manifest '{manifest_name}' not found. Discovered: {discovered}")
    lock_sources = [s for s in sources if s.is_lock]
    return lock_sources[0] if lock_sources else sources[0]


def write_constraints_file(project_path: Path, upgrade_constraint, overrides) -> Path | None:
    """Build the combined constraints (upgrade file + per-package overrides).

    Returns the path to a temp constraints file, or ``None`` if there are none.
    The caller owns deleting the returned file.
    """
    temp_constraints: list[str] = []
    if upgrade_constraint:
        try:
            temp_constraints.extend(
                Path(upgrade_constraint).read_text(encoding="utf-8").splitlines()
            )
        except OSError:
            pass

    for pkg, override in (overrides or {}).items():
        if override and not any(op in override for op in ("==", ">=", "<=", "<", ">", "!=")):
            temp_constraints.append(f"{pkg}=={override}")
        else:
            temp_constraints.append(f"{pkg}{override}")

    if not temp_constraints:
        return None

    cache_dir = project_path / ".pymolt" / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    fd, path_str = tempfile.mkstemp(dir=str(cache_dir), suffix=".txt", prefix="pymolt_constraints_")
    os.close(fd)
    with open(path_str, "w", encoding="utf-8") as f:
        f.write("\n".join(temp_constraints) + "\n")
    return Path(path_str)


def extract_constraint(declared: str | None) -> str | None:
    """Reduce a declared requirement to just its version-constraint string."""
    if not declared:
        return None
    from packaging.requirements import Requirement

    try:
        req = Requirement(declared)
        return str(req.specifier) if req.specifier else "any"
    except Exception:
        match = re.match(r"^[\w\-\.]+\[?[\w\-\.,]*\]?\s*(.*)$", declared)
        text = match.group(1).strip() if match else declared
        return text or "any"


#: A base version pymolt did not observe: the base graph names the package but no lock,
#: interpreter, container or exact pin said which version is installed (a ``declared-only``
#: base). Never a version to compute API changes from.
UNKNOWN_VERSION = "unknown"


def build_comparison_rows(
    baseline_graph, target_graph, target_python, target_error, target_culprits=None,
) -> list[dict]:
    """Compute the baseline/target comparison as plain dicts (no rich markup).

    When the target did not resolve, only the packages the resolver named
    (``target_culprits``, else the ones ``target_error`` carries) are ``conflict``;
    every other row is ``unresolved``: the resolver never judged it. A base package
    whose version is unknown is ``base-unknown`` with ``baseline_version`` "unknown",
    never an ``upgrade``: nothing says it moves.

    Shared by the CLI table renderer and ``--json``. One row per package
    in the union; visibility filtering is the renderer's concern.
    """
    rows = []
    if target_culprits is None:
        target_culprits = getattr(target_error, "culprits", None)
    culprit_set = {canonical_name(c) for c in (target_culprits or [])}
    all_packages = sorted(
        set(baseline_graph.nodes.keys() if baseline_graph else [])
        | set(target_graph.nodes.keys() if target_graph else [])
    )
    for name in all_packages:
        b = baseline_graph.nodes.get(name) if baseline_graph else None
        t = target_graph.nodes.get(name) if target_graph else None
        node_for_prov = b or t

        declared = None
        if b and b.declared_requirement:
            declared = b.declared_requirement
        elif t and t.declared_requirement:
            declared = t.declared_requirement

        baseline_version = (b.version or UNKNOWN_VERSION) if b else None

        if target_python:
            if target_error:
                target_version = None
                status = "conflict" if canonical_name(name) in culprit_set else "unresolved"
            elif t:
                target_version = t.version or "unpinned"
                if not b:
                    status = "added"
                elif not b.version:
                    status = "base-unknown"
                elif b.version != t.version:
                    status = "upgrade"
                else:
                    status = "unchanged"
            else:
                target_version, status = None, "removed"
        else:
            target_version, status = None, "baseline"

        rows.append({
            "name": name,
            "declared_constraint": extract_constraint(declared),
            "baseline_version": baseline_version,
            "target_version": target_version,
            "origin": node_for_prov.provenance.value if node_for_prov else None,
            "direct": bool((b and b.direct) or (t and t.direct)),
            "manual_bridge": bool((b and b.manual_bridge) or (t and t.manual_bridge)),
            "status": status,
        })
    return rows


def resolve_target_graph(
    project_dir, project_path, chosen_source, target_python, container_id,
    upgrade_constraint, overrides, use_cache=True, tool=None,
):
    """Resolve the prospective target-Python graph; return (graph_or_None, error_or_None)."""
    if not target_python:
        return None, None
    constraint_path = write_constraints_file(project_path, upgrade_constraint, overrides)
    try:
        graph, _ = orchestrate_ingestion(
            project_dir,
            chosen_source=chosen_source,
            target_python=target_python,
            container_id=container_id,
            base_python=target_python,
            constraint_file=constraint_path,
            force_recompile=True,
            use_cache=use_cache,
            tool=tool,
        )
        return graph, None
    except Exception as e:
        return None, describe_resolver_failure(e)
    finally:
        if constraint_path and constraint_path.exists():
            try:
                constraint_path.unlink()
            except OSError:
                pass


def fetch_pypi_hashes(name: str, version: str) -> list[str]:
    """Best-effort fetch of the sha256 hashes for a pinned release from PyPI."""
    import json
    import urllib.request

    url = f"https://pypi.org/pypi/{name}/{version}/json"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "PyMolt/1.0"})
        with urllib.request.urlopen(req, timeout=3) as response:
            data = json.loads(response.read().decode("utf-8"))
        hashes = []
        for f in data.get("urls", []):
            digest = f.get("digests", {}).get("sha256")
            if digest:
                hashes.append(digest)
        return hashes
    except Exception:
        return []


def write_target_manifest(project_path, target_graph, selected_tool, target_python, with_hashes):
    """Write a pinned target manifest from the resolved target graph.

    PyPI -> ``requirements-target.txt`` (with ``--hash`` lines when every package
    resolves to PyPI hashes; pip requires all-or-nothing, so a single miss falls
    back to a plain pinned file). Conda -> ``environment-target.yml`` (integrity
    is conda-lock's job, so no hashes there). Returns the written path or None.
    """
    project_path = Path(project_path)
    nodes = sorted(target_graph.nodes.items())

    if selected_tool == "conda":
        conda_deps, pip_deps = [], []
        for name, node in nodes:
            prov = node.provenance.value if hasattr(node.provenance, "value") else str(node.provenance)
            ver = node.version or "*"
            if prov in ("pypi", "pip-in-conda"):
                pypi_name = node.mapping.pypi_name if node.mapping else name
                pip_deps.append(f"{pypi_name}=={ver}")
            elif name != "python":
                conda_name = node.mapping.conda_name if node.mapping else name
                conda_deps.append(f"{conda_name}={ver}")

        lines = ["name: target_env", "channels:", "  - conda-forge", "  - defaults",
                 "dependencies:", f"  - python={target_python}"]
        lines += [f"  - {d}" for d in conda_deps]
        if pip_deps:
            lines.append("  - pip:")
            lines += [f"    - {d}" for d in pip_deps]
        target_file = project_path / "environment-target.yml"
        atomic_write_text(target_file, "\n".join(lines) + "\n")
        return target_file

    # PyPI requirements file.
    pinned = [(name, node.version) for name, node in nodes if node.version]
    hashes_by_pkg = {}
    use_hashes = with_hashes and bool(pinned)
    if use_hashes:
        for name, version in pinned:
            digests = fetch_pypi_hashes(name, version)
            if not digests:
                use_hashes = False  # pip --require-hashes is all-or-nothing
                break
            hashes_by_pkg[name] = digests

    header = [f"# Generated by pymolt audit for Python {target_python}", f"# Base Tool: {selected_tool}"]
    if with_hashes and not use_hashes:
        header.append("# NOTE: hashes omitted — could not resolve PyPI hashes for every package.")
    out = ["\n".join(header), ""]
    for name, version in sorted(pinned):
        if use_hashes:
            parts = [f"{name}=={version}"] + [f"--hash=sha256:{h}" for h in hashes_by_pkg[name]]
            out.append(" \\\n    ".join(parts))
        else:
            out.append(f"{name}=={version}")
    target_file = project_path / "requirements-target.txt"
    atomic_write_text(target_file, "\n".join(out) + "\n")
    return target_file


class BaselineTier(StrEnum):
    """Trust in the baseline, earned by evidence — a medallion tier (Bronze→Gold).

    The baseline is what the migration is measured *against*, so its trustworthiness
    is a first-class honesty signal, not a footnote.
    """

    GOLD = "gold"  # pinned lockfile + resolves — a production-exact snapshot
    SILVER = "silver"  # unpinned but its tests pass — behaviourally validated, not pin-exact
    BRONZE = "bronze"  # unpinned reconstruction that resolves, but unvalidated
    NONE = "none"  # could not be resolved (no legacy interpreter) — there is no baseline


def classify_baseline_tier(
    *, baseline_resolved: bool, source_fixation: str | None, tests_passed: bool,
    base_python_assumed: bool = False,
) -> BaselineTier:
    """Assign the medallion tier from what we can prove about the baseline.

    ``base_python_assumed`` caps the tier at SILVER. GOLD claims a
    "production-exact baseline", and a pinned manifest resolved against a
    *guessed* interpreter is not that — the pins are exact, the environment they
    were resolved in is a fallback to whatever Python pymolt happened to run on.
    Tests passing still earns SILVER: that evidence is real regardless.
    """
    if not baseline_resolved:
        return BaselineTier.NONE
    if source_fixation == "pinned" and not base_python_assumed:
        return BaselineTier.GOLD
    if source_fixation == "pinned" or tests_passed:
        return BaselineTier.SILVER
    return BaselineTier.BRONZE


def baseline_tests_passed(project_dir: str | Path) -> bool:
    """Silver evidence: a captured contract baseline whose test run PASSED (rc 0).

    Reads ``.pymolt/contract_state.json`` — the baseline slot's ``returncode``. This
    ties the medallion to existing machinery: Silver is earned by the contract phase,
    not asserted. "Passed" means rc == 0, not merely "the command ran". A state file that
    is not JSON, or a newer pymolt's, raises ``StateFileError``.
    """
    from pymolt.state_io import read_state_json
    from pymolt.verify.models import (
        CONTRACT_STATE_KIND,
        CONTRACT_STATE_REMEDY,
        CONTRACT_STATE_SCHEMA_VERSION,
    )

    data = read_state_json(Path(project_dir) / ".pymolt" / "contract_state.json",
                           kind=CONTRACT_STATE_KIND, newest=CONTRACT_STATE_SCHEMA_VERSION,
                           remedy=CONTRACT_STATE_REMEDY)
    slot = (data or {}).get("baseline")
    return isinstance(slot, dict) and slot.get("returncode") == 0


def compatibility_verdict(
    target_python: str | None,
    target_error: str | None,
    baseline_tier: BaselineTier,
) -> tuple[str, str, str] | None:
    """The honest target-Python verdict as ``(title, body, style)`` — or None.

    Never claims "upgrade feasible" from a resolve alone: a resolve only proves that
    *some* versions satisfying the constraints install on the target Python. What the
    verdict can honestly say is bounded by the base's medallion tier — Gold/Silver
    earn a green verdict, Bronze/None stay amber — and it always points at
    ``pymolt capture`` + ``pymolt verify`` for behavioural proof. Bronze→Silver is an
    explicit upgrade path (run the baseline's tests), not a dead end.
    """
    if not target_python:
        return None
    tp = target_python
    if target_error:
        return (
            f"❌ Target dependencies do not resolve (Python {tp})",
            f"Resolution failed for Python {tp}, so the project cannot move there as-is:\n\n"
            f"{target_error}",
            "red",
        )
    if baseline_tier == BaselineTier.NONE:
        return (
            f"⚠ Not a feasibility verdict — no base (Python {tp})",
            f"The newest versions matching your constraints install on Python {tp}, but the "
            "base dependencies could not be resolved (no interpreter) — there is nothing to "
            "compare against. Build the legacy environment (hint above), re-run with "
            "--container, then capture and verify (`pymolt capture`, `pymolt verify`).",
            "yellow",
        )
    if baseline_tier == BaselineTier.BRONZE:
        return (
            f"⚠ BRONZE base — resolves, migration unproven (Python {tp})",
            f"The latest releases matching your UNPINNED bounds install on Python {tp} — not "
            "evidence the code migrates, and the base is an unvalidated reconstruction "
            "(e.g. `tensorflow>=1.3` → 2.x). Capture and verify: if the project's tests "
            "pass it becomes SILVER; pin the manifest to reach GOLD.",
            "yellow",
        )
    if baseline_tier == BaselineTier.SILVER:
        return (
            f"✔ SILVER base — resolves on Python {tp}",
            "Not pinned, but its tests pass, so the base is validated by them (not "
            f"pin-exact GOLD). Dependencies resolve on Python {tp}; capture and verify "
            "against the target to confirm the behaviour carries over.",
            "green",
        )
    return (
        f"✔ GOLD base — resolves on Python {tp}",
        f"Pinned, production-exact base; resolves cleanly on Python {tp} with no conflicts. "
        "Capture and verify (`pymolt capture`, `pymolt verify`) to check runtime "
        "behaviour.",
        "green",
    )


def _is_missing_container_error(err: Exception) -> bool:
    """True when a resolution failure is a configured-container-not-running case
    (a dead container_id in the saved config), as opposed to a real conflict.

    The baseline env's container is incidental — a stale id must not sink the
    whole assess; the target side never touches it anyway.
    """
    msg = str(err).lower()
    return "container" in msg and ("was not found running" in msg or "not matching" in msg)


def _is_missing_interpreter_error(err: Exception) -> bool:
    """True when baseline resolution failed for lack of the legacy interpreter.

    Installing an EOL Python (3.4/3.6) is often impractical, so this must not sink
    the whole assess: the target side resolves without it. The caller degrades to a
    baseline-unresolved state and emits a build-it hint instead of crashing.
    """
    msg = str(err).lower()
    return "interpreter was not found" in msg or "interpreter is required" in msg


def _resolve_baseline_graph(
    *, project_dir, chosen, target_python, container_id, base_python, use_cache, tool,
):
    """Resolve the baseline graph, degrading gracefully if the configured
    container is gone.

    A stale ``container_id`` in the saved config would otherwise raise out of the
    baseline resolve and blank the entire assess (every row a bogus conflict). If
    that specific error fires, fall back to the container-free lock/static path
    (uv) and return a warning to surface — the baseline is a reconstruction, but
    the target side (resolved independently for the new Python) is unaffected.
    Returns ``(baseline_graph, report, warning_or_None)``.
    """
    try:
        graph, report = orchestrate_ingestion(
            project_dir,
            chosen_source=chosen,
            target_python=target_python,
            container_id=container_id,
            base_python=base_python,
            use_cache=use_cache,
            tool=tool,
        )
        return graph, report, None
    except Exception as exc:
        if _is_missing_interpreter_error(exc):
            # No legacy interpreter → degrade to a baseline-unresolved state rather
            # than crash. We deliberately do NOT reconstruct the baseline (resolving
            # unpinned `>=` to latest would be a fiction); the caller emits a hint.
            from pymolt.core.layers import IngestionReport

            first_line = str(exc).splitlines()[0] if str(exc) else str(exc)
            warning = (
                f"base unresolved: {first_line} The base dependency graph was NOT "
                "reconstructed — build the base environment and re-run with --container "
                "(see the hint)."
            )
            stub = IngestionReport(
                resolution_quality="unresolved",
                source_fixation=getattr(chosen, "fixation", None) or "unknown",
                manual_zone=[],
                warnings=[warning],
                detected_python=base_python,
            )
            return None, stub, warning
        if not (container_id and _is_missing_container_error(exc)):
            raise
        fallback_tool = "uv" if tool == "container" else tool
        try:
            graph, report = orchestrate_ingestion(
                project_dir,
                chosen_source=chosen,
                target_python=target_python,
                container_id=None,
                base_python=base_python,
                use_cache=use_cache,
                tool=fallback_tool,
            )
        except Exception as fallback_exc:
            # The retry usually fails for the very reason the container
            # existed: no legacy interpreter on this machine. Left alone, its
            # error replaces the original and the user is told to install
            # Python 3.6 while never learning their configured container is
            # dead. Lead with the cause they can actually act on.
            raise ValueError(
                f"configured container {container_id} is not running, and "
                f"resolving without it also failed.\n"
                f"Start the container (or re-run setup to pick a new one).\n\n"
                f"Fallback error: {fallback_exc}"
            ) from fallback_exc
        warning = (
            f"configured container {container_id} not running — base resolved "
            "from manifest/lock; re-run setup to refresh."
        )
        report.warnings.append(warning)
        return graph, report, warning


def base_python_assumed(config: dict, base_python: str | None) -> bool:
    """Whether the base Python number is setup's guess rather than a stated fact.

    An explicit ``base_python`` from the caller is a fact, unless it is just setup's
    recorded guess handed back (an adapter passing the config's own value): then it is
    still assumed. Without one, trust what setup recorded about where the number came from.
    """
    if config.get("base_python_source") != "assumed":
        return False
    return base_python is None or base_python == config.get("base_python")


def run_assess(
    project_dir: str | Path,
    *,
    target_python: str,
    config: dict | None = None,
    source_manifest: str | None = None,
    base_python: str | None = None,
    container_id: str | None = None,
    upgrade_constraint: str | None = None,
    overrides: dict | None = None,
    use_cache: bool = True,
    tool: str = "uv",
    with_risk: bool = False,
    write_manifest: bool = True,
    with_hashes: bool = True,
    progress: Callable[[str, float], None] | None = None,
) -> AssessResult:
    """Resolve baseline + target graphs, compare, and (optionally) assess risk.

    ``target_python`` is required — resolving/validating it (and any interactive
    pick) is the caller's job. Raises ``ValueError`` when no sources are detected
    or a named manifest is missing. ``progress(stage, fraction)`` is called between
    the (slow) stages so a caller can drive a progress bar.

    On a successful target resolve, ``write_manifest`` writes the pinned target
    manifest (``requirements-target.txt`` / ``environment-target.yml``) and records
    its path on the result (``with_hashes`` controls PyPI hash fetching, the
    all-or-nothing ``--hash`` lines). Set ``write_manifest=False`` for a read-only
    assess.
    """
    def _p(stage: str, fraction: float) -> None:
        if progress is not None:
            progress(stage, fraction)

    config = config or {}
    project_path = Path(project_dir)

    sources = detect_sources(project_path, include_subroots=True)
    if not sources:
        raise ValueError(f"No python dependency sources detected in directory {project_dir}")
    chosen = resolve_chosen_source(
        sources, source_manifest or config.get("selected_manifest"), project_path)

    overrides = overrides if overrides is not None else config.get("target_overrides", {})

    _p("baseline", 0.1)
    baseline_graph, report, _degrade_warning = _resolve_baseline_graph(
        project_dir=str(project_dir),
        chosen=chosen,
        target_python=target_python,
        container_id=container_id,
        base_python=base_python,
        use_cache=use_cache,
        tool=tool,
    )
    _p("target", 0.45)
    # The container/legacy interpreter is the *baseline* environment. The TARGET
    # graph is resolved fresh for the new Python via uv's `--python-version`
    # (cross-version resolution needs neither that interpreter nor a container) —
    # so we deliberately drop container_id and force the uv strategy here. Using
    # the legacy (e.g. 3.6) container to resolve a modern target is both wrong and
    # the cause of the spurious "container not matching" error.
    target_tool = "uv" if tool == "container" else tool
    target_graph, target_error = resolve_target_graph(
        str(project_dir), project_path, chosen, target_python,
        None, upgrade_constraint, overrides, use_cache=use_cache, tool=target_tool,
    )
    _p("compare", 0.8)
    rows = build_comparison_rows(baseline_graph, target_graph, target_python, target_error)
    error_detail, error_culprits = failure_evidence(target_error)

    risk = None
    if with_risk and baseline_graph is not None:
        from pymolt.risk.assess import assess_risk

        _p("risk", 0.85)
        risk = assess_risk(baseline_graph, target_graph, target_python, project_path)

    target_resolved = target_error is None and target_graph is not None
    target_manifest_path: str | None = None
    if write_manifest and target_resolved:
        try:
            written = write_target_manifest(
                project_path, target_graph, tool, target_python, with_hashes=with_hashes,
            )
            if written is not None:
                target_manifest_path = str(written)
        except OSError:
            target_manifest_path = None
    _p("done", 1.0)

    return AssessResult(
        project_dir=str(project_path),
        source_manifest=chosen.path.name,
        source_fixation=manifest_fixation(chosen),
        resolution_quality=report.resolution_quality,
        detected_python=report.detected_python,
        target_python=target_python,
        target_resolved=target_resolved,
        target_error=None if target_error is None else str(target_error),
        target_error_detail=error_detail,
        target_culprits=error_culprits,
        baseline_unresolved=baseline_graph is None,
        baseline_tier=classify_baseline_tier(
            baseline_resolved=baseline_graph is not None,
            source_fixation=chosen.fixation,
            tests_passed=baseline_tests_passed(project_dir),
            base_python_assumed=base_python_assumed(config, base_python),
        ).value,
        target_manifest_path=target_manifest_path,
        warnings=report.warnings,
        manual_zone=report.manual_zone,
        rows=rows,
        baseline_graph=baseline_graph,
        target_graph=target_graph,
        risk=risk,
    )
