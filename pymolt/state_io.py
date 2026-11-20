"""The project's state files under ``.pymolt/``: read strictly, written atomically, changed
under one lock.

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

**Every write is atomic** (``atomic_write_bytes`` / ``_text`` / ``_json``): a sibling temp file,
fsynced, then ``os.replace``. A crash or a full disk leaves the old file or the new one.

**Read-modify-writes hold the state lock** (``state_lock``, ``.pymolt/state.lock``): a capture's
slot update, setup's config, the saved plan and verdict. It waits (60 s by default) for
another holder: its critical sections are short. ``migration.lock`` serializes the source
mutations (apply, rollback, record, approvals, accepts) and refuses at once when busy; both
are ``FileLock``. **Lock order:** ``migration.lock`` (outer, long) before ``state.lock``
(inner, short); never acquire ``migration.lock`` while holding ``state.lock``
(``migration_plan._migration_lock`` refuses it in the same thread).
"""

from __future__ import annotations

import json
import os
import secrets
import stat
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
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


# ── writing: atomic ─────────────────────────────────────────────────────────────────────────


def atomic_write_bytes(path: str | Path, data: bytes) -> Path:
    """Replace ``path`` with ``data`` all at once: a crash or a full disk leaves the old file
    or the new one, never a torn one.

    The bytes go to a hidden sibling (``.<name>.<random>.tmp``, created exclusively, with the
    permissions an ordinary write would give it, or the replaced file's), are flushed and
    fsynced, then ``os.replace`` it over ``path``; on POSIX the directory is fsynced too (best
    effort). Any failure removes the sibling and re-raises. Creates the parent directories."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    fd = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                 0o666)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.chmod(staged, stat.S_IMODE(os.stat(path).st_mode))
        except FileNotFoundError:
            pass
        os.replace(staged, path)
    except BaseException:
        try:
            os.unlink(staged)
        except OSError:
            pass
        raise
    _fsync_directory(path.parent)
    return path


def atomic_write_text(path: str | Path, text: str, *, encoding: str = "utf-8") -> Path:
    """``atomic_write_bytes`` of ``text`` (no newline translation)."""
    return atomic_write_bytes(path, text.encode(encoding))


def atomic_write_json(path: str | Path, payload: Any, *, indent: int | None = 2,
                      sort_keys: bool = False, default: Callable[[Any], Any] | None = None
                      ) -> Path:
    """``atomic_write_bytes`` of ``payload`` as JSON, ending with a newline."""
    return atomic_write_text(path, json.dumps(payload, indent=indent, sort_keys=sort_keys,
                                              default=default) + "\n")


def _fsync_directory(directory: Path) -> None:
    """Make a rename in ``directory`` durable (POSIX; best effort, never raises)."""
    if os.name == "nt":
        return
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


# ── locking ─────────────────────────────────────────────────────────────────────────────────


class LockBusy(Exception):
    """Another process (or thread) holds a lock file; ``holder``: what it wrote there."""

    def __init__(self, path: Path, holder: str) -> None:
        super().__init__(f"{display_path(path)} is held by {holder}")
        self.path = path
        self.holder = holder


class StateLockTimeout(Exception):
    """``state_lock`` waited ``timeout`` seconds and another pymolt still holds it."""

    def __init__(self, path: Path, holder: str, timeout: float) -> None:
        super().__init__(
            f"another pymolt command is changing this project's state ({holder}); waited "
            f"{timeout:g}s for {display_path(path)}")
        self.path = path
        self.holder = holder
        self.timeout = timeout
        self.hint = ("Let it finish and run the command again. If no pymolt is running, the "
                     "operating system has released the lock already: run it again.")


#: The lock files this thread holds: resolved path -> [depth, stream].
_HELD = threading.local()


def _held() -> dict[Path, list]:
    held = getattr(_HELD, "locks", None)
    if held is None:
        held = _HELD.locks = {}
    return held


def _describe_holder(text: str) -> str:
    """What a lock file says about its holder, for a person."""
    try:
        data = json.loads(text)
    except ValueError:
        return text.strip() or "an unknown process"
    if not isinstance(data, dict):
        return text.strip() or "an unknown process"
    parts = [f"pid {data.get('pid', '?')}"]
    if data.get("operation"):
        parts.append(f"({data['operation']})")
    if data.get("started_at"):
        parts.append(f"since {data['started_at']}")
    return " ".join(parts)


class FileLock:
    """An exclusive OS lock on ``path`` (``fcntl.flock`` on POSIX, ``msvcrt.locking`` on
    Windows): the operating system releases it when the holder dies. The holder writes its
    pid, operation and start into the file, which a refused or waiting process reports.

    ``timeout``: 0 refuses at once when the lock is busy (``LockBusy``); otherwise the lock is
    polled until it is free or ``timeout`` seconds passed. ``reentrant``: the same thread may
    acquire it again (a depth count); otherwise a second acquisition in the same thread is
    refused like any other holder's."""

    def __init__(self, path: str | Path, operation: str, *, timeout: float = 0.0,
                 reentrant: bool = False, poll: float = 0.05) -> None:
        self.path = Path(path)
        self.operation = operation
        self.timeout = timeout
        self.reentrant = reentrant
        self.poll = poll
        self._key: Path | None = None
        self._stream = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        key = self.path.resolve()
        held = _held()
        if self.reentrant and key in held:
            held[key][0] += 1
            self._key = key
            return
        stream = self.path.open("a+", encoding="utf-8")
        deadline = time.monotonic() + self.timeout
        try:
            while True:
                try:
                    _os_lock(stream)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        stream.seek(0)
                        raise LockBusy(self.path, _describe_holder(stream.read())) from None
                    time.sleep(self.poll)
            stream.seek(0)
            stream.truncate()
            json.dump({"pid": os.getpid(), "operation": self.operation,
                       "started_at": datetime.now(UTC).isoformat(timespec="seconds")}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            stream.close()
            raise
        if self.reentrant:
            held[key] = [1, stream]
        self._key, self._stream = key, stream

    def release(self) -> None:
        key = self._key
        if key is None:
            return
        held = _held()
        if self.reentrant:
            entry = held.get(key)
            if entry is not None and entry[0] > 1:
                entry[0] -= 1
                self._key = None
                return
            stream = held.pop(key)[1] if entry is not None else self._stream
        else:
            stream = self._stream
        self._key, self._stream = None, None
        if stream is None:
            return
        try:
            stream.seek(0)
            _os_unlock(stream)
        finally:
            stream.close()

    def __enter__(self) -> FileLock:
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()


def _os_lock(stream) -> None:
    """Take the lock without waiting; OSError when another holder has it."""
    try:
        import fcntl
    except ImportError:  # pragma: no cover - Windows
        import msvcrt

        stream.seek(0)
        if not stream.read(1):
            stream.write("\0")
            stream.flush()
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        return
    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _os_unlock(stream) -> None:
    try:
        import fcntl
    except ImportError:  # pragma: no cover - Windows
        import msvcrt

        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        return
    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


STATE_LOCK = Path(".pymolt") / "state.lock"
MIGRATION_LOCK = Path(".pymolt") / "migration.lock"


def holds_state_lock(project_dir: str | Path) -> bool:
    """Whether this thread holds the project's state lock."""
    return (Path(project_dir) / STATE_LOCK).resolve() in _held()


@contextmanager
def state_lock(project_dir: str | Path, *, timeout: float = 60.0,
               operation: str = "update state") -> Iterator[None]:
    """Hold ``.pymolt/state.lock`` for a read-modify-write of a state file (short: load,
    change, save). Waits up to ``timeout`` seconds for another holder, then raises
    ``StateLockTimeout`` naming it. Re-entrant in a thread: a locked writer may call another.

    **Lock order:** ``migration.lock`` (outer: apply, rollback, record, approvals, accepts;
    held for a whole mutation, refused at once when busy) before ``state.lock`` (inner, short).
    Never acquire ``migration.lock`` while holding ``state.lock``."""
    path = Path(project_dir) / STATE_LOCK
    lock = FileLock(path, operation, timeout=timeout, reentrant=True)
    try:
        lock.acquire()
    except LockBusy as exc:
        raise StateLockTimeout(path, exc.holder, timeout) from None
    try:
        yield
    finally:
        lock.release()

__all__ = [
    "ARCHIVE_DIR", "MIGRATION_LOCK", "NEWER_REMEDY", "STATE_LOCK", "FileLock", "LockBusy",
    "StateFileError", "StateLockTimeout", "atomic_write_bytes", "atomic_write_json",
    "atomic_write_text", "display_path", "holds_state_lock", "ignoring", "keep_remedy",
    "load_state", "read_state_json", "rebuild_remedy", "schema_version", "state_lock",
    "validation_detail",
]
