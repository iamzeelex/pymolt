"""The fact diff wired through capture, report, verdict and CLI.

Unit behaviour of the fold/diff lives in ``test_facts.py``; these pin the integration:
call-site text is recorded at capture time (the project changes between captures), the
report carries every non-equal fact, an undecidable fact diff blocks PASS with a reason,
and the CLI names the call, the site and old -> new.
"""
import json
import sys

from pymolt.interfaces.cli import verify_cmd
from pymolt.verify import service
from pymolt.verify.facts import load_site_text
from pymolt.verify.models import CaptureMode, VerificationVerdict
from pymolt.verify.report import ContractReport
from pymolt.verify.service import _fact_diff_reasons, _stamp_verdict


def test_named_capture_records_call_site_text(tmp_path):
    app = tmp_path / "app.py"
    app.write_text("import json\n\nprint(json.dumps({'a': 1}))\n")
    slot = service.capture_named_trace(
        tmp_path, "baseline", CaptureMode.TEST_SUITE,
        command=[sys.executable, str(app)], target="json", cwd=tmp_path,
    )
    assert slot.events >= 1
    site_text = load_site_text(slot.trace_path)
    assert "print(json.dumps({'a': 1}))" in site_text.values()


def _event(sym, line, result, text_root, t="return"):
    ev = {"v": 2, "t": t, "q": sym, "sym": sym, "in": {"bound": {}},
          "where": {"file": str(text_root / "app.py"), "line": line, "func": "main"}}
    if t == "raise":
        ev["raised"] = result
    else:
        ev["result"] = result
    return ev


def _write(path, events):
    path.write_text("".join(json.dumps(e) + "\n" for e in events))
    return path


def test_report_lists_changed_fact_with_site_and_outcomes(tmp_path):
    (tmp_path / "app.py").write_text("import dep\n\ndep.compute()\n")
    old = _write(tmp_path / "old.jsonl", [_event("dep.compute", 3, 1, tmp_path)])
    new = _write(tmp_path / "new.jsonl", [_event("dep.compute", 3, 1.0, tmp_path)])
    from pymolt.verify.report import build_contract_report

    rep = build_contract_report(tmp_path, trace=new, against=old)
    assert rep.diff_clean is False
    (entry,) = rep.fact_diff["entries"]
    assert entry["category"] == "changed"
    assert entry["symbol"] == "dep.compute"
    assert entry["old_site"]["line"] == 3
    assert [o["value"] for o in entry["old"]] == [1]
    assert [o["value"] for o in entry["new"]] == [1.0]


def test_undecidable_fact_diff_blocks_pass_with_a_reason(tmp_path):
    fact_diff = {
        "verdict": "inconclusive",
        "counts": {"rewritten": 1, "equal": 3},
        "entries": [{"category": "rewritten", "comparison": "missing"}],
    }
    reasons = _fact_diff_reasons(fact_diff)
    assert reasons == ["1 rewritten call site(s) could not be paired with a post-migration call"]
    assert _fact_diff_reasons({"verdict": "pass", "counts": {"equal": 1}}) == []
    assert _fact_diff_reasons(None) == []

    report = ContractReport(
        root=".", diff={"disappeared": 0}, diff_clean=True, static_targets=1, confirmed=1,
        fact_diff=fact_diff,
    )
    old = _write(tmp_path / "old.jsonl", [_event("dep.f", 1, 1, tmp_path)])
    new = _write(tmp_path / "new.jsonl", [_event("dep.f", 1, 1, tmp_path)])
    _stamp_verdict(report, str(new), str(old), [])
    assert report.verdict is VerificationVerdict.INCONCLUSIVE
    assert reasons[0] in report.verdict_reasons


def test_cli_renders_each_non_equal_fact(capsys, monkeypatch):
    from rich.console import Console

    monkeypatch.setattr(verify_cmd, "console", Console(width=240))
    fact_diff = {
        "counts": {"changed": 1, "equal": 2},
        "entries": [
            {"category": "changed", "fails": True, "symbol": "pandas.core.frame.DataFrame.mean",
             "old_site": {"file": "toyapp/core.py", "line": 24, "func": "average"},
             "old": [{"kind": "return", "value": {"v": [2.0]}, "count": 1}],
             "new": [{"kind": "raise", "value": "TypeError", "count": 1}]},
            {"category": "equal", "symbol": "x.y"},
        ],
    }
    verify_cmd._render_fact_changes(fact_diff)
    out = capsys.readouterr().out
    assert "CHANGED" in out and "DataFrame.mean" in out
    assert "toyapp/core.py:24" in out
    assert "raises TypeError" in out
    assert "x.y" not in out  # equal facts are not listed


def _views_project(root):
    (root / "myapp").mkdir()
    (root / "myapp" / "__init__.py").write_text("")
    (root / "myapp" / "views.py").write_text(
        "import dep\n\n\ndef handler():\n    result = dep.compute()\n    return result.size()\n"
    )
    return root


def _view_event(sym, result, root, t="return", line=5):
    ev = {"v": 2, "t": t, "q": sym, "sym": sym, "in": {"bound": {}},
          "where": {"file": str(root / "myapp" / "views.py"), "line": line, "func": "handler"}}
    ev["raised" if t == "raise" else "result"] = result
    return ev


def _verdict_for(root, old_events, new_events):
    from pymolt.verify.report import build_contract_report

    old = _write(root / "old.jsonl", old_events)
    new = _write(root / "new.jsonl", new_events)
    rep = build_contract_report(root, trace=new, against=old, resolve_identities=False)
    _stamp_verdict(rep, str(new), str(old), [])
    return rep


def test_dynamic_only_symbols_compared_equal_do_not_block_pass(tmp_path):
    root = _views_project(tmp_path)
    # dep.compute is in the static map; dep.Result.size (a method on its return value)
    # is not, but it was observed on both sides with the same outcome.
    events = [_view_event("dep.compute", {"__opaque__": "dep.Result"}, root),
              _view_event("dep.Result.size", 3, root, line=6)]
    rep = _verdict_for(root, events, events)
    assert rep.dynamic_only == 1
    assert rep.fact_equal_symbols == ["dep.Result.size", "dep.compute"]
    assert rep.verdict is VerificationVerdict.PASS, rep.verdict_reasons
    assert any("under-approximation" in note for note in rep.notes)


def test_dynamic_only_symbol_with_unsettled_fact_still_blocks_pass(tmp_path):
    root = _views_project(tmp_path)
    old = [_view_event("dep.compute", {"__opaque__": "dep.Result"}, root),
           _view_event("dep.Result.size", 3, root, line=6)]
    # The method's outcome cannot be compared: it lost its distribution on the new side.
    new = [_view_event("dep.compute", {"__opaque__": "dep.Result"}, root),
           _view_event("dep.Result.size", {"__nondeterministic__": True}, root, line=6)]
    rep = _verdict_for(root, old, new)
    assert "dep.Result.size" not in rep.fact_equal_symbols
    assert rep.verdict is not VerificationVerdict.PASS
    assert any("not compared equal on both sides" in r for r in rep.verdict_reasons)


def test_dynamic_only_blocks_pass_without_a_fact_diff(tmp_path):
    root = _views_project(tmp_path)
    report = ContractReport(
        root=str(root), diff={"disappeared": 0}, diff_clean=True, static_targets=1,
        confirmed=1, dynamic_only=1,
    )
    report.symbols = []
    from pymolt.verify.report import SymbolStatus

    report.symbols = [SymbolStatus(target="dep.Result.size", dep="dep", status="dynamic-only")]
    trace = _write(root / "t.jsonl", [_view_event("dep.compute", 1, root)])
    _stamp_verdict(report, str(trace), str(trace), [])
    assert report.verdict is VerificationVerdict.INCONCLUSIVE
    assert "1 observed symbol(s) are missing from the static map" in report.verdict_reasons
