"""
tests/test_ghost_stubs.py

Tests for Ghost Stub Synthesizer.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from axiom_graph.analyzers.ghost_stubs import synthesize_ghost_stubs


def test_synthesize_ghost_stubs_math() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        stub_path = synthesize_ghost_stubs(
            package_dir=root,
            module_name="math",
            output_dir=root / ".stubs",
            timeout=5.0,
        )

        assert stub_path is not None
        assert stub_path.exists()

        content = stub_path.read_text(encoding="utf-8")
        assert "from typing import Any" in content
        assert "def sin" in content
        assert "def cos" in content
        assert "pi: Any = ..." in content


def test_synthesize_ghost_stubs_invalid_module() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        stub_path = synthesize_ghost_stubs(
            package_dir=root,
            module_name="non_existent_fake_c_module_xyz",
            output_dir=root / ".stubs",
            timeout=3.0,
        )
        assert stub_path is None
