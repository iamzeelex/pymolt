"""What the local git repository says about a project: its commit, whether its tracked files
changed, and where it sits in the repository.

A plan records these facts (``PlanSource``) so a person can tell which code it was made from,
and ``status`` compares the current plan's commit with ``HEAD``. This module is a thin system
adapter: every git call goes through ``subprocess_runner.run_command`` (``check=False``, a 5 s
timeout) in the project folder, with ``GIT_OPTIONAL_LOCKS=0`` (a read takes no index lock),
``GIT_TERMINAL_PROMPT=0`` and ``LC_ALL=C``. It never raises: whatever fails is a ``quality``
and a fixed note.

**Privacy.** It reads the local repository only: never ``remote``, ``config``, ``log`` or
``ls-remote``. It keeps no branch, author, toplevel path or git output text beyond the commit
id and the project's path inside the repository. pymolt never overrides ``safe.directory``: a
repository git refuses as of dubious ownership is an ``error``.

The calls:

1. ``git -c core.fsmonitor=false rev-parse --show-prefix``: exit 128 is ``not-a-repo``, no git
   executable is ``no-git``;
2. ``git rev-parse --verify --quiet HEAD^{commit}``: exit 1 is ``no-commit``;
3. ``git -c core.fsmonitor=false status --porcelain=v1 -z --untracked-files=no -- .
   ":(exclude).pymolt"``: any output is a dirty tree. Only tracked files under the project
   count: ``.pymolt/`` and untracked files (assess's target manifest) never do.

A call that times out, fails or writes undecodable output leaves every fact None, with quality
``error`` and a fixed note; except ``status``: when only it fails, the commit and prefix are
kept (quality ``git``), ``git_dirty`` is None and the note says so.

``tracked_files`` (plan retention never removes a plan folder git tracks) is one
``git -c core.fsmonitor=false ls-files -z -- <pathspec>``: outside a repository, or without
git, nothing is tracked (``[]``); a call that fails, times out or meets a repository of
dubious ownership cannot say (None).
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pymolt.adapters.subprocess_runner import run_command

Quality = Literal["git", "no-commit", "not-a-repo", "no-git", "error"]

TIMEOUT = 5
GIT_ENV = {"GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}
_COMMIT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")

# The notes are fixed phrases: no git output, path or name ever reaches them.
NOTE_NO_GIT = "git is not installed or not on PATH"
NOTE_NOT_A_DIR = "the project folder is not a directory"
NOTE_DUBIOUS = ("git refuses this repository as of dubious ownership (pymolt does not change "
                "safe.directory)")
NOTE_FAILED = "git could not be run here (it failed, timed out or wrote unreadable output)"
NOTE_NO_HEAD = "git could not read HEAD"
NOTE_STATUS = "git status failed: whether tracked files changed is unknown"


@dataclass(frozen=True)
class GitFacts:
    """The project's git facts, as a plan's ``source`` records them (``PlanSource``).

    ``git_commit``: ``HEAD``'s commit id; ``git_dirty``: whether a tracked file under the
    project differs from it (``.pymolt/`` aside); ``git_prefix``: the project's folder inside
    the repository, POSIX form, ``""`` at its top. Each is None when unknown, and ``quality``
    says why (``note`` in words)."""

    git_commit: str | None = None
    git_dirty: bool | None = None
    git_prefix: str | None = None
    quality: Quality = "error"
    note: str | None = None


class _Failed(Exception):
    """A git call that did not run to an answer (timeout, OS error, undecodable output)."""


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    try:
        return run_command(["git", *args], check=False, cwd=str(root),
                           env={**os.environ, **GIT_ENV}, timeout=TIMEOUT)
    except FileNotFoundError:
        raise
    except (subprocess.TimeoutExpired, OSError, ValueError) as exc:
        raise _Failed from exc


def head_commit(project_dir: str | Path) -> str | None:
    """``HEAD``'s commit id in the project's repository; None when there is none or git cannot
    say."""
    root = Path(project_dir)
    if not root.is_dir():
        return None
    try:
        done = _git(root, "rev-parse", "--verify", "--quiet", "HEAD^{commit}")
    except (FileNotFoundError, _Failed):
        return None
    commit = (done.stdout or "").strip()
    return commit if done.returncode == 0 and _COMMIT.fullmatch(commit) else None


def git_facts(project_dir: str | Path) -> GitFacts:
    """The project's ``GitFacts`` (see the module docstring). Never raises."""
    root = Path(project_dir)
    if not root.is_dir():
        return GitFacts(quality="error", note=NOTE_NOT_A_DIR)
    try:
        shown = _git(root, "-c", "core.fsmonitor=false", "rev-parse", "--show-prefix")
    except FileNotFoundError:
        return GitFacts(quality="no-git", note=NOTE_NO_GIT)
    except _Failed:
        return GitFacts(quality="error", note=NOTE_FAILED)
    if shown.returncode != 0:
        if "dubious ownership" in (shown.stderr or ""):
            return GitFacts(quality="error", note=NOTE_DUBIOUS)
        if shown.returncode == 128:
            return GitFacts(quality="not-a-repo")
        return GitFacts(quality="error", note=NOTE_FAILED)
    prefix = (shown.stdout or "").strip("\r\n").rstrip("/")
    try:
        head = _git(root, "rev-parse", "--verify", "--quiet", "HEAD^{commit}")
    except (FileNotFoundError, _Failed):
        return GitFacts(quality="error", note=NOTE_FAILED)
    if head.returncode == 1:
        return GitFacts(git_prefix=prefix, quality="no-commit")
    commit = (head.stdout or "").strip()
    if head.returncode != 0 or not _COMMIT.fullmatch(commit):
        return GitFacts(quality="error", note=NOTE_NO_HEAD)
    try:
        status = _git(root, "-c", "core.fsmonitor=false", "status", "--porcelain=v1", "-z",
                      "--untracked-files=no", "--", ".", ":(exclude).pymolt")
    except (FileNotFoundError, _Failed):
        status = None
    if status is None or status.returncode != 0:
        return GitFacts(git_commit=commit, git_prefix=prefix, quality="git", note=NOTE_STATUS)
    return GitFacts(git_commit=commit, git_dirty=bool(status.stdout), git_prefix=prefix,
                    quality="git")


def tracked_files(project_dir: str | Path, pathspec: str) -> list[str] | None:
    """The files git tracks under ``pathspec``, as git prints them (POSIX, relative to the
    project folder): ``[]`` outside a repository or without git, where nothing is tracked;
    None when git cannot say (it failed, timed out, wrote unreadable output, or refuses the
    repository as of dubious ownership). Never raises."""
    root = Path(project_dir)
    if not root.is_dir():
        return None
    try:
        done = _git(root, "-c", "core.fsmonitor=false", "ls-files", "-z", "--", pathspec)
    except FileNotFoundError:
        return []
    except _Failed:
        return None
    if done.returncode != 0:
        stderr = done.stderr or ""
        if done.returncode == 128 and "not a git repository" in stderr \
                and "dubious ownership" not in stderr:
            return []
        return None
    return [path for path in (done.stdout or "").split("\0") if path]


__all__ = ["GIT_ENV", "GitFacts", "Quality", "git_facts", "head_commit", "tracked_files"]
