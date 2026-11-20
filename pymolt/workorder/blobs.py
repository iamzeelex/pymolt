"""The blob store: every text a ledger event or a ``--record`` snapshot names, once, by sha256.

HOST side (3.12+, stdlib). ``.pymolt/blobs/<sha256>.gz`` holds one text: its UTF-8 bytes, gzip
compressed with no timestamp, so a blob's bytes depend on its text alone. A blob is written once,
atomically, and never changed; reading one checks its hash, and a blob that does not match is
treated as missing. Design: ``docs/design/apply-work-order.md`` 2.1.

The store holds copies of project source, so it writes ``.pymolt/.gitignore`` with ``blobs/``,
``cache/`` and ``migrations/*/steps/``. The project's own ``.gitignore`` is never touched.

**Retention.** A blob stays while one of these names it:

- an event of a run in the receipt chain (the runs whose code is on disk);
- a snapshot manifest of a step ``in_progress``
  (``migrations/<run>/steps/<step>/snapshot-*.json``, ``{file: sha256}``);
- an event of one of the two runs that most recently left the chain: closed, with no step in
  effect (a schema-1 receipt records no closing, so these are the two latest other runs of the
  ledger).

``collect_blobs`` removes the rest; ``plan`` and ``apply`` run it under the migration lock. It
removes nothing when the ledger or the receipt is damaged (``StateFileError``).
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from pymolt.state_io import atomic_write_bytes

logger = logging.getLogger(__name__)

GITIGNORE_LINES = ("blobs/", "cache/", "migrations/*/steps/")
_GITIGNORE_HEAD = "# pymolt: local stores that hold copies of the project's source\n"
_SHA = re.compile(r"^[0-9a-f]{64}$")
#: How many runs that left the chain keep their blobs.
CLOSED_RUNS_KEPT = 2
_UNSET: Any = object()


def pymolt_dir(project_dir: str | Path) -> Path:
    return Path(project_dir) / ".pymolt"


def blobs_dir(project_dir: str | Path) -> Path:
    return pymolt_dir(project_dir) / "blobs"


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def blob_path(project_dir: str | Path, digest: str) -> Path:
    if not _SHA.match(digest or ""):
        raise ValueError(f"not a sha256: {digest!r}")
    return blobs_dir(project_dir) / f"{digest}.gz"


def ensure_gitignore(project_dir: str | Path) -> Path:
    """``.pymolt/.gitignore`` with the store's lines (missing ones are appended; the rest of the
    file is kept)."""
    path = pymolt_dir(project_dir) / ".gitignore"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        current = path.read_text(encoding="utf-8")
    except OSError:
        current = ""
    have = {line.strip() for line in current.splitlines()}
    missing = [line for line in GITIGNORE_LINES if line not in have]
    if missing:
        text = current if current else _GITIGNORE_HEAD
        if text and not text.endswith("\n"):
            text += "\n"
        _write_atomic(path, (text + "".join(f"{line}\n" for line in missing)).encode())
    return path


def _write_atomic(path: Path, data: bytes) -> None:
    atomic_write_bytes(path, data)


def put_text(project_dir: str | Path, text: str) -> str:
    """Store ``text`` (once) and return its sha256."""
    digest = sha256(text)
    path = blob_path(project_dir, digest)
    if path.is_file():
        return digest
    ensure_gitignore(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_atomic(path, gzip.compress(text.encode("utf-8", "surrogatepass"), mtime=0))
    return digest


def put_texts(project_dir: str | Path, texts: Iterable[str]) -> list[str]:
    return [put_text(project_dir, text) for text in texts]


def get_text(project_dir: str | Path, digest: str) -> str | None:
    """The text stored under ``digest``; None when it is missing or does not match its hash."""
    try:
        raw = gzip.decompress(blob_path(project_dir, digest).read_bytes())
    except (OSError, ValueError, EOFError, gzip.BadGzipFile):
        return None
    if hashlib.sha256(raw).hexdigest() != digest:
        return None
    return raw.decode("utf-8", "surrogatepass")


def has_blob(project_dir: str | Path, digest: str) -> bool:
    try:
        return blob_path(project_dir, digest).is_file()
    except ValueError:
        return False


def stored(project_dir: str | Path) -> set[str]:
    """Every sha256 the store holds."""
    root = blobs_dir(project_dir)
    if not root.is_dir():
        return set()
    return {p.name[:-3] for p in root.iterdir() if p.name.endswith(".gz")
            and _SHA.match(p.name[:-3])}


# ── retention ─────────────────────────────────────────────────────────────────────────────


def snapshot_hashes(manifest: Path) -> set[str]:
    """The hashes a ``--record`` snapshot manifest names (``{file: sha256}``; a file listed by
    hash only, ``{"sha256": ..., "hash_only": true}``, names one too)."""
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    files = data.get("files", data) if isinstance(data, dict) else {}
    out: set[str] = set()
    for value in files.values() if isinstance(files, dict) else ():
        digest = value.get("sha256") if isinstance(value, dict) else value
        if isinstance(digest, str) and _SHA.match(digest):
            out.add(digest)
    return out


def _in_progress_manifests(root: Path, receipt: Any) -> list[Path]:
    steps = getattr(receipt, "steps", None) or {}
    out: list[Path] = []
    for key, step in steps.items() if isinstance(steps, dict) else ():
        if getattr(step, "status", None) != "in_progress" or ":" not in key:
            continue
        run, step_id = key.split(":", 1)
        folder = pymolt_dir(root) / "migrations" / run / "steps" / step_id
        out.extend(sorted(folder.glob("snapshot-*.json")) if folder.is_dir() else ())
    return out


def kept_runs(ledger: Any, receipt: Any) -> set[str]:
    """The runs whose events keep their blobs: the chain, and the two that left it last."""
    chain: set[str] = set()
    if receipt is not None:
        method = getattr(receipt, "chain_run_ids", None)
        chain = set(method()) if callable(method) else set()
    runs = list(getattr(receipt, "runs", None) or ())
    if runs:
        left = sorted((r for r in runs if getattr(r, "closed", False) and r.run_id not in chain),
                      key=lambda r: r.closed_at or "", reverse=True)
        return chain | {r.run_id for r in left[:CLOSED_RUNS_KEPT]}
    # No run history (a schema-1 receipt, or none): the latest other runs of the ledger.
    last: dict[str, int] = {}
    for event in getattr(ledger, "events", None) or ():
        last[event.run_id] = max(last.get(event.run_id, 0), event.seq)
    others = sorted((run for run in last if run not in chain), key=lambda r: -last[r])
    return chain | set(others[:CLOSED_RUNS_KEPT])


def referenced(project_dir: str | Path, ledger: Any, receipt: Any) -> set[str]:
    """Every hash retention keeps (see the module docstring)."""
    root = Path(project_dir)
    runs = kept_runs(ledger, receipt)
    keep: set[str] = set()
    for event in getattr(ledger, "events", None) or ():
        if event.run_id not in runs:
            continue
        for item in event.files:
            keep.update(h for h in (item.before, item.after) if h)
    for manifest in _in_progress_manifests(root, receipt):
        keep |= snapshot_hashes(manifest)
    return keep


def collect_blobs(project_dir: str | Path, *, ledger: Any = _UNSET,
                  receipt: Any = _UNSET) -> list[str]:
    """Remove the blobs retention does not keep; returns their hashes. The caller holds the
    migration lock. Nothing is removed while the ledger or the receipt exists but cannot be
    read: what it references is unknown."""
    from pymolt.state_io import StateFileError

    root = Path(project_dir)
    if not blobs_dir(root).is_dir():
        return []
    try:
        if receipt is _UNSET:
            from pymolt.migration_state import MigrationReceipt

            receipt = MigrationReceipt.load(root)
        if ledger is _UNSET:
            from pymolt.apply_ledger import load_ledger

            ledger = load_ledger(root, receipt=receipt)
    except StateFileError:
        return []                       # what a damaged receipt or ledger references is unknown
    keep = referenced(root, ledger, receipt)
    removed: list[str] = []
    for digest in sorted(stored(root) - keep):
        try:
            blob_path(root, digest).unlink()
            removed.append(digest)
        except OSError:
            continue
    # A write interrupted before its rename (``state_io.atomic_write_bytes``; ``.pending``:
    # an older pymolt's). Blobs are written under the migration lock, which the caller holds.
    for partial in [*blobs_dir(root).glob(".*.tmp"), *blobs_dir(root).glob(".*.pending")]:
        try:
            partial.unlink()
        except OSError:
            pass
    return removed


def collect_after_plan(project_dir: str | Path) -> list[str]:
    """``plan``'s collection: under the migration lock when it is free (another migration
    operation running leaves it for later). Never raises."""
    from pymolt.migration_plan import MigrationConflictError, _migration_lock

    try:
        if not blobs_dir(project_dir).is_dir():
            return []
        with _migration_lock(project_dir, "plan: collect blobs"):
            return collect_blobs(project_dir)
    except MigrationConflictError:
        return []
    except Exception:  # noqa: BLE001 - housekeeping never fails the command
        logger.warning("could not collect the blob store", exc_info=True)
        return []


__all__ = [
    "CLOSED_RUNS_KEPT", "GITIGNORE_LINES", "blob_path", "blobs_dir", "collect_after_plan",
    "collect_blobs", "ensure_gitignore", "get_text", "has_blob", "kept_runs", "put_text",
    "put_texts", "referenced", "sha256", "snapshot_hashes", "stored",
]
