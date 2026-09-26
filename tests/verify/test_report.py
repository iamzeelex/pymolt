import json
import sys

from pymolt.verify.models import VerificationVerdict
from pymolt.verify.report import build_contract_report
from pymolt.verify.service import _stamp_verdict


def _project(tmp_path):
    app = tmp_path / "myapp"
    app.mkdir()
    (app / "__init__.py").write_text("", encoding="utf-8")
    (app / "views.py").write_text(
        "import flask\n"
        "from flask.cli import with_appcontext\n\n"
        "def handler():\n"
        "    return flask.Flask(__name__)\n\n"
        "def admin():\n"  # blind path: only touches with_appcontext
        "    return with_appcontext(admin)\n",
        encoding="utf-8",
    )
    return tmp_path


def _trace(tmp_path, records):
    p = tmp_path / "trace.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    return p


def test_report_static_only_all_blind(tmp_path):
    rep = build_contract_report(_project(tmp_path))
    assert rep.static_targets == 2
    assert rep.blind == 2
    assert rep.confirmed == 0
    assert rep.trust == 0.0
    assert any("BLIND" in n for n in rep.notes)


def test_report_confirmed_and_blind(tmp_path):
    root = _project(tmp_path)
    # Trace observed only flask.Flask (handler ran); with_appcontext stayed blind.
    trace = _trace(
        tmp_path,
        [
            {"q": "flask.Flask", "where": {"file": "myapp/views.py", "line": 4}},
        ],
    )
    rep = build_contract_report(root, trace=trace)
    assert rep.confirmed == 1
    assert rep.blind == 1
    assert rep.trust == 0.5
    by_target = {s.target: s.status for s in rep.symbols}
    assert by_target["flask.Flask"] == "confirmed"
    assert by_target["flask.cli.with_appcontext"] == "blind"


def test_report_dynamic_only(tmp_path):
    root = _project(tmp_path)
    # Trace observed a symbol the static map never found (e.g. via getattr).
    trace = _trace(
        tmp_path,
        [
            {"q": "flask.Flask", "where": {"file": "myapp/views.py", "line": 4}},
            {"q": "flask.json.dumps", "where": {"file": "myapp/views.py", "line": 9}},
        ],
    )
    rep = build_contract_report(root, trace=trace)
    statuses = {s.target: s.status for s in rep.symbols}
    assert statuses["flask.json.dumps"] == "dynamic-only"
    assert rep.dynamic_only == 1


def test_report_attaches_version_diff(tmp_path):
    root = _project(tmp_path)
    old = _trace(
        tmp_path,
        [
            {
                "q": "flask.Flask",
                "in": {"bound": {}},
                "result": "v1",
                "t": "return",
                "where": {"file": "myapp/views.py", "line": 4},
            }
        ],
    )
    new = tmp_path / "new.jsonl"
    new.write_text(
        json.dumps(
            {
                "q": "flask.Flask",
                "in": {"bound": {}},
                "result": "v2",
                "t": "return",
                "where": {"file": "myapp/views.py", "line": 4},
            }
        ),
        encoding="utf-8",
    )
    rep = build_contract_report(root, trace=new, against=old)
    assert rep.diff is not None
    assert rep.diff_clean is False  # result changed v1 -> v2


def test_report_sandbox_probe(tmp_path):
    # Fixture uses pyyaml (installed) so the probe can really re-invoke yaml.safe_load.
    app = tmp_path / "myapp"
    app.mkdir()
    (app / "__init__.py").write_text("", encoding="utf-8")
    (app / "m.py").write_text(
        "import yaml\n\ndef load():\n    return yaml.safe_load('a: 1')\n",
        encoding="utf-8",
    )
    trace = tmp_path / "trace.jsonl"
    trace.write_text(
        json.dumps(
            {
                "q": "yaml.safe_load",
                "in": {"bound": {"stream": "a: 1"}},
                "result": {"__dict__": [["a", 1]]},
                "t": "return",
            }
        ),
        encoding="utf-8",
    )

    rep = build_contract_report(tmp_path, trace=trace, probe_python=sys.executable)
    by_target = {s.target: s for s in rep.symbols}
    sym = by_target["yaml.safe_load"]
    assert sym.status == "confirmed"
    assert sym.probed is True
    assert sym.probe_status == "stable"  # re-invoking yaml.safe_load matches the captured result
    assert rep.probed == 1
    assert rep.probe_changed == 0


def test_impact_set_focuses_metrics_and_preserves_full_surface(tmp_path):
    root = _project(tmp_path)
    trace = _trace(
        tmp_path,
        [
            {"q": "flask.Flask", "where": {"file": "myapp/views.py", "line": 4}},
            {
                "q": "flask.cli.pass_context",
                "where": {"file": "myapp/views.py", "line": 8},
            },
        ],
    )

    rep = build_contract_report(
        root,
        trace=trace,
        changed_api_paths=[
            {
                "path": "flask.cli.with_appcontext",
                "replacement_path": "flask.cli.pass_context",
            }
        ],
    )

    assert rep.impact_filter_active is True
    assert rep.impact_paths == [
        "flask.cli.pass_context",
        "flask.cli.with_appcontext",
    ]
    assert rep.static_targets == rep.impacted_static_targets == 1
    assert rep.confirmed == rep.impacted_confirmed == 0
    assert rep.blind == rep.impacted_blind == 1
    assert rep.dynamic_only == rep.impacted_dynamic_only == 1
    assert rep.trust == rep.impacted_trust == 0.0

    # Full totals and every symbol remain available for audit/UI display.
    assert rep.all_static_targets == 2
    assert rep.all_confirmed == 1
    assert rep.all_blind == 1
    assert rep.all_dynamic_only == 1
    assert rep.all_trust == 0.5
    assert len(rep.symbols) == 3
    assert {symbol.target for symbol in rep.impacted_symbols} == {
        "flask.cli.pass_context",
        "flask.cli.with_appcontext",
    }
    old = next(
        symbol for symbol in rep.impacted_symbols if symbol.target == "flask.cli.with_appcontext"
    )
    assert old.contact_kinds == ["call"]
    assert old.matched_impact_paths == ["flask.cli.with_appcontext"]
    assert rep.impact_by_kind == {"call": 1}


def test_impact_set_filters_diff_and_existing_verdict_fold(tmp_path):
    root = _project(tmp_path)
    old = tmp_path / "old.jsonl"
    new = tmp_path / "new.jsonl"
    old.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "q": "flask.Flask",
                        "in": {"bound": {}},
                        "result": "old",
                        "t": "return",
                    }
                ),
                json.dumps(
                    {
                        "q": "flask.cli.with_appcontext",
                        "in": {"bound": {}},
                        "result": "stable",
                        "t": "return",
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )
    new.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "q": "flask.Flask",
                        "in": {"bound": {}},
                        "result": "new",
                        "t": "return",
                    }
                ),
                json.dumps(
                    {
                        "q": "flask.cli.with_appcontext",
                        "in": {"bound": {}},
                        "result": "stable",
                        "t": "return",
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )

    focused = build_contract_report(
        root,
        trace=new,
        against=old,
        impact_set={"flask.cli.with_appcontext"},
    )
    _stamp_verdict(focused, str(new), str(old), [])

    assert focused.diff_clean is True
    assert focused.diff == {
        "disappeared": 0,
        "result_changed": 0,
        "raise_changed": 0,
        "appeared": 0,
        "skipped_opaque": 0,
    }
    assert focused.all_diff_clean is False
    assert focused.all_diff is not None
    assert focused.all_diff["result_changed"] == 1
    assert focused.impact_diff_details is not None
    assert focused.impact_diff_details["result_changed"] == []
    assert focused.verdict is VerificationVerdict.PASS

    full = build_contract_report(root, trace=new, against=old)
    _stamp_verdict(full, str(new), str(old), [])
    assert full.verdict is VerificationVerdict.FAIL


def test_empty_impact_set_is_active_and_reports_unmatched_paths(tmp_path):
    rep = build_contract_report(
        _project(tmp_path),
        changed_api_paths={"flask.no_longer_public"},
    )

    assert rep.impact_filter_active is True
    assert rep.static_targets == 0
    assert rep.symbols  # full details are not filtered away
    assert rep.impacted_symbols == []
    assert rep.unmatched_impact_paths == ["flask.no_longer_public"]


def test_single_serialized_axiom_impact_mapping_is_accepted(tmp_path):
    rep = build_contract_report(
        _project(tmp_path),
        impact_set={
            "path": "flask.Flask",
            "replacement_path": "flask.Application",
            "kind": "object-removed",
        },
    )

    assert rep.impact_paths == ["flask.Application", "flask.Flask"]
    assert [symbol.target for symbol in rep.impacted_symbols] == ["flask.Flask"]
