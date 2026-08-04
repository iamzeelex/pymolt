"""Fork-CoW execution sandbox — the 'hard' state-snapshot (Level 2).

Runs a single point (a zero-arg callable — typically a dependency interaction)
inside a forked child process. Copy-on-write gives the child the parent's exact
memory at fork time, so the point executes against a real snapshot of program
state, yet **any side effect, crash, hang, or global mutation stays in the child**
— the parent is never polluted and remains at the clean fork point.

The child computes the point, normalizes the result (sharing the boundary
tracer's normalization so values compare identically across L2/L3), and streams
it back over a pipe. On crash or timeout the child is killed and the parent
records the failure. This serves both roles: safely capturing the contract for
risky/uncovered points, and 'what-if' runs of a point under a target dependency
version (by forking inside that version's interpreter).

Level 1 (a softer ``dill``-serialized snapshot of globals/closures, no fork) is a
planned addition for cheap, isolated pure functions.

Caveat: ``fork()`` in a *multi-threaded* parent can deadlock (the child inherits
mutex state held by threads that no longer exist). The production-grade path is a
single-threaded **fork-server** that does the forking on behalf of a threaded host
(e.g. a UI with worker threads); this core is the building block for it.
"""

import json
import os
import select
import signal
import time
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from pymolt.verify._normalize import normalize

_READ_CHUNK = 65536


class SandboxResult(BaseModel):
    """Outcome of one isolated execution."""

    outcome: str                 # returned | raised | crashed | timeout
    value: Any = None            # normalized return value (or exception args)
    error: str | None = None     # exception type name when outcome == "raised"
    exit_code: int | None = None # child exit/-signal code when outcome == "crashed"
    duration: float = 0.0


def _reap(pid: int) -> int | None:
    try:
        _, status = os.waitpid(pid, 0)
    except OSError:
        return None
    if os.WIFSIGNALED(status):
        return -os.WTERMSIG(status)
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    return None


def _kill(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    _reap(pid)


def run_isolated(point: Callable[[], Any], *, timeout: float = 10.0) -> SandboxResult:
    """Execute ``point()`` in a forked child and return its normalized outcome.

    The parent's state is unaffected regardless of what the point does. Requires a
    POSIX platform (``os.fork``).
    """
    if not hasattr(os, "fork"):
        raise RuntimeError("the fork-CoW sandbox requires a POSIX platform")

    read_fd, write_fd = os.pipe()
    start = time.monotonic()
    pid = os.fork()

    if pid == 0:  # ── child ──────────────────────────────────────────────
        os.close(read_fd)
        try:
            payload = {"outcome": "returned", "value": normalize(point())}
        except Exception as exc:  # noqa: BLE001 — capture anything the point throws
            payload = {
                "outcome": "raised",
                "error": type(exc).__name__,
                "value": normalize(getattr(exc, "args", None)),
            }
        try:
            os.write(write_fd, json.dumps(payload).encode("utf-8"))
        except Exception:
            pass
        finally:
            os.close(write_fd)
        os._exit(0)

    # ── parent ─────────────────────────────────────────────────────────────
    os.close(write_fd)
    buf = b""
    deadline = start + timeout
    timed_out = False
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            ready, _, _ = select.select([read_fd], [], [], remaining)
            if not ready:
                timed_out = True
                break
            chunk = os.read(read_fd, _READ_CHUNK)
            if not chunk:
                break  # child closed the pipe (EOF)
            buf += chunk
    finally:
        os.close(read_fd)

    duration = time.monotonic() - start

    if timed_out:
        _kill(pid)
        return SandboxResult(outcome="timeout", duration=duration)

    exit_code = _reap(pid)

    if buf:
        try:
            data = json.loads(buf.decode("utf-8"))
            return SandboxResult(
                outcome=data["outcome"], value=data.get("value"),
                error=data.get("error"), duration=duration,
            )
        except (ValueError, KeyError):
            pass

    # No payload => the child died before reporting (os._exit, signal, OOM, ...).
    return SandboxResult(outcome="crashed", exit_code=exit_code, duration=duration)
