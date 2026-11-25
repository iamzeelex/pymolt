"""``LocalDeltaProvider``: codemods from local delta facts, the default for ``pymolt plan``.

``fetch_bundle`` returns ``{package: CodemodBundle}``.

- **patterns** come from facts with a concrete, same-signature successor: a MOVED spelling
  (the same object is importable elsewhere) or a REMOVED one whose successor resolves in
  the new version with an identical signature. Same leaf → ``rewrite-import``; a new leaf
  → ``rename-call`` (binding-aware). Only spellings a module exports (``from m import x``)
  get one: a class member has no import to rewrite.
- **rules** are the curated structural rewrites from ``pymolt/knowledge`` (e.g. pandas
  ``append`` → ``pd.concat``) for the migration's window, re-verified locally against their
  golden pair. A rule is left out only when the delta is evidence against it: it probed a
  symbol the rule addresses and found it still there. Its silence is no such evidence: the
  delta probes only what the baseline ran and the contact map resolved, and
  ``df.append(...)`` on a local the baseline never ran is neither.
  When a package has rules, its patterns travel as kind-tagged LEGACY rules (the
  rules-only policy in ``codemods.service._split_bundles`` would otherwise drop them).
- **impacts** hold every non-UNCHANGED fact, so the plan shows what changed even when
  nothing is auto-fixable, plus one INCONCLUSIVE entry per package whose delta could not
  be computed (never an empty "no changes").
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from pathlib import Path

from pymolt.codemods.models import ApiImpact, CodemodBundle, CodemodPattern
from pymolt.codemods.rules import CodemodRule, verify_rule
from pymolt.delta.engine import compute_deltas
from pymolt.delta.models import DeltaFact, DeltaReport

log = logging.getLogger(__name__)

_STATE = {"REMOVED": "removed", "MOVED": "moved", "SIGNATURE": "changed",
          "DEFAULT": "changed", "DEPRECATED": "deprecated"}
_RISK = {"REMOVED": "high", "MOVED": "low", "SIGNATURE": "medium", "DEFAULT": "medium",
         "DEPRECATED": "low"}
_SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "env", ".env", "site-packages",
              "node_modules", "build", "dist", ".tox", ".nox", ".mypy_cache", ".pytest_cache",
              ".eggs", ".pymolt"}


def explain(fact: DeltaFact, report: DeltaReport) -> str:
    window = f"{report.package} {report.from_version} → {report.to_version}"
    if fact.kind == "REMOVED":
        tail = f"; use {fact.successor}" if fact.successor else ""
        return f"{fact.symbol} no longer exists in {window}{tail}"
    if fact.kind == "MOVED":
        return f"{fact.symbol} is gone in {window}, the same object is {fact.successor}"
    if fact.kind == "DEFAULT":
        return (f"{fact.symbol}: default of {fact.param} changes {fact.old} → {fact.new} "
                f"({window})")
    if fact.kind == "SIGNATURE":
        if fact.param is None:
            return f"{fact.symbol}: base classes change {fact.old} → {fact.new} ({window})"
        tail = f"; use {fact.successor}" if fact.successor else ""
        return (f"{fact.symbol}: parameter {fact.param} changes {fact.old} → {fact.new} "
                f"({window}){tail}")
    if fact.kind == "DEPRECATED":
        when = "already in the old version" if fact.deprecated_in_old else "in the new version"
        if fact.param:
            tail = f"; use {fact.successor}" if fact.successor else ""
            return f"{fact.symbol}: parameter {fact.param} is deprecated {when} ({window}){tail}"
        return f"{fact.symbol} is deprecated {when} ({window})"
    return f"{fact.symbol}: {fact.kind}"


def _impact(fact: DeltaFact, report: DeltaReport) -> ApiImpact:
    hint = next((e.split(":", 1)[1] for e in fact.evidence if e.startswith("successor:")), None)
    trust = (fact.succession or {}).get("trust")
    return ApiImpact(
        path=fact.symbol,
        replacement_path=fact.successor,
        kind=fact.kind,
        state=_STATE.get(fact.kind, "changed"),
        risk=_RISK.get(fact.kind, "medium"),
        explanation=explain(fact, report),
        transition_confidence=("same-object" if fact.kind == "MOVED"
                               else f"{trust}:{hint}" if fact.successor and hint and trust
                               else f"hint:{hint}" if fact.successor and hint else None),
        transition_reason="; ".join(fact.evidence[:3]) or None,
        param=fact.param,
    )


def inconclusive_impact(report: DeltaReport) -> ApiImpact:
    """The one INCONCLUSIVE entry of a package whose delta could not be computed."""
    reason = "; ".join(report.notes[:2]) or "unknown reason"
    return ApiImpact(
        path=report.package, kind="INCONCLUSIVE", state="unknown", risk="unknown",
        explanation=(f"API changes for {report.package} {report.from_version} → "
                     f"{report.to_version} could not be computed: {reason}"),
    )


def unknown_base_report(package: str, to_version: str) -> DeltaReport:
    """The delta of a move whose base version pymolt never observed: inconclusive, not
    computed. Comparing against the manifest's floor or any other old release would rest
    the plan on a version the app may never have run."""
    return DeltaReport(
        package=package, from_version="unknown", to_version=to_version,
        quality="inconclusive",
        notes=["base version unknown: no lock, base interpreter, container or exact pin "
               f"says which {package} the project runs today",
               "a lock, the base interpreter or container (pymolt setup --base-python or "
               "--container), an exact pin, or a baseline capture settles it"],
    )


def _pattern(fact: DeltaFact) -> CodemodPattern | None:
    """A same-signature move/rename of an importable (module-level) spelling, else None.

    A DEPRECATED fact qualifies when its successor was verified on the new side and the
    signatures match (``force_text`` -> ``force_str``). Method renames (``.dict()`` ->
    ``.model_dump()``) are never importable spellings, so they stay hints: rewriting them needs
    the receiver's type, which the codemods do not have."""
    successor = fact.successor
    if not successor or fact.kind not in ("MOVED", "REMOVED", "DEPRECATED"):
        return None
    if not fact.detail.get("successor_importable") or not fact.detail.get(
        "successor_same_signature"
    ):
        return None
    importable = fact.detail.get("importable") or []
    old = fact.symbol if fact.symbol in importable else (importable[0] if importable else None)
    if old is None or "." not in old or "." not in successor:
        return None
    kind = "rewrite-import" if old.rsplit(".", 1)[1] == successor.rsplit(".", 1)[1] \
        else "rename-call"
    confidence = "high" if fact.kind == "MOVED" else "medium"
    return CodemodPattern(
        old_qualname=old, new_qualname=successor, kind=kind, confidence=confidence,
        evidence=[f"pymolt.delta {fact.kind}: {old} -> {successor}", *fact.evidence[:3]],
    )


def _golden_pair(pattern: CodemodPattern) -> tuple[str, str]:
    old_mod, old_leaf = pattern.old_qualname.rsplit(".", 1)
    new_mod, new_leaf = pattern.new_qualname.rsplit(".", 1)
    if pattern.kind == "rewrite-import":
        return f"from {old_mod} import {old_leaf}\n", f"from {new_mod} import {new_leaf}\n"
    return (f"from {old_mod} import {old_leaf}\n{old_leaf}()\n",
            f"from {new_mod} import {new_leaf}\n{new_leaf}()\n")


def as_legacy_rule(pattern: CodemodPattern, report: DeltaReport) -> CodemodRule:
    """A pattern as a kind-tagged LEGACY rule, verified against a synthesized golden pair."""
    before, after = _golden_pair(pattern)
    rule = CodemodRule(
        library=report.package, from_version=report.from_version,
        to_version=report.to_version, match=pattern.old_qualname,
        rewrite=[pattern.new_qualname], kind=pattern.kind,
        old_qualname=pattern.old_qualname, new_qualname=pattern.new_qualname,
        confidence="heuristic", test_before=before, test_after=after,
        evidence=list(pattern.evidence),
    )
    if verify_rule(rule):
        rule = rule.model_copy(update={"confidence": "verified"})
    return rule


def _python_files(root: Path):
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).parts
        if any(part in _SKIP_DIRS or part.startswith(".") for part in rel[:-1]):
            continue
        yield path


def _import_binding_missing(root: Path, anchors: list[str], module: str, alias: str | None,
                            rule: CodemodRule) -> list[str]:
    """Project files where ``rule`` fires but ``import MODULE as ALIAS`` is absent."""
    from pymolt.codemods.rules import apply_rule_detailed

    bind = re.compile(
        rf"^\s*import\s+{re.escape(module)}\s+as\s+{re.escape(alias)}\b" if alias
        else rf"^\s*import\s+{re.escape(module)}\b", re.MULTILINE,
    )
    missing = []
    for path in _python_files(root):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if not all(a in text for a in anchors):
            continue
        try:
            if apply_rule_detailed(text, rule).sites == 0:
                continue
        except Exception:  # noqa: BLE001 — an unparseable file is not ours to judge here
            continue
        if not bind.search(text):
            missing.append(str(path.relative_to(root)))
    return missing


class LocalDeltaProvider:
    """Codemod bundles computed on this machine from the two environments."""

    def __init__(self, project_dir: str | Path, *, online: bool = False,
                 symbols=None) -> None:
        self.project_dir = Path(project_dir)
        self.online = online
        self.symbols = symbols
        self.reports: list[DeltaReport] = []

    def fetch_bundle(
        self,
        migrations,
        *,
        progress: Callable[[str], None] | None = None,
        use_cache: bool | None = None,
    ) -> dict[str, CodemodBundle]:
        if not migrations:
            return {}
        if progress:
            names = ", ".join(m.name for m in migrations)
            progress(f"Computing API changes locally for {len(migrations)} package(s) — {names}…")
        self.reports = compute_deltas(
            self.project_dir, migrations, symbols=self.symbols, online=self.online,
            use_cache=True if use_cache is None else use_cache,
        )
        if progress:
            progress("Mapping API changes to codemods…")
        return {r.package: self._bundle(r) for r in self.reports}

    def _bundle(self, report: DeltaReport) -> CodemodBundle:
        from pymolt import knowledge

        impacts = [_impact(f, report) for f in report.changes()]
        if report.quality == "inconclusive":
            impacts.append(inconclusive_impact(report))
        patterns: list[CodemodPattern] = []
        seen: set[tuple[str, str]] = set()
        for fact in report.facts:
            p = _pattern(fact)
            if p is not None and (p.old_qualname, p.new_qualname) not in seen:
                seen.add((p.old_qualname, p.new_qualname))
                patterns.append(p)

        rules: list[CodemodRule] = []
        downgraded: list[str] = []
        gone = {s for f in report.facts if f.kind in ("REMOVED", "MOVED") for s in f.spellings()}
        there = {s for f in report.facts if f.kind not in ("REMOVED", "MOVED")
                 for s in f.spellings()}
        for entry in knowledge.curated_rules(report.package, report.from_version,
                                             report.to_version):
            names = set(entry.symbols)
            if report.quality == "complete" and there & names and not gone & names:
                continue        # probed, and still there in the new version
            payload = dict(entry.rule)
            payload.update(library=report.package, from_version=report.from_version,
                           to_version=report.to_version)
            payload["evidence"] = [*payload.get("evidence", []),
                                   f"pymolt knowledge {entry.id} ({entry.source}, {entry.date})"]
            rule = CodemodRule.model_validate(payload)
            label = f"{entry.id}: {rule.match} → {rule.rewrite[0]}"
            if not verify_rule(rule):
                if rule.confidence == "verified":
                    downgraded.append(f"{label} (its golden pair did not verify)")
                rule = rule.model_copy(update={"confidence": "heuristic"})
            elif entry.requires_import is not None:
                lacking = _import_binding_missing(
                    self.project_dir, entry.anchors, entry.requires_import.module,
                    entry.requires_import.as_, rule)
                if lacking:
                    need = (f"import {entry.requires_import.module}"
                            + (f" as {entry.requires_import.as_}"
                               if entry.requires_import.as_ else ""))
                    downgraded.append(f"{label} (needs `{need}` in {', '.join(lacking[:3])})")
                    rule = rule.model_copy(update={
                        "confidence": "heuristic",
                        "evidence": [*rule.evidence, f"needs `{need}` in {lacking[0]}"],
                    })
            rules.append(rule)
        if rules and patterns:
            rules = [*rules, *(as_legacy_rule(p, report) for p in patterns)]
            patterns = []
        return CodemodBundle(patterns=patterns, rules=rules, impacts=impacts,
                             downgraded=downgraded)
