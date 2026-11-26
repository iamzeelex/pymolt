"""pymolt.adapters.git on real repositories in tmp folders (and a stubbed ``run_command`` for
what a test machine cannot make: a timeout, a repository of dubious ownership).

Every commit here is made with ``user.email=t@example.invalid``, under a git config of the
test's own (no global or system config), never the real git identity."""
from __future__ import annotations

import shutil
import subprocess

import pytest

from pymolt.adapters import git
from pymolt.adapters.git import GitFacts, git_facts, head_commit

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

NAME = "Tess Tester"
EMAIL = "t@example.invalid"
BRANCH = "topic-unlisted"


@pytest.fixture(autouse=True)
def _isolated_git(tmp_path, monkeypatch):
    """No global or system git config, no repository above ``tmp_path``, no inherited
    repository variables."""
    config = tmp_path / "gitconfig"
    config.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
                 "GIT_OBJECT_DIRECTORY", "GIT_NAMESPACE"):
        monkeypatch.delenv(name, raising=False)


def _run(root, *args):
    return subprocess.run(["git", "-c", f"user.email={EMAIL}", "-c", f"user.name={NAME}",
                           "-c", "commit.gpgsign=false", *args], cwd=root, check=True,
                          capture_output=True, text=True).stdout.strip()


def _repo(path, files=None, *, commit=True):
    path.mkdir(parents=True, exist_ok=True)
    _run(path, "init", "-q", "-b", BRANCH)
    for rel, text in (files or {"app.py": "print(1)\n"}).items():
        (path / rel).parent.mkdir(parents=True, exist_ok=True)
        (path / rel).write_text(text)
    if commit:
        _run(path, "add", "-A")
        _run(path, "commit", "-q", "-m", "init")
    return path


def test_clean_dirty_and_staged(tmp_path):
    root = _repo(tmp_path / "r")
    sha = _run(root, "rev-parse", "HEAD")
    assert git_facts(root) == GitFacts(git_commit=sha, git_dirty=False, git_prefix="",
                                       quality="git")
    assert head_commit(root) == sha
    (root / "app.py").write_text("print(2)\n")
    assert git_facts(root).git_dirty is True
    _run(root, "add", "app.py")
    assert git_facts(root).git_dirty is True          # staged, not committed
    _run(root, "commit", "-q", "-m", "two")
    facts = git_facts(root)
    assert facts.git_dirty is False and facts.git_commit == _run(root, "rev-parse", "HEAD") != sha


def test_untracked_files_and_pymolt_never_make_it_dirty(tmp_path):
    root = _repo(tmp_path / "r", {"app.py": "x = 1\n",
                                  ".pymolt/plans/p/plan.json": "{}\n"})
    (root / "requirements-target.txt").write_text("pandas==2.2.3\n")   # assess writes it
    (root / ".pymolt" / "contract_state.json").write_text("{}\n")
    (root / ".pymolt" / "plans" / "p" / "plan.json").write_text('{"changed": 1}\n')
    assert git_facts(root).git_dirty is False
    (root / "app.py").write_text("x = 2\n")
    assert git_facts(root).git_dirty is True


def test_a_project_in_a_subfolder(tmp_path):
    repo = _repo(tmp_path / "mono", {"services/api/app.py": "a = 1\n", "other/b.py": "b = 1\n",
                                     "services/api/.pymolt/plans/p/plan.json": "{}\n"})
    root = repo / "services" / "api"
    facts = git_facts(root)
    assert facts.git_prefix == "services/api" and facts.git_dirty is False
    (repo / "other" / "b.py").write_text("b = 2\n")       # outside the project
    (root / ".pymolt" / "plans" / "p" / "plan.json").write_text("[]\n")
    assert git_facts(root).git_dirty is False
    (root / "app.py").write_text("a = 2\n")
    assert git_facts(root).git_dirty is True
    assert git_facts(repo).git_prefix == ""


def test_a_repository_with_no_commit(tmp_path):
    root = _repo(tmp_path / "r", commit=False)
    assert git_facts(root) == GitFacts(git_prefix="", quality="no-commit")
    assert head_commit(root) is None


def test_not_a_repository(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert git_facts(plain) == GitFacts(quality="not-a-repo")
    assert head_commit(plain) is None


def test_no_git_on_path(tmp_path, monkeypatch):
    root = _repo(tmp_path / "r")
    monkeypatch.setenv("PATH", "")
    assert git_facts(root) == GitFacts(quality="no-git", note=git.NOTE_NO_GIT)
    assert head_commit(root) is None


def test_not_a_directory(tmp_path):
    (tmp_path / "file").write_text("")
    assert git_facts(tmp_path / "file").quality == "error"
    assert git_facts(tmp_path / "missing") == GitFacts(quality="error", note=git.NOTE_NOT_A_DIR)
    assert head_commit(tmp_path / "missing") is None


def _stub(monkeypatch, answer):
    """Replace ``run_command`` with ``answer(args)``: a CompletedProcess, or an exception to
    raise. Records the calls."""
    calls = []

    def fake(args, **kw):
        calls.append((args, kw))
        result = answer(args)
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(git, "run_command", fake)
    return calls


def _done(args, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args, returncode, stdout, stderr)


def test_a_timeout_leaves_every_fact_unknown(tmp_path, monkeypatch):
    _stub(monkeypatch, lambda args: subprocess.TimeoutExpired(args, git.TIMEOUT))
    assert git_facts(tmp_path) == GitFacts(quality="error", note=git.NOTE_FAILED)
    assert head_commit(tmp_path) is None


@pytest.mark.parametrize("head, note", [
    (subprocess.TimeoutExpired(["git"], 5), "NOTE_FAILED"),
    ((128, ""), "NOTE_NO_HEAD"),
    ((0, "not-a-commit\n"), "NOTE_NO_HEAD"),
])
def test_a_failing_head_leaves_every_fact_unknown(tmp_path, monkeypatch, head, note):
    def answer(args):
        if "--show-prefix" in args:
            return _done(args, stdout="pkg/\n")
        return head if isinstance(head, BaseException) else _done(args, head[0], head[1])

    _stub(monkeypatch, answer)
    assert git_facts(tmp_path) == GitFacts(quality="error", note=getattr(git, note))


@pytest.mark.parametrize("failure", [subprocess.TimeoutExpired(["git"], 5), OSError("busy"),
                                     UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad"), None])
def test_a_failing_status_alone_keeps_the_commit(tmp_path, monkeypatch, failure):
    sha = "a" * 40

    def answer(args):
        if "--show-prefix" in args:
            return _done(args, stdout="pkg/\n")
        if "HEAD^{commit}" in args:
            return _done(args, stdout=sha + "\n")
        return failure if failure is not None else _done(args, 128, stderr="fatal: index")

    _stub(monkeypatch, answer)
    assert git_facts(tmp_path) == GitFacts(git_commit=sha, git_prefix="pkg", quality="git",
                                           note=git.NOTE_STATUS)


def test_dubious_ownership_is_an_error_and_keeps_nothing_of_what_git_said(tmp_path,
                                                                         monkeypatch):
    stderr = (f"fatal: detected dubious ownership in repository at '{tmp_path}'\n"
              f"To add an exception for this directory, call:\n"
              f"\tgit config --global --add safe.directory {tmp_path}\n")
    calls = _stub(monkeypatch, lambda args: _done(args, 128, stderr=stderr))
    facts = git_facts(tmp_path)
    assert facts == GitFacts(quality="error", note=git.NOTE_DUBIOUS)
    assert str(tmp_path) not in repr(facts)
    assert not any("safe.directory" in a for args, _kw in calls for a in args)


def test_the_calls_read_the_local_repository_only(tmp_path, monkeypatch):
    root = _repo(tmp_path / "r")
    _run(root, "remote", "add", "origin", f"https://{EMAIL.split('@')[0]}.invalid/secret.git")
    seen = []
    real = git.run_command

    def spy(args, **kw):
        seen.append((args, kw))
        return real(args, **kw)

    monkeypatch.setattr(git, "run_command", spy)
    git_facts(root)
    head_commit(root)
    assert [args[:1] for args, _kw in seen] == [["git"]] * 4
    words = {a for args, _kw in seen for a in args}
    assert not words & {"remote", "config", "log", "ls-remote", "fetch", "branch"}
    for _args, kw in seen:
        assert kw["check"] is False and kw["timeout"] == 5 and kw["cwd"] == str(root)
        assert {k: kw["env"][k] for k in git.GIT_ENV} == git.GIT_ENV


def test_nothing_absolute_or_personal_in_the_facts(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "mono", {"svc/app.py": "a = 1\n"})
    (repo / "svc" / "app.py").write_text("a = 2\n")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    facts = [git_facts(repo / "svc"), git_facts(repo), git_facts(tmp_path),
             git_facts(_repo(tmp_path / "empty", commit=False))]
    text = repr(facts)
    for private in (str(tmp_path), str(tmp_path.resolve()), "mono", NAME, EMAIL, BRANCH,
                    "secret"):
        assert private not in text
    assert facts[0].git_prefix == "svc" and facts[0].git_dirty is True


# ── tracked_files (plan retention) ───────────────────────────────────────────────────────


def test_tracked_files_lists_what_git_tracks_under_the_project(tmp_path):
    repo = _repo(tmp_path / "mono", {"svc/.pymolt/plans/a/plan.json": "{}\n",
                                     "svc/app.py": "x = 1\n"})
    root = repo / "svc"
    (root / ".pymolt" / "plans" / "b").mkdir()
    (root / ".pymolt" / "plans" / "b" / "plan.json").write_text("{}\n")      # untracked
    assert git.tracked_files(root, ".pymolt/plans") == [".pymolt/plans/a/plan.json"]
    assert git.tracked_files(repo, "svc/.pymolt/plans") == ["svc/.pymolt/plans/a/plan.json"]


def test_tracked_files_outside_a_repository_or_without_git_is_empty(tmp_path, monkeypatch):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert git.tracked_files(plain, ".pymolt/plans") == []
    root = _repo(tmp_path / "r", {".pymolt/plans/a/plan.json": "{}\n"})
    monkeypatch.setenv("PATH", "")
    assert git.tracked_files(root, ".pymolt/plans") == []


@pytest.mark.parametrize("answer", [
    subprocess.TimeoutExpired(["git"], 5), OSError("busy"),
    (128, "fatal: detected dubious ownership in repository at '/x'\n"),
    (1, "fatal: something else\n"),
])
def test_tracked_files_cannot_say_when_git_fails(tmp_path, monkeypatch, answer):
    _stub(monkeypatch, lambda args: answer if isinstance(answer, BaseException)
          else _done(args, answer[0], stderr=answer[1]))
    assert git.tracked_files(tmp_path, ".pymolt/plans") is None
    assert git.tracked_files(tmp_path / "missing", ".pymolt/plans") is None
