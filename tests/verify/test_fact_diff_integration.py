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
    assert rep.verdict is VerificationVerdict.INCONCLUSIVE
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


# ── facts snapshots through report and verdict (phase 2) ─────────────────────────────────

def _facts_trace(tmp_path, name, calls, **kw):
    """A trace the way a facts capture leaves it: snapshot + its projection in the JSONL."""
    from pymolt.verify._sinks import FactSink
    from pymolt.verify.facts import merge_fact_snapshots, project_snapshot, snapshot_path

    trace = tmp_path / f"{name}.jsonl"
    sink = FactSink(str(tmp_path / f"raw-{name}.jsonl"), flush_seconds=0, **kw)
    for line, n in calls:
        sink.on_return("dep.compute", {"bound": {"n": n}}, n,
                       site={"file": str(tmp_path / "app.py"), "line": line, "func": "main"},
                       sym="dep.compute")
    sink.close()
    merge_fact_snapshots([sink.path], snapshot_path(trace))
    trace.write_text("")
    project_snapshot(snapshot_path(trace), trace)
    return trace


def test_an_evicting_snapshot_blocks_pass_with_a_reason(tmp_path):
    from pymolt.verify.report import build_contract_report

    (tmp_path / "app.py").write_text("import dep\n\ndep.compute()\n")
    calls = [(3, 0)] * 5 + [(3, n) for n in range(1, 30)]
    old = _facts_trace(tmp_path, "old", calls, max_facts=8)
    new = _facts_trace(tmp_path, "new", calls, max_facts=8)
    rep = build_contract_report(tmp_path, trace=new, against=old)
    assert rep.diff_clean is True
    assert rep.fact_diff["verdict"] == "inconclusive" and rep.fact_diff["incomplete"] is True
    reasons = _fact_diff_reasons(rep.fact_diff)
    assert any("baseline facts snapshot reached its fact cap" in r for r in reasons)
    _stamp_verdict(rep, str(new), str(old), [])
    assert rep.verdict is VerificationVerdict.INCONCLUSIVE
    assert any("evicted" in r for r in rep.verdict_reasons)


def test_widened_snapshot_is_a_note_not_a_blocker(tmp_path):
    from pymolt.verify.report import build_contract_report

    (tmp_path / "app.py").write_text("import dep\n\ndep.compute()\n")
    old = _facts_trace(tmp_path, "old", [(3, n) for n in range(10)], facts_per_site=4)
    new = _facts_trace(tmp_path, "new", [(3, n) for n in reversed(range(10))],
                       facts_per_site=4)
    rep = build_contract_report(tmp_path, trace=new, against=old)
    assert rep.fact_diff["verdict"] == "pass"
    _stamp_verdict(rep, str(new), str(old), [])
    assert any(note.startswith("widened:") for note in rep.notes)
    assert not any("widened" in r for r in rep.verdict_reasons)


def test_command_capture_uses_the_facts_sink_end_to_end(tmp_path):
    from pymolt.verify.facts import load_facts, snapshot_path

    app = tmp_path / "app.py"
    app.write_text(
        "import json, threading\n"
        "def work():\n"
        "    for i in range(500):\n"
        "        json.loads('[%d]' % (i % 3))\n"
        "ts = [threading.Thread(target=work) for _ in range(4)]\n"
        "[t.start() for t in ts]\n"
        "[t.join() for t in ts]\n"
    )
    slot = service.capture_named_trace(
        tmp_path, "baseline", CaptureMode.LIVE_COMMAND,
        command=[sys.executable, str(app)], target="json", cwd=tmp_path,
    )
    assert slot.validity.value == "valid", slot.validity_reason
    # A long run samples values (novelty first, then 10%) and still counts every call.
    assert slot.sample_rate == service.LONG_RUN_SAMPLE_RATE
    assert slot.events_seen == 2000 and slot.dropped_events == 0
    assert 0 < slot.events < 2000
    meta = json.loads(open(slot.metadata_path).read())
    assert meta["sampling"] == {"rate": 0.1, "first": 32, "source": "default"}
    assert snapshot_path(slot.trace_path).is_file()
    fs = load_facts(slot.trace_path, root=tmp_path)
    assert fs.snapshot and fs.calls == 2000 and fs.tallies_complete
    assert sum(f.count for f in fs.facts) == slot.events
    site_text = load_site_text(slot.trace_path)
    assert "json.loads('[%d]' % (i % 3))" in site_text.values()


def _two_calls(tmp_path, other_new):
    (tmp_path / "app.py").write_text("import dep\n\ndep.compute()\ndep.other()\n")
    old = _write(tmp_path / "old.jsonl", [_event("dep.compute", 3, 1, tmp_path),
                                          _event("dep.other", 4, "a", tmp_path)])
    new = _write(tmp_path / "new.jsonl", [_event("dep.compute", 3, 1, tmp_path),
                                          _event("dep.other", 4, other_new, tmp_path)])
    return old, new


def test_a_change_outside_the_impact_set_still_fails_and_is_listed(tmp_path):
    from pymolt.interfaces import views
    from pymolt.verify.report import build_contract_report

    old, new = _two_calls(tmp_path, "b")
    rep = build_contract_report(tmp_path, trace=new, against=old, impact_set={"dep.compute"})
    _stamp_verdict(rep, str(new), str(old), [])
    assert rep.impact_filter_active and rep.diff_clean is True        # the focused view is clean
    assert rep.all_diff_clean is False
    assert rep.verdict is VerificationVerdict.FAIL
    assert [e["symbol"] for e in rep.unpredicted_changes] == ["dep.other"]
    assert any("outside the plan's predicted impacts" in r and "dep.other" in r
               for r in rep.verdict_reasons)
    assert views._site_text(rep.unpredicted_changes[0]["old_site"]).endswith("app.py:4")


def test_a_change_inside_the_impact_set_fails_and_nothing_is_unpredicted(tmp_path):
    from pymolt.verify.report import build_contract_report

    old, new = _two_calls(tmp_path, "b")
    rep = build_contract_report(tmp_path, trace=new, against=old, impact_set={"dep.other"})
    _stamp_verdict(rep, str(new), str(old), [])
    assert rep.diff_clean is False and rep.verdict is VerificationVerdict.FAIL
    assert rep.unpredicted_changes == []


def _plan_impact(path):
    from pymolt.migration_plan import PlannedApiImpact

    return PlannedApiImpact(package="dep", path=path, kind="signature-changed",
                            state="changed", risk="medium", explanation="changed")


def _one_call_run(tmp_path):
    """dep.compute and dep.other are both in the code; the run only reaches dep.compute,
    with the same outcome on both sides."""
    (tmp_path / "app.py").write_text(
        "import dep\n\n\ndef main():\n    dep.compute()\n\n\ndef never():\n    dep.other()\n")
    events = [_event("dep.compute", 5, 1, tmp_path)]
    return _write(tmp_path / "old.jsonl", events), _write(tmp_path / "new.jsonl", events)


def test_after_apply_an_unexercised_symbol_outside_the_plan_blocks_pass(tmp_path):
    from pymolt.verify.report import build_contract_report

    old, new = _one_call_run(tmp_path)
    by_hand = build_contract_report(tmp_path, trace=new, against=old)
    _stamp_verdict(by_hand, str(new), str(old), [])
    applied = build_contract_report(tmp_path, trace=new, against=old,
                                    changed_api_paths=[_plan_impact("dep.compute")])
    _stamp_verdict(applied, str(new), str(old), [])

    # What the plan changed was exercised and compared equal, but dep.other never ran: the
    # same captures without a plan are INCONCLUSIVE, and so is the applied migration.
    assert applied.impacted_confirmed == 1 and applied.impacted_blind == 0
    assert by_hand.verdict is VerificationVerdict.INCONCLUSIVE
    assert applied.verdict is VerificationVerdict.INCONCLUSIVE
    # (no executed record: app.py ran, so dep.other's line counts as ran by its file)
    assert applied.verdict_reasons == [
        "1 static dependency symbol(s) outside the plan's predicted impacts were not "
        "exercised (1 ran but not recorded at file granularity): dep.other"]
    # the headline numbers are the whole surface, the plan's paths are the focus
    assert (applied.static_targets, applied.confirmed, applied.blind, applied.coverage,
            applied.recall) == (by_hand.static_targets, by_hand.confirmed, by_hand.blind,
                                by_hand.coverage, by_hand.recall)
    assert (applied.impacted_static_targets, applied.impacted_blind) == (1, 0)


def test_after_apply_blind_reasons_name_inside_and_outside_the_plan_apart(tmp_path):
    from pymolt.verify.report import build_contract_report

    old, new = _one_call_run(tmp_path)
    (tmp_path / "more.py").write_text("import dep\n\n\ndef later():\n    dep.later()\n")
    rep = build_contract_report(tmp_path, trace=new, against=old,
                                changed_api_paths=[_plan_impact("dep.later")])
    _stamp_verdict(rep, str(new), str(old), [])
    assert rep.verdict is VerificationVerdict.INCONCLUSIVE
    assert rep.verdict_reasons == [
        "1 static dependency symbol(s) inside the plan's predicted impacts were not "
        "exercised (1 not run by the workload): dep.later",
        "1 static dependency symbol(s) outside the plan's predicted impacts were not "
        "exercised (1 ran but not recorded at file granularity): dep.other"]


def test_after_apply_the_whole_surface_can_pass(tmp_path):
    from pymolt.verify.report import build_contract_report

    root = _views_project(tmp_path)
    # dep.Result.size is dynamic-only and outside the plan; its facts compared equal.
    events = [_view_event("dep.compute", {"__opaque__": "dep.Result"}, root),
              _view_event("dep.Result.size", 3, root, line=6)]
    old = _write(root / "old.jsonl", events)
    new = _write(root / "new.jsonl", events)
    rep = build_contract_report(root, trace=new, against=old, resolve_identities=False,
                                changed_api_paths=[_plan_impact("dep.compute")])
    _stamp_verdict(rep, str(new), str(old), [])
    assert rep.dynamic_only == 1 and rep.impacted_dynamic_only == 0
    assert rep.verdict is VerificationVerdict.PASS, rep.verdict_reasons


def test_a_pytest_fixture_in_a_test_file_does_not_keep_a_pass_from_passing(tmp_path):
    """QA V2a: the test runner is not traced and is not the project's dependency surface, so
    `@pytest.fixture` in a test is no BLIND symbol and no reason."""
    root = _views_project(tmp_path)
    (root / "tests").mkdir()
    (root / "tests" / "test_views.py").write_text(
        "import pytest\nfrom myapp.views import handler\n\n\n@pytest.fixture\n"
        "def rows():\n    return [1]\n\n\ndef test_handler(rows):\n    handler()\n")
    events = [_view_event("dep.compute", {"__opaque__": "dep.Result"}, root),
              _view_event("dep.Result.size", 3, root, line=6)]
    rep = _verdict_for(root, events, events)
    assert rep.blind == 0 and rep.test_runner_symbols == ["pytest.fixture"]
    assert rep.verdict is VerificationVerdict.PASS, rep.verdict_reasons


def test_after_apply_a_dynamic_only_symbol_outside_the_plan_must_be_compared_equal(tmp_path):
    from pymolt.verify.report import SymbolStatus

    root = _views_project(tmp_path)
    compute = SymbolStatus(target="dep.compute", dep="dep", status="confirmed", impacted=True,
                           matched_impact_paths=["dep.compute"])
    outside = SymbolStatus(target="dep.Result.size", dep="dep", status="dynamic-only",
                           impacted=False)
    # A custom comparator gives counts only, so nothing is compared fact by fact.
    report = ContractReport(
        root=str(root), diff={"disappeared": 0}, diff_clean=True, all_diff={"disappeared": 0},
        all_diff_clean=True, static_targets=1, confirmed=1, dynamic_only=1,
        impact_filter_active=True, impact_paths=["dep.compute"],
        symbols=[compute, outside], impacted_symbols=[compute],
    )
    trace = _write(root / "t.jsonl", [_view_event("dep.compute", 1, root)])
    _stamp_verdict(report, str(trace), str(trace), [])
    assert report.verdict is VerificationVerdict.INCONCLUSIVE
    assert "1 observed symbol(s) are missing from the static map" in report.verdict_reasons


def test_unverified_edited_sites_never_block_pass_but_review_items_do():
    inconclusive = {"verdict": "inconclusive", "counts": {"rewritten": 2}, "entries": [
        {"category": "rewritten", "comparison": "missing", "symbol": "dep.a", "unverified": True},
        {"category": "rewritten", "comparison": "uncomparable", "symbol": "dep.b",
         "unverified": True}]}
    # only unverified entries: nothing blocks, so no reason mentions pairing
    assert not any("could not be paired" in r for r in _fact_diff_reasons(inconclusive))
    with_review = {"verdict": "inconclusive", "counts": {"rewritten": 2}, "entries": [
        *inconclusive["entries"],
        {"category": "rewritten", "comparison": "changed", "symbol": "dep.c",
         "review": "the value differs on a hand-edited line: review"}]}
    reasons = _fact_diff_reasons(with_review)
    assert reasons == ["1 edited call(s) need review: dep.c"]
    paired = {"verdict": "inconclusive", "counts": {"rewritten": 1}, "entries": [
        {"category": "rewritten", "comparison": "missing", "symbol": "dep.a"}]}
    assert "could not be paired" in _fact_diff_reasons(paired)[0]


def test_unmeasured_noise_reviews_have_their_own_reason():
    from pymolt.verify.facts import UNMEASURED_NOISE

    diff = {"verdict": "inconclusive", "counts": {"changed": 2, "uncomparable": 1}, "entries": [
        {"category": "changed", "comparison": "changed", "symbol": "jinja2.Template.render",
         "review": f"{UNMEASURED_NOISE}: the value differs and its input is not fully observed"},
        {"category": "changed", "comparison": "changed", "symbol": "dep.c",
         "review": "the value differs on a hand-edited line: review"},
        {"category": "uncomparable", "comparison": "missing", "symbol": "json.dumps",
         "notes": ["possibly-dropped:new"]}]}
    reasons = _fact_diff_reasons(diff)
    assert reasons[0].startswith("1 value difference(s) may be run-to-run noise")
    assert "jinja2.Template.render" in reasons[0]
    assert "1 edited call(s) need review: dep.c" in reasons
    assert any("dropped events" in r for r in reasons)
    assert not any("lost their outcome distribution" in r for r in reasons)


def test_backend_unobservable_raises_have_their_own_reason():
    diff = {"verdict": "inconclusive", "counts": {"uncomparable": 1}, "entries": [
        {"category": "uncomparable", "comparison": "missing", "symbol": "lib.native.decode",
         "notes": ["backend-unobservable"]}]}
    reasons = _fact_diff_reasons(diff)
    assert any("could not record them" in r for r in reasons)
    assert not any("lost their outcome distribution" in r for r in reasons)


def test_capped_and_shape_mismatched_facts_have_their_own_reasons():
    diff = {"verdict": "inconclusive", "counts": {"uncomparable": 3}, "entries": [
        {"category": "uncomparable", "comparison": "missing", "symbol": "lib.codec_b",
         "notes": ["c-site-capped:new"]},
        {"category": "uncomparable", "comparison": "missing", "symbol": "msgpack.unpackb",
         "notes": ["call-shape:new"]},
        {"category": "uncomparable", "comparison": "missing", "symbol": "msgpack.unpackb",
         "notes": ["c-site-capped:new", "call-shape:new"]}]}
    reasons = _fact_diff_reasons(diff)
    assert any(r.startswith("2 call fact(s) are missing at compiled-call sites") for r in reasons)
    assert any(r.startswith("1 call fact(s) were recorded as compiled calls") for r in reasons)
    assert not any("lost their outcome distribution" in r for r in reasons)
