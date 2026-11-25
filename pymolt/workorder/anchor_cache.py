"""The anchor cache: what ``status`` needs to project steps cheaply (design 1.3, 6).

HOST side. ``.pymolt/cache/anchors.json`` keeps two things:

- ``files``: each project file's sha256, keyed by its ``(mtime_ns, size)``. A file is re-read and
  re-hashed only when either changed, so a status call on an unchanged project reads no source;
- ``anchors``: each anchor's resolution (``match`` / ``lost`` / ``ambiguous``) on a text, keyed by
  the text's sha256 and the anchor's identity (key, scope, neighbours, ordinal, key kind). A text
  is parsed only when one of its anchors is not cached.

It is a cache: unreadable, missing or stale entries are recomputed, and saving it never fails the
command (``.pymolt/.gitignore`` lists ``cache/``). It is written only inside an existing
``.pymolt`` directory: a project pymolt never touched gets none.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from pymolt.state_io import atomic_write_json

logger = logging.getLogger(__name__)

CACHE_SCHEMA = "pymolt.anchor_cache/1"
#: Entries kept per section; the oldest go first when a project's history grows past it.
MAX_ENTRIES = 20000


def cache_path(project_dir: str | Path) -> Path:
    return Path(project_dir) / ".pymolt" / "cache" / "anchors.json"


def anchor_identity(anchor: Any) -> str:
    raw = "|".join(str(x) for x in (anchor.key_kind, anchor.scope, anchor.key, anchor.prev,
                                    anchor.next, anchor.nth, anchor.count))
    return hashlib.sha1(raw.encode("utf-8", "surrogatepass")).hexdigest()[:20]


class AnchorCache:
    """File hashes and anchor resolutions (module docstring). ``hits``/``misses`` count the
    lookups, for tests and profiling."""

    def __init__(self, project_dir: str | Path):
        self.root = Path(project_dir)
        self.path = cache_path(self.root)
        self.files: dict[str, list] = {}
        self.anchors: dict[str, str] = {}
        self._texts: dict[str, str] = {}
        self._indexes: dict[str, Any] = {}
        self.dirty = False
        self.hits = self.misses = self.reads = 0
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("schema") == CACHE_SCHEMA:
                self.files = dict(data.get("files") or {})
                self.anchors = dict(data.get("anchors") or {})
        except (OSError, ValueError):
            pass

    # files

    def sha(self, rel: str) -> str | None:
        """The file's sha256 now (None: missing or unreadable)."""
        path = self.root / rel
        try:
            st = path.stat()
        except OSError:
            return None
        stamp = [st.st_mtime_ns, st.st_size]
        known = self.files.get(rel)
        if known and known[:2] == stamp and isinstance(known[2], str):
            return known[2]
        try:
            data = path.read_bytes()
        except OSError:
            return None
        self.reads += 1
        digest = hashlib.sha256(data).hexdigest()
        self.files[rel] = [*stamp, digest]
        self.dirty = True
        try:
            self._texts[digest] = data.decode("utf-8")
        except UnicodeDecodeError:
            pass
        return digest

    def text(self, rel: str) -> str | None:
        digest = self.sha(rel)
        if digest is None:
            return None
        if digest not in self._texts:
            try:
                data = (self.root / rel).read_bytes()
            except OSError:
                return None
            self.reads += 1
            if hashlib.sha256(data).hexdigest() != digest:
                return None
            try:
                self._texts[digest] = data.decode("utf-8")
            except UnicodeDecodeError:
                return None
        return self._texts[digest]

    def index(self, rel: str, pythons: Any):
        """The file's statement index (None when it neither parses nor tokenizes)."""
        digest = self.sha(rel)
        if digest is None:
            return None
        if digest not in self._indexes:
            text = self.text(rel)
            self._indexes[digest] = pythons.index(text) if text is not None else None
        return self._indexes[digest]

    # anchors

    def resolve(self, rel: str, anchor: Any, pythons: Any) -> str:
        """``match`` / ``lost`` / ``ambiguous`` of ``anchor`` on the file as it is now."""
        digest = self.sha(rel)
        if digest is None:
            return "lost"
        key = f"{digest}|{anchor_identity(anchor)}"
        found = self.anchors.get(key)
        if found is not None:
            self.hits += 1
            return found
        self.misses += 1
        index = self.index(rel, pythons)
        status = index.resolve(anchor).status if index is not None else "lost"
        self.anchors[key] = status
        self.dirty = True
        return status

    def present(self, rel: str, edit: Any, pythons: Any) -> bool:
        """Whether the edit's effect sits in its slots in the file now (apply's rule 4),
        cached by the file's sha256 and the edit's two texts."""
        digest = self.sha(rel)
        if digest is None:
            return False
        key = f"{digest}|present|{edit.before_sha256}|{edit.after_sha256}"
        found = self.anchors.get(key)
        if found is not None:
            self.hits += 1
            return found == "yes"
        self.misses += 1
        from pymolt.workorder.rules import present_reasons

        index = self.index(rel, pythons)
        yes = index is not None and not present_reasons(edit, index)
        self.anchors[key] = "yes" if yes else "no"
        self.dirty = True
        return yes

    def save(self) -> None:
        """Write the cache inside an existing ``.pymolt`` (never fails the caller)."""
        if not self.dirty or not (self.root / ".pymolt").is_dir():
            return
        try:
            files = dict(list(self.files.items())[-MAX_ENTRIES:])
            anchors = dict(list(self.anchors.items())[-MAX_ENTRIES:])
            self.path.parent.mkdir(parents=True, exist_ok=True)
            from pymolt.workorder.blobs import ensure_gitignore

            ensure_gitignore(self.root)
            atomic_write_json(self.path, {"schema": CACHE_SCHEMA, "files": files,
                                          "anchors": anchors}, indent=None)
            self.dirty = False
        except OSError:
            logger.debug("could not save the anchor cache", exc_info=True)


def changed_since(project_dir: str | Path, sources_path: str | None,
                  cache: AnchorCache | None = None) -> list[str] | None:
    """The files a capture's source snapshot recorded whose text differs now (or that are gone):
    ``verify.service.sources_changed`` with the file hashes cached. None: no snapshot."""
    from pymolt.verify.service import _read_snapshot, slot_path

    data = _read_snapshot(slot_path(project_dir, sources_path)) if sources_path else None
    if data is None:
        return None
    cache = cache or AnchorCache(project_dir)
    changed = []
    for rel, entry in sorted(data["files"].items()):
        digest = cache.sha(rel)
        if digest is None or not isinstance(entry, dict) or digest != entry.get("sha256"):
            changed.append(rel)
    return changed


__all__ = ["AnchorCache", "anchor_identity", "cache_path", "changed_since"]
