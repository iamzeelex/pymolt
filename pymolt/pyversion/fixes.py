"""Python fixers: the plan's rewrites for stdlib removals, built from python facts.

``python_patterns(report, sites)`` turns a python ``DeltaReport`` plus the curated ``fix`` kind
(``core.stdlib.changes_for``) into ``CodemodPattern``s with ``scope="python"``:

- ``advisory`` gives no pattern: the site stays manual;
- ``rewrite-attr`` / ``replace-attr-path`` rewrite attribute chains (``collections.Iterable`` →
  ``collections.abc.Iterable`` with ``import collections.abc`` added; ``imp.reload`` →
  ``importlib.reload`` with ``import imp`` dropped when unused), and, where the old spelling is a
  module-level name, the ``from M import name`` form;
- ``rewrite-import`` renames a module (``xml.etree.cElementTree``);
- ``runs_on_base`` is true when the successor is the same object, or existed on the base
  (``successor_since <= base``); false when it is newer than the base; ``None`` when unknown.

``apply_python_fixes`` applies them to the files that hold flagged, live sites only (a guarded
fallback is never rewritten), enforces the minimal-diff rule (D8: every changed line holds a
flagged spelling) and re-compiles each rewritten file on the target (and on the base when the
rewrite ``runs_on_base``). A file that fails either check keeps its pre-python source and the
reason goes into the notes.
"""

from __future__ import annotations

import difflib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pymolt.codemods.models import CodemodPattern, FilePreview
from pymolt.pyversion.guards import evaluate_guard, parse_version

_RECOMPILE_SCRIPT = r'''
import json, sys
request = json.loads(sys.stdin.read())
bad = []
for item in request["files"]:
    try:
        compile(item["source"], item["path"], "exec", dont_inherit=True)
    except (SyntaxError, ValueError, RecursionError) as exc:
        bad.append({"path": item["path"], "error": "%s: %s (line %s)" % (
            type(exc).__name__, getattr(exc, "msg", None) or exc, getattr(exc, "lineno", None))})
print("@@pymolt-recompile@@ " + json.dumps({"python": "%d.%d" % sys.version_info[:2],
                                            "bad": bad}))
'''


def _get(obj: Any, name: str) -> Any:
    return obj.get(name) if isinstance(obj, Mapping) else getattr(obj, name, None)


def _stdlib():
    from pymolt.core import stdlib

    return stdlib


def _minor(value: str | None) -> tuple[int, int] | None:
    return parse_version(value)


def _runs_on_base(change, base: str | None) -> bool | None:
    """Does the successor exist on the base? Same object, or introduced at or before it."""
    if change.same_object:
        return True
    since, base_v = _minor(change.successor_since), _minor(base)
    if since is None or base_v is None:
        return None
    return since <= base_v


def _root(dotted: str) -> str:
    return dotted.split(".")[0]


def _common_root(a: str, b: str) -> str:
    out: list[str] = []
    for x, y in zip(a.split("."), b.split("."), strict=False):
        if x != y:
            break
        out.append(x)
    return ".".join(out)


def _module_part(dotted: str) -> str | None:
    """The longest module prefix of ``dotted`` below the name itself (``collections.abc`` for
    ``collections.abc.Iterable``), or None."""
    sd = _stdlib()
    parts = dotted.split(".")
    for i in range(len(parts) - 1, 0, -1):
        prefix = ".".join(parts[:i])
        if sd.is_module(prefix):
            return prefix
    return None


def _best_change(spelling: str):
    changes = _stdlib().changes_for(spelling)
    exact = [c for c in changes if spelling in c.symbols]
    pool = exact or changes
    return pool[0] if pool else None


def _live(site: Any, target: str | None) -> bool:
    """Is this site one the plan may rewrite? Unguarded, under a broad ``except`` (it still
    runs), or behind a version test that is live on the target. A fallback is never live."""
    guard = _get(site, "guard")
    if not isinstance(guard, Mapping):
        return True
    kind = guard.get("kind")
    if kind == "broad":
        return True
    if kind == "version":
        target_v = _minor(target)
        if target_v is None:
            return False
        sd = _stdlib()
        live = evaluate_guard(str(guard.get("detail") or ""), target_v,
                              lambda dotted: sd.present_in(dotted, target or ""))
        return live is True
    return False


def live_lines(sites: Iterable[Any], target: str | None) -> dict[tuple[str, str], frozenset[int]]:
    """``{(file, spelling): lines}`` of the live sites."""
    out: dict[tuple[str, str], set[int]] = {}
    for site in sites or ():
        file, spelling, line = _get(site, "file"), _get(site, "spelling"), _get(site, "line")
        if not isinstance(file, str) or not isinstance(spelling, str) or not isinstance(line, int):
            continue
        if _live(site, target):
            out.setdefault((Path(file).as_posix(), spelling), set()).add(line)
    return {k: frozenset(v) for k, v in out.items()}


def _fact_fields(fact: Any) -> tuple[str | None, str | None, str | None]:
    return _get(fact, "symbol"), _get(fact, "kind"), _get(fact, "successor")


def python_patterns(report: Any, sites: Iterable[Any]) -> list[CodemodPattern]:
    """Fix patterns for the python delta's REMOVED/MOVED facts that have a live site."""
    facts = _get(report, "facts") or []
    base = _get(report, "from_version") or _get(report, "from")
    target = _get(report, "to_version") or _get(report, "to")
    live_spellings = {spelling for (_, spelling) in live_lines(sites, target)}
    out: list[CodemodPattern] = []
    seen: set[tuple[str, str, str]] = set()
    for fact in facts:
        symbol, kind, successor = _fact_fields(fact)
        if not symbol or kind not in ("REMOVED", "MOVED") or symbol not in live_spellings:
            continue
        change = _best_change(symbol)
        if change is None or change.fix == "advisory":
            continue
        new = successor or change.successor_for(symbol)
        if not new or new == symbol:
            continue
        for pattern in _patterns_for(symbol, new, change, base):
            key = (pattern.kind, pattern.old_qualname, pattern.new_qualname)
            if key not in seen:
                seen.add(key)
                out.append(pattern)
    return out


def _make(kind: str, old: str, new: str, change, base: str | None,
          requires_import: str | None = None) -> CodemodPattern:
    return CodemodPattern(
        old_qualname=old, new_qualname=new, kind=kind,
        confidence="high" if change.same_object else "medium",
        evidence=[f"curated {change.id}: {old} -> {new}",
                  *([change.hint] if change.hint else [])],
        requires_import=requires_import, scope="python",
        runs_on_base=_runs_on_base(change, base))


def _patterns_for(old: str, new: str, change, base: str | None) -> list[CodemodPattern]:
    sd = _stdlib()
    out: list[CodemodPattern] = []
    if change.fix == "rewrite-import" and sd.is_module(old):
        out.append(_make("rewrite-module", old, new, change, base))
    elif change.fix in ("rewrite-attr", "replace-attr-path"):
        new_module = _module_part(new)
        if change.fix == "rewrite-attr" and _root(old) == _root(new):
            if sd.is_module(_common_root(old, new)):
                need = new_module if new_module and new_module != _common_root(old, new) \
                    else None
                out.append(_make("rewrite-attr", old, new, change, base, need))
        else:
            need = new_module if new_module and _root(new) != _root(old) else None
            out.append(_make("replace-attr-path", old, new, change, base, need))
    else:
        return out
    # the ``from MODULE import name`` form of a module-level name
    parent_old, _, leaf_old = old.rpartition(".")
    parent_new, _, leaf_new = new.rpartition(".")
    if (parent_old and sd.is_module(parent_old) and parent_new and sd.is_module(parent_new)
            and change.fix != "rewrite-import"):
        out.append(_make("rewrite-import" if leaf_old == leaf_new else "rename-call", old, new,
                         change, base))
    elif change.fix == "rewrite-import" and parent_old and sd.is_module(parent_old) \
            and sd.is_module(parent_new):
        out.append(_make("rename-call", old, new, change, base))
    return out


# ── minimal diff (D8) ─────────────────────────────────────────────────────────────────────


_IMPORT_LINE = re.compile(r"^\s*(import|from)\s+[\w.]+")


def minimal_diff_violations(old: str, new: str, patterns: Iterable[CodemodPattern]) -> list[str]:
    """Changed lines that hold no flagged spelling. Every removed/replaced old line must name
    the leaf (or module) of a rewritten spelling; an inserted line must be an import (the
    ``requires_import`` the rewrite adds) or blank; a deleted line must be an import."""
    leaves = set()
    for p in patterns:
        leaves.add(p.old_qualname.rsplit(".", 1)[-1])
        leaves.add(p.old_qualname.split(".")[0])
        leaves.add(p.old_qualname)
    bad: list[str] = []
    old_lines, new_lines = old.splitlines(), new.splitlines()
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        for line in old_lines[i1:i2]:
            if tag == "delete" and _IMPORT_LINE.match(line):
                continue
            if not any(leaf in line for leaf in leaves):
                bad.append(f"line {old_lines.index(line, i1) + 1}: {line.strip()[:80]}")
        for line in new_lines[j1:j2]:
            if tag == "insert" and (not line.strip() or _IMPORT_LINE.match(line)):
                continue
            if tag == "replace" and (not line.strip() or _IMPORT_LINE.match(line)
                                     or any(leaf in line for leaf in leaves)):
                continue
            if tag == "insert":
                bad.append(f"inserted: {line.strip()[:80]}")
    return bad


# ── re-compile ────────────────────────────────────────────────────────────────────────────


def recompile(files: dict[str, str], executable: str, *, runner: Any = None,
              timeout: float = 120.0) -> tuple[dict[str, str], str | None]:
    """Compile each ``{path: source}`` on ``executable`` (through ``runner`` when given).

    Returns ``({path: error}, failure)``: ``failure`` is set when the check itself could not
    run (nothing is then known about the files)."""
    from pymolt.adapters.python_runner import run_in

    request = json.dumps({"files": [{"path": p, "source": s} for p, s in files.items()]})
    try:
        proc = run_in(runner, [executable, "-B", "-c", _RECOMPILE_SCRIPT], stdin=request,
                      timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - the check not running is a note, not a crash
        return {}, f"{type(exc).__name__}: {exc}"
    for line in (proc.stdout or "").splitlines():
        if line.startswith("@@pymolt-recompile@@ "):
            try:
                data = json.loads(line[len("@@pymolt-recompile@@ "):])
            except ValueError:
                break
            return {b["path"]: b["error"] for b in data["bad"]}, None
    tail = ((proc.stderr or "").strip().splitlines() or [""])[-1]
    return {}, f"exit {proc.returncode}: {tail[:160]}"


# ── applying ──────────────────────────────────────────────────────────────────────────────


@dataclass
class FixOutcome:
    previews: list[FilePreview]
    notes: list[str] = field(default_factory=list)
    patterns: list[CodemodPattern] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    dropped: dict[str, str] = field(default_factory=dict)


def apply_python_fixes(
    root: str | Path, previews: list[FilePreview], patterns: list[CodemodPattern],
    sites: Iterable[Any], *, target: str | None, target_executable: str | None = None,
    base_executable: str | None = None, base_runner: Any = None,
) -> FixOutcome:
    """Layer the python ``patterns`` onto ``previews`` (dependency rewrites), site files only.

    Nothing is written. The result's previews hold, per touched file, the dependency rewrite
    followed by the python rewrite, so one diff shows both."""
    from pymolt.codemods.apply import apply_patterns

    root = Path(root)
    outcome = FixOutcome(previews=list(previews))
    if not patterns:
        return outcome
    lines = live_lines(sites, target)
    by_file: dict[str, set[str]] = {}
    for rel, spelling in lines:
        by_file.setdefault(rel, set()).add(spelling)
    existing = {Path(p.path).resolve(): p for p in previews}

    staged: dict[str, tuple[FilePreview | None, str, str, list]] = {}
    for rel in sorted(by_file):
        wanted = [p for p in patterns if p.old_qualname in by_file[rel]]
        if not wanted:
            continue
        path = (root / rel)
        prior = existing.get(path.resolve())
        try:
            original = path.read_text(encoding="utf-8") if prior is None else prior.old_source
        except (OSError, UnicodeDecodeError):
            continue
        start = original if prior is None else prior.new_source

        def lines_for(pattern: CodemodPattern, rel=rel) -> frozenset[int]:
            return lines.get((rel, pattern.old_qualname), frozenset())

        try:
            new, hits = apply_patterns(start, wanted, lines_for=lines_for)
        except Exception as exc:  # noqa: BLE001 - an unparseable file is skipped
            outcome.notes.append(f"python fixes skipped {rel}: {type(exc).__name__}: {exc}")
            continue
        if new == start or not hits:
            continue
        bad = minimal_diff_violations(start, new, [p for p, _ in hits])
        if bad:
            reason = "its rewrite changed a line with no flagged spelling (" + bad[0] + ")"
            outcome.dropped[rel] = reason
            outcome.notes.append(f"python rewrite of {rel} dropped: {reason}")
            continue
        staged[rel] = (prior, original, new, hits)

    if staged:
        _recompile_gate(staged, outcome, target_executable, base_executable, base_runner)

    merged = {Path(p.path).resolve(): p for p in previews}
    for rel, (prior, original, new, hits) in staged.items():
        if rel in outcome.dropped:
            continue
        path = root / rel
        used = [p for p, _ in hits]
        n = sum(c for _, c in hits)
        if prior is None:
            merged[path.resolve()] = FilePreview(
                path=str(path), old_source=original, new_source=new, sites=n, patterns=used)
        else:
            merged[path.resolve()] = prior.model_copy(update={
                "new_source": new, "sites": prior.sites + n,
                "patterns": [*prior.patterns, *used]})
        outcome.files.append(rel)
        outcome.patterns.extend(p for p in used if p not in outcome.patterns)
    order = {Path(p.path).resolve(): i for i, p in enumerate(previews)}
    outcome.previews = sorted(merged.values(), key=lambda p: (
        order.get(Path(p.path).resolve(), len(order)), p.path))
    return outcome


def _recompile_gate(staged: dict, outcome: FixOutcome, target_exe: str | None,
                    base_exe: str | None, base_runner: Any) -> None:
    files = {rel: item[2] for rel, item in staged.items()}
    if target_exe:
        bad, failure = recompile(files, target_exe)
        if failure:
            outcome.notes.append(f"re-compile on the target did not run ({failure})")
        for rel, error in bad.items():
            outcome.dropped[rel] = f"does not compile on the target: {error}"
            outcome.notes.append(f"python rewrite of {rel} dropped: {outcome.dropped[rel]}")
    else:
        outcome.notes.append("re-compile on the target skipped: no interpreter for it")
    on_base = {rel: staged[rel][2] for rel in staged
               if rel not in outcome.dropped
               and all(p.runs_on_base is True for p, _ in staged[rel][3])}
    if not on_base:
        return
    if base_runner is None and not base_exe:
        outcome.notes.append("re-compile on the base skipped: no base interpreter or runner")
        return
    exe = base_exe or getattr(base_runner, "executable", "python")
    bad, failure = recompile(on_base, str(exe), runner=base_runner)
    if failure:
        outcome.notes.append(f"re-compile on the base did not run ({failure})")
    for rel, error in bad.items():
        outcome.dropped[rel] = f"does not compile on the base: {error}"
        outcome.notes.append(f"python rewrite of {rel} dropped: {outcome.dropped[rel]}")
