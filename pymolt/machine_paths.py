"""Keep the folders of the machine out of what pymolt writes for other people to read.

A plan can be committed and attached to a ticket, so the text pymolt writes about the machine it
ran on (an interpreter it probed, the target environment setup names, the baseline's command)
names no folder of that machine: a path inside the project is written relative to it (the
project itself is ``.``), a path under the home folder as ``~/...``. Other paths are kept: they
name no person. The project's own text (sources, anchors, manifest lines) is data and is never
rewritten; ``scrub_prose`` touches only the fields that hold pymolt's prose.

Pure and stdlib only.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel

# The fields that hold pymolt's prose about the machine, by name, wherever they sit in a model.
PROSE_FIELDS = frozenset({
    "command", "commands", "evidence", "explanation", "note", "notes", "pitfalls", "reason",
    "sides", "summary",
})

_SEP = r"[/\\]"
# A path-name character: ``/a/repo`` is not the start of ``/a/repo-old`` or of ``x/a/repo``.
_NAME = r"[\w.-]"


def _spellings(path: str | Path) -> set[str]:
    out = set()
    for spelling in (os.path.abspath(path), os.path.realpath(path)):
        spelling = spelling.rstrip("/\\")
        if spelling:
            out.add(spelling)
    return out


def _home() -> str | None:
    try:
        return str(Path.home())
    except (KeyError, RuntimeError):  # no home folder known
        return None


class PathScrubber:
    """Rewrites the project's folder and the home folder in text (``text``) or in a JSON-like
    value (``__call__``)."""

    def __init__(self, project_dir: str | Path | None, *, home: str | Path | None = None):
        kinds: dict[str, str] = {}
        home = _home() if home is None else home
        if home:
            kinds.update(dict.fromkeys(_spellings(home), "home"))
        if project_dir is not None:
            for spelling in _spellings(project_dir):
                kinds[spelling] = "project"
        self._kinds = kinds
        # The longest spelling first: a project under the home folder is the project.
        alternatives = "|".join(re.escape(s) for s in sorted(kinds, key=len, reverse=True))
        self._pattern = (re.compile(rf"(?<!{_NAME})(?P<path>{alternatives})"
                                    rf"(?:(?P<sep>{_SEP})|(?!{_NAME}))")
                         if kinds else None)

    def _replace(self, match: re.Match[str]) -> str:
        sep = match.group("sep")
        if self._kinds[match.group("path")] == "project":
            return "" if sep else "."
        return "~/" if sep else "~"

    def text(self, value: str) -> str:
        return self._pattern.sub(self._replace, value) if self._pattern else value

    def __call__(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self(item) for item in value)
        if isinstance(value, dict):
            return {key: self(item) for key, item in value.items()}
        return value


def scrub_prose(value: Any, scrubber: PathScrubber) -> None:
    """Rewrite, in place, every ``PROSE_FIELDS`` field under ``value`` (pydantic models, dicts
    and lists); every other field is left as it is."""
    if isinstance(value, BaseModel):
        for name in type(value).model_fields:
            item = getattr(value, name, None)
            if name in PROSE_FIELDS:
                setattr(value, name, scrubber(item))
            else:
                scrub_prose(item, scrubber)
    elif isinstance(value, dict):
        for key, item in value.items():
            if key in PROSE_FIELDS:
                value[key] = scrubber(item)
            else:
                scrub_prose(item, scrubber)
    elif isinstance(value, list):
        for item in value:
            scrub_prose(item, scrubber)
