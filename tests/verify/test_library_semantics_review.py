"""Regression tests for the two reviews of verify's Stage 1 semantics: each reproduces a
confirmed defect (most of them a false PASS) and asserts the honest verdict."""

from _semantics import ev, one, run

from pymolt.verify.facts import Comparison
from pymolt.verify.models import VerificationVerdict

PASS, FAIL = VerificationVerdict.PASS, VerificationVerdict.FAIL
INCONCLUSIVE = VerificationVerdict.INCONCLUSIVE


# ── 1. a src layout's own records are values ──────────────────────────────────────────────

def test_a_src_layout_project_record_is_a_value(tmp_path):
    models = ("from dataclasses import dataclass\n@dataclass\nclass Order:\n    id: int\n"
              "    total: float\n")
    app = "import dep\ndef main(raw):\n    o = dep.parse(raw)\n    return o.id\n"
    src = {"src/mypkg/__init__.py": "", "src/mypkg/models.py": models, "src/mypkg/app.py": app}

    def order(total):
        return {"__dataclass__": "mypkg.models.Order", "fields": [["id", 1], ["total", total]]}

    app_py = "src/mypkg/app.py"
    d = run(tmp_path, [ev("dep.parse", {"raw": "x"}, result=order(10.0), file=app_py)],
            [ev("dep.parse", {"raw": "x"}, result=order(99.0), file=app_py)], src)
    assert d.verdict is FAIL and not one(d, "dep.parse").absorbed


# ── 2. an unpacked library namedtuple compares its fields ─────────────────────────────────

TTEST_SRC = ("from scipy import stats\ndef main(a, b):\n    t, p = stats.ttest_ind(a, b)\n"
             "    return p\n")


def ttest(p, name="scipy.stats._stats_py.TtestResult"):
    return ev("scipy.stats._stats_py.ttest_ind", {"a": 1},
              result={"__object__": name, "attrs": {"statistic": 2.1, "pvalue": p}})


def test_an_unpacked_library_namedtuple_compares_its_fields(tmp_path):
    assert run(tmp_path, [ttest(0.04)], [ttest(0.40)], TTEST_SRC).verdict is FAIL
    assert run(tmp_path, [ttest(0.04)], [ttest(0.04)], TTEST_SRC).verdict is PASS
    # unpacking erases the container type, as for a list against a tuple
    renamed = run(tmp_path, [ttest(0.04)], [ttest(0.04, "scipy.stats._result.TtestResult")],
                  TTEST_SRC)
    assert renamed.verdict is PASS


# ── 3. introspection through a base class or an import alias ──────────────────────────────

def query(cls, line=3):
    return ev("dep.query", {}, result={"__object__": cls, "attrs": {"status": 200}}, line=line)


def test_isinstance_against_a_base_class_of_the_package_keeps_the_ladder_strict(tmp_path):
    src = ("import dep\ndef main():\n    r = dep.query()\n"
           "    if isinstance(r, dep.Response):\n        return 1\n")
    d = run(tmp_path, [query("dep.models.JSONResponse")],
            [query("dep.models.StreamingPage")], src)
    assert d.verdict is FAIL


def test_isinstance_through_an_import_alias_keeps_the_ladder_strict(tmp_path):
    src = ("from dep.models import JSONResponse as JR\nimport dep\ndef main():\n"
           "    r = dep.query()\n    return isinstance(r, JR)\n")
    d = run(tmp_path, [query("dep.models.JSONResponse", 4)],
            [query("dep.models.StreamingPage", 4)], src)
    assert d.verdict is FAIL


def test_an_except_clause_of_the_package_does_not_check_a_returned_object(tmp_path):
    src = ("import dep\ndef main():\n    try:\n        r = dep.query()\n"
           "    except dep.ClientError:\n        return None\n")
    d = run(tmp_path, [query("dep.client._RequestCM", 4)],
            [query("dep.client._BaseRequestCM", 4)], src)
    assert d.verdict is PASS


def test_a_check_of_another_package_leaves_the_drift(tmp_path):
    src = ("import dep\nimport other\ndef main():\n    r = dep.query()\n"
           "    return isinstance(r, other.Thing)\n")
    d = run(tmp_path, [query("dep.models.JSONResponse", 4)],
            [query("dep.models.StreamingPage", 4)], src)
    assert d.verdict is PASS and one(d, "dep.query").absorbed == ["type-drift:unintrospected"]


# ── 4. reads the renderings cannot see keep a class change strict ─────────────────────────

def get(cls):
    return ev("dep.get", {}, result={"__object__": cls, "attrs": {"code": 1}})


def test_a_class_attribute_read_off_the_result_keeps_the_class_strict(tmp_path):
    src = "import dep\ndef main():\n    r = dep.get()\n    return r.streaming\n"
    d = run(tmp_path, [get("dep.http.Response")], [get("dep.http.StreamedResponse")], src)
    assert d.verdict is not PASS


def test_a_hasattr_probe_off_the_result_keeps_the_class_strict(tmp_path):
    src = ("import dep\ndef main():\n    r = dep.get()\n"
           "    return r.json() if hasattr(r, 'json') else None\n")
    d = run(tmp_path, [get("dep.http.Response")], [get("dep.http.RawStream")], src)
    assert d.verdict is not PASS


def test_a_read_the_rendering_holds_compares_and_the_class_drifts(tmp_path):
    src = "import dep\ndef main():\n    r = dep.get()\n    return r.code\n"
    d = run(tmp_path, [get("dep.http.Response")], [get("dep.http.StreamedResponse")], src)
    assert d.verdict is PASS and "type-drift:unintrospected" in one(d, "dep.get").absorbed


# ── 5. a read attribute present on one side only ──────────────────────────────────────────

def test_a_read_attribute_that_vanished_is_a_change(tmp_path):
    src = ("import dep\ndef main(X):\n    km = dep.KMeans(3).fit(X)\n"
           "    return km.inertia_, km.score_\n")

    def km(extra=None):
        return {"__object__": "dep.cluster.KMeans", "attrs": {"inertia_": 1.5, **(extra or {})}}

    d = run(tmp_path, [ev("dep.cluster.KMeans.fit", {"X": 1}, result=km({"score_": 3}))],
            [ev("dep.cluster.KMeans.fit", {"X": 1}, result=km())], src)
    # an ``__object__`` holds instance state only (``score_`` may be a property there): the
    # pair cannot be compared, it is not a pass
    e = one(d, "dep.cluster.KMeans.fit")
    assert not e.fails and e.comparison is Comparison.UNCOMPARABLE
    assert d.verdict is INCONCLUSIVE
    # a record rendered whole (a dataclass) that lacks it: the attribute is really gone
    def rec(**fields):
        return {"__dataclass__": "dep.cluster.Result",
                "fields": [["inertia_", 1.5], *([k, v] for k, v in fields.items())]}

    for old, new in ((rec(score_=3), rec()), (rec(), rec(score_=3))):
        d = run(tmp_path, [ev("dep.cluster.KMeans.fit", {"X": 1}, result=old)],
                [ev("dep.cluster.KMeans.fit", {"X": 1}, result=new)], src)
        assert d.verdict is FAIL and one(d, "dep.cluster.KMeans.fit").fails


# SQLAlchemy 1.3 -> 2.0: ``Table.c`` is a property in 1.3 (``columns`` in the instance) and is
# cached in the instance in 2.0 (``columns`` no longer there); the project reads ``authors.c``.
TABLE_SRC = "import dep\nauthors = dep.Table('authors')\nprint(authors.c)\n"


def table(**attrs):
    return {"__object__": "dep.sql.schema.Table", "attrs": {"name": "authors", **attrs}}


def tables(tmp_path, old, new, **kw):
    return run(tmp_path, [ev("dep.sql.schema.Table.__new__", {"name": "authors"}, result=old,
                             line=2, func="<module>")],
               [ev("dep.sql.schema.Table.__new__", {"name": "authors"}, result=new, line=2,
                   func="<module>")], TABLE_SRC, **kw)


def test_a_read_attribute_cached_on_one_side_only_is_uncomparable_both_ways(tmp_path):
    from pymolt.verify.service import _fact_diff_causes

    cols = {"__list__": ["id", "name"]}
    for old, new, side in ((table(columns=cols), table(c=cols), "baseline"),
                           (table(c=cols), table(columns=cols), "post-migration")):
        d = tables(tmp_path, old, new)
        e = one(d, "dep.sql.schema.Table.__new__")
        assert not e.fails and e.comparison is Comparison.UNCOMPARABLE
        assert f"read attribute not rendered: Table.c ({side})" in e.notes
        assert d.verdict is INCONCLUSIVE
        (found,) = [c for c in _fact_diff_causes(d.to_json_dict()) if c.cause == "uncomparable"]
        assert found.kind.value == "needs_evidence" and f"Table.c ({side})" in found.reason
        assert "property" in found.settle
    # ``--strict`` compares the full state as before (no projection, so no read attribute)
    strict = tables(tmp_path, table(columns=cols), table(c=cols), strict=True)
    e = one(strict, "dep.sql.schema.Table.__new__")
    assert strict.handle_comparison == "full" and e.comparison is not Comparison.UNCOMPARABLE


def test_mutation_a_read_attribute_on_both_sides_with_another_value_fails(tmp_path):
    d = tables(tmp_path, table(c={"__list__": ["id", "name"]}),
               table(c={"__list__": ["id"]}))
    assert d.verdict is FAIL and one(d, "dep.sql.schema.Table.__new__").fails


def test_an_attribute_each_side_reads_under_its_own_name_is_counted(tmp_path):
    # a hand edit moved the read: legendHandles (old code) -> legend_handles (new code)
    src_old = "import dep\ndef main(ax):\n    lg = ax.legend()\n    return lg.legendHandles\n"
    src_new = src_old.replace("legendHandles", "legend_handles")

    def legend(name):
        return {"__object__": "dep.legend.Legend", "attrs": {name: 1}}

    d = run(tmp_path, [ev("dep.Axes.legend", {}, result=legend("legendHandles"))],
            [ev("dep.Axes.legend", {}, result=legend("legend_handles"))], src_old, src_new)
    e = one(d, "dep.Axes.legend")
    assert not e.fails and "handle-state" in e.absorbed
# ── 6. via: aliases, --strict, a side without line text ───────────────────────────────────

MSE_SRC = ("import numpy as np\nfrom dep.metrics import mean_squared_error as mse\n"
           "def score(y, p):\n    return np.sqrt(mse(y, p))\n")


def mse_calls(value):
    sqrt = ev("numpy.sqrt", line=4, func="score")
    sqrt["in"] = {"__c_call__": True}
    sqrt["result"] = {"__opaque__": "unobserved:c-return"}
    return [sqrt, ev("dep.metrics.mean_squared_error", {"y": 1, "p": 2}, result=value, line=4,
                     func="score")]


def test_a_call_through_an_import_alias_is_the_line_s_own(tmp_path):
    d = run(tmp_path, mse_calls(0.25), mse_calls(4.0), MSE_SRC)
    assert d.verdict is FAIL and d.via == 0


def test_a_call_through_a_parameter_is_never_dropped(tmp_path):
    src = "import numpy as np\ndef score(loss, y, p):\n    return np.sqrt(loss(y, p))\n"
    old, new = mse_calls(0.25), mse_calls(4.0)
    for e in old + new:
        e["where"]["line"] = 3
    d = run(tmp_path, old, new, src)
    assert d.verdict is FAIL and d.via == 0


def test_strict_never_drops_a_call(tmp_path):
    src = ("import numpy as np\ndef churn():\n    rng = np.random.RandomState(0)\n"
           "    return rng\n")
    rs = ev("numpy.random.mtrand.RandomState", result={"__opaque__": "unobserved:c-return"})
    rs["in"] = {"__c_call__": True}
    conc = [ev("numpy.core.multiarray.concatenate", {"a": {"__list__": [v]}},
               result={"__list__": [v]}) for v in (1, 2)]
    assert run(tmp_path, [conc[0], rs], [conc[1], rs], src).via == 2
    assert run(tmp_path, [conc[0], rs], [conc[1], rs], src, strict=True).via == 0


def test_a_side_without_line_text_is_dropped_symmetrically():
    from pymolt.verify.facts import Fact, Outcome, _drop_via

    def fact(symbol, text, value, c_call=False):
        return Fact(symbol=symbol, file="app.py", func="main", line=3, text=text,
                    outcomes=[Outcome(kind="return", value=value)], count=1,
                    input={"__c_call__": True} if c_call else {"arrays": [value]})

    text = "rng = np.random.RandomState(0)"
    old = [fact("numpy.core.multiarray.concatenate", text, 1),
           fact("numpy.random.mtrand.RandomState", text, "c", True)]
    new = [fact("numpy.core.multiarray.concatenate", None, 2),
           fact("numpy.random.mtrand.RandomState", None, "c", True)]
    old_kept, new_kept, dropped = _drop_via(old, new, None)
    assert len(old_kept) == len(new_kept) == 1 and dropped == 2


# ── 7. numeric drift at each value's own precision ────────────────────────────────────────

PRED_SRC = "def main(m, X):\n    y = m.predict(X)\n"


def pred(values, dtype="float64"):
    return ev("dep.Model.predict", {"self": {"__opaque__": "dep.Model"}, "X": 1},
              result={"__structure__": "numpy.ndarray", "dtype": dtype, "shape": [len(values)],
                      "values": {"__list__": list(values)}}, line=2)


def test_a_small_element_of_a_large_scale_array_compares_by_its_own_magnitude(tmp_path):
    assert run(tmp_path, [pred([2.1e13, 1.5])], [pred([2.1e13, 4.5])], PRED_SRC).verdict is FAIL
    assert run(tmp_path, [pred([1e18, 100.0])], [pred([1e18, 0.0])], PRED_SRC).verdict is FAIL


def test_float16_and_float32_moves_are_changes(tmp_path):
    assert run(tmp_path, [pred([0.75, 0.25], "float16")], [pred([0.5, 0.5], "float16")],
               PRED_SRC).verdict is FAIL
    assert run(tmp_path, [pred([1000.0, 0.01], "float32")], [pred([1000.0, 0.05], "float32")],
               PRED_SRC).verdict is FAIL


def test_the_relative_cap_holds_whatever_the_ulps_bound(tmp_path):
    a = 1.0
    b = a * (1 + 2.0 ** -9)       # 2**-9 relative: past the cap
    assert run(tmp_path, [pred([a], "float16")], [pred([b], "float16")], PRED_SRC).verdict is FAIL


def test_rounding_of_each_element_stays_numeric_drift(tmp_path):
    import math
    xs = [6.0, 1e-3, 2.5e13]
    ys = [x + 13 * math.ulp(x) for x in xs]
    d = run(tmp_path, [pred(xs)], [pred(ys)], PRED_SRC)
    assert d.verdict is PASS and one(d, "dep.Model.predict").numeric["max_ulps"] == 13


def test_a_cent_on_a_1e11_float_is_rounding_and_stamped(tmp_path):
    src = "import dep\ndef main():\n    t = dep.total()\n    return t\n"
    d = run(tmp_path, [ev("dep.total", {}, result=123456789012.34)],
            [ev("dep.total", {}, result=123456789012.35)], src)
    assert d.verdict is PASS and d.numeric_drift["max_ulps"] > 600
    assert run(tmp_path, [ev("dep.total", {}, result=123456789012.34)],
               [ev("dep.total", {}, result=123456789012.35)], src,
               float_ulps=0).verdict is FAIL


# ── 8. an edit's types carry only along its data chain ────────────────────────────────────

def test_a_discarded_edit_carries_nothing_to_an_unrelated_introspected_call(tmp_path):
    app_old = "import dep\ndef init():\n    dep.connect('db', pool=False)\n"
    app_new = "import dep\ndef init():\n    dep.connect('db', pool=True)\n"
    test = ("import dep\nfrom dep.v1 import Engine\ndef test():\n    r = dep.engine()\n"
            "    assert isinstance(r, Engine)\n")

    def obj(t):
        return {"__object__": t, "attrs": {"url": "db"}}

    old = [ev("dep.connect", {"u": "db", "pool": False}, result=obj("dep.v1.Engine"),
              func="init"),
           ev("dep.engine", {}, result=obj("dep.v1.Engine"), file="test_app.py", line=4,
              func="test")]
    new = [ev("dep.connect", {"u": "db", "pool": True}, result=obj("dep.v2.Pool"), func="init"),
           ev("dep.engine", {}, result=obj("dep.v2.Pool"), file="test_app.py", line=4,
              func="test")]
    d = run(tmp_path, old, new, {"app.py": app_old, "test_app.py": test},
            {"app.py": app_new, "test_app.py": test})
    assert d.verdict is FAIL and not one(d, "dep.engine").review


def test_an_edit_s_type_is_not_carried_off_its_data_chain(tmp_path):
    app_old = "import dep\ndef fig():\n    return dep.trace('gl')\n"
    app_new = "import dep\ndef fig():\n    return dep.trace('hm')\n"
    test = "import dep\ndef test():\n    other = dep.load()\n    return other\n"
    old = [ev("dep.trace", {"k": "gl"}, result={"__type__": "dep.gl.Gl"}, func="fig"),
           ev("dep.load", {}, result={"__tuple__": [{"__opaque__": "dep.gl.Gl"}]},
              file="test_app.py", func="test")]
    new = [ev("dep.trace", {"k": "hm"}, result={"__type__": "dep.hm.Hm"}, func="fig"),
           ev("dep.load", {}, result={"__tuple__": [{"__opaque__": "dep.hm.Hm"}]},
              file="test_app.py", func="test")]
    d = run(tmp_path, old, new, {"app.py": app_old, "test_app.py": test},
            {"app.py": app_new, "test_app.py": test})
    e = one(d, "dep.load")
    assert "type-drift:carried" not in e.absorbed     # the ladder judges it on its own


# ── 9. record inputs keyed by their type do not absorb moved results ──────────────────────

PRIO_SRC = "import dep\ndef main(tasks):\n    return [t.priority() for t in tasks]\n"


def task(name):
    return {"__dataclass__": "dep.sched.Task", "fields": [["name", name]]}


def prio(pairs):
    return [ev("dep.sched.Task.priority", {"self": task(n)}, result=r) for n, r in pairs]


def test_results_swapped_between_the_same_record_receivers_fail(tmp_path):
    d = run(tmp_path, prio([("a", 1), ("b", 2)]), prio([("a", 2), ("b", 1)]), PRIO_SRC)
    assert d.verdict is FAIL
    ok = run(tmp_path, prio([("a", 1), ("b", 2)]), prio([("b", 2), ("a", 1)]), PRIO_SRC)
    assert ok.verdict is PASS


def test_results_at_records_that_differ_between_captures_are_a_review(tmp_path):
    d = run(tmp_path, prio([("a", 1), ("b", 2)]), prio([("a2", 2), ("b2", 1)]), PRIO_SRC)
    e = one(d, "dep.sched.Task.priority")
    assert d.verdict is not PASS and e.review and "record-input" in e.absorbed


def test_a_single_result_at_a_differing_record_is_counted_drift(tmp_path):
    src = "import dep\ndef main(el):\n    v = el.value\n"

    def el(tree):
        return {"__dataclass__": "dep.Metric", "fields": [["tree", tree]]}

    d = run(tmp_path, [ev("dep.Metric.value", {"self": el(1)}, result="8")],
            [ev("dep.Metric.value", {"self": el(2)}, result="8")], src)
    assert d.verdict is PASS and d.absorbed.get("record-input") == 1


# ── 10. value packages' opaque types are values; class drift within one package ───────────

def test_an_opaque_array_becoming_a_series_is_a_value_type_change(tmp_path):
    src = "import dep\ndef main():\n    r = dep.fit()\n    return r\n"
    old = [ev("dep.fit", {}, result={"__list__": [{"__opaque__": "numpy.ndarray"}]})]
    new = [ev("dep.fit", {}, result={"__list__": [{"__opaque__": "pandas.core.series.Series"}]})]
    assert run(tmp_path, old, new, src).verdict is FAIL
    dates = [ev("dep.fit", {}, result={"__opaque__": "datetime.datetime"})]
    paths = [ev("dep.fit", {}, result={"__opaque__": "pathlib.PosixPath"})]
    assert run(tmp_path, dates, paths, src).verdict is FAIL


def test_class_drift_needs_the_same_top_level_package(tmp_path):
    src = "import dep\ndef main():\n    return dep.redirect('/x')\n"

    def r(cls):
        return ev("dep.redirect", {"to": "/x"}, result={"__structure__": cls, "status_code": 302})

    cross = run(tmp_path, [r("werkzeug.wrappers.Response")], [r("flask.wrappers.Response")], src)
    assert cross.verdict is VerificationVerdict.INCONCLUSIVE    # a review: relation unknown
    assert run(tmp_path, [r("werkzeug.wrappers.Response")],
               [r("werkzeug.sansio.response.Response2")], src).verdict is PASS


def test_mutation_a_cross_package_class_change_with_a_differing_read_attribute_fails(tmp_path):
    src = "import dep\ndef main():\n    r = dep.get()\n    return r.code\n"

    def get_(cls, code):
        return ev("dep.get", {}, result={"__object__": cls, "attrs": {"code": code}})

    assert run(tmp_path, [get_("werkzeug.wrappers.Response", 1)],
               [get_("flask.wrappers.Response", 2)], src).verdict is FAIL
    # an introspected class, or a read the rendering lacks, stays strict across packages too
    checked = src + "def is_resp(r):\n    return isinstance(r, dep.Response)\n"
    unrendered = src.replace("r.code", "r.streaming")
    for s in (checked, unrendered):
        assert run(tmp_path, [get_("werkzeug.wrappers.Response", 1)],
                   [get_("flask.wrappers.Response", 1)], s).verdict is FAIL


# ── 11. a project file that does not parse, or is too deep to walk ────────────────────────

def test_an_unparsed_project_file_turns_the_projections_off(tmp_path):
    app = "import dep\ndef main():\n    job = dep.submit()\n    return job\n"
    worker = "def report(job, async=False):\n    return job.status\n"

    def job(s):
        return {"__object__": "dep.Job", "attrs": {"status": s}}

    d = run(tmp_path, [ev("dep.submit", {}, result=job("done"))],
            [ev("dep.submit", {}, result=job("failed"))], {"app.py": app, "worker.py": worker})
    assert d.verdict is FAIL
    assert d.handle_comparison.startswith("full: 1 project file(s) do not parse")
    assert any(w.startswith("objects-compared-in-full") for w in d.warnings)


def test_a_long_generated_expression_does_not_crash_verify(tmp_path):
    app = "import dep\ndef main():\n    r = dep.get()\n    return r.x\n"
    gen = "SQL = " + " + ".join(["'a'"] * 600) + "\n"
    d = run(tmp_path, [ev("dep.get", {}, result=1)], [ev("dep.get", {}, result=1)],
            {"app.py": app, "gen_sql.py": gen})
    assert d.verdict is PASS


def test_reads_in_a_file_the_post_migration_snapshot_lacks_come_from_the_checkout(tmp_path):
    import json

    from pymolt.verify.facts import DiffContext, diff_facts, load_facts
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "app.py").write_text("import dep\ndef main():\n    return dep.submit()\n")
    (proj / "report.py").write_text("def show(job):\n    return job.status\n")
    facts = {}
    for side, status in (("old", "done"), ("new", "failed")):
        p = tmp_path / f"{side}.jsonl"
        e = ev("dep.submit", {}, result={"__object__": "dep.Job", "attrs": {"status": status}})
        e["where"]["file"] = str(proj / "app.py")
        p.write_text(json.dumps(e) + "\n")
        facts[side] = load_facts(p, root=str(proj))
    src = (proj / "app.py").read_text()
    ctx = DiffContext(old_sources={"app.py": src}, new_sources={"app.py": src}, edits=[])
    assert diff_facts(facts["old"], facts["new"], context=ctx).verdict is FAIL


# ── 12. a discarded result that is an object of another kind compares by type ─────────────

def test_a_discarded_call_that_now_returns_a_deferred_fails(tmp_path):
    src = "import dep\ndef main(job):\n    dep.run_job(job)\n"
    old = [ev("dep.run_job", {"job": 1}, result=None)]
    new = [ev("dep.run_job", {"job": 1}, result={"__object__": "dep.tasks.AsyncResult",
                                                 "attrs": {"id": "x"}})]
    assert run(tmp_path, old, new, src).verdict is FAIL


def test_a_discarded_fluent_return_of_the_receiver_is_not_observed(tmp_path):
    src = "import dep\ndef main(resp):\n    resp.raise_for_status()\n"

    def rfs(result):
        return ev("dep.Response.raise_for_status", {"self": {"__opaque__": "dep.Response"}},
                  result=result)

    d = run(tmp_path, [rfs(None)], [rfs({"__structure__": "dep.Response", "status_code": 200})],
            src)
    assert d.verdict is PASS
    # an object the call was handed as an argument and hands back (``sns.barplot(ax=ax)``)
    plot = "import sns\ndef main(ax):\n    sns.barplot(x=1, ax=ax)\n"

    def bar(result, ax="dep.axes.AxesSubplot"):
        return ev("sns.barplot", {"x": 1, "ax": {"__opaque__": ax}}, result=result)

    d = run(tmp_path, [bar(None)], [bar({"__opaque__": "dep.axes.AxesSubplot"})], plot)
    assert d.verdict is PASS


# ── 13. a private field the project reads by name ─────────────────────────────────────────

def test_a_private_field_read_by_name_is_compared(tmp_path):
    src = "import dep\ndef main():\n    p = dep.page()\n    return p._cursor\n"

    def page(c):
        return {"__dataclass__": "dep.Page", "fields": [["items", 1], ["_cursor", c]]}

    d = run(tmp_path, [ev("dep.page", {}, result=page("a"))],
            [ev("dep.page", {}, result=page("b"))], src)
    assert d.verdict is FAIL


# ── 14. a record that became a coroutine ──────────────────────────────────────────────────

def test_a_sync_record_becoming_a_coroutine_fails(tmp_path):
    src = "import dep\ndef main():\n    r = dep.fetch(1)\n    return r.value\n"
    old = [ev("dep.fetch", {"n": 1}, result={"__dataclass__": "dep.Result",
                                             "fields": [["value", 1]]})]
    new = [ev("dep.fetch", {"n": 1}, result={"__opaque__": "coroutine"})]
    assert run(tmp_path, old, new, src).verdict is FAIL


# ── 15. counted totals; clock readings keep their counts ──────────────────────────────────

def test_a_call_more_with_one_outcome_fails(tmp_path):
    src = "import dep\ndef main(lines):\n    ys = [ln.get_ydata() for ln in lines]\n"

    def calls(n):
        return [ev("dep.Line2D.get_ydata", {"self": {"__opaque__": "dep.Line2D"}}, result=3.0)
                for _ in range(n)]

    e = one(run(tmp_path, calls(2), calls(3), src), "dep.Line2D.get_ydata")
    assert e.fails and "call-count-changed" in e.notes


def test_clock_readings_must_keep_their_counts():
    from pymolt.verify.facts import _COUNTED, Comparison, Outcome, compare_outcomes
    old = [Outcome(kind="return", value="⟨clock:@[+10,+20]s⟩", count=1),
           Outcome(kind="return", value="static", count=9)]
    new = [Outcome(kind="return", value="⟨clock:@[+15,+25]s⟩", count=9),
           Outcome(kind="return", value="static", count=1)]
    token = _COUNTED.set(True)
    try:
        assert compare_outcomes(old, new).comparison is Comparison.CHANGED
        same = [Outcome(kind="return", value="⟨clock:@[+15,+25]s⟩", count=1),
                Outcome(kind="return", value="static", count=9)]
        assert compare_outcomes(old, same).comparison is Comparison.EQUAL
    finally:
        _COUNTED.reset(token)


# ── 16. --strict and counting for added None defaults; wrappers that relate ───────────────

LINSPACE_SRC = "import numpy as np\ndef f():\n    return np.linspace(-1, 1, 20)\n"
LIN_OLD = [ev("numpy.linspace", {"start": -1, "stop": 1, "num": 20}, result=1)]
LIN_NEW = [ev("numpy.linspace", {"start": -1, "stop": 1, "num": 20, "device": None}, result=1)]


def test_an_added_none_default_is_counted_and_strict_reviews_it(tmp_path):
    d = run(tmp_path, LIN_OLD, LIN_NEW, LINSPACE_SRC)
    assert d.absorbed.get("added-default-none") == 1 and d.verdict is PASS
    strict = run(tmp_path, LIN_OLD, LIN_NEW, LINSPACE_SRC, strict=True)
    assert one(strict, "numpy.linspace").review and strict.verdict is not PASS


DECO2_SRC = ("from dep import deco\n@deco\ndef task_handler():\n    return 1\n"
             "def unauthorized_backdoor():\n    return 2\n")


def deco_call(result):
    return ev("dep.deco", {"func": {"__callable__": "app.task_handler"}},
              result={"__callable__": result}, line=2)


def test_a_decorator_returning_another_project_function_fails(tmp_path):
    d = run(tmp_path, [deco_call("app.WrappedHandler")], [deco_call("app.unauthorized_backdoor")],
            DECO2_SRC)
    assert d.verdict is FAIL


def test_a_library_made_wrapper_or_closure_still_drifts(tmp_path):
    d = run(tmp_path, [deco_call("app.WrappedHandler")],
            [deco_call("app.View.as_view.<locals>.view")], DECO2_SRC)
    assert d.verdict is PASS and one(d, "dep.deco").absorbed == ["type-drift:wrapper"]


# ── 17. many rounded floats in a loop ─────────────────────────────────────────────────────

def test_65_calls_each_within_rounding_pass(tmp_path):
    import math
    src = "import dep\ndef main(lines):\n    ys = [ln.get_ydata() for ln in lines]\n"

    def calls(vals):
        return [ev("dep.Line2D.get_ydata", {"self": {"__opaque__": "dep.Line2D"}}, result=v)
                for v in vals]

    xs = [float(i) + 1.0 for i in range(65)]
    d = run(tmp_path, calls(xs), calls([x + 4 * math.ulp(x) for x in xs]), src)
    e = one(d, "dep.Line2D.get_ydata")
    assert not e.fails and d.verdict is PASS and e.numeric["values"] > 0
    moved = run(tmp_path, calls(xs), calls(xs[:-1] + [100.0]), src)
    assert one(moved, "dep.Line2D.get_ydata").fails
