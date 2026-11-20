import ast
import re
import subprocess
import sys
from pathlib import Path

import pytest

from pymolt.ingestion.probes import LEGACY_PROBES, PYTHON_VERSION_PROBE

_BANNED = (ast.JoinedStr, ast.NamedExpr, ast.AnnAssign)


@pytest.mark.parametrize("name", sorted(LEGACY_PROBES))
def test_legacy_probe_has_no_modern_syntax(name):
    src = LEGACY_PROBES[name]
    compile(src, "<probe>", "exec")
    tree = ast.parse(src)
    assert not [n for n in ast.walk(tree) if isinstance(n, _BANNED)]


def test_python_version_probe_output():
    out = subprocess.run(
        [sys.executable, "-c", PYTHON_VERSION_PROBE], capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert out == "{}.{}".format(*sys.version_info[:2])


def test_no_inline_fstring_probes_in_ingestion():
    """No `"-c", "...f'..."` literals sneak back into host-side probe call sites."""
    root = Path(__file__).resolve().parents[2] / "pymolt" / "ingestion"
    pattern = re.compile(r"\"-c\",\s*\"[^\"]*f'")
    offenders = [p.name for p in root.glob("*.py") if pattern.search(p.read_text(encoding="utf-8"))]
    assert offenders == []
