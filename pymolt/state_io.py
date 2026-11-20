"""The project's state files under ``.pymolt/``: read strictly.

HOST side (3.12+, pydantic). Never imported by the injected runtime (``pymolt.verify``'s
watcher modules run inside the traced program's interpreter and have their own writers).

**State files are strict.** ``contract_state.json``, ``migration_state.json``,
``apply_ledger.json``, ``migration_plan.json``, ``migrations/<run>/state.json`` and
``plan.json``, ``env_config.json``, ``verification.json`` and the approvals are read with
``read_state_json`` / ``load_state``: a missing file is ``None``, but a damaged one (unreadable,
not JSON, not an object, or not valid for its model) or one written by a newer pymolt (its
schema above what this pymolt reads) raises ``StateFileError``. Read as "missing", a damaged
receipt or ledger would make apply and verify act on a world without the run, and the next
write would replace the file: the error names the file and what to do, and nothing overwrites
it. ``status`` reports such a file as a problem of its phase instead of failing.

**Derived files are lenient.** Everything under ``.pymolt/cache/`` and the other caches
(delta, risk, forks, resolve, name mapping, anchors, identities, contact map, noise profile)
is rebuilt when it cannot be read: their own readers treat damage as a miss.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

Problem = Literal["damaged", "newer"]

#: What a damaged state file's message suggests when it can be rebuilt.
ARCHIVE_DIR = ".pymolt/archive/"


class StateFileError(Exception):
    """A state file pymolt will not read as missing, nor overwrite: it is damaged (unreadable,
    not JSON, not an object, invalid for its model) or was written by a newer pymolt.

    ``path``: the file. ``kind``: what it is, for a person ("capture state", "apply receipt",
    ...). ``problem``: ``damaged`` or ``newer``. ``detail``: what is wrong. ``remedy``: what to
    do. ``headline`` is the message without the remedy (the CLI prints the remedy as its hint);
    ``str()`` is both."""

    def __init__(self, path: str | Path, *, kind: str, problem: Problem, detail: str,
                 remedy: str) -> None:
        self.path = Path(path)
        self.kind = kind
        self.problem = problem
        self.detail = detail
        self.remedy = remedy
        super().__init__(f"{self.headline} {self.remedy}")

    @property
    def shown_path(self) -> str:
        return display_path(self.path)

    @property
    def headline(self) -> str:
        if self.problem == "newer":
            return f"{self.shown_path} ({self.kind}) was written by a newer pymolt ({self.detail})."
        return (f"{self.shown_path} ({self.kind}) is damaged: {self.detail}. "
                "pymolt will not overwrite it.")

    def fields(self) -> dict[str, str]:
        """The structured part of a failure envelope (``--json``, ``--agent``, MCP)."""
        return {"path": str(self.path), "kind": self.kind, "problem": self.problem,
                "detail": self.detail, "remedy": self.remedy}

    def as_dict(self) -> dict[str, str]:
        return {"error": self.headline, **self.fields()}

    def __reduce__(self):  # keyword-only arguments: picklable across processes
        return (_rebuild_error, (str(self.path), self.kind, self.problem, self.detail,
                                 self.remedy))


def _rebuild_error(path: str, kind: str, problem: Problem, detail: str,
                   remedy: str) -> StateFileError:
    return StateFileError(path, kind=kind, problem=problem, detail=detail, remedy=remedy)


def display_path(path: str | Path) -> str:
    """A state file as people see it: from its ``.pymolt`` directory on
    (``.pymolt/migrations/<run>/plan.json``), else the path as given."""
    parts = Path(path).parts
    if ".pymolt" in parts:
        start = len(parts) - 1 - parts[::-1].index(".pymolt")
        return Path(*parts[start:]).as_posix()
    return str(path)


def rebuild_remedy(then: str) -> str:
    """The remedy for a state file pymolt can rebuild: keep the damaged one, rebuild it."""
    return f"Move it into {ARCHIVE_DIR} to keep it, then {then}."


def keep_remedy(holds: str) -> str:
    """The remedy for a state file nothing rebuilds (the receipt, the ledger, a run's files)."""
    return (f"It holds {holds}: restore it from version control or a backup (or from "
            f"{ARCHIVE_DIR}) before anything else, and do not delete it.")


NEWER_REMEDY = "Upgrade pymolt."


def schema_version(data: dict[str, Any]) -> int:
    """The ``schema_version`` of a state file (absent or null: 1)."""
    value = data.get("schema_version")
    if value is None:
        return 1
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"its schema_version is {value!r}, not a number")
    return value


#: The state files ``status`` reports as problems instead of reading (``ignoring``).
_IGNORED: ContextVar[frozenset[Path]] = ContextVar("pymolt_state_ignored", default=frozenset())


@contextmanager
def ignoring(paths: Iterable[str | Path]) -> Iterator[None]:
    """Within the block, ``read_state_json`` treats ``paths`` as missing. Only for a reader
    that writes nothing and reports each of them instead (``pymolt.status``)."""
    token = _IGNORED.set(_IGNORED.get() | {Path(p).resolve() for p in paths})
    try:
        yield
    finally:
        _IGNORED.reset(token)


def _damaged(path: Path, kind: str, detail: str, remedy: str) -> StateFileError:
    return StateFileError(path, kind=kind, problem="damaged", detail=detail, remedy=remedy)


def read_state_json(path: str | Path, *, kind: str, newest: int | None, remedy: str,
                    version_of: Callable[[dict[str, Any]], int] | None = None
                    ) -> dict[str, Any] | None:
    """A state file's JSON object; None when the file does not exist.

    Raises ``StateFileError``: ``damaged`` when it cannot be read, is not JSON or not a JSON
    object (or its version is not a number); ``newer`` when its version (``version_of``,
    default ``schema_version``) is above ``newest`` (None: the file has no version). An older
    version is returned as it is, for the caller's adapter."""
    path = Path(path)
    ignored = _IGNORED.get()
    if ignored and path.resolve() in ignored:
        return None
    try:
        raw = path.read_bytes()
    except (FileNotFoundError, NotADirectoryError):
        return None
    except IsADirectoryError:
        raise _damaged(path, kind, "it is a directory, not a file", remedy) from None
    except OSError as exc:
        raise _damaged(path, kind, f"it cannot be read ({exc.strerror or exc})", remedy) from exc
    if not raw.strip():
        raise _damaged(path, kind, "the file is empty", remedy)
    try:
        data = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise _damaged(path, kind, "it is not UTF-8 text", remedy) from exc
    except json.JSONDecodeError as exc:
        raise _damaged(path, kind, f"it is not JSON ({exc.msg} at line {exc.lineno} column "
                                   f"{exc.colno})", remedy) from exc
    except (ValueError, RecursionError) as exc:
        raise _damaged(path, kind, f"it is not JSON ({exc})", remedy) from exc
    if not isinstance(data, dict):
        raise _damaged(path, kind, f"it holds a JSON {_json_type(data)}, not an object", remedy)
    if newest is not None:
        try:
            version = (version_of or schema_version)(data)
        except ValueError as exc:
            raise _damaged(path, kind, str(exc), remedy) from exc
        if version > newest:
            raise StateFileError(
                path, kind=kind, problem="newer",
                detail=f"schema {version}; this pymolt reads up to {newest}",
                remedy=NEWER_REMEDY)
    return data


def load_state[M: BaseModel](path: str | Path, model: type[M], *, kind: str, newest: int | None,
               remedy: str, adapt: Callable[[dict[str, Any]], M] | None = None,
               version_of: Callable[[dict[str, Any]], int] | None = None) -> M | None:
    """``read_state_json`` validated as ``model`` (``adapt``: the caller's reader of the
    object, e.g. one that adapts an older schema); a validation error is ``damaged``, with
    its first problem as the detail."""
    data = read_state_json(path, kind=kind, newest=newest, remedy=remedy,
                           version_of=version_of)
    if data is None:
        return None
    try:
        return adapt(data) if adapt is not None else model.model_validate(data)
    except ValidationError as exc:
        raise _damaged(Path(path), kind, validation_detail(exc), remedy) from exc


def validation_detail(exc: ValidationError) -> str:
    """The first problem of a validation error, short: ``<field>: <message>`` (and how many
    more there are)."""
    errors = exc.errors()
    if not errors:
        return "it does not match what pymolt expects"
    first = errors[0]
    where = ".".join(str(part) for part in first.get("loc", ())) or "the top level"
    message = str(first.get("msg", "invalid"))
    if len(message) > 120:
        message = message[:117] + "..."
    more = len(errors) - 1
    return f"{where}: {message}" + (f" (and {more} more problem{'s' if more > 1 else ''})"
                                    if more else "")


def _json_type(value: Any) -> str:
    if isinstance(value, list):
        return "list"
    if isinstance(value, str):
        return "string"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int | float):
        return "number"
    return "null" if value is None else type(value).__name__


__all__ = [
    "ARCHIVE_DIR", "NEWER_REMEDY", "StateFileError", "display_path", "ignoring",
    "keep_remedy", "load_state", "read_state_json", "rebuild_remedy", "schema_version",
    "validation_detail",
]
