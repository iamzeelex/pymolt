import json
import sys

from pymolt.verify.report import build_contract_report


def _project(tmp_path):
    app = tmp_path / "myapp"
    app.mkdir()
    (app / "__init__.py").write_text("", encoding="utf-8")
    (app / "views.py").write_text(
        "import flask\n"
        "from flask.cli import with_appcontext\n\n"
        "def handler():\n"
        "    return flask.Flask(__name__)\n\n"
        "def admin():\n"                    # blind path: only touches with_appcontext
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
    trace = _trace(tmp_path, [
        {"q": "flask.Flask", "where": {"file": "myapp/views.py", "line": 4}},
    ])
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
    trace = _trace(tmp_path, [
        {"q": "flask.Flask", "where": {"file": "myapp/views.py", "line": 4}},
        {"q": "flask.json.dumps", "where": {"file": "myapp/views.py", "line": 9}},
    ])
    rep = build_contract_report(root, trace=trace)
    statuses = {s.target: s.status for s in rep.symbols}
    assert statuses["flask.json.dumps"] == "dynamic-only"
    assert rep.dynamic_only == 1


def test_report_attaches_version_diff(tmp_path):
    root = _project(tmp_path)
    old = _trace(tmp_path, [{"q": "flask.Flask", "in": {"bound": {}}, "result": "v1", "t": "return",
                             "where": {"file": "myapp/views.py", "line": 4}}])
    new = tmp_path / "new.jsonl"
    new.write_text(json.dumps({"q": "flask.Flask", "in": {"bound": {}}, "result": "v2", "t": "return",
                               "where": {"file": "myapp/views.py", "line": 4}}), encoding="utf-8")
    rep = build_contract_report(root, trace=new, against=old)
    assert rep.diff is not None
    assert rep.diff_clean is False  # result changed v1 -> v2


def test_report_sandbox_probe(tmp_path):
    # Fixture uses pyyaml (installed) so the probe can really re-invoke yaml.safe_load.
    app = tmp_path / "myapp"
    app.mkdir()
    (app / "__init__.py").write_text("", encoding="utf-8")
    (app / "m.py").write_text(
        "import yaml\n\ndef load():\n    return yaml.safe_load('a: 1')\n", encoding="utf-8",
    )
    trace = tmp_path / "trace.jsonl"
    trace.write_text(json.dumps({
        "q": "yaml.safe_load", "in": {"bound": {"stream": "a: 1"}},
        "result": {"__dict__": [["a", 1]]}, "t": "return",
    }), encoding="utf-8")

    rep = build_contract_report(tmp_path, trace=trace, probe_python=sys.executable)
    by_target = {s.target: s for s in rep.symbols}
    sym = by_target["yaml.safe_load"]
    assert sym.status == "confirmed"
    assert sym.probed is True
    assert sym.probe_status == "stable"   # re-invoking yaml.safe_load matches the captured result
    assert rep.probed == 1
    assert rep.probe_changed == 0
