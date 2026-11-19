"""The repository is public: no tracked file may carry a person's private data.

Every file ``git ls-files`` lists is read (binaries and submodules aside) and checked for:

- a home directory with a real user name (``/Users/<name>/``, ``/home/<name>/``,
  ``C:\\Users\\<name>\\``); placeholders such as ``/Users/me`` are fine;
- a per-user temp directory (``/var/folders/xx/<long id>``, an agent's scratch directory);
- an e-mail address outside the example domains and the project's public addresses;
- a credential (GitHub, Anthropic, OpenAI, AWS or Slack token, a private key).

When it fails, make the value relative (``~``, the repo root) or a placeholder; never add a
real address or user name to the allow lists below. Benchmark writers scrub their results with
``bench/_paths.py``. CI also checks the e-mail addresses of a pull request's commits
(``.github/scripts/check_commit_emails.sh``).
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

# Names that stand for "some user" in docs and fixtures.
PLACEHOLDER_USERS = {"me", "you", "someone", "user", "username", "runner", "dev", "p", "x",
                     "alice", "bob", "example", "name", "...", "<name>", "<user>", "<you>",
                     "$USER"}
# Addresses that are public on purpose: the project's contact and the commit trailers.
PUBLIC_ADDRESSES = {"pymolt@zeelex.me", "noreply@anthropic.com", "noreply@github.com"}
PLACEHOLDER_DOMAINS = re.compile(
    r"(?:^|\.)(?:example\.(?:com|org|net)|example|invalid|test|localhost|"
    r"users\.noreply\.github\.com)$", re.IGNORECASE)

HOME = re.compile(r"(?:/Users/|/home/|[A-Za-z]:\\\\?Users\\\\?)([A-Za-z0-9._$<>-]+)")
TEMP = re.compile(r"/var/folders/[A-Za-z0-9_+-]{2}/[A-Za-z0-9_+-]{12,}"
                  r"|/private/tmp/claude-\d+|\.claude/project[s]/-")  # [s]: not a match itself
EMAIL = re.compile(r"(?<![\w.%+\\-])([A-Za-z0-9._%+-]+)@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*"
                   r"\.[A-Za-z]{2,})\b")
SECRET = re.compile(
    r"ghp_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{40,}|sk-ant-[A-Za-z0-9_-]{20,}"
    r"|sk-[A-Za-z0-9]{32,}|AKIA[0-9A-Z]{16}|xox[abprs]-[A-Za-z0-9-]{10,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----")


def _tracked_files() -> list[Path]:
    git = shutil.which("git")
    if git is None or not (REPO / ".git").exists():
        pytest.skip("not a git checkout")
    out = subprocess.run([git, "ls-files", "-z"], cwd=REPO, capture_output=True, check=True)
    return [REPO / name for name in out.stdout.decode().split("\0") if name]


def leaks(text: str) -> list[str]:
    """What in ``text`` looks like private data, one description per finding."""
    found = []
    for m in HOME.finditer(text):
        if m.group(1) not in PLACEHOLDER_USERS:
            found.append(f"home directory of {m.group(1)!r}")
    found += [f"per-user temp directory {m.group()!r}" for m in TEMP.finditer(text)]
    for m in EMAIL.finditer(text):
        address, domain = m.group(), m.group(2)
        if address.lower() not in PUBLIC_ADDRESSES and not PLACEHOLDER_DOMAINS.search(domain):
            found.append(f"e-mail address {address!r}")
    found += [f"credential {m.group()[:12]}…" for m in SECRET.finditer(text)]
    return found


def test_no_tracked_file_carries_private_data():
    problems = []
    for path in _tracked_files():
        if not path.is_file():  # a submodule or a deleted file
            continue
        data = path.read_bytes()
        if b"\0" in data:
            continue
        for lineno, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
            problems += [f"{path.relative_to(REPO)}:{lineno}: {what}" for what in leaks(line)]
    assert not problems, "private data in tracked files:\n" + "\n".join(problems[:50])


# Built from pieces, so this file passes its own check.
@pytest.mark.parametrize("text", [
    "cd /Users/" + "jdoe/src/app",
    "/home/" + "jdoe/.cache/pymolt",
    "C:\\Users\\" + "jdoe\\AppData",
    "tmp at /var/folders/ab/" + "c" * 28 + "/T/x",
    "scratch /private/tmp/claude-" + "123/abc",
    "memory in ~/.claude/" + "projects/-home-x/memory",
    "mail jane.doe" + "@gmail.com about it",
    "token ghp_" + "a" * 36,
    "-----BEGIN OPENSSH " + "PRIVATE KEY-----",
])
def test_the_guard_sees_each_kind_of_leak(text):
    assert leaks(text)


@pytest.mark.parametrize("text", [
    "/Users/me/src/locust and /home/dev/locust",
    "/var/folders/hm/xy/T/",
    "ada@example.org, b@example.com, someone@ci.test",
    "Co-Authored-By: Claude <noreply@anthropic.com>",
    "12345+octocat@users.noreply.github.com",
    "pymolt@zeelex.me",
    r'"text": "\n@click.option(\"--x\")"',
])
def test_placeholders_and_public_addresses_pass(text):
    assert leaks(text) == []
