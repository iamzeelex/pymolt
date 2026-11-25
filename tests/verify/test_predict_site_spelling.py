"""A site is rated by what it writes, not by where the old version defined the callee.

The legacy QA (alphalens, zipline: pandas 0.2x -> 3.1, SQLAlchemy 1.3 -> 2.1) found
``certain: removed`` on calls that still run: the baseline records ``df.groupby(...)`` as
``NDFrame.groupby`` and ``pd.Timedelta(...)`` as ``pandas._libs.tslib.Timedelta.__new__``,
names the new versions no longer have. ``certain`` must mean the call as the site writes it
breaks; real removals (``DataFrame.append``, ``pd.Int64Index``) stay certain."""

from __future__ import annotations

from pymolt.verify.facts import fold_facts
from pymolt.verify.predict import RiskTier, predict_risks
from pymolt.workorder.manual import default_claim

ROOT = "/proj"
FILE = "app/perf.py"
FRAME = "pandas.core.frame.DataFrame"
SERIES = "pandas.core.series.Series"
NDFRAME_GROUPBY = "pandas.core.generic.NDFrame.groupby"


def _site(sym, text, *, line=10, recv=None, key="self", marker="__structure__", bound=None,
          defaulted=()):
    path = f"{ROOT}/{FILE}"
    receiver = {key: {marker: recv}} if recv else {}
    ev = {"v": 2, "t": "return", "q": sym, "sym": sym, "result": 1,
          "in": {"bound": {**receiver, **(bound or {})}, "defaulted": list(defaulted)},
          "where": {"file": path, "line": line, "func": "f"}}
    return fold_facts([ev], root=ROOT, site_text={f"{path}:{line}": text})


def _delta(*facts, package="pandas"):
    return {"schema": "pymolt.delta/1", "package": package, "from": "0.25.3", "to": "3.1.1",
            "quality": "complete", "facts": list(facts)}


def _writes(target, line=10):
    return {"caller": "app.perf.f", "target": target, "dep": target.split(".")[0],
            "file": FILE, "line": line, "spans": [[line, line, line]]}


def _predict(baseline, *facts, writes=(), package="pandas"):
    return predict_risks(baseline, [_delta(*facts, package=package)],
                         static_contacts=[_writes(t) for t in writes], root=ROOT)


GONE_FROM_NDFRAME = {"symbol": NDFRAME_GROUPBY, "kind": "MOVED", "aliases": [],
                     "successor": f"{FRAME}.groupby",
                     "vanished_at": {NDFRAME_GROUPBY: NDFRAME_GROUPBY}}
STILL_ON_FRAME = {"symbol": f"{FRAME}.groupby", "kind": "UNCHANGED",
                  "aliases": ["pandas.DataFrame.groupby", f"{SERIES}.groupby",
                              "pandas.Series.groupby"]}


# ── an internal move is no break ─────────────────────────────────────────────────────────


def test_a_method_the_defining_class_lost_is_no_risk_while_the_receivers_class_has_it():
    site = _site(NDFRAME_GROUPBY, "ic = factor_data.groupby(grouper).apply(src_ic)", recv=FRAME)
    report = _predict(site, GONE_FROM_NDFRAME, STILL_ON_FRAME)
    assert report.risks == []


def test_a_param_change_on_another_spelling_of_the_old_object_does_not_reach_the_site():
    site = _site(NDFRAME_GROUPBY, "df.groupby(by, squeeze=True)", recv=FRAME,
                 bound={"by": "k", "squeeze": True})
    series_only = {"symbol": NDFRAME_GROUPBY, "aliases": [f"{SERIES}.groupby"],
                   "kind": "SIGNATURE", "param": "squeeze", "old": "squeeze", "new": None}
    frame = {"symbol": f"{FRAME}.groupby", "aliases": ["pandas.DataFrame.groupby"],
             "kind": "UNCHANGED"}
    assert _predict(site, GONE_FROM_NDFRAME, series_only, frame).risks == []
    both = {**series_only, "aliases": [f"{FRAME}.groupby", f"{SERIES}.groupby"]}
    (risk,) = _predict(site, GONE_FROM_NDFRAME, both, frame).risks
    assert risk.tier is RiskTier.MEDIUM and risk.fact["kind"] == "SIGNATURE"


def test_a_class_spelled_publicly_on_the_statement_decides_its_constructor():
    """``pd.Timedelta(minutes=1)``: the old version recorded the constructor under the module
    the class was defined in, which pandas 3 moved; ``pandas.Timedelta`` is still there."""
    sym = "pandas._libs.tslib.Timedelta.__new__"
    site = _site(sym, "MINUTE = pd.Timedelta(minutes=1)", recv="pandas._libs.tslib.Timedelta",
                 key="cls", marker="__type__")
    gone = {"symbol": sym, "kind": "REMOVED", "aliases": [],
            "vanished_at": {sym: "pandas._libs.tslib.Timedelta"}}
    kept = {"symbol": "pandas.Timedelta", "kind": "UNCHANGED",
            "aliases": ["pandas.Timedelta.__new__"]}
    assert _predict(site, gone, kept, writes=["pandas.Timedelta"]).risks == []


def test_a_name_made_inside_a_function_is_not_matched_by_that_functions_removal():
    sym = "pandas.io.parsers._make_parser_function.<locals>.parser_f"
    site = _site(sym, "df = pd.read_csv(path, parse_dates=['date'])")
    factory = {"symbol": "pandas.io.parsers._make_parser_function", "kind": "REMOVED",
               "successor": "skipfooter"}
    assert _predict(site, factory, writes=["pandas.read_csv"]).risks == []


def test_a_deprecated_module_path_the_site_does_not_write_is_no_risk():
    """numpy 2 warns on ``numpy.core.*`` access; the site writes ``np.sum``."""
    sym = "numpy.core.fromnumeric.sum"
    site = _site(sym, "total = np.sum(values)")
    shim = {"symbol": sym, "kind": "DEPRECATED", "successor": "numpy._core.fromnumeric.sum",
            "evidence": ["deprecated-in-new:access-warning: DeprecationWarning: numpy.core is "
                         "deprecated and has been renamed to numpy._core"]}
    kept = {"symbol": "numpy.sum", "kind": "UNCHANGED", "aliases": []}
    assert _predict(site, shim, kept, writes=["numpy.sum"], package="numpy").risks == []
    # The same deprecation on the very name the statement writes is still a risk.
    (risk,) = _predict(site, {**shim, "symbol": "numpy.sum"}, writes=["numpy.sum"],
                       package="numpy").risks
    assert risk.tier is RiskTier.LOW and not risk.unverified


# ── real removals stay certain ───────────────────────────────────────────────────────────


def test_a_method_gone_from_the_receivers_class_is_certain():
    sym = f"{FRAME}.append"
    site = _site(sym, "df = df.append(other, ignore_index=True)", recv=FRAME)
    gone = {"symbol": sym, "kind": "REMOVED", "aliases": ["pandas.DataFrame.append"],
            "successor": "pandas.concat",
            "vanished_at": {sym: sym, "pandas.DataFrame.append": "pandas.DataFrame.append"}}
    (risk,) = _predict(site, gone).risks
    assert risk.tier is RiskTier.CERTAIN and not risk.unverified
    assert risk.spelling == sym


def test_a_removed_class_the_statement_writes_is_certain_for_its_constructor_call():
    """``Int64Index([1, 2])``: recorded as ``NumericIndex.__new__`` on an ``Int64Index``."""
    sym = "pandas.core.indexes.numeric.NumericIndex.__new__"
    site = _site(sym, "Int64Index([1, 2], name='group'),",
                 recv="pandas.core.indexes.numeric.Int64Index", key="cls", marker="__type__")
    public = {"symbol": "pandas.Int64Index", "kind": "REMOVED", "aliases": [],
              "vanished_at": {"pandas.Int64Index": "pandas.Int64Index"}}
    internal = {"symbol": sym, "kind": "REMOVED", "aliases": [],
                "vanished_at": {sym: "pandas.core.indexes.numeric"}}
    report = _predict(site, internal, public, writes=["pandas.Int64Index"])
    (risk,) = report.risks
    assert risk.tier is RiskTier.CERTAIN and risk.basis == "trace"
    assert risk.spelling == "pandas.Int64Index" and risk.fact["symbol"] == "pandas.Int64Index"
    assert "site: this statement writes pandas.Int64Index" in risk.evidence


# ── unknown is said, not guessed ─────────────────────────────────────────────────────────


def test_an_internal_name_gone_with_the_sites_spelling_unprobed_is_low_and_unverified():
    """A delta made before receiver spellings were probed knows only the defining name."""
    site = _site(NDFRAME_GROUPBY, "ic = factor_data.groupby(grouper).apply(src_ic)", recv=FRAME)
    removed = {"symbol": NDFRAME_GROUPBY, "kind": "REMOVED", "aliases": []}
    (risk,) = _predict(site, removed).risks
    assert risk.tier is RiskTier.LOW and risk.unverified
    assert risk.spelling == f"{FRAME}.groupby"
    assert "was not probed" in risk.reason and NDFRAME_GROUPBY in risk.reason
    claim, expect, reason = default_claim("api-change", risk)
    assert (claim, expect) == ("preserve", None) and "not loosened" in reason


def test_a_receiver_spelling_gone_with_its_class_is_unverified():
    """``.fetchall()`` on a SQLAlchemy 1.3 ``ResultProxy``: 2.x has no such class, and the
    receiver there is another class; whether it has ``fetchall`` is not what the delta saw."""
    sym = "sqlalchemy.engine.result.ResultProxy.fetchall"
    site = _site(sym, "rows = query.execute().fetchall()",
                 recv="sqlalchemy.engine.result.ResultProxy", marker="__object__")
    gone = {"symbol": sym, "kind": "REMOVED", "aliases": [],
            "vanished_at": {sym: "sqlalchemy.engine.result.ResultProxy"}}
    (risk,) = _predict(site, gone, package="sqlalchemy").risks
    assert risk.tier is RiskTier.LOW and risk.unverified
    assert "together with its class" in risk.reason
    assert risk.spelling is None        # no name to check settles it: run the site


def test_a_protocol_call_the_statement_does_not_name_is_never_certain():
    """``np.sqrt(df)``: numpy calls ``__array_wrap__``; the statement does not."""
    sym = "pandas.core.generic.NDFrame.__array_wrap__"
    site = _site(sym, "/ np.sqrt(group_stats.T.xs('count', level=1).T)", recv=FRAME)
    gone = {"symbol": sym, "kind": "REMOVED",
            "aliases": [f"{FRAME}.__array_wrap__", "pandas.DataFrame.__array_wrap__"],
            "vanished_at": {sym: sym, f"{FRAME}.__array_wrap__": f"{FRAME}.__array_wrap__",
                            "pandas.DataFrame.__array_wrap__":
                                "pandas.DataFrame.__array_wrap__"}}
    (risk,) = _predict(site, gone, writes=["numpy.sqrt"]).risks
    assert risk.tier is RiskTier.LOW and risk.unverified
    assert "does not call `__array_wrap__` by name" in risk.reason


def test_a_function_the_statement_does_not_name_is_not_the_sites_call():
    """``mv.any()``: numpy's C method calls ``numpy.core._methods._any``; the statement
    neither names nor passes anything to it."""
    sym = "numpy.core._methods._any"
    site = _site(sym, "if mv.any():")
    gone = {"symbol": sym, "kind": "REMOVED", "aliases": [],
            "vanished_at": {sym: "numpy.core"}}
    assert _predict(site, gone, package="numpy").risks == []


# ── Wave G: what a statement writes, wherever the call lands ──────────────────────────────


def test_a_moved_name_the_statement_writes_is_certain_and_keeps_the_objects_changes():
    """jinja2 3.1: ``jinja2.Markup(...) % args`` fails (``jinja2.Markup`` is gone, the same
    class lives in markupsafe), and ``__mod__``'s own signature change still applies."""
    sym = "markupsafe.Markup.__mod__"
    site = _site(sym, "return jinja2.Markup('<b>%s</b>') % (text,)", bound={"arg": "x"})
    moved = {"symbol": "jinja2.Markup.__mod__", "kind": "MOVED", "aliases": [],
             "successor": "markupsafe.Markup.__mod__",
             "vanished_at": {"jinja2.Markup.__mod__": "jinja2.Markup"}}
    signature = {"symbol": sym, "kind": "SIGNATURE", "param": "arg", "old": "arg",
                 "new": "value", "aliases": []}
    (risk,) = _predict(site, moved, signature, writes=["jinja2.Markup", "jinja2.Markup.__mod__"],
                       package="jinja2").risks
    assert risk.tier is RiskTier.CERTAIN and risk.fact["kind"] == "MOVED"
    assert "the spelling jinja2.Markup.__mod__ no longer exists" in risk.reason
    assert signature in risk.facts


def test_a_generator_consumed_on_another_line_is_judged_by_what_its_function_writes():
    """networkx: ``parts = nx.connected_component_subgraphs(G)``; its body runs, and is
    recorded, at ``max(parts, ...)`` two lines down."""
    sym = "networkx.algorithms.components.connected.connected_component_subgraphs"
    site = _site(sym, "return max(parts, key=lambda g: g.number_of_nodes())", line=12)
    gone = {"symbol": sym, "kind": "REMOVED",
            "aliases": ["networkx.connected_component_subgraphs"],
            "vanished_at": {sym: sym, "networkx.connected_component_subgraphs":
                            "networkx.connected_component_subgraphs"}}
    elsewhere = _writes("networkx.connected_component_subgraphs", line=10)
    report = predict_risks(site, [_delta(gone, package="networkx")],
                           static_contacts=[elsewhere], root=ROOT)
    (risk,) = report.risks
    assert risk.tier is RiskTier.CERTAIN
    assert risk.spelling == "networkx.connected_component_subgraphs"


def test_a_method_compiled_code_calls_on_a_receiver_the_statement_passes_is_unverified():
    """SQLAlchemy 2: ``dict(row)`` calls ``row.keys()`` from C; ``RowProxy`` is gone."""
    sym = "sqlalchemy.engine.result.RowProxy.keys"
    site = _site(sym, "return dict(author_row(engine, author_id))",
                 recv="sqlalchemy.engine.result.RowProxy", marker="__object__")
    gone = {"symbol": sym, "kind": "REMOVED", "aliases": ["sqlalchemy.engine.RowProxy.keys"],
            "vanished_at": {sym: "sqlalchemy.engine.result.RowProxy",
                            "sqlalchemy.engine.RowProxy.keys": "sqlalchemy.engine.RowProxy"}}
    (risk,) = _predict(site, gone, package="sqlalchemy").risks
    assert risk.tier is RiskTier.LOW and risk.unverified


def test_a_project_name_with_no_dependency_base_is_no_receiver(tmp_path):
    """``Base = declarative_base()``; ``class Book(Base)`` runs ``DeclarativeMeta.__init__``
    on ``Book``. The project's ``Base`` is no dependency class to spell anything through."""
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "core.py").write_text(
        "from sqlalchemy.ext.declarative import declarative_base\n"
        "Base = declarative_base()\n\n\nclass Book(Base):\n    pass\n", encoding="utf-8")
    sym = "sqlalchemy.ext.declarative.api.DeclarativeMeta.__init__"
    gone = {"symbol": sym, "kind": "REMOVED", "aliases": [],
            "vanished_at": {sym: "sqlalchemy.ext.declarative.api"}}
    for receiver in ("app.core.Base", "app.core.Book"):     # ``Book``'s base is ``Base``
        ev = {"v": 2, "t": "return", "q": sym, "sym": sym, "result": None,
              "in": {"bound": {"cls": {"__type__": receiver}}},
              "where": {"file": str(tmp_path / "app" / "core.py"), "line": 5,
                        "func": "<module>"}}
        site = fold_facts([ev], root=str(tmp_path))
        assert predict_risks(site, [_delta(gone, package="sqlalchemy")], static_contacts=[],
                             root=str(tmp_path)).risks == [], receiver


def test_a_name_the_site_does_not_write_is_still_rated_where_a_statement_writes_it():
    """click 8.2 warns on access to ``click.MultiCommand``: not at ``PluginGroup(...)``, which
    the baseline ran, but at ``class PluginGroup(click.MultiCommand)``, which writes it."""
    sym = "click.core.MultiCommand.__init__"
    site = _site(sym, 'cli.add_command(PluginGroup(name="plugins"))',
                 recv="click.core.MultiCommand", marker="__opaque__")
    warns = {"symbol": "click.core.MultiCommand", "aliases": ["click.MultiCommand"],
             "kind": "DEPRECATED", "successor": "click.Group",
             "evidence": ["deprecated-in-new:access-warning: DeprecationWarning: "
                          "'MultiCommand' is deprecated"]}
    base = {"caller": "app.perf.PluginGroup", "target": "click.MultiCommand", "dep": "click",
            "file": FILE, "line": 3, "spans": [[3, 3, 3]], "kind": "class-base"}
    report = predict_risks(site, [_delta(warns, package="click")], static_contacts=[base],
                           root=ROOT)
    (risk,) = report.risks
    assert risk.basis == "static" and risk.symbol == "click.MultiCommand"
    assert risk.fact["kind"] == "DEPRECATED"
