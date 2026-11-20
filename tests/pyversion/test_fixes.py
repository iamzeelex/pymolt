"""Python fixers: patterns from facts, the minimal-diff gate, the re-compile gate, the plan
stage, and a differential against Ruff's UP035."""
from __future__ import annotations

import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from pymolt.codemods.apply import apply_patterns
from pymolt.codemods.models import CodemodPattern, FilePreview
from pymolt.delta.models import DeltaFact, DeltaReport
from pymolt.pyversion.fixes import (
    apply_python_fixes,
    minimal_diff_violations,
    python_patterns,
    recompile,
)


def _site(spelling, line, file="app.py", guard=None, kind="attr"):
    return SimpleNamespace(spelling=spelling, module=spelling.split(".")[0], file=file,
                           line=line, kind=kind, reached_at="import", guard=guard)


def _report(*symbols, kind="MOVED", base="3.6", target="3.12"):
    facts = [DeltaFact(symbol=s, kind=kind, successor=succ) for s, succ in symbols]
    return DeltaReport(package="python", from_version=base, to_version=target,
                       quality="complete", subject="python", facts=facts)


def test_advisory_removals_get_no_pattern():
    report = _report(("imp.find_module", None), ("ssl.wrap_socket", None), kind="REMOVED")
    sites = [_site("imp.find_module", 1), _site("ssl.wrap_socket", 2)]
    assert python_patterns(report, sites) == []


def test_same_object_runs_on_base_and_newer_successors_do_not():
    report = _report(("collections.Iterable", "collections.abc.Iterable"),
                     ("asyncio.Task.current_task", None), kind="REMOVED")
    sites = [_site("collections.Iterable", 1), _site("asyncio.Task.current_task", 2)]
    by_old = {(p.old_qualname, p.kind): p for p in python_patterns(report, sites)}
    abc = by_old[("collections.Iterable", "rewrite-attr")]
    assert abc.runs_on_base is True and abc.scope == "python"
    assert abc.requires_import == "collections.abc"
    current = by_old[("asyncio.Task.current_task", "replace-attr-path")]
    assert current.runs_on_base is False and current.new_qualname == "asyncio.current_task"
    assert ("collections.Iterable", "rewrite-import") in by_old


def test_a_spelling_with_only_guarded_sites_gets_no_pattern():
    guard = {"kind": "guarded-fallback", "detail": "except ImportError"}
    report = _report(("collections.Iterable", "collections.abc.Iterable"))
    assert python_patterns(report, [_site("collections.Iterable", 4, guard=guard)]) == []


def test_import_gets_replaced_and_the_gate_accepts_it():
    old = "import imp\nimp.reload(m)\n"
    pats = [CodemodPattern(old_qualname="imp.reload", new_qualname="importlib.reload",
                           kind="replace-attr-path", scope="python")]
    new, hits = apply_patterns(old, pats)
    # `import imp` fails on 3.12, so it goes with its last use
    assert new == "import importlib\n\nimportlib.reload(m)\n" and len(hits) == 1
    assert minimal_diff_violations(old, new, [p for p, _ in hits]) == []


def test_the_gate_flags_a_line_with_no_flagged_spelling():
    pats = [CodemodPattern(old_qualname="imp.reload", new_qualname="importlib.reload",
                           kind="replace-attr-path", scope="python")]
    assert minimal_diff_violations("x = 1\nimp.reload(m)\n", "x = 2\nimportlib.reload(m)\n",
                                   pats) == ["line 1: x = 1"]


def test_recompile_reports_a_file_that_does_not_compile():
    bad, failure = recompile({"ok.py": "x = 1\n", "bad.py": "def (:\n"}, sys.executable)
    assert failure is None and list(bad) == ["bad.py"]


def _project(tmp_path):
    (tmp_path / "app.py").write_text(
        "import collections\nfrom collections import Iterable, OrderedDict\n"
        "try:\n    from collections.abc import Mapping\nexcept ImportError:\n"
        "    from collections import Mapping\n"
        "x = collections.Iterable\n")
    return tmp_path


def _sites():
    guard = {"kind": "guarded-fallback", "detail": "except ImportError"}
    return [_site("collections.Iterable", 2, kind="from"),
            _site("collections.Mapping", 6, kind="from", guard=guard),
            _site("collections.Iterable", 7)]


def test_apply_python_fixes_skips_the_fallback_and_records_the_patterns(tmp_path):
    root = _project(tmp_path)
    report = _report(("collections.Iterable", "collections.abc.Iterable"),
                     ("collections.Mapping", "collections.abc.Mapping"))
    pats = python_patterns(report, _sites())
    out = apply_python_fixes(root, [], pats, _sites(), target="3.12",
                             target_executable=sys.executable)
    assert [p.path for p in out.previews] == [str(root / "app.py")]
    new = out.previews[0].new_source
    assert "    from collections import Mapping\n" in new           # the fallback is intact
    assert "from collections import OrderedDict\nfrom collections.abc import Iterable\n" in new
    assert "x = collections.abc.Iterable" in new and "import collections.abc" in new
    assert out.dropped == {}


def test_a_file_that_fails_to_compile_keeps_its_old_source(tmp_path, monkeypatch):
    root = _project(tmp_path)
    report = _report(("collections.Iterable", "collections.abc.Iterable"))
    pats = python_patterns(report, _sites())
    monkeypatch.setattr("pymolt.pyversion.fixes.recompile",
                        lambda files, exe, **k: ({p: "SyntaxError: boom" for p in files}, None))
    out = apply_python_fixes(root, [], pats, _sites(), target="3.12",
                             target_executable=sys.executable)
    assert out.previews == [] and "app.py" in out.dropped
    assert any("dropped" in n for n in out.notes)


def test_python_fixes_layer_onto_a_dependency_preview(tmp_path):
    root = _project(tmp_path)
    source = (root / "app.py").read_text()
    prior = FilePreview(path=str(root / "app.py"), old_source=source,
                        new_source=source.replace("OrderedDict", "OrderedDict2"), sites=1)
    report = _report(("collections.Iterable", "collections.abc.Iterable"))
    pats = python_patterns(report, _sites())
    out = apply_python_fixes(root, [prior], pats, _sites(), target="3.12",
                             target_executable=sys.executable)
    (merged,) = out.previews
    assert merged.old_source == source and "OrderedDict2" in merged.new_source
    assert "collections.abc import Iterable" in merged.new_source


pytestmark_plan = pytest.mark.skipif(sys.version_info < (3, 12), reason="targets this interpreter")


@pytestmark_plan
def test_the_plan_marks_fixed_sites_and_keeps_advisories_manual(tmp_path, monkeypatch):
    from pymolt.pyversion.plan import plan_python_stage

    (tmp_path / "app.py").write_text(
        "import collections\nimport ssl\nx = collections.Iterable\ny = ssl.wrap_socket\n")
    sites = [_site("collections.Iterable", 3), _site("ssl.wrap_socket", 4)]
    monkeypatch.setattr("pymolt.pyversion.facts.find_target_interpreter",
                        lambda *a, **k: sys.executable)
    stage = plan_python_stage(tmp_path, "3.6", "3.12", sites, previews=[])
    risks = {r.symbol: r for r in stage.risks.risks}
    assert risks["collections.Iterable"].fixed_by_plan
    assert not risks["ssl.wrap_socket"].fixed_by_plan and risks["ssl.wrap_socket"].fix == "advisory"
    assert "collections.abc.Iterable" in stage.previews[0].new_source


# ── differential against Ruff's UP035 (dev only) ──────────────────────────────────────────


def _ruff():
    exe = shutil.which("ruff")
    if exe:
        return [exe]
    probe = subprocess.run([sys.executable, "-m", "ruff", "--version"], capture_output=True)
    return [sys.executable, "-m", "ruff"] if probe.returncode == 0 else None


def _normalise(source: str) -> list[str]:
    """Imports as a sorted list of ``module: sorted names``, so statement order and wrapping
    do not matter."""
    import ast

    out = []
    for node in ast.parse(source).body:
        if isinstance(node, ast.ImportFrom):
            names = sorted(a.name + (f" as {a.asname}" if a.asname else "") for a in node.names)
            out.append(f"{node.module}: {', '.join(names)}")
    return sorted(out)


@pytest.mark.parametrize("source", [
    "from collections import Iterable\n",
    "from collections import Iterable, Mapping\n",
    "from collections import Iterable, Mapping, OrderedDict\n",
    "from collections import (Iterable,\n                         OrderedDict)\n",
    "from collections import Callable as C, deque\n",
])
def test_the_rewrite_equals_ruff_up035(tmp_path, source):
    ruff = _ruff()
    if ruff is None:
        pytest.skip("ruff is not available")
    names = {n.strip().split(" ")[0].strip("()") for n in
             source.split("import", 1)[1].replace("\n", " ").split(",")}
    moved = sorted(n for n in names if n in {"Iterable", "Mapping", "Callable"})
    pats = [CodemodPattern(old_qualname=f"collections.{n}", new_qualname=f"collections.abc.{n}",
                           kind="rewrite-import", scope="python") for n in moved]
    ours, _ = apply_patterns(source, pats)
    target = tmp_path / "u.py"
    target.write_text(source)
    subprocess.run([*ruff, "check", "--select", "UP035", "--fix", "--target-version", "py312",
                    "--isolated", "--quiet", str(target)], capture_output=True)
    assert _normalise(ours) == _normalise(target.read_text())


def test_python_fixes_land_on_the_live_site_after_a_dependency_rewrite_moved_it(tmp_path):
    """The sites' lines are the original file's; a dependency rewrite that splits a
    multi-name import (+1 line) shifts them. Applied unmapped, the fix rewrote the guarded
    fallback (old line 6, new line 7) and left the live use (old line 7, new line 8)."""
    source = ("import collections\n"                                          # 1
              "from pandas.util.testing import assert_frame_equal, makeDataFrame\n"  # 2
              "try:\n"                                                         # 3
              "    from collections.abc import Iterable\n"                     # 4
              "except ImportError:\n"                                          # 5
              "    Iterable = collections.Iterable\n"                          # 6 guarded
              "X = collections.Iterable\n")                                    # 7 live
    (tmp_path / "app.py").write_text(source)
    split = source.replace(
        "from pandas.util.testing import assert_frame_equal, makeDataFrame\n",
        "from pandas.util.testing import makeDataFrame\n"
        "from pandas.testing import assert_frame_equal\n")
    prior = FilePreview(path=str(tmp_path / "app.py"), old_source=source, new_source=split,
                        sites=1)
    guard = {"kind": "guarded-fallback", "detail": "except ImportError"}
    sites = [_site("collections.Iterable", 6, guard=guard), _site("collections.Iterable", 7)]
    report = _report(("collections.Iterable", "collections.abc.Iterable"))
    pats = [p for p in python_patterns(report, sites) if p.kind == "rewrite-attr"]

    out = apply_python_fixes(tmp_path, [prior], pats, sites, target="3.12",
                             target_executable=sys.executable)

    (merged,) = out.previews
    lines = merged.new_source.splitlines()
    assert "    Iterable = collections.Iterable" in lines           # the fallback is intact
    assert "X = collections.abc.Iterable" in lines                   # the live use is fixed
    assert "from pandas.testing import assert_frame_equal" in lines  # the dependency rewrite
    assert out.dropped == {}


def test_the_gate_flags_a_rewrite_of_a_line_that_holds_no_live_site():
    pats = [CodemodPattern(old_qualname="collections.Iterable",
                           new_qualname="collections.abc.Iterable", kind="rewrite-attr",
                           scope="python")]
    old = "import collections\nA = collections.Iterable\nB = collections.Iterable\n"
    new = "import collections\nA = collections.abc.Iterable\nB = collections.Iterable\n"
    assert minimal_diff_violations(old, new, pats, live={2}) == []
    assert minimal_diff_violations(old, new, pats, live={3}) == [
        "line 2: A = collections.Iterable (no live site)"]


# ── several fixes in one file: each lands on its own site ─────────────────────────────────


def _fix_file(tmp_path, source, sites, *symbols, prior_source=None):
    (tmp_path / "app.py").write_text(source)
    previews = [] if prior_source is None else [FilePreview(
        path=str(tmp_path / "app.py"), old_source=source, new_source=prior_source, sites=1)]
    pats = python_patterns(_report(*symbols), sites)
    return apply_python_fixes(tmp_path, previews, pats, sites, target="3.12",
                              target_executable=sys.executable)


def test_a_second_fix_lands_after_the_first_inserted_an_import(tmp_path):
    """The first fix adds ``import collections.abc`` (+1 line); the second fix's site line is
    the input's, so applied unmapped it looked at ``A`` and left ``B`` unfixed."""
    out = _fix_file(tmp_path, "import collections\nA = collections.Iterable\n"
                              "B = collections.Mapping\n",
                    [_site("collections.Iterable", 2), _site("collections.Mapping", 3)],
                    ("collections.Iterable", "collections.abc.Iterable"),
                    ("collections.Mapping", "collections.abc.Mapping"))
    (fixed,) = out.previews
    assert fixed.new_source == ("import collections\nimport collections.abc\n\n"
                                "A = collections.abc.Iterable\nB = collections.abc.Mapping\n")
    assert fixed.sites == 2 and out.dropped == {}


def test_three_fixes_in_one_file_each_land_on_their_site(tmp_path):
    """Three spellings, two of them in one statement: the imports the fixes add and drop move
    every later site; each fix still finds its own."""
    source = ("import collections\nimport imp\n"
              "A = collections.Iterable\n"                                        # 3
              "imp.reload(collections)\n"                                         # 4
              "B = isinstance(A, (collections.Mapping, collections.Iterable))\n")  # 5
    sites = [_site("collections.Iterable", 3), _site("imp.reload", 4),
             _site("collections.Mapping", 5), _site("collections.Iterable", 5)]
    report = DeltaReport(
        package="python", from_version="3.6", to_version="3.12", quality="complete",
        subject="python", facts=[
            DeltaFact(symbol="collections.Iterable", kind="MOVED",
                      successor="collections.abc.Iterable"),
            DeltaFact(symbol="imp.reload", kind="REMOVED", successor=None),
            DeltaFact(symbol="collections.Mapping", kind="MOVED",
                      successor="collections.abc.Mapping")])
    (tmp_path / "app.py").write_text(source)
    out = apply_python_fixes(tmp_path, [], python_patterns(report, sites), sites,
                             target="3.12", target_executable=sys.executable)
    (fixed,) = out.previews
    assert fixed.new_source == (
        "import collections\nimport collections.abc\nimport importlib\n\n"
        "A = collections.abc.Iterable\n"
        "importlib.reload(collections)\n"
        "B = isinstance(A, (collections.abc.Mapping, collections.abc.Iterable))\n")
    assert fixed.sites == 4 and out.dropped == {}


def test_fixes_after_a_dependency_rewrite_that_changed_the_line_count(tmp_path):
    """Two shifts in a row: the dependency rewrite splits an import (+1), then the first fix
    adds one. The second fix's site is carried through both."""
    source = ("import collections\n"
              "from pandas.util.testing import assert_frame_equal, makeDataFrame\n"
              "A = collections.Iterable\n"                                        # 3
              "B = collections.Mapping\n")                                        # 4
    split = source.replace(
        "from pandas.util.testing import assert_frame_equal, makeDataFrame\n",
        "from pandas.util.testing import makeDataFrame\n"
        "from pandas.testing import assert_frame_equal\n")
    out = _fix_file(tmp_path, source,
                    [_site("collections.Iterable", 3), _site("collections.Mapping", 4)],
                    ("collections.Iterable", "collections.abc.Iterable"),
                    ("collections.Mapping", "collections.abc.Mapping"), prior_source=split)
    (merged,) = out.previews
    lines = merged.new_source.splitlines()
    assert merged.old_source == source
    assert "from pandas.testing import assert_frame_equal" in lines       # the dependency rewrite
    assert "A = collections.abc.Iterable" in lines
    assert "B = collections.abc.Mapping" in lines
    assert merged.sites == 1 + 2 and out.dropped == {}


_FALLBACK = ("import collections\n"
             "A = collections.Iterable\n"                                      # 2 live
             "try:\n"
             "    from collections.abc import Mapping\n"
             "except ImportError:\n"
             "    Mapping = collections.Mapping\n"                             # 6 guarded
             "print(Mapping)\n"
             "B = collections.Mapping\n")                                      # 8 live


def _fallback_sites():
    guard = {"kind": "guarded-fallback", "detail": "except ImportError"}
    return [_site("collections.Iterable", 2), _site("collections.Mapping", 6, guard=guard),
            _site("collections.Mapping", 8)]


def test_a_guarded_fallback_stays_untouched_when_an_earlier_fix_moved_the_lines(tmp_path):
    """The first fix adds two lines (``import collections.abc`` and a blank), so the live
    ``B`` (line 8) is now where the fallback was carried to. Unmapped, the ``Mapping`` fix
    rewrote the fallback; the gate then dropped the file and both fixes were lost."""
    out = _fix_file(tmp_path, _FALLBACK, _fallback_sites(),
                    ("collections.Iterable", "collections.abc.Iterable"),
                    ("collections.Mapping", "collections.abc.Mapping"))
    (fixed,) = out.previews
    lines = fixed.new_source.splitlines()
    assert "    Mapping = collections.Mapping" in lines                # the fallback is intact
    assert "A = collections.abc.Iterable" in lines
    assert "B = collections.abc.Mapping" in lines
    assert out.dropped == {}


def test_the_minimal_diff_gate_still_drops_a_stray_edit_among_several_fixes(tmp_path,
                                                                           monkeypatch):
    """The gate checks the whole input -> output change against the live sites on the input:
    a fixer that also rewrote the guarded fallback is still caught."""
    import pymolt.codemods.apply as applier

    real = applier.apply_patterns

    def stray(source, patterns, **kw):
        new, hits = real(source, patterns, **kw)
        return new.replace("    Mapping = collections.Mapping",
                           "    Mapping = collections.abc.Mapping"), hits

    monkeypatch.setattr(applier, "apply_patterns", stray)
    out = _fix_file(tmp_path, _FALLBACK, _fallback_sites(),
                    ("collections.Iterable", "collections.abc.Iterable"),
                    ("collections.Mapping", "collections.abc.Mapping"))
    assert out.previews == [] and "no live site" in out.dropped["app.py"]
