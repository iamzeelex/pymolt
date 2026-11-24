"""The env phase: pin the target versions in the manifest, and build the target environment
(design 1.1, decision 1).

HOST side, read-only: it reads the source and target manifests and writes nothing.

``manifest-pin`` (pymolt): the source manifest's lines ``name[extras]==X [; marker]`` with no
``--hash``, for a package the target manifest pins at another version, rewritten to the target's
pin with the extras, marker, comment and line ending kept. Its edit is a line rewrite of the
manifest: hunks, and an effect over the lines' text (no Python statements, so no anchors).
Every other line naming such a package (unpinned, another specifier, hashed or continued), and
every manifest that is not requirements-style (pyproject, poetry, conda), goes to a
``manifest-pin`` step for a person, with the exact lines. ``changes`` never holds the manifest:
it is the code steps' view.

``target-env`` (person): the commands that build the target environment (``setup.target_hint``).
Status projects it from the environment check; nothing depends on it.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from pymolt.core.versions import same_version
from pymolt.workorder.edits import hunks_of, sha256, split_lines
from pymolt.workorder.models import (
    Effect,
    ManifestPin,
    ManualDo,
    PlanStep,
    StepDo,
    StepEdit,
    StepEffort,
    StepRecipe,
    StepVerify,
    StepWhy,
    step_id,
    step_key,
)

_REQUIREMENTS = (".txt", ".in")
_PIN = re.compile(
    r"^(?P<lead>\s*)(?P<name>[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)\s*"
    r"(?P<extras>\[[^\]]*\])?\s*==\s*(?P<version>[^\s;#\\]+)(?P<tail>\s*(?:;[^#\r\n]*)?"
    r"(?:\s*#[^\r\n]*)?)(?P<end>\r\n|\r|\n)?$")
_NAME = re.compile(r"^\s*(?P<name>[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)")


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def target_pins(text: str) -> dict[str, str]:
    """``{normalized name: version}`` of a pinned target manifest (hashes and continuations
    allowed)."""
    joined = re.sub(r"\\\r?\n", " ", text)
    out: dict[str, str] = {}
    for line in joined.splitlines():
        spec = line.split(" --hash", 1)[0].split("#", 1)[0].strip()
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*==\s*([^\s;]+)", spec)
        if m:
            out[normalize(m.group(1))] = m.group(3)
    return out


def _line_key(line: str) -> str:
    return hashlib.sha1(("req|" + " ".join(line.split())).encode()).hexdigest()[:16]


def line_key(line: str) -> str:
    """A requirements line's key: its text with whitespace runs collapsed (the effect of a
    ``manifest-pin`` step is over these)."""
    return _line_key(line)


def rewrite_pins(text: str, pins: Iterable[ManifestPin]) -> tuple[str, int]:
    """``text`` with each pin's line rewritten (re-basing, design 3.2 rule 3): the one line
    whose key is the pin's old line's key, wherever it moved, gets the pin's new line with its
    own line ending. Returns ``(text, pins rewritten)``; a pin whose old line is missing or not
    unique is left out."""
    lines = split_lines(text)
    keys = [_line_key(line) for line in lines]
    done = 0
    for pin in pins:
        found = [n for n, key in enumerate(keys) if key == _line_key(pin.old)]
        if len(found) != 1:
            continue
        n = found[0]
        line = lines[n]
        ending = line[len(line.rstrip("\r\n")):]
        lines[n] = pin.new.rstrip("\r\n") + ending
        keys[n] = _line_key(lines[n])
        done += 1
    return "".join(lines), done


def manifest_pins(source: str, pins: dict[str, str]
                  ) -> tuple[list[ManifestPin], list[tuple[int, str, str]]]:
    """``(pins pymolt writes, [(line, text, target pin)] for a person)`` over ``source``."""
    rewrite: list[ManifestPin] = []
    other: list[tuple[int, str, str]] = []
    lines = split_lines(source)
    continued = False
    for n, line in enumerate(lines, 1):
        stripped = line.strip()
        was_continued, continued = continued, stripped.endswith("\\")
        if was_continued:
            continue                       # a hash or option line of the previous entry
        if not stripped or stripped.startswith(("#", "-")):
            continue
        head = _NAME.match(line)
        if head is None or normalize(head.group("name")) not in pins:
            continue
        name = head.group("name")
        to = pins[normalize(name)]
        m = _PIN.match(line)
        if m and "--hash" not in line and not continued:
            if same_version(m.group("version"), to):
                continue                   # `==3.2` already pins 3.2.0
            new = (f"{m.group('lead')}{name}{m.group('extras') or ''}=={to}"
                   f"{m.group('tail') or ''}{m.group('end') or ''}")
            rewrite.append(ManifestPin(name=name, from_version=m.group("version"),
                                       to_version=to, old=line, new=new, line=n))
        else:
            other.append((n, stripped, f"{name}=={to}"))
    return rewrite, other


def _manifest_edit(file: str, before: str, pins: list[ManifestPin]) -> tuple[str, StepEdit]:
    lines = split_lines(before)
    for pin in pins:
        lines[pin.line - 1] = pin.new
    after = "".join(lines)
    effect = Effect(replace=[(_line_key(p.old), _line_key(p.new)) for p in pins],
                    key_kind="tokens")
    return after, StepEdit(file=file, before_sha256=sha256(before), after_sha256=sha256(after),
                           hunks=hunks_of(before, after), effect=effect)


def _read(root: Path, rel: str | None) -> str | None:
    if not rel:
        return None
    try:
        return (root / rel).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError, ValueError):
        return None


def env_steps(root: str | Path, *, source_manifest: str | None,
              target_manifest: str | None, target_python: str | None,
              dependencies: Iterable[Any] = (), target_env: str | None = None
              ) -> list[PlanStep]:
    """The env phase's steps (see the module docstring), numbered ``env-1``..."""
    root = Path(root)
    drafts: list[dict] = []
    source = _read(root, source_manifest)
    target_text = _read(root, target_manifest)
    if source is not None and target_text is not None:
        file = Path(source_manifest).as_posix()
        if Path(file).suffix in _REQUIREMENTS:
            rewrite, other = manifest_pins(source, target_pins(target_text))
        else:
            rewrite = []
            other = [(0, f"{d.name} {d.from_version}", f"{d.name}=={d.to_version}")
                     for d in dependencies if getattr(d, "from_version", None)
                     != getattr(d, "to_version", None)]
        common = dict(phase="env", type="manifest-pin", claim="intended",
                      basis="target-manifest", scope="env",
                      verify=StepVerify(judge="none"))
        if rewrite:
            _after, edit = _manifest_edit(file, source, rewrite)
            drafts.append(dict(
                common, key=step_key("manifest-pin", f"{file}|pymolt"), executor="pymolt",
                title=f"Pin the target versions in {file}: {len(rewrite)} line(s)",
                why=StepWhy(evidence=[f"{target_manifest} pins {p.name} at {p.to_version}; "
                                      f"{file} pins {p.from_version}" for p in rewrite][:10]),
                do=StepDo(recipe=StepRecipe(
                    kind="manifest-pin", pins=rewrite, sites="all",
                    keys=[f"manifest-pin:{p.name}=={p.from_version}→{p.to_version}"
                          for p in rewrite])),
                edits=[edit],
                effort=StepEffort(sites=len(rewrite), files=1)))
        if other:
            requirements = Path(file).suffix in _REQUIREMENTS
            lines = [(f"line {n}: `{text}` → `{pin}`" if n else f"`{text}` → `{pin}`")
                     for n, text, pin in other]
            why = ("pymolt rewrites only `name==version` lines with no hash" if requirements
                   else f"pymolt does not rewrite a {Path(file).name} manifest")
            drafts.append(dict(
                common, key=step_key("manifest-pin", f"{file}|person"), executor="person",
                title=f"Pin the target versions in {file} by hand: {len(other)} line(s)",
                why=StepWhy(evidence=[why]),
                do=StepDo(manual=ManualDo(
                    summary=f"Change these lines of {file} to the target's pins ({why}).",
                    lines=lines)),
                effort=StepEffort(sites=len(other), files=1)))
    if target_python and target_manifest:
        from pymolt.setup.target_hint import build_target_hint

        hint = build_target_hint(root, target_python, manifest=target_manifest)
        evidence = ["the post-migration capture and apply's smoke import need it"]
        if target_env:
            evidence.append(f"setup names a target environment: {target_env}")
        drafts.append(dict(
            phase="env", type="target-env", key=step_key("target-env", str(target_python)),
            executor="person", scope="env",
            title=f"Build the target environment (Python {target_python})",
            why=StepWhy(evidence=evidence),
            do=StepDo(manual=ManualDo(
                summary="Build the target environment; pymolt does not build or install it.",
                commands=list(hint.suggested_uv_commands))),
            verify=StepVerify(judge="none"),
            effort=StepEffort(sites=0, files=0)))
    return [PlanStep(id=step_id("env", n), **draft) for n, draft in enumerate(drafts, 1)]
