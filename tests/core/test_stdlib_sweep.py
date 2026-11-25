"""Cross-check the curated ``stdlib_changes.json`` against the generated lifetimes.

Two layers:

- **data**: for every entry with ``removed_in``, the shipped lifetimes say the symbol is absent
  from that version on and present just before it (no interpreter needed);
- **live**: the same claims against real interpreters, for every uv-managed 3.8+ interpreter
  that is installed (skipped when none is).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from pymolt.core import stdlib

PROBE = Path(__file__).resolve().parents[2] / "bench" / "stdlib_sweep" / "probe.py"
SWEPT = ("3.6", "3.7", "3.8", "3.9", "3.10", "3.11", "3.12", "3.13", "3.14")
#: modules whose presence is platform-dependent (not importable on macOS even when they "exist").
PLATFORM_MODULES = {"msilib", "nis", "ossaudiodev", "spwd"}


def _prev(version: str) -> str:
    major, minor = version.split(".")
    return f"{major}.{int(minor) - 1}"


def _claims():
    """(entry id, symbol, removed_in) for every symbol of every entry that has a removal."""
    for change in stdlib.all_changes():
        if change.removed_in:
            for symbol in change.symbols:
                yield change.id, symbol, change.removed_in


CLAIMS = sorted(_claims())


@pytest.mark.parametrize(
    ("entry", "symbol", "removed_in"), CLAIMS, ids=[f"{e}:{sym}" for e, sym, _ in CLAIMS]
)
def test_data_agrees_with_the_curated_removal(entry, symbol, removed_in):
    assert stdlib.present_in(symbol, removed_in) is False, f"{symbol} present in {removed_in}"
    assert stdlib.present_in(symbol, "3.14") is not True, f"{symbol} is still present on 3.14"
    before = _prev(removed_in)
    if tuple(map(int, before.split("."))) >= (3, 6):
        assert stdlib.present_in(symbol, before) is True, f"{symbol} should be present in {before}"


def test_successors_exist_where_the_change_lands():
    for change in stdlib.all_changes():
        if not change.removed_in or not change.successor:
            continue
        for symbol in change.symbols:
            successor = change.successor_for(symbol)
            if successor and stdlib.is_stdlib(successor.split(".")[0]):
                assert stdlib.present_in(successor, change.removed_in) is not False, (
                    f"{change.id}: successor {successor} is absent in {change.removed_in}"
                )


def test_deprecated_only_entries_are_still_present_on_the_newest():
    for change in stdlib.all_changes():
        if change.removed_in is None:
            for symbol in change.symbols:
                assert stdlib.present_in(symbol, "3.14") is not False, f"{change.id}: {symbol}"


def _uv_python(version: str) -> bool:
    uv = shutil.which("uv")
    if not uv:
        return False
    cmd = [uv, "python", "find", "--managed-python", version]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return proc.returncode == 0


def _probe(version: str, names: list[str]) -> dict[str, str]:
    proc = subprocess.run(
        ["uv", "run", "--managed-python", "--no-project", "--python", version,
         "python", "-I", "-B", str(PROBE), "check"],
        input=json.dumps({"names": names}), capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, proc.stderr[-500:]
    return json.loads(proc.stdout)["present"]


@pytest.mark.skipif(not PROBE.exists(), reason="bench/ is repo-only")
@pytest.mark.parametrize("version", [v for v in SWEPT if tuple(map(int, v.split("."))) >= (3, 8)])
def test_live_interpreter_agrees(version):
    if not _uv_python(version):
        pytest.skip(f"no uv-managed Python {version}")
    ver = tuple(map(int, version.split(".")))
    absent, present = [], []
    for _, symbol, removed_in in CLAIMS:
        if symbol.split(".")[0] in PLATFORM_MODULES:
            continue
        removed = tuple(map(int, removed_in.split(".")))
        (absent if ver >= removed else present).append(symbol)
    # a symbol that disappears only counts as "present before" while its first version has passed
    present = [s for s in present if (stdlib.present_in(s, version) is True)]
    answers = _probe(version, sorted(set(absent) | set(present)))
    wrong_absent = [s for s in absent if answers[s] == "yes"]
    wrong_present = [s for s in present if answers[s] == "no"]
    assert not wrong_absent, f"still present on {version}: {wrong_absent}"
    assert not wrong_present, f"missing on {version}: {wrong_present}"
