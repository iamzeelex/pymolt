"""Regression tests for the two reviews of verify's Stage 1 semantics: each reproduces a
confirmed defect (most of them a false PASS) and asserts the honest verdict."""

from _semantics import ev, one, run

from pymolt.verify.models import VerificationVerdict

PASS, FAIL = VerificationVerdict.PASS, VerificationVerdict.FAIL


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
    assert one(d, "dep.cluster.KMeans.fit").fails


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
