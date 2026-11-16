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
    assert minimal_diff_violations(old, new, [p for p, _ in hits]) == []


def test_the_gate_flags_a_line_with_no_flagged_spelling():
    pats = [CodemodPattern(old_qualname="imp.reload", new_qualname="importlib.reload",
                           kind="replace-attr-path", scope="python")]
    assert minimal_diff_violations("x = 1\nimp.reload(m)\n", "x = 2\nimportlib.reload(m)\n",
                                   pats)


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
