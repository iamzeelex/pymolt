"""The apply ledger: what ``pymolt apply`` changed, line by line, and what it claimed.

``.pymolt/apply_ledger.json`` is the provenance verify judges an edited call site by: a line
pymolt itself rewrote was promised to behave the same (``claim``), so any difference falsifies
the promise; a line a person edited carries no such promise. Ranges are computed with difflib
from the file's text before and after the write, so they are exact, 1-based and inclusive.

Format::

    {"schema": "pymolt.apply_ledger/1", "run_id": ..., "applied_at": ..., "entries": [
      {"file": "app/x.py", "old_lines": [a, b], "new_lines": [c, d], "kind": "rename-call",
       "rule": "rename-call:pkg.old→pkg.new", "claim": "same-signature",
       "old_symbol": "pkg.old", "new_symbol": "pkg.new", "run_id": ...}]}

A pure insertion (an added import line) has ``old_lines`` null, a pure deletion ``new_lines``
null. ``claim`` is ``same-object``, ``same-signature``,
``verified-rule`` or ``python-fixer``; null when the producing recipe made none (heuristic).
Each entry also names the run that wrote it (``run_id``, as the file does).

Verify reads the ledger only while the run it records is the one in effect
(``ledger_in_effect``): after a rollback that restored files, its lines are no longer pymolt's.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

LEDGER_SCHEMA = "pymolt.apply_ledger/1"
CLAIMS = ("same-object", "same-signature", "verified-rule", "python-fixer")


def ledger_path(project_dir: str | Path) -> Path:
    return Path(project_dir) / ".pymolt" / "apply_ledger.json"


def claim_for(item: Any) -> str | None:
    """The claim a codemod pattern or rule makes about its edit (None: it makes none). A rule
    (it has a ``match``) that is not verified makes none, whatever its evidence says: its
    golden pair did not pass, so nothing vouches that its edit keeps the behaviour."""
    if getattr(item, "scope", "dependency") == "python":
        return "python-fixer"
    if getattr(item, "match", None) is not None and getattr(item, "confidence", None) != \
            "verified":
        return None
    evidence = [str(e) for e in getattr(item, "evidence", None) or ()]
    if any(e.startswith("pymolt.delta MOVED") for e in evidence):
        return "same-object"
    if any(e.startswith("pymolt.delta") for e in evidence):
        return "same-signature"  # the delta engine only proposes a rewrite after checking it
    if getattr(item, "confidence", None) == "verified":
        return "verified-rule"
    return None


def _parse_recipe(key: str) -> tuple[str, str | None, str | None]:
    head, sep, new = key.partition("→")
    kind, _, old = head.partition(":")
    return kind, (old or None), (new or None) if sep else None


def _recipe_for(recipes: list[str], old_text: str, new_text: str) -> str | None:
    """The recipe that most plausibly produced a hunk: the one whose old leaf is in the old
    lines, else whose new leaf or module is in the new lines; the only one if there is one."""
    def leaf(q: str | None) -> str:
        return (q or "").rsplit(".", 1)[-1]

    for key in recipes:
        _kind, old, new = _parse_recipe(key)
        if old and (leaf(old) in old_text or old in old_text) and old_text:
            return key
    for key in recipes:
        _kind, old, new = _parse_recipe(key)
        mod = (new or "").rsplit(".", 1)[0]
        if new and (leaf(new) in new_text or (mod and mod in new_text)):
            return key
    return recipes[0] if len(recipes) == 1 else None


def build_entries(file: str, old: str, new: str, recipes: list[str],
                  claims: dict[str, str]) -> list[dict[str, Any]]:
    old_lines, new_lines = old.splitlines(), new.splitlines()
    out: list[dict[str, Any]] = []
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        key = _recipe_for(recipes, "\n".join(old_lines[i1:i2]), "\n".join(new_lines[j1:j2]))
        kind, old_sym, new_sym = _parse_recipe(key) if key else (None, None, None)
        out.append({
            "file": file,
            "old_lines": [i1 + 1, i2] if i2 > i1 else None,
            "new_lines": [j1 + 1, j2] if j2 > j1 else None,
            "kind": kind, "rule": key, "claim": claims.get(key) if key else None,
            "old_symbol": old_sym, "new_symbol": new_sym,
        })
    return out


def write_ledger(project_dir: str | Path, run_id: str, applied_at: str,
                 files: list[tuple[str, str, str, list[str], dict[str, str]]]) -> Path:
    """Write the ledger for ``files`` = ``(relative path, text before, text after, recipes,
    claims)``; a previous ledger is archived to ``.pymolt/archive/``."""
    root = Path(project_dir)
    path = ledger_path(root)
    if path.is_file():
        archive = root / ".pymolt" / "archive"
        archive.mkdir(parents=True, exist_ok=True)
        stamp = re.sub(r"[^0-9A-Za-z]", "-", applied_at)
        target = archive / f"apply_ledger-{stamp}.json"
        n = 2
        while target.exists():
            target = archive / f"apply_ledger-{stamp}-{n}.json"
            n += 1
        os.replace(path, target)
    entries: list[dict[str, Any]] = []
    for rel, before, after, recipes, claims in files:
        entries += [{**entry, "run_id": run_id}
                    for entry in build_entries(rel, before, after, recipes, claims)]
    payload = {"schema": LEDGER_SCHEMA, "run_id": run_id, "applied_at": applied_at,
               "entries": entries}
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(f".{path.name}.{uuid.uuid4().hex}.pending")
    pending.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(pending, path)
    return path


def load_ledger(project_dir: str | Path) -> list[dict[str, Any]]:
    """The ledger's entries ([] when there is none or it is unreadable)."""
    found = load_ledger_run(project_dir)
    return found[1] if found is not None else []


def load_ledger_run(project_dir: str | Path) -> tuple[str | None, list[dict[str, Any]]] | None:
    """``(run_id, entries)`` of the ledger; None when there is none or it is unreadable."""
    try:
        data = json.loads(ledger_path(project_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    run = data.get("run_id")
    entries = [e for e in data.get("entries") or () if isinstance(e, dict)]
    return (run if isinstance(run, str) else None), entries


def ledger_in_effect(project_dir: str | Path, receipt: Any) -> tuple[list[dict[str, Any]] | None,
                                                                    str | None]:
    """The ledger's entries when the run it records is the one in effect (``receipt``: the
    project's ``MigrationReceipt``, or None), else None; and, when a ledger exists but is not
    used, a note that says why. A rollback that restored files leaves the ledger behind: its
    lines are no longer pymolt's, so a later edit there is judged as a hand edit."""
    found = load_ledger_run(project_dir)
    if found is None:
        return None, None
    run, entries = found
    if receipt is not None and getattr(receipt, "in_effect", False) and run is not None \
            and run == getattr(receipt, "run_id", None):
        return entries, None
    if receipt is None or not getattr(receipt, "in_effect", False):
        why = "no apply run is in effect"
    else:
        why = f"run {str(receipt.run_id)[:8]} is the one in effect"
    return None, (f"the apply ledger of run {(run or 'unknown')[:8]} is not used ({why}): "
                  "edits are judged as hand edits")
