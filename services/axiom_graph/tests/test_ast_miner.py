"""
Tests for ast_miner.py — uses in-memory fixture files, no filesystem outside tmp.
"""

import ast
import textwrap
from pathlib import Path

import pytest

from axiom_graph.analyzers.ast_miner import mine_deprecation_hints, _DeprecationVisitor


class TestDeprecationVisitor:
    def _visit(self, source: str, prefix: str = "pkg") -> dict[str, str]:
        tree = ast.parse(textwrap.dedent(source))
        visitor = _DeprecationVisitor(prefix)
        visitor.visit(tree)
        return visitor.hints

    def test_decorator_form(self):
        src = """
        from typing_extensions import deprecated

        @deprecated("Use bar() instead")
        def foo():
            pass
        """
        hints = self._visit(src)
        assert "pkg.foo" in hints
        assert "bar()" in hints["pkg.foo"]

    def test_warnings_warn_form(self):
        src = """
        import warnings

        def old_api():
            warnings.warn("old_api is deprecated, use new_api", DeprecationWarning)
        """
        hints = self._visit(src)
        assert "pkg.old_api" in hints
        assert "deprecated" in hints["pkg.old_api"].lower()

    def test_decorator_priority_over_warn(self):
        src = """
        @deprecated("Cleaner decorator message")
        def mixed():
            import warnings
            warnings.warn("Messier warn message", DeprecationWarning)
        """
        hints = self._visit(src)
        assert hints.get("pkg.mixed") == "Cleaner decorator message"

    def test_class_method(self):
        src = """
        class DataFrame:
            @deprecated("Use concat() instead")
            def append(self, other):
                pass
        """
        hints = self._visit(src)
        assert "pkg.DataFrame.append" in hints

    def test_nested_class(self):
        src = """
        class Outer:
            class Inner:
                @deprecated("Inner.method is gone")
                def method(self):
                    pass
        """
        hints = self._visit(src)
        assert "pkg.Outer.Inner.method" in hints

    def test_no_deprecation(self):
        src = """
        def normal_function():
            x = 1 + 1
        """
        hints = self._visit(src)
        assert hints == {}

    def test_bare_warn_ignored_if_no_deprecation_keyword(self):
        src = """
        def fn():
            import warnings
            warnings.warn("something bad happened")
        """
        hints = self._visit(src)
        assert "pkg.fn" not in hints


class TestMineDeprecationHints:
    def test_mine_from_directory(self, tmp_path: Path):
        pkg_dir = tmp_path / "mypkg"
        pkg_dir.mkdir()
        (pkg_dir / "__init__.py").write_text("")
        (pkg_dir / "utils.py").write_text(textwrap.dedent("""
            import warnings

            def legacy():
                warnings.warn("legacy() is deprecated, use modern()", DeprecationWarning)
        """))

        hints = mine_deprecation_hints(pkg_dir, "mypkg")
        assert any("legacy" in k for k in hints)

    def test_skips_test_files(self, tmp_path: Path):
        pkg_dir = tmp_path / "mypkg"
        pkg_dir.mkdir()
        (pkg_dir / "__init__.py").write_text("")
        (pkg_dir / "test_utils.py").write_text(textwrap.dedent("""
            import warnings
            def test_fn():
                warnings.warn("should be ignored", DeprecationWarning)
        """))

        hints = mine_deprecation_hints(pkg_dir, "mypkg")
        assert not any("test_fn" in k for k in hints)

    def test_handles_syntax_error_gracefully(self, tmp_path: Path):
        pkg_dir = tmp_path / "mypkg"
        pkg_dir.mkdir()
        (pkg_dir / "__init__.py").write_text("")
        (pkg_dir / "broken.py").write_text("def (:")  # invalid syntax

        # Should not raise
        hints = mine_deprecation_hints(pkg_dir, "mypkg")
        assert isinstance(hints, dict)

    def test_empty_dir(self, tmp_path: Path):
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        hints = mine_deprecation_hints(empty_dir, "empty")
        assert hints == {}
