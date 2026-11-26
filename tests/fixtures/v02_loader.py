"""The 0.2 project fixture (``tests/fixtures/v02_project/``): a real ``.pymolt/`` that pymolt 0.2
wrote, and how to put it on disk for a test.

``make_v02_project.py`` made it with pymolt 0.2 on the e2e_pandas toy: setup, a baseline
capture, ``plan`` (the legacy single plan file, schema 2) and ``apply --step env-1``, which
leaves an open run. ``expected.json`` records what 0.2 said about it, ``plan_digest`` among it.

**How the tree is stored.** ``tree/`` is the project root, with three spellings git and pytest
leave alone:

- a path part ``dot-NAME`` is ``.NAME`` (``dot-pymolt`` is ``.pymolt``: the repository ignores
  every ``.pymolt/``, and ``.pymolt/.gitignore`` would hide ``blobs/`` from git);
- a file ``NAME.gz.txt`` is the text of ``NAME.gz``, compressed again on the way out, so every
  file is text the privacy guard reads;
- ``<ROOT>`` stands for the project's absolute path, ``<ENVS>`` for the e2e_pandas environments
  folder and ``<TMPDIR>`` for the generating machine's temp folder; ``~`` is its home.

``materialize`` writes the project. By default it relocates it: ``<ROOT>`` becomes the
destination and ``<ENVS>`` the given environments folder, and every hash 0.2 took over a file
that holds the root (the plan's ``baseline_trace_sha256``) is taken again, so the project is
consistent where it lands, as 0.2 checks it (``make_v02_project.py`` checks this with 0.2
itself). ``relocate=False`` copies the files byte for byte, as committed: the form
``expected.json``'s digests are of.

The fixture carries no agent approval: an approval is a person's attestation, which an agent
never makes, and it binds this machine's key and the project's real path, so a committed one
could not hold anywhere else. A test that needs one makes it in the test's attestation sandbox
(``tests/workorder/_verifying.py``'s ``FakeTerminal``) and can compare its ``plan_sha256`` with
``legacy_plan_digest``, 0.2's formula.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import re
import shutil
from collections.abc import Iterator
from pathlib import Path

FIXTURE = Path(__file__).resolve().parent / "v02_project"
TREE = FIXTURE / "tree"
EXPECTED = FIXTURE / "expected.json"
REPO = Path(__file__).resolve().parents[2]
DEFAULT_ENVS = REPO / "bench" / "e2e_pandas" / ".envs"

ROOT_MARK = "<ROOT>"
ENVS_MARK = "<ENVS>"
TMPDIR_MARK = "<TMPDIR>"
DOT = "dot-"
GZ_TEXT = ".gz.txt"
# Files whose text is JSON: a substituted path is JSON-escaped there.
_JSON_SUFFIXES = (".json", ".jsonl", ".json" + GZ_TEXT)


def expected() -> dict:
    """What 0.2 recorded about the fixture (``expected.json``)."""
    return json.loads(EXPECTED.read_text(encoding="utf-8"))


def project_path(stored: str) -> str:
    """The project-relative POSIX path of a file stored at ``stored`` (relative to ``tree/``)."""
    parts = [("." + p[len(DOT):]) if p.startswith(DOT) else p for p in stored.split("/")]
    if parts[-1].endswith(GZ_TEXT):
        parts[-1] = parts[-1][: -len(".txt")]
    return "/".join(parts)


def stored_path(project: str) -> str:
    """The inverse of ``project_path``."""
    parts = [(DOT + p[1:]) if p.startswith(".") and p not in (".", "..") else p
             for p in project.split("/")]
    if parts[-1].endswith(".gz"):
        parts[-1] += ".txt"
    return "/".join(parts)


def stored_files(tree: Path = TREE) -> Iterator[tuple[str, str]]:
    """``(stored path, project path)`` of every file of the fixture, sorted."""
    for path in sorted(p for p in tree.rglob("*") if p.is_file()):
        stored = path.relative_to(tree).as_posix()
        yield stored, project_path(stored)


def _is_json(name: str) -> bool:
    return name.endswith(_JSON_SUFFIXES)


def _substitute(text: str, name: str, marks: dict[str, str]) -> str:
    for mark, value in marks.items():
        text = text.replace(mark, json.dumps(value)[1:-1] if _is_json(name) else value)
    return text


def legacy_plan_digest(path: str | Path) -> str:
    """0.2's ``plan_digest`` of a plan file it wrote: the sha256 of its canonical JSON. 0.2
    writes ``model_dump``, so hashing the stored object equals hashing the loaded model
    (``make_v02_project.py`` checks that with 0.2)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def materialize(dest: str | Path, *, relocate: bool = True, envs: str | Path | None = None,
                tree: Path = TREE, rehash: list[dict] | None = None) -> Path:
    """Write the fixture's project to ``dest`` (absent or empty) and return its resolved path.

    ``relocate`` (default): ``<ROOT>`` becomes ``dest``, ``<ENVS>`` becomes ``envs`` (default:
    ``bench/e2e_pandas/.envs``, which need not exist), and each file 0.2 hashed is hashed again
    (``rehash``, default ``expected.json``'s). ``relocate=False``: the files as committed,
    placeholders and all (``.gz`` files are still compressed)."""
    dest = Path(dest)
    if dest.exists() and any(dest.iterdir()):
        raise FileExistsError(f"{dest} is not empty")
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    marks = {ROOT_MARK: str(root),
             ENVS_MARK: str(Path(envs).resolve() if envs is not None else DEFAULT_ENVS)}
    texts: dict[str, str] = {}
    for stored, project in stored_files(tree):
        source = tree / stored
        target = root / project
        target.parent.mkdir(parents=True, exist_ok=True)
        if not stored.endswith(GZ_TEXT) and not relocate:
            shutil.copyfile(source, target)
            continue
        text = source.read_text(encoding="utf-8")
        if relocate:
            text = _substitute(text, stored, marks)
        texts[project] = text
        _write(root, project, text)
    if relocate:
        _rehash(root, texts, expected()["relocate"]["rehash"] if rehash is None else rehash)
    return root


def rehash_text(text: str, old: str, new: str) -> str:
    """``text`` with the sha256 ``old`` written as ``new``: in full, and in a capture stamp
    ``YYYYMMDDTHHMMSSZ-<sha[:8]>`` (0.2's PLAN.md header names the baseline so)."""
    text = text.replace(old, new)
    return re.sub(r"(\d{8}T\d{6}Z-)" + old[:8] + r"(?![0-9a-f])", r"\g<1>" + new[:8], text)


def _rehash(root: Path, texts: dict[str, str], bindings: list[dict]) -> None:
    """Take again each hash 0.2 took over a file that holds the root, and write it wherever
    the committed form has the old one."""
    for binding in bindings:
        old = binding["sha256"]
        new = hashlib.sha256((root / binding["file"]).read_bytes()).hexdigest()
        if new == old:
            continue
        for project, text in texts.items():
            changed = rehash_text(text, old, new)
            if changed != text:
                texts[project] = changed
                _write(root, project, changed)


def _write(root: Path, project: str, text: str) -> None:
    data = text.encode("utf-8")
    (root / project).write_bytes(gzip.compress(data, mtime=0) if project.endswith(".gz")
                                 else data)


__all__ = ["DEFAULT_ENVS", "ENVS_MARK", "EXPECTED", "FIXTURE", "ROOT_MARK", "TMPDIR_MARK",
           "TREE", "expected", "legacy_plan_digest", "materialize", "project_path", "rehash_text",
           "stored_files", "stored_path"]
