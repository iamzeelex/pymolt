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
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

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
    if not successor or fact.kind != "MOVED":
        # A successor only *named* (by a warning, a docstring) proves nothing about behaviour,
        # whatever its signature: it is an agent step. A proven one comes from
        # ``successor_patterns``.
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


# ── successor patterns (successors v2, stage S2) ─────────────────────────────────────────

_TRANSFERS = ("at-target", "successor-unchanged", "per-site")


def _proven(fact: DeltaFact) -> dict | None:
    """The succession record of a fact whose rewrite the library's own code proves and that
    transfers to the target: trust ``proof`` or ``identity``, a public successor."""
    rec = fact.succession or {}
    if rec.get("trust") not in ("proof", "identity") or not fact.successor:
        return None
    if rec.get("transfer") not in _TRANSFERS:
        return None
    if any(part.startswith("_") and not part.startswith("__")
           for part in fact.successor.split(".")):
        return None
    return rec


def _strip_ctor(name: str) -> str:
    return name[: -len(".__init__")] if name.endswith(".__init__") else name


class BaselineSites:
    """The baseline's call sites of a member, with the receiver classes recorded there:
    what gates a member's rewrite (``rename-member``, a member's ``rename-keyword`` or
    ``forward-template``). Read once per plan from the baseline capture."""

    def __init__(self, root: Path) -> None:
        from pymolt.delta import envs as envs_mod
        from pymolt.delta import touched as touched_mod

        self.root = root
        self.facts: list[Any] = []
        self.classes: dict = {}
        self.texts: dict[str, str | None] = {}
        self._touched = touched_mod
        trace = envs_mod._baseline_trace(root)
        if trace is None:
            return
        try:
            from pymolt.verify.facts import load_facts

            self.facts = list(load_facts(trace, root=root).facts)
            self.classes, _ = touched_mod.project_classes(root)
        except Exception as exc:  # noqa: BLE001 - no sites, no gated rewrite
            log.debug("baseline sites unavailable: %s", exc)
            self.facts = []

    def _index(self, rel: str):
        from pymolt.anchors import StatementIndex

        if rel not in self.texts:
            try:
                self.texts[rel] = (self.root / rel).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                self.texts[rel] = None
        text = self.texts[rel]
        if text is None:
            return None
        try:
            return StatementIndex.build(text)
        except Exception:  # noqa: BLE001
            return None

    def gate(self, old_spellings: set[str], old_leaf: str, new_leaf: str,
             owners: Iterable[str] = ()) -> tuple[list[Any], list[str], list[str]]:
        """``(anchors, receivers, refused)``: the statements of the baseline's calls of the
        member whose every recorded receiver the proof covers (its class, or a public spelling
        of it, owns the proven old member), with no project class in the receiver's hierarchy
        defining the old or the new name; and why the other sites were refused."""
        parents = {s.rsplit(".", 1)[0] for s in old_spellings if "." in s} | set(owners)
        by_site: dict[tuple[str, int], set[str | None]] = {}
        for f in self.facts:
            symbol = getattr(f, "symbol", "") or ""
            text = getattr(f, "text", None) or ""
            if symbol in old_spellings:
                pass
            elif "<locals>" in symbol and f".{old_leaf}(" in text:
                pass                        # recorded under a decorator's generic wrapper
            else:
                continue
            raw = self._touched._receiver_type(f)
            cls = self._touched.receiver_class(f, self.root, self.classes)
            ok = cls is not None and any(c in parents
                                         for c in self._touched.public_candidates(cls))
            if ok and raw in self.classes and (
                    self._touched._ancestors_define(raw, new_leaf, self.classes)
                    or self._touched._ancestors_define(raw, old_leaf, self.classes)):
                ok = False
            for line in sorted({*(getattr(f, "lines", None) or ()),
                                *([f.line] if getattr(f, "line", None) else [])}):
                by_site.setdefault((f.file, line), set()).add(cls if ok else None)
        anchors, receivers, refused = [], [], []
        for (rel, line), classes in sorted(by_site.items()):
            if None in classes:
                refused.append(f"{rel}:{line}: a receiver the proof does not cover")
                continue
            index = self._index(rel)
            anchor = index.anchor_at(line, file=rel) if index is not None else None
            if anchor is None:
                refused.append(f"{rel}:{line}: no statement to anchor")
                continue
            if all(a.id != anchor.id for a in anchors):
                anchors.append(anchor)
            receivers += [c for c in classes if c and c not in receivers]
        return anchors, receivers, refused


def _blocked(fact: DeltaFact, report: DeltaReport) -> list[dict]:
    """The symbol's other changed parameters: a rewritten call that passes one of them, or
    leaves a changed default alone, is left to the agent (``successors.site_blocked``)."""
    names = set(fact.spellings())
    out = []
    for other in report.facts:
        if other is fact or not other.param or not (set(other.spellings()) & names):
            continue
        if other.param in (fact.param, fact.successor):
            continue
        if other.kind in ("SIGNATURE", "DEFAULT", "DEPRECATED"):
            out.append({"param": other.param, "kind": other.kind, "old": other.old,
                        "new": other.new})
    return out


def succession_summary(fact: DeltaFact) -> dict:
    """What a step shows of its successor's provenance (``StepSuccessor``)."""
    rec = fact.succession or {}
    proof = rec.get("proof") or {}
    return {"trust": rec.get("trust"),
            "proof": f"{proof.get('kind')}: {proof.get('locus')}" if proof else None,
            "version": proof.get("version"), "transfer": rec.get("transfer"),
            "caveats": list(rec.get("caveats") or ())[:4]}


def successor_patterns(report: DeltaReport, sites: BaselineSites | None
                       ) -> tuple[list[CodemodPattern], list[str]]:
    """The successor patterns the facts' proofs license, and why the other proven facts make
    none (``(patterns, notes)``). Never from prose: only from ``_proven`` facts."""
    out: list[CodemodPattern] = []
    notes: list[str] = []
    # every proven keyword rename of one callable is one pattern: a call passing two of them
    # is rewritten whole, never blocked by the other rename
    renames: dict[str, list[DeltaFact]] = {}
    for fact in report.facts:
        rec = _proven(fact)
        if rec is not None and fact.param and rec.get("params"):
            renames.setdefault(fact.symbol, []).append(fact)
    for group in renames.values():
        _rename_keyword_pattern(group, report, sites, out, notes)
    for fact in report.facts:
        rec = _proven(fact)
        if rec is None or (fact.param and rec.get("params")):
            continue
        template = dict(rec.get("template") or {})
        proof = rec.get("proof") or {}
        evidence = [f"pymolt.delta proof ({proof.get('kind')}, {proof.get('side')} side): "
                    f"{fact.symbol} -> {fact.successor}",
                    f"proof locus {proof.get('locus')} ({proof.get('version')}); transfer "
                    f"{rec.get('transfer')}"]
        old_spellings = {_strip_ctor(s) for s in fact.spellings()} | set(
            (rec.get("old") or {}).get("spellings") or ())
        if fact.param:
            continue
        old_leaf = fact.symbol.rsplit(".", 1)[-1]
        new_leaf = fact.successor.rsplit(".", 1)[-1]
        if rec.get("trust") == "identity":
            # the same object under a public spelling of the same package: every attribute
            # path through the package's import (``np.NaN`` -> ``np.nan``, ``np.float_`` ->
            # ``np.float64``); a move to another package or a same-leaf move stays an import
            # rewrite (``_pattern``)
            olds = [fact.symbol] if fact.kind == "REMOVED" else list(
                fact.detail.get("importable") or [])
            same = [f"pymolt.delta MOVED: {fact.symbol} -> {fact.successor}", *evidence[1:]]
            for old in olds:
                if "." not in old or "." not in fact.successor:
                    continue
                if old.rsplit(".", 1)[-1] == new_leaf:
                    if fact.kind == "DEPRECATED":   # a deprecated spelling of the same object
                        out.append(CodemodPattern(
                            old_qualname=old, new_qualname=fact.successor,
                            kind="rewrite-import", confidence="high", evidence=same))
                elif old.split(".")[0] == fact.successor.split(".")[0]:
                    out.append(CodemodPattern(
                        old_qualname=old, new_qualname=fact.successor,
                        kind="replace-attr-path", confidence="high",
                        evidence=evidence if fact.kind == "REMOVED" else same))
            continue
        member = bool(template.get("member"))
        if member:
            if sites is None:
                notes.append(f"{fact.symbol}: no baseline sites to gate on")
                continue
            anchors, receivers, refused = sites.gate(
                old_spellings, old_leaf, new_leaf, (rec.get("old") or {}).get("owners") or ())
            if not anchors:
                notes.append(f"{fact.symbol}: no site the gate accepts"
                             + (f" ({refused[0]})" if refused else ""))
                continue
            kind = "rename-member" if template.get("kind") == "rename" and (
                template.get("attr") or not _defaults_differ(template)) else "forward-template"
            out.append(CodemodPattern(
                old_qualname=fact.symbol, new_qualname=fact.successor, kind=kind,
                confidence="high", sites=anchors, receivers=receivers,
                template={**template, "member_leaf": old_leaf,
                          "succession": succession_summary(fact)},
                aliases=sorted(old_spellings - {fact.symbol}), evidence=evidence))
            continue
        # a module-level callable: through its module binding (``alias.old(...)``)
        if template.get("kind") == "rename" and rec.get("transfer") in (
                "at-target", "successor-unchanged") and fact.detail.get("importable"):
            # every importable spelling (``numpy.product`` and ``numpy.core.fromnumeric.product``)
            # to the successor's public one, binding-aware
            forbid = _renamed_keywords(template)
            for old in sorted(fact.detail.get("importable") or [], key=len):
                out.append(CodemodPattern(
                    old_qualname=old, new_qualname=fact.successor,
                    kind="rewrite-import" if old.rsplit(".", 1)[-1] == new_leaf
                    else "rename-call", confidence="high", evidence=evidence,
                    template={"forbid_keywords": forbid} if forbid else None))
            continue
        out.append(CodemodPattern(
            old_qualname=fact.symbol, new_qualname=fact.successor, kind="forward-template",
            confidence="high", template={**template, "member_leaf": None,
                                         "succession": succession_summary(fact)},
            aliases=sorted(old_spellings - {fact.symbol}), evidence=evidence))
    return out, notes


def _rename_keyword_pattern(group: list[DeltaFact], report: DeltaReport,
                            sites: BaselineSites | None, out: list[CodemodPattern],
                            notes: list[str]) -> None:
    """One ``rename-keyword`` pattern for the proven keyword renames of one callable."""
    fact = group[0]
    params = {k: v for f in group for k, v in ((f.succession or {}).get("params") or {}).items()}
    template = dict((fact.succession or {}).get("template") or {})
    renamed = set(params) | set(params.values())
    blocked = [b for f in group for b in _blocked(f, report) if b["param"] not in renamed]
    blocked = list({(b["param"], b["kind"], str(b["old"]), str(b["new"])): b
                    for b in blocked}.values())
    old_spellings = {_strip_ctor(s) for f in group for s in f.spellings()}
    member = _is_member_symbol(fact.symbol)
    evidence = []
    for f in group:
        rec = f.succession or {}
        proof = rec.get("proof") or {}
        evidence.append(f"pymolt.delta proof ({proof.get('kind')}, {proof.get('side')} side): "
                        f"{f.symbol}({f.param}=) -> {f.successor}= ({proof.get('locus')}, "
                        f"{proof.get('version')}; transfer {rec.get('transfer')})")
    pattern = CodemodPattern(
        old_qualname=_strip_ctor(fact.symbol), new_qualname=_strip_ctor(fact.symbol),
        kind="rename-keyword", confidence="high", params=dict(sorted(params.items())),
        aliases=sorted(old_spellings - {_strip_ctor(fact.symbol)}),
        template={"old_shape": template.get("old_shape"), "new_shape": template.get("new_shape"),
                  "blocked": blocked, "succession": succession_summary(fact),
                  "member_leaf": fact.symbol.rsplit(".", 1)[-1] if member else None},
        evidence=evidence)
    if member:
        label = f"{fact.symbol}({', '.join(sorted(params))}=)"
        if sites is None:
            notes.append(f"{label}: no baseline sites to gate on")
            return
        leaf = fact.symbol.rsplit(".", 1)[-1]
        anchors, receivers, refused = sites.gate(
            old_spellings, leaf, leaf, (fact.succession.get("old") or {}).get("owners") or ())
        if not anchors:
            notes.append(f"{label}: no site the gate accepts"
                         + (f" ({refused[0]})" if refused else ""))
            return
        pattern.sites, pattern.receivers = anchors, receivers
    out.append(pattern)


def _renamed_keywords(template: dict) -> list[str]:
    """The old parameters a pass-through forward hands on by position to a parameter of
    another name (``url(regex, ...)`` -> ``re_path(route, ...)``): a call that passes one of
    them by keyword, or a ``**mapping``, would bind otherwise."""
    old = [p for p in template.get("old_shape") or ()
           if p[1] in ("POSITIONAL_ONLY", "POSITIONAL_OR_KEYWORD")]
    callee = [p for p in template.get("callee_old_shape") or ()
              if p[1] in ("POSITIONAL_ONLY", "POSITIONAL_OR_KEYWORD")]
    names = {p[0] for p in template.get("callee_old_shape") or ()}
    out = [p[0] for i, p in enumerate(old)
           if (i < len(callee) and callee[i][0] != p[0]) or p[0] not in names]
    return sorted(set(out))


def _defaults_differ(template: dict) -> bool:
    """A pass-through forward hands on the old method's own defaults: renaming the call is only
    the same when the callee's defaults for those parameters are equal."""
    old = {p[0]: p[2] for p in template.get("old_shape") or ()}
    callee = {p[0]: p[2] for p in template.get("callee_old_shape") or ()}
    return any(name in callee and str(old[name]) != str(callee[name]) for name in old
               if old[name] is not None)


def _is_member_symbol(symbol: str) -> bool:
    parts = _strip_ctor(symbol).split(".")
    return len(parts) > 2 and parts[-2][:1].isupper() and not symbol.endswith(".__init__") \
        and parts[-1][:1].islower()


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
        self._baseline_sites: BaselineSites | None = None

    def _sites(self) -> BaselineSites:
        if self._baseline_sites is None:
            self._baseline_sites = BaselineSites(self.project_dir)
        return self._baseline_sites

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
        proven, notes = successor_patterns(report, self._sites())
        for p in proven:
            if (p.old_qualname, p.new_qualname, p.kind, str(p.params)) not in seen:
                seen.add((p.old_qualname, p.new_qualname, p.kind, str(p.params)))
                if p.kind != "replace-attr-path":   # an import rewrite may still come too
                    seen.add((p.old_qualname, p.new_qualname))
                patterns.append(p)
        for note in notes:
            if note not in report.notes:
                report.notes.append(f"no rewrite for {note}")
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
            from pymolt.codemods.successors import KINDS as SUCCESSOR_KINDS

            # a successor pattern has no rule form: it stays a pattern
            rules = [*rules, *(as_legacy_rule(p, report) for p in patterns
                               if p.kind not in SUCCESSOR_KINDS)]
            patterns = [p for p in patterns if p.kind in SUCCESSOR_KINDS]
        return CodemodBundle(patterns=patterns, rules=rules, impacts=impacts,
                             downgraded=downgraded)
