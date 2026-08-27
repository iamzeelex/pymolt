"""
axiom_graph/acquisition/checkpoint.py

Incremental checkpoint system for long-running pipeline runs.

Format: JSONL (one PairwiseDelta per line) stored at:
  {CHECKPOINT_DIR}/{package}__{from_v}__{to_v}.jsonl

Design:
- Append-only: each completed step is immediately flushed.
- Idempotent: duplicate step keys (from_v, to_v) are deduplicated on read.
- Resumable: pipeline reads existing checkpoints and skips already-done steps.
- Human-readable: valid JSON per line, inspectable with jq.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import platformdirs

from axiom_graph.core.models import PairwiseDelta

log = logging.getLogger(__name__)

CHECKPOINT_DIR: Path = Path(platformdirs.user_cache_dir("axiom_graph")) / "checkpoints"


def _checkpoint_path(package: str, from_v: str, to_v: str) -> Path:
    """Canonical checkpoint file path for a given delta run."""
    # Double underscore separator — versions can't contain '__'
    safe_name = f"{package}__{from_v}__{to_v}.jsonl"
    return CHECKPOINT_DIR / safe_name


def load_checkpoint(package: str, from_v: str, to_v: str) -> list[PairwiseDelta]:
    """
    Load all previously completed PairwiseDelta steps from the checkpoint file.

    Returns:
        List of PairwiseDelta objects, deduplicated by (from_version, to_version).
        Empty list if no checkpoint exists or file is unreadable.
    """
    path = _checkpoint_path(package, from_v, to_v)
    if not path.exists():
        return []

    seen: set[tuple[str, str]] = set()
    deltas: list[PairwiseDelta] = []

    try:
        for line_num, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                delta = PairwiseDelta.model_validate(data)
                key = (delta.from_version, delta.to_version)
                if key not in seen:
                    seen.add(key)
                    deltas.append(delta)
            except Exception as exc:
                log.warning("Checkpoint line %d unreadable (skipping): %s", line_num, exc)
    except OSError as exc:
        log.warning("Could not read checkpoint %s: %s", path, exc)
        return []

    if deltas:
        log.info(
            "Checkpoint resumed: %d completed step(s) for %s %s→%s",
            len(deltas), package, from_v, to_v
        )
    return deltas


def save_step(delta: PairwiseDelta, package: str, from_v: str, to_v: str) -> None:
    """
    Append a completed PairwiseDelta to the checkpoint file.

    This is called immediately after each step completes, so a crash
    or interruption never loses already-computed steps.

    Never raises — logs and silently continues on write failure.
    """
    path = _checkpoint_path(package, from_v, to_v)
    try:
        CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
        line = delta.model_dump_json() + "\n"
        with path.open("a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
    except OSError as exc:
        log.warning("Could not write checkpoint step to %s: %s", path, exc)


def clear_checkpoint(package: str, from_v: str, to_v: str) -> bool:
    """
    Delete the checkpoint file for a run. Use when forcing a full re-run.

    Returns True if deleted, False if not found.
    """
    path = _checkpoint_path(package, from_v, to_v)
    if path.exists():
        path.unlink()
        log.info("Cleared checkpoint: %s", path)
        return True
    return False


def checkpoint_path(package: str, from_v: str, to_v: str) -> Path:
    """Public accessor for the checkpoint file path (for display in CLI)."""
    return _checkpoint_path(package, from_v, to_v)
