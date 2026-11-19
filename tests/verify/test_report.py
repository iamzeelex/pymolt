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
    # no trace: nothing ran, nothing to recall
    assert (rep.coverage, rep.recall) == (0.0, None)
    assert rep.contacts_not_run == 2 and rep.coverage_granularity is None
    assert any("BLIND" in n for n in rep.notes)


def test_report_confirmed_and_blind(tmp_path):
    root = _project(tmp_path)
    # Trace observed only flask.Flask (handler ran); with_appcontext stayed blind.
    trace = _trace(
        tmp_path,
        [
            {"q": "flask.Flask", "where": {"file": "myapp/views.py", "line": 5}},
        ],
    )
    rep = build_contract_report(root, trace=trace)
    assert rep.confirmed == 1
    assert rep.blind == 1
    # no executed record: views.py ran (a call site there), so with_appcontext's line counts
    # as run but not recorded (granularity file)
    assert (rep.coverage, rep.recall, rep.coverage_granularity) == (1.0, 0.5, "file")
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
            {"q": "flask.Flask", "where": {"file": "myapp/views.py", "line": 5}},
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
    # The focus: what the plan's paths name.
    assert rep.impacted_static_targets == 1
    assert rep.impacted_confirmed == 0
    assert rep.impacted_blind == 1
    assert rep.impacted_dynamic_only == 1
    # with_appcontext's contact ran (its file did) and was not recorded
    assert (rep.impacted_coverage, rep.impacted_recall) == (1.0, 0.0)

    # The headline is the whole surface, the one the verdict judges.
    assert rep.static_targets == rep.all_static_targets == 2
    assert rep.confirmed == rep.all_confirmed == 1
    assert rep.blind == rep.all_blind == 1
    assert rep.dynamic_only == rep.all_dynamic_only == 1
    assert (rep.contacts_recorded, rep.contacts_ran_not_recorded) == (1, 1)
    assert (rep.coverage, rep.recall) == (1.0, 0.5)
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
    # The focus narrows what is reported, never what can fail: flask.Flask changed outside
    # the impact set, and that still fails the verdict (an unpredicted change).
    assert focused.verdict is VerificationVerdict.FAIL

    impacted = build_contract_report(root, trace=new, against=old, impact_set={"flask.Flask"})
    _stamp_verdict(impacted, str(new), str(old), [])
    assert impacted.diff_clean is False and impacted.verdict is VerificationVerdict.FAIL

    same = build_contract_report(root, trace=old, against=old,
                                 impact_set={"flask.cli.with_appcontext"})
    _stamp_verdict(same, str(old), str(old), [])
    assert same.all_diff_clean is True and same.verdict is not VerificationVerdict.FAIL
    # coverage stays scoped to the impacted surface, not the whole one
    assert same.blind == same.impacted_blind

    full = build_contract_report(root, trace=new, against=old)
    _stamp_verdict(full, str(new), str(old), [])
    assert full.verdict is VerificationVerdict.FAIL


def test_empty_impact_set_is_active_and_reports_unmatched_paths(tmp_path):
    rep = build_contract_report(
        _project(tmp_path),
        changed_api_paths={"flask.no_longer_public"},
    )

    assert rep.impact_filter_active is True
    assert rep.impacted_static_targets == 0
    # the headline (and the verdict) still cover every static symbol
    assert rep.static_targets == rep.all_static_targets > 0
    assert rep.symbols  # full details are not filtered away
    assert rep.impacted_symbols == []
    assert rep.unmatched_impact_paths == ["flask.no_longer_public"]
    assert not any("narrowed" in note for note in rep.notes)


def test_single_serialized_api_impact_mapping_is_accepted(tmp_path):
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


# ── canonical identity (fact model v2) ──────────────────────────────────────────────────────
#
# A fake dependency lives OUTSIDE the project (on PYTHONPATH for the resolver subprocess), so
# the contact map sees it as third-party and the resolver can import it.

_FAKEDEP_IMPL = """\
class Client:
    def __init__(self):
        self.sent = 0

    def send(self):
        self.sent += 1


class Other:
    def __init__(self):  # second __init__ in the module: the v1 name is ambiguous
        pass


class Base:
    pass


def fetch(value):
    return value


def helper():
    return None


def other_thing():
    return None
"""


def _canonical_project(tmp_path, monkeypatch):
    site = tmp_path / "site"
    (site / "fakedep").mkdir(parents=True)
    (site / "fakedep" / "__init__.py").write_text(
        "from fakedep._impl import Client, Other, Base, fetch, helper, other_thing\n"
        "CONSTANT = 3\n",
        encoding="utf-8",
    )
    (site / "fakedep" / "_impl.py").write_text(_FAKEDEP_IMPL, encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(site))

    root = tmp_path / "proj"
    app = root / "app"
    app.mkdir(parents=True)
    (app / "__init__.py").write_text("", encoding="utf-8")
    (app / "main.py").write_text(
        "import fakedep\n"
        "from fakedep import Base, CONSTANT, Client, Other, fetch\n\n"
        "UNION = Client | Other\n\n"  # type.__or__: interpreter code, not dependency code
        "class Mine(Base):\n"
        "    pass\n\n"
        "def run():\n"
        "    Client()\n"
        "    fetch(1)\n"
        "    return CONSTANT\n\n"
        "def gone():\n"
        "    return fakedep.vanished()\n",
        encoding="utf-8",
    )
    (app / "cold.py").write_text(
        "from fakedep import helper\n\ndef cold():\n    return helper()\n",
        encoding="utf-8",
    )
    return root


# Where each symbol is called in app/main.py (the trace's call sites).
_MAIN_LINES = {"fakedep._impl.Client.__init__": 10, "fakedep._impl.fetch": 11,
               "fakedep.vanished": 15}


def _events(root, *events, file="app/main.py"):
    out = []
    for event in events:
        record = {"t": "return", "in": {"bound": {}}, "result": None, **event}
        if file is not None:
            line = _MAIN_LINES.get(event.get("sym") or event.get("q"), 9)
            record.setdefault("where", {"file": str(root / file), "line": line, "func": "run"})
        out.append(record)
    return out


def _write_trace(path, records, executable=None):
    path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    if executable:
        meta = {"processes": [{"executable": executable, "python": "3.x"}]}
        (path.parent / (path.name + ".meta.json")).write_text(json.dumps(meta), encoding="utf-8")
    return path


def test_v2_sym_confirms_constructor_reexport_and_unresolvable_exact(tmp_path, monkeypatch):
    root = _canonical_project(tmp_path, monkeypatch)
    trace = _write_trace(
        tmp_path / "v2.jsonl",
        _events(
            root,
            {"q": "fakedep._impl.__init__", "sym": "fakedep._impl.Client.__init__"},
            {"q": "fakedep._impl.fetch", "sym": "fakedep._impl.fetch"},
            {"q": "fakedep.vanished"},  # unresolvable static path: exact-string fallback
            {"q": "fakedep._impl.other_thing", "sym": "fakedep._impl.other_thing"},
        ),
        executable=sys.executable,
    )

    rep = build_contract_report(root, trace=trace)
    by = {s.target: s for s in rep.symbols}

    assert rep.trace_schema == 2
    assert rep.identity_source == "trace-metadata"
    assert by["fakedep.Client"].status == "confirmed"  # class: sym under Client.
    assert by["fakedep.Client"].sym == "fakedep._impl.Client"
    assert by["fakedep.Client"].matched_by == "canonical"
    assert by["fakedep.fetch"].status == "confirmed"  # re-export resolved to its definer
    assert by["fakedep.fetch"].observed_as == ["fakedep._impl.fetch"]
    vanished = by["fakedep.vanished"]
    assert (vanished.status, vanished.matched_by) == ("confirmed", "exact")
    assert "AttributeError" in vanished.identity_error
    assert by["fakedep._impl.other_thing"].status == "dynamic-only"
    assert by["fakedep.helper"].status == "blind"

    assert by["fakedep.Base"].contact_class == "existence"
    assert by["fakedep.CONSTANT"].contact_class == "existence"
    assert by["fakedep.Client.__or__"].contact_class == "interpreter"
    assert rep.executed_files == ["app/main.py"]
    assert rep.identity_unresolved == 1
    assert rep.identity_resolved == 6  # CONSTANT is a value: imported fine, no code identity
    assert any("1 unresolvable (fakedep.vanished)" in n for n in rep.notes)

    tiers = {t.name: (t.confirmed, t.static) for t in rep.identity_tiers}
    assert tiers == {
        "exact": (1, 8),  # Base CONSTANT Client fetch helper vanished Client.__or__ Other.__ror__
        "canonical": (3, 8),
    }
    assert (rep.confirmed, rep.blind, rep.dynamic_only) == (3, 5, 1)
    # Contacts: Client, fetch, vanished recorded at their lines; helper in cold.py, which
    # never ran (no call site there; no executed record: the file rule); Base and CONSTANT
    # are existence contacts of symbols never observed and the two ``|`` protocols resolve to
    # interpreter code: unobservable.
    status = {c.target: c.status for c in rep.contacts}
    assert status == {
        "fakedep.Client": "recorded", "fakedep.fetch": "recorded",
        "fakedep.vanished": "recorded", "fakedep.helper": "not-run",
        "fakedep.Base": "unobservable", "fakedep.CONSTANT": "unobservable",
        "fakedep.Client.__or__": "unobservable", "fakedep.Other.__ror__": "unobservable",
    }
    assert rep.unobservable_by_reason == {"existence": 2, "interpreter": 2}
    assert (rep.coverage, rep.recall, rep.coverage_granularity) == (0.75, 1.0, "file")
    assert by["fakedep.helper"].blind_reason == "not-run"
    assert by["fakedep.Base"].blind_reason == "unobservable"
    assert (rep.blind_not_run, rep.blind_ran_not_recorded, rep.blind_unobservable) == (1, 0, 4)


def test_v1_trace_matches_only_unambiguous_legacy_names(tmp_path, monkeypatch):
    root = _canonical_project(tmp_path, monkeypatch)
    ambiguous = _write_trace(
        tmp_path / "v1.jsonl",
        _events(root, {"q": "fakedep._impl.__init__"}, {"q": "fakedep._impl.fetch"}),
    )

    rep = build_contract_report(root, trace=ambiguous)
    by = {s.target: s for s in rep.symbols}
    assert rep.trace_schema == 1
    assert rep.identity_source == "host"
    assert any("trace schema v1" in n for n in rep.notes)
    assert any("pymolt's own interpreter" in n for n in rep.notes)
    assert by["fakedep.fetch"].status == "confirmed"  # unique code name in fakedep._impl
    # Client.__init__ and Other.__init__ both read "fakedep._impl.__init__" in v1: no claim.
    assert by["fakedep.Client"].status == "blind"
    assert by["fakedep._impl.__init__"].status == "dynamic-only"

    method = _write_trace(tmp_path / "v1b.jsonl", _events(root, {"q": "fakedep._impl.send"}))
    rep = build_contract_report(root, trace=method)
    assert {s.target: s.status for s in rep.symbols}["fakedep.Client"] == "confirmed"


def test_executed_scope_relative_suffix_and_missing_sites(tmp_path, monkeypatch):
    root = _canonical_project(tmp_path, monkeypatch)
    fetch = {"q": "fakedep._impl.fetch", "sym": "fakedep._impl.fetch"}

    relative = _write_trace(
        tmp_path / "rel.jsonl",
        [{**fetch, "where": {"file": "app/cold.py", "line": 4}},
         {**fetch, "where": {"file": "<string>", "line": 1}}],
    )
    rep = build_contract_report(root, trace=relative)
    assert rep.executed_files == ["app/cold.py"]
    helper = [c for c in rep.contacts if c.target == "fakedep.helper"]
    assert [(c.status, c.granularity) for c in helper] == [("ran-not-recorded", "file")]

    moved = _write_trace(
        tmp_path / "moved.jsonl",
        [{**fetch, "where": {"file": "/srv/container-app/app/main.py", "line": 9}}],
    )
    rep = build_contract_report(root, trace=moved)
    assert rep.executed_files == ["app/main.py"]
    assert any("matched by path suffix" in n for n in rep.notes)

    no_sites = _write_trace(tmp_path / "nosites.jsonl", [fetch])
    rep = build_contract_report(root, trace=no_sites)
    assert rep.executed_scope is False
    # no call sites: recorded by symbol, "ran" unknown (granularity symbol)
    by = {c.target: c for c in rep.contacts}
    assert (by["fakedep.fetch"].status, by["fakedep.fetch"].matched_by) == ("recorded", "symbol")
    assert by["fakedep.helper"].status == "not-run"
    assert rep.coverage_granularity == "symbol" and rep.recall == 1.0
    assert any("records no call sites" in n for n in rep.notes)


def test_impact_paths_match_resolved_symbols_on_both_sides(tmp_path, monkeypatch):
    root = _canonical_project(tmp_path, monkeypatch)
    trace = _write_trace(
        tmp_path / "v2.jsonl",
        _events(
            root,
            {"q": "fakedep._impl.fetch", "sym": "fakedep._impl.fetch"},
            {"q": "fakedep._impl.other_thing", "sym": "fakedep._impl.other_thing"},
        ),
    )

    rep = build_contract_report(
        root,
        trace=trace,
        # canonical spelling for a public static path; public spelling for a runtime sym
        changed_api_paths=["fakedep._impl.fetch", "fakedep.other_thing"],
    )

    impacted = {s.target: s.status for s in rep.impacted_symbols}
    assert impacted == {"fakedep.fetch": "confirmed", "fakedep._impl.other_thing": "dynamic-only"}
    assert rep.unmatched_impact_paths == []
    assert [c.target for c in rep.impacted_contacts] == ["fakedep.fetch"]
    assert (rep.impacted_static_targets, rep.impacted_confirmed,
            rep.impacted_dynamic_only) == (1, 1, 1)
    assert (rep.static_targets, rep.confirmed, rep.dynamic_only) == (
        rep.all_static_targets, rep.all_confirmed, rep.all_dynamic_only)
    assert rep.all_static_targets == 8
    # the plan's focus: fetch's contact was recorded at its line
    assert (rep.impacted_coverage, rep.impacted_recall) == (1.0, 1.0)
    # the whole surface: Client and vanished ran in main.py (file rule) but were not recorded
    assert (rep.contacts_recorded, rep.contacts_ran_not_recorded, rep.contacts_not_run) == (
        1, 2, 1)


def test_identity_resolution_can_be_disabled(tmp_path, monkeypatch):
    root = _canonical_project(tmp_path, monkeypatch)
    trace = _write_trace(
        tmp_path / "v2.jsonl",
        _events(root, {"q": "fakedep._impl.fetch", "sym": "fakedep._impl.fetch"}),
    )
    rep = build_contract_report(root, trace=trace, resolve_identities=False)
    assert rep.identity_interpreter is None
    assert [t.share for t in rep.identity_tiers] == [0.0, 0.0]
    # No identity cache; the contact-map cache (cache/contacts) is allowed.
    assert not list((root / ".pymolt" / "cache").glob("identity-*.json"))


def test_unobservable_static_paths_are_classified_not_blind():
    from pymolt.verify.report import _unresolvable_class

    assert _unresolvable_class(
        "AttributeError: 'function' object has no attribute 'strip'", "typer"
    ) == "returned-value"
    assert _unresolvable_class(
        "ModuleNotFoundError: No module named 'legacylib'", "legacylib"
    ) == "absent-dependency"
    # A missing submodule of an installed package is a real resolution failure.
    assert _unresolvable_class(
        "ModuleNotFoundError: No module named 'legacylib.core'", "legacylib"
    ) is None
    assert _unresolvable_class(
        "AttributeError: type object 'Requirement' has no attribute 'name'", "packaging"
    ) is None


def _stub_resolution(monkeypatch, source, results):
    from pymolt.verify import identity_resolver
    from pymolt.verify.identity_resolver import IdentityResolution, ResolvedIdentity

    def fake(paths, *args, **kwargs):
        return IdentityResolution(
            interpreter="/capture/python", interpreter_source=source,
            results={p: ResolvedIdentity(**results.get(p, {"error": "not resolved"}))
                     for p in paths},
        )

    monkeypatch.setattr(identity_resolver, "resolve_identities", fake)


def test_absent_dependency_leaves_coverage_only_with_capture_interpreter_evidence(
    tmp_path, monkeypatch
):
    root = _project(tmp_path)
    absent = {"error": "ModuleNotFoundError: No module named 'flask'"}
    trace = _trace(tmp_path, [{"q": "other.thing", "where": {"file": "myapp/views.py",
                                                             "line": 4}}])
    results = {"flask.Flask": absent, "flask.cli.with_appcontext": absent}

    # Resolved in the capture's interpreter and nothing from flask in the trace:
    # flask really was not installed there -> unobservable, not BLIND.
    _stub_resolution(monkeypatch, "trace-metadata", results)
    rep = build_contract_report(root, trace=trace)
    classes = {s.target: s.contact_class for s in rep.symbols if s.target.startswith("flask")}
    assert set(classes.values()) == {"absent-dependency"}
    assert any("not installed" in note for note in rep.notes)

    # Resolved under pymolt's own interpreter: "absent" proves nothing -> stays behavioural.
    _stub_resolution(monkeypatch, "host", results)
    rep = build_contract_report(root, trace=trace)
    classes = {s.target: s.contact_class for s in rep.symbols if s.target.startswith("flask")}
    assert set(classes.values()) == {"behavioural"}


def test_test_runner_contacts_are_left_out_of_coverage_unless_traced(tmp_path):
    """Captures never trace the test runner, so its contacts can't be confirmed: they are
    unobservable, left out of coverage and recall. Traced (a target named it) they count."""
    root = _project(tmp_path)
    (root / "tests").mkdir()
    (root / "tests" / "test_views.py").write_text(
        "import pytest\nfrom myapp import views\n\n"
        "def test_handler():\n"
        "    with pytest.raises(ZeroDivisionError):\n"
        "        1 / 0\n"
        "    assert views.handler()\n",
        encoding="utf-8",
    )
    trace = _trace(tmp_path, [
        {"q": "flask.Flask", "where": {"file": "myapp/views.py", "line": 5}}])
    rep = build_contract_report(root, trace=trace)
    runner = [s for s in rep.symbols if s.dep == "pytest"]
    assert runner and {s.contact_class for s in runner} == {"test-runner"}
    assert any("test runner" in note for note in rep.notes)
    # flask.Flask recorded, with_appcontext ran (its file did) unrecorded; pytest.raises is in
    # neither denominator
    assert rep.unobservable_by_reason == {"test-runner": 1}
    assert (rep.coverage, rep.recall) == (1.0, 0.5)

    traced = _trace(tmp_path, [
        {"q": "flask.Flask", "where": {"file": "myapp/views.py", "line": 5}},
        {"q": "_pytest.python_api.raises", "where": {"file": "tests/test_views.py", "line": 5}}])
    rep = build_contract_report(root, trace=traced)
    assert {s.contact_class for s in rep.symbols if s.dep == "pytest"} == {"behavioural"}


def test_the_report_and_the_runtime_agree_on_the_test_runner_packages():
    from pymolt.verify import _backend, report

    assert report.TEST_RUNNER_PACKAGES == _backend.TEST_RUNNER_PACKAGES
