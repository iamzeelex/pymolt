"""The compile check: the target interpreter compiles every project file."""
from __future__ import annotations

import sys

import pytest

from pymolt.pyversion.compile_check import compile_check

pytestmark = pytest.mark.skipif(sys.version_info < (3, 12), reason="needs a 3.12+ target")


def _project(tmp_path):
    (tmp_path / "ok.py").write_text("x = 1\n")
    (tmp_path / "escape.py").write_text("import re\nre.compile('\\d+')\n")
    (tmp_path / "py2.py").write_text("print 'hello'\n")
    (tmp_path / "bad_new.py").write_text("async = 1\n")
    for skipped in (".venv", ".git", ".pymolt", "site-packages"):
        (tmp_path / skipped).mkdir()
        (tmp_path / skipped / "broken.py").write_text("def (:\n")
    return tmp_path


def test_reports_syntax_errors_and_warnings_and_skips_tooling_dirs(tmp_path):
    result = compile_check(_project(tmp_path), sys.executable)
    got = {(f["file"], f["kind"]) for f in result["findings"]}
    assert ("escape.py", "SyntaxWarning") in got
    assert ("py2.py", "SyntaxError") in got
    assert not any(f["file"].startswith((".venv", ".git", ".pymolt", "site-packages"))
                   for f in result["findings"])
    assert result["files"] == 4
    finding = next(f for f in result["findings"] if f["file"] == "escape.py")
    assert finding["line"] == 2 and finding["python"].startswith("3.")
    assert "invalid escape" in finding["message"]


def test_a_python2_leftover_is_marked_as_failing_on_the_base_too(tmp_path):
    result = compile_check(_project(tmp_path), sys.executable, base_python="3.6")
    by_file = {f["file"]: f for f in result["findings"] if f["kind"] == "SyntaxError"}
    assert by_file["py2.py"]["base"] is True
    # `async = 1` compiled on 3.6: the migration broke it (not proven, so not True)
    assert by_file["bad_new.py"]["base"] is None


def test_an_interpreter_that_cannot_start_is_a_failure_not_a_crash(tmp_path):
    result = compile_check(tmp_path, "/nonexistent/python")
    assert result["failure"] and result["findings"] == []
