"""Verify's comparison semantics for library objects (Stage 1, host only).

Each relaxation has a mutation test that must FAIL next to the test that it absorbs the
equivalent difference."""

from _semantics import ev, one, run

from pymolt.verify.facts import (
    AWAITED_UNRECORDED,
    Comparison,
    FactCategory,
)
from pymolt.verify.inconclusive import entry_cause, uncomparable_mechanism
from pymolt.verify.models import VerificationVerdict

COROUTINE = {"__opaque__": "coroutine"}


# ── 1. awaited results ────────────────────────────────────────────────────────────────────

AWAIT_SRC = "import dep\nasync def main():\n    r = await dep.fetch(1)\n"


def test_two_coroutine_markers_are_uncomparable_not_equal(tmp_path):
    old = [ev("dep.fetch", {"n": 1}, result=COROUTINE)]
    new = [ev("dep.fetch", {"n": 1}, result=COROUTINE)]
    d = run(tmp_path, old, new, AWAIT_SRC)
    e = one(d, "dep.fetch")
    assert e.category is FactCategory.UNCOMPARABLE and e.comparison is Comparison.UNCOMPARABLE
    assert AWAITED_UNRECORDED in e.notes
    assert d.verdict is VerificationVerdict.INCONCLUSIVE
    kind, why = entry_cause(e.model_dump(mode="json"), noise_measured=False)
    assert (kind.value, why) == ("needs_evidence", "uncomparable")
    assert uncomparable_mechanism(e.model_dump(mode="json")) == "awaited-unrecorded"


def test_coroutine_against_an_awaitable_handle_is_uncomparable(tmp_path):
    old = [ev("dep.fetch", {"n": 1}, result={"__opaque__": "dep.client._RequestContextManager"})]
    new = [ev("dep.fetch", {"n": 1}, result=COROUTINE)]
    e = one(run(tmp_path, old, new, AWAIT_SRC), "dep.fetch")
    assert e.comparison is Comparison.UNCOMPARABLE and AWAITED_UNRECORDED in e.notes


def test_mutation_plain_value_becoming_a_coroutine_fails(tmp_path):
    old = [ev("dep.fetch", {"n": 1}, result=None)]
    new = [ev("dep.fetch", {"n": 1}, result=COROUTINE)]
    d = run(tmp_path, old, new, AWAIT_SRC)
    assert one(d, "dep.fetch").fails and d.verdict is VerificationVerdict.FAIL


def test_mutation_coroutine_call_that_now_raises_fails(tmp_path):
    old = [ev("dep.fetch", {"n": 1}, result=COROUTINE)]
    new = [ev("dep.fetch", {"n": 1}, raised="TypeError")]
    d = run(tmp_path, old, new, AWAIT_SRC)
    assert one(d, "dep.fetch").fails and d.verdict is VerificationVerdict.FAIL


# ── 2. handles: compared by what the project reads of them ────────────────────────────────

KM_SRC = ("import dep\n"
          "def main(X):\n"
          "    km = dep.KMeans(3).fit(X)\n"
          "    return km.inertia_, km.labels_\n")


def kmeans(inertia=1.5, labels=(0, 1), n_iter=7, extra=None):
    attrs = {"inertia_": inertia, "labels_": {"__list__": list(labels)}, "n_iter_": n_iter,
             "_tol": 0.1, **(extra or {})}
    return {"__object__": "dep.cluster.KMeans", "attrs": attrs}


def fit(result, **kw):
    return ev("dep.cluster.KMeans.fit", {"X": {"__list__": [1, 2]}}, result=result, **kw)


def test_unread_handle_state_is_absorbed_and_counted(tmp_path):
    d = run(tmp_path, [fit(kmeans(n_iter=7))], [fit(kmeans(n_iter=9))], KM_SRC)
    e = one(d, "dep.cluster.KMeans.fit")
    assert e.category is FactCategory.DRIFT and e.comparison is Comparison.EQUAL
    assert e.absorbed == ["handle-state"] and e.compared_equal
    assert any(n.startswith("handle state not observed") and "n_iter_" in n for n in e.notes)
    assert d.absorbed == {"handle-state": 1} and d.handle_comparison == "projected"
    assert d.verdict is VerificationVerdict.PASS


def test_mutation_a_read_handle_attribute_that_changes_fails(tmp_path):
    d = run(tmp_path, [fit(kmeans(inertia=1.5))], [fit(kmeans(inertia=2.5))], KM_SRC)
    assert one(d, "dep.cluster.KMeans.fit").fails and d.verdict is VerificationVerdict.FAIL


def test_without_source_snapshots_handles_compare_by_full_state(tmp_path):
    d = run(tmp_path, [fit(kmeans(n_iter=7))], [fit(kmeans(n_iter=9))], KM_SRC, context=False)
    assert one(d, "dep.cluster.KMeans.fit").fails
    assert d.handle_comparison == "full: no source snapshot"


def test_strict_compares_the_full_public_state(tmp_path):
    d = run(tmp_path, [fit(kmeans(n_iter=7))], [fit(kmeans(n_iter=9))], KM_SRC, strict=True)
    assert one(d, "dep.cluster.KMeans.fit").fails and d.handle_comparison == "full"
    assert d.absorbed == {}


def test_private_state_is_never_compared(tmp_path):
    src = KM_SRC + "print(km._tol)\n"
    old, new = kmeans(), kmeans()
    new["attrs"]["_tol"] = 0.5
    d = run(tmp_path, [fit(old)], [fit(new)], src)
    assert not one(d, "dep.cluster.KMeans.fit").fails


def test_planned_attribute_rename_compares_old_name_with_new(tmp_path):
    src_old = KM_SRC.replace("km.labels_", "km.legendHandles")
    src_new = KM_SRC.replace("km.labels_", "km.legend_handles")
    old = kmeans(extra={"legendHandles": 1})
    new = kmeans(extra={"legend_handles": 2})
    d = run(tmp_path, [fit(old)], [fit(new)], src_old, src_new,
            successors={"dep.cluster.KMeans.legendHandles": "dep.cluster.KMeans.legend_handles"})
    assert one(d, "dep.cluster.KMeans.fit").fails


def test_a_read_attribute_on_one_side_only_is_drift_unless_probed(tmp_path):
    old, new = kmeans(), kmeans(extra={"labels_": {"__list__": [0, 1]}, "score_": 3})
    d = run(tmp_path, [fit(old)], [fit(new)], KM_SRC + "print(km.score_)\n")
    e = one(d, "dep.cluster.KMeans.fit")
    assert not e.fails and "attribute added: score_" in e.notes
    probed = run(tmp_path, [fit(old)], [fit(new)], KM_SRC + "print(hasattr(km, 'score_'))\n")
    assert one(probed, "dep.cluster.KMeans.fit").fails


def test_a_dict_attribute_read_by_literal_keys_compares_those_keys(tmp_path):
    src = "import dep\ndef main():\n    app = dep.make()\n    return app.config['SEED']\n"

    def app(seed, env):
        return {"__object__": "dep.App", "attrs": {"config": {"__dict__": [
            ["ENV", env], ["SEED", seed]]}}}

    def call(r):
        return ev("dep.make", {}, result=r)

    ok = run(tmp_path, [call(app(1, "prod"))], [call(app(1, "dev"))], src)
    assert not one(ok, "dep.make").fails and "handle-state" in one(ok, "dep.make").absorbed
    bad = run(tmp_path, [call(app(1, "prod"))], [call(app(2, "prod"))], src)
    assert one(bad, "dep.make").fails
    whole = run(tmp_path, [call(app(1, "prod"))], [call(app(1, "dev"))],
                src + "    print(dict(app.config))\n")
    assert one(whole, "dep.make").fails


def test_a_module_attribute_read_is_no_read_of_an_object(tmp_path):
    src = ("import dep\nimport stl as st\ndef main():\n    at = dep.App()\n"
           "    return st.query_params.get('region')\n")

    def app(region):
        return {"__object__": "dep.App", "attrs": {"query_params": {"__dict__": [
            ["region", region]]}}}

    d = run(tmp_path, [ev("dep.App", {}, result=app({"__list__": ["North"]}), line=4)],
            [ev("dep.App", {}, result=app("North"), line=4)], src)
    assert not one(d, "dep.App").fails
    held = src.replace("st.query_params", "at.query_params")
    d = run(tmp_path, [ev("dep.App", {}, result=app({"__list__": ["North"]}), line=4)],
            [ev("dep.App", {}, result=app("North"), line=4)], held)
    assert one(d, "dep.App").fails


def test_a_library_record_and_object_of_one_type_compare_as_a_handle(tmp_path):
    src = "import dep\ndef main():\n    d = dep.Depends(get)\n"
    old = {"__object__": "dep.params.Depends", "attrs": {"dependency": 1, "use_cache": True}}
    new = {"__dataclass__": "dep.params.Depends",
           "fields": [["dependency", 1], ["use_cache", True], ["scope", None]]}
    d = run(tmp_path, [ev("dep.Depends", {}, result=old)], [ev("dep.Depends", {}, result=new)],
            src)
    assert not one(d, "dep.Depends").fails


def test_mutation_a_project_record_is_a_value(tmp_path):
    src = "import dep\nfrom dataclasses import dataclass\n@dataclass\nclass P:\n    x: int\n"
    src += "def main():\n    p = dep.load()\n"

    def rec(x):
        return {"__dataclass__": "app.P", "fields": [["x", x]]}

    d = run(tmp_path, [ev("dep.load", {}, result=rec(1), line=7)],
            [ev("dep.load", {}, result=rec(2), line=7)], src)
    assert one(d, "dep.load").fails


def test_a_library_record_receiver_is_keyed_by_its_type(tmp_path):
    src = "import dep\ndef main(el):\n    v = el.value\n"

    def el(tree):
        return {"__dataclass__": "dep.Metric", "fields": [["tree", tree]]}

    old = [ev("dep.Metric.value", {"self": el(1)}, result="8")]
    new = [ev("dep.Metric.value", {"self": el(2)}, result="8")]
    e = one(run(tmp_path, old, new, src), "dep.Metric.value")
    assert e.category is FactCategory.EQUAL and not e.notes


def test_dynamic_attribute_reads_are_listed_when_handle_state_was_absorbed(tmp_path):
    src = KM_SRC + "    return getattr(km, name), vars(km)\n"
    d = run(tmp_path, [fit(kmeans(n_iter=7))], [fit(kmeans(n_iter=9))], src)
    assert d.verdict is VerificationVerdict.PASS
    assert d.dynamic_reads == ["app.py:5 getattr(x, <computed name>)", "app.py:5 vars(x)"]
    quiet = run(tmp_path, [fit(kmeans())], [fit(kmeans())], src)
    assert quiet.dynamic_reads == []


# ── 7. outcomes matched with their counts ─────────────────────────────────────────────────

LOOP_SRC = "import dep\ndef main(lines):\n    ys = [ln.get_ydata() for ln in lines]\n"


def ydata(values, **kw):
    return [ev("dep.Line2D.get_ydata", {"self": {"__opaque__": "dep.Line2D"}}, result=v, **kw)
            for v in values]


def test_the_same_calls_in_another_order_are_equal(tmp_path):
    d = run(tmp_path, ydata([1.0, 2.0, 3.0, 4.0]), ydata([4.0, 3.0, 2.0, 1.0]), LOOP_SRC)
    e = one(d, "dep.Line2D.get_ydata")
    assert e.category is FactCategory.EQUAL and not e.fails


def test_mutation_one_of_n_calls_in_a_loop_changes_fails(tmp_path):
    d = run(tmp_path, ydata([1.0, 2.0, 3.0, 4.0]), ydata([1.0, 2.0, 3.0, 5.0]), LOOP_SRC)
    e = one(d, "dep.Line2D.get_ydata")
    assert e.fails and d.verdict is VerificationVerdict.FAIL


def test_mutation_a_call_more_in_a_loop_fails(tmp_path):
    d = run(tmp_path, ydata([1.0, 2.0, 3.0]), ydata([1.0, 2.0, 3.0, 3.0]), LOOP_SRC)
    e = one(d, "dep.Line2D.get_ydata")
    assert e.fails and "call-count-changed" in e.notes


def test_mutation_counts_moving_between_outcome_kinds_fails(tmp_path):
    src = "import dep\ndef main(p, qs):\n    page = p.paginate(qs)\n"

    def calls(raises, returns):
        out = [ev("dep.Paginator.paginate", {"self": {"__opaque__": "dep.Paginator"}},
                  raised="dep.NotFound") for _ in range(raises)]
        return out + [ev("dep.Paginator.paginate", {"self": {"__opaque__": "dep.Paginator"}},
                         result=[1]) for _ in range(returns)]

    d = run(tmp_path, calls(2, 1), calls(1, 2), src)
    e = one(d, "dep.Paginator.paginate")
    assert e.fails and "outcome-kinds-moved" in e.notes


# ── 3. the type ladder (without the MRO step) ─────────────────────────────────────────────

GET_SRC = "import dep\nasync def main(s):\n    async with s.get('/x') as resp:\n        pass\n"


def test_two_private_classes_of_one_module_are_drift(tmp_path):
    old = [ev("dep.Session.get", {"url": "/x"}, result={"__opaque__": "dep.client._RequestCM"})]
    new = [ev("dep.Session.get", {"url": "/x"},
              result={"__opaque__": "dep.client._BaseRequestCM"})]
    e = one(run(tmp_path, old, new, GET_SRC), "dep.Session.get")
    assert e.category is FactCategory.DRIFT and e.compared_equal
    assert e.absorbed == ["type-drift:private-same-module"]


REDIRECT_SRC = "from dep import redirect\ndef view():\n    return redirect('/items')\n"


def redirect(name, code=302):
    return ev("dep.redirect", {"location": "/items"},
              result={"__structure__": name, "status_code": code})


def test_a_handle_class_the_project_never_checks_is_drift(tmp_path):
    d = run(tmp_path, [redirect("werkzeug.wrappers.Response")],
            [redirect("flask.wrappers.Response")], REDIRECT_SRC)
    e = one(d, "dep.redirect")
    assert not e.fails and e.absorbed == ["type-drift:unintrospected"]
    assert d.verdict is VerificationVerdict.PASS


def test_mutation_a_handle_class_the_project_checks_with_isinstance_fails(tmp_path):
    src = REDIRECT_SRC + "def check(r):\n    return isinstance(r, Response)\n"
    d = run(tmp_path, [redirect("werkzeug.wrappers.Response")],
            [redirect("flask.wrappers.Response")], src)
    assert one(d, "dep.redirect").fails
    named = REDIRECT_SRC + "def name(r):\n    return type(r).__name__\n"
    d = run(tmp_path, [redirect("werkzeug.wrappers.Response")],
            [redirect("flask.wrappers.Response")], named)
    assert one(d, "dep.redirect").fails


def test_mutation_a_handle_status_code_that_changes_fails(tmp_path):
    d = run(tmp_path, [redirect("werkzeug.wrappers.Response", 302)],
            [redirect("flask.wrappers.Response", 301)], REDIRECT_SRC)
    assert one(d, "dep.redirect").fails


def test_a_relocated_structure_compares_its_values(tmp_path):
    src = "import dep\ndef main(c):\n    n = c.get_offsets()\n"

    def offsets(name, values):
        return ev("dep.Collection.get_offsets", {"self": {"__opaque__": "dep.Collection"}},
                  result={"__structure__": name, "dtype": "float64", "shape": [2],
                          "values": {"__list__": values}})

    ok = run(tmp_path, [offsets("numpy.ma.core.MaskedArray", [1.0, 2.0])],
             [offsets("numpy.ma.MaskedArray", [1.0, 2.0])], src)
    e = one(ok, "dep.Collection.get_offsets")
    assert not e.fails and any(n.startswith("relocated:") for n in e.notes)
    bad = run(tmp_path, [offsets("numpy.ma.core.MaskedArray", [1.0, 2.0])],
              [offsets("numpy.ma.MaskedArray", [1.0, 3.0])], src)
    assert one(bad, "dep.Collection.get_offsets").fails


def test_mutation_a_value_type_change_fails_even_unintrospected(tmp_path):
    src = "import numpy as np\ndef main(v):\n    return np.round(v, 6)\n"
    old = [ev("numpy.round", {"a": 0.38, "decimals": 6}, result=0.38)]
    new = [ev("numpy.round", {"a": 0.38, "decimals": 6},
              result={"__opaque__": "numpy.float32"})]
    assert one(run(tmp_path, old, new, src), "numpy.round").fails


DECO_SRC = ("from dep import api_view\n"
            "@api_view(['GET'])\n"
            "def task_list(request):\n"
            "    return 1\n")


def deco(result, func="app.task_list"):
    return ev("dep.decorators.decorator", {"func": {"__callable__": func}},
              result={"__callable__": result}, line=2)


def test_a_decorator_wrapper_named_differently_is_drift(tmp_path):
    d = run(tmp_path, [deco("app.WrappedAPIView")], [deco("app.View.as_view.<locals>.view")],
            DECO_SRC)
    e = one(d, "dep.decorators.decorator")
    assert not e.fails and e.absorbed == ["type-drift:wrapper"]


def test_mutation_a_wrapper_whose_name_the_project_reads_fails(tmp_path):
    src = DECO_SRC + "print(task_list.__name__)\n"
    d = run(tmp_path, [deco("app.WrappedAPIView")], [deco("app.View.as_view.<locals>.view")],
            src)
    assert one(d, "dep.decorators.decorator").fails


def test_mutation_another_project_function_returned_for_the_same_key_fails(tmp_path):
    src = "import dep\ndef main():\n    h = dep.lookup('save')\n"
    old = [ev("dep.lookup", {"key": "save"}, result={"__callable__": "app.handlers.save"})]
    new = [ev("dep.lookup", {"key": "save"}, result={"__callable__": "app.handlers.delete"})]
    assert one(run(tmp_path, old, new, src), "dep.lookup").fails


CARRY_OLD = "import dep\ndef fig():\n    return dep.Figure(dep.trace('heatmapgl', z=1))\n"
CARRY_NEW = "import dep\ndef fig():\n    return dep.Figure(dep.trace('heatmap', z=1))\n"
CARRY_TEST = "from app import fig\ndef test():\n    assert fig().data[0].z == 1\n"


def carry(tmp_path, downstream_new):
    old = [ev("dep.trace", {"kind": "heatmapgl"}, result={"__type__": "dep.gl.Heatmapgl"}),
           ev("dep.Figure.data", {"self": {"__opaque__": "dep.Figure"}}, file="test_app.py",
              result={"__tuple__": [{"__opaque__": "dep.gl.Heatmapgl"}]})]
    new = [ev("dep.trace", {"kind": "heatmap"}, result={"__type__": "dep.hm.Heatmap"}),
           ev("dep.Figure.data", {"self": {"__opaque__": "dep.Figure"}}, file="test_app.py",
              result={"__tuple__": [{"__opaque__": downstream_new}]})]
    return run(tmp_path, old, new, {"app.py": CARRY_OLD, "test_app.py": CARRY_TEST},
               {"app.py": CARRY_NEW, "test_app.py": CARRY_TEST})


def test_a_type_an_edited_line_produced_is_judged_with_that_edit(tmp_path):
    d = carry(tmp_path, "dep.hm.Heatmap")
    e = one(d, "dep.Figure.data")
    assert not e.fails and e.review and e.review.startswith("carried from the edit")
    assert "type-drift:carried" in e.absorbed
    assert d.verdict is VerificationVerdict.INCONCLUSIVE


def test_a_downstream_class_no_edit_produced_is_the_ladder_s(tmp_path):
    e = one(carry(tmp_path, "dep.other.Contour"), "dep.Figure.data")
    assert e.absorbed == ["type-drift:unintrospected"] and not e.review


def test_mutation_more_than_the_carried_type_changes_fails(tmp_path):
    d = carry(tmp_path, "dep.hm.Heatmap")
    assert not one(d, "dep.Figure.data").fails
    old = [ev("dep.trace", {"kind": "heatmapgl"}, result={"__type__": "dep.gl.Heatmapgl"}),
           ev("dep.Figure.data", {"self": {"__opaque__": "dep.Figure"}}, file="test_app.py",
              result={"__tuple__": [{"__opaque__": "dep.gl.Heatmapgl"}]})]
    new = [ev("dep.trace", {"kind": "heatmap"}, result={"__type__": "dep.hm.Heatmap"}),
           ev("dep.Figure.data", {"self": {"__opaque__": "dep.Figure"}}, file="test_app.py",
              result={"__tuple__": [{"__opaque__": "dep.hm.Heatmap"},
                                    {"__opaque__": "dep.hm.Heatmap"}]})]
    d = run(tmp_path, old, new, {"app.py": CARRY_OLD, "test_app.py": CARRY_TEST},
            {"app.py": CARRY_NEW, "test_app.py": CARRY_TEST})
    assert one(d, "dep.Figure.data").fails


def test_raises_relocate_but_a_subclass_fails(tmp_path):
    src = "import dep\ndef main():\n    dep.load('x')\n"
    old = [ev("dep.load", {"p": "x"}, raised="dep.core._exceptions.AxisError")]
    moved = [ev("dep.load", {"p": "x"}, raised="dep.exceptions.AxisError")]
    assert not one(run(tmp_path, old, moved, src), "dep.load").fails
    sub = [ev("dep.load", {"p": "x"}, raised="dep.exceptions.AxisIndexError")]
    assert one(run(tmp_path, old, sub, src), "dep.load").fails


# ── 4. consumer projections ───────────────────────────────────────────────────────────────

RFS_SRC = "import dep\ndef main(resp):\n    resp.raise_for_status()\n    return resp.json()\n"
RESP = {"__structure__": "dep.Response", "status_code": 200}


def rfs(result=None, raised=None):
    return ev("dep.Response.raise_for_status", {"self": {"__opaque__": "dep.Response"}},
              result=result, raised=raised)


def test_a_discarded_result_compares_by_outcome_kind(tmp_path):
    d = run(tmp_path, [rfs(None)], [rfs(RESP)], RFS_SRC)
    e = one(d, "dep.Response.raise_for_status")
    assert not e.fails and e.absorbed == ["projected:discarded"]
    assert any(n.startswith("projected (discarded)") for n in e.notes)
    assert d.verdict is VerificationVerdict.PASS


def test_mutation_a_discarded_call_that_starts_returning_a_coroutine_fails(tmp_path):
    d = run(tmp_path, [rfs(None)], [rfs(COROUTINE)], RFS_SRC)
    assert one(d, "dep.Response.raise_for_status").fails


def test_mutation_a_discarded_call_that_now_raises_fails(tmp_path):
    d = run(tmp_path, [rfs(None)], [rfs(raised="dep.HTTPStatusError")], RFS_SRC)
    assert one(d, "dep.Response.raise_for_status").fails


def test_a_used_result_is_compared_in_full(tmp_path):
    src = RFS_SRC.replace("    resp.raise_for_status()", "    r = resp.raise_for_status()")
    d = run(tmp_path, [rfs(None)], [rfs(RESP)], src)
    assert one(d, "dep.Response.raise_for_status").fails


MESH_SRC = "import numpy as np\ndef main(a, b):\n    x, y = np.meshgrid(a, b)\n"


def mesh(tag, values):
    return ev("numpy.meshgrid", {"xi": {"__tuple__": [1]}}, result={tag: values})


def test_an_unpacked_result_erases_the_container_type(tmp_path):
    d = run(tmp_path, [mesh("__list__", [1, 2])], [mesh("__tuple__", [1, 2])], MESH_SRC)
    e = one(d, "numpy.meshgrid")
    assert not e.fails and e.absorbed == ["projected:unpacked"]


def test_mutation_an_unpacked_element_that_changes_fails(tmp_path):
    d = run(tmp_path, [mesh("__list__", [1, 2])], [mesh("__tuple__", [1, 3])], MESH_SRC)
    assert one(d, "numpy.meshgrid").fails
    arity = run(tmp_path, [mesh("__list__", [1, 2])], [mesh("__tuple__", [1, 2, 3])], MESH_SRC)
    assert one(arity, "numpy.meshgrid").fails


INT_SRC = "def main(bars):\n    return [int(b.get_height()) for b in bars]\n"


def height(values):
    return [ev("dep.Rect.get_height", {"self": {"__opaque__": "dep.Rect"}}, result=v, line=2)
            for v in values]


def test_int_of_a_result_compares_as_int_makes_it(tmp_path):
    d = run(tmp_path, height([4.0, 5.0]), height([4, 5]), INT_SRC)
    e = one(d, "dep.Rect.get_height")
    assert not e.fails and e.absorbed == ["projected:int"]
    numpy = run(tmp_path, height([4.0]), height([{"__opaque__": "numpy.int64"}]), INT_SRC)
    assert not one(numpy, "dep.Rect.get_height").fails


def test_mutation_int_of_a_result_that_changes_fails(tmp_path):
    d = run(tmp_path, height([4.0]), height([5.0]), INT_SRC)
    assert one(d, "dep.Rect.get_height").fails


def test_a_shadowed_builtin_is_no_projection(tmp_path):
    src = "int = str\n" + INT_SRC
    d = run(tmp_path, [ev("dep.Rect.get_height", {"self": {"__opaque__": "dep.Rect"}},
                          result=4.0, line=3)],
            [ev("dep.Rect.get_height", {"self": {"__opaque__": "dep.Rect"}}, result=4,
                line=3)], src)
    assert one(d, "dep.Rect.get_height").fails


def test_a_truth_test_compares_truthiness(tmp_path):
    src = "def main(r):\n    if r.ok():\n        return 1\n"

    def ok(v):
        return ev("dep.R.ok", {"self": {"__opaque__": "dep.R"}}, result=v, line=2)

    assert not one(run(tmp_path, [ok(True)], [ok(1)], src), "dep.R.ok").fails
    assert one(run(tmp_path, [ok(True)], [ok(0)], src), "dep.R.ok").fails


# ── 5. numeric drift ──────────────────────────────────────────────────────────────────────

PRED_SRC = "def main(m, X):\n    y = m.predict(X)\n"


def pred(values, dtype="float64", x=(1.0, 2.0)):
    return ev("dep.Model.predict",
              {"self": {"__opaque__": "dep.Model"}, "X": {"__list__": list(x)}},
              result={"__structure__": "numpy.ndarray", "dtype": dtype, "shape": [len(values)],
                      "values": {"__list__": list(values)}}, line=2)


def test_floats_within_rounding_are_numeric_drift_and_stamped(tmp_path):
    a = 5.999999999999991
    b = 6.000000000000003
    d = run(tmp_path, [pred([a, 1.0])], [pred([b, 1.0])], PRED_SRC)
    e = one(d, "dep.Model.predict")
    assert e.category is FactCategory.DRIFT and e.level.value == "numeric"
    assert e.absorbed == ["numeric"] and e.compared_equal
    assert e.numeric["values"] == 1 and 1 <= e.numeric["max_ulps"] <= 1024
    assert d.numeric_drift["bound_ulps"] == 1024 and d.numeric_drift["values"] == 1
    assert d.verdict is VerificationVerdict.PASS


def test_mutation_floats_apart_by_1e_9_relative_fail(tmp_path):
    d = run(tmp_path, [pred([6.0])], [pred([6.0 * (1 + 1e-9)])], PRED_SRC)
    assert one(d, "dep.Model.predict").fails


def test_a_4_ulp_difference_stays_drift(tmp_path):
    import math
    x = 6.0
    d = run(tmp_path, [pred([x])], [pred([x + 4 * math.ulp(x)])], PRED_SRC)
    e = one(d, "dep.Model.predict")
    assert not e.fails and e.numeric["max_ulps"] == 4


def test_mutation_float32_against_float64_with_near_values_fails(tmp_path):
    d = run(tmp_path, [pred([0.38], "float64")], [pred([0.38000001311302185], "float32")],
            PRED_SRC)
    assert one(d, "dep.Model.predict").fails


def test_float32_compares_at_its_own_precision(tmp_path):
    import struct

    def f32(x):
        return struct.unpack("f", struct.pack("f", x))[0]

    a = f32(0.1)
    b = struct.unpack("f", struct.pack("I", struct.unpack("I", struct.pack("f", a))[0] + 3))[0]
    ok = run(tmp_path, [pred([a], "float32")], [pred([b], "float32")], PRED_SRC)
    assert not one(ok, "dep.Model.predict").fails
    far = run(tmp_path, [pred([a], "float32")], [pred([a * (1 + 1e-4)], "float32")], PRED_SRC)
    assert one(far, "dep.Model.predict").fails


def test_zero_signs_and_scales(tmp_path):
    assert not one(run(tmp_path, [pred([0.0])], [pred([-0.0])], PRED_SRC),
                   "dep.Model.predict").fails
    # a scalar zero against a tiny non-zero has no scale: exact
    src = "def main(m):\n    y = m.score()\n"
    zero = [ev("dep.Model.score", {"self": {"__opaque__": "dep.Model"}}, result=0.0, line=2)]
    tiny = [ev("dep.Model.score", {"self": {"__opaque__": "dep.Model"}}, result=1e-300, line=2)]
    assert one(run(tmp_path, zero, tiny, src), "dep.Model.score").fails
    # inside an array the array's largest magnitude is the scale
    assert not one(run(tmp_path, [pred([1.0, 0.0])], [pred([1.0, 1e-17])], PRED_SRC),
                   "dep.Model.predict").fails
    # across the sign boundary, as any distance
    assert one(run(tmp_path, [pred([1e-3])], [pred([-1e-3])], PRED_SRC),
               "dep.Model.predict").fails


def test_float_ulps_zero_is_exact_and_strict_reviews(tmp_path):
    a, b = 5.999999999999991, 6.000000000000003
    exact = run(tmp_path, [pred([a])], [pred([b])], PRED_SRC, float_ulps=0)
    assert one(exact, "dep.Model.predict").fails and exact.float_ulps == 0
    strict = run(tmp_path, [pred([a])], [pred([b])], PRED_SRC, strict=True)
    e = one(strict, "dep.Model.predict")
    assert not e.fails and e.review and e.review.startswith("numeric drift")
    assert strict.verdict is VerificationVerdict.INCONCLUSIVE


def test_inputs_within_rounding_are_the_same_input(tmp_path):
    x_old, x_new = (1.0, 2.0000000000000004), (1.0, 2.0)
    d = run(tmp_path, [pred([3.0], x=x_old)], [pred([3.0], x=x_new)], PRED_SRC)
    e = one(d, "dep.Model.predict")
    assert "matched-modulo-numeric" in e.notes and not e.review and not e.fails
    changed = run(tmp_path, [pred([3.0], x=x_old)], [pred([3.5], x=x_new)], PRED_SRC)
    e = one(changed, "dep.Model.predict")
    assert e.fails and not e.review


# ── 6. site identity ──────────────────────────────────────────────────────────────────────

def test_an_inlined_comprehension_takes_its_function_from_the_source(tmp_path):
    src = ("import dep\n"
           "def test_bad(client):\n"
           "    assert [client.post(b).status for b in [1, 2]] == [422, 422]\n")
    old = [ev("dep.Client.post", {"b": 1}, result=422, func="<listcomp>")]
    new = [ev("dep.Client.post", {"b": 1}, result=422, func="test_bad")]
    e = one(run(tmp_path, old, new, src), "dep.Client.post")
    assert e.category is FactCategory.EQUAL


def test_a_subclass_constructor_is_an_observation_of_the_subclass(tmp_path):
    import json

    from pymolt.verify.report import _load_observations
    p = tmp_path / "t.jsonl"
    p.write_text(json.dumps({"t": "return", "q": "dep.Response.__init__", "v": 2,
                             "sym": "dep.Response.__init__", "own": "dep.PlainTextResponse",
                             "in": {"bound": {}}, "result": None,
                             "where": {"file": "/proj/app.py", "line": 3, "func": "f"}}) + "\n")
    view = _load_observations(p)
    alias = view.observations["dep.PlainTextResponse.__init__"]
    assert alias.alias and alias.site_lines == {("/proj/app.py", 3)}
    assert not view.observations["dep.Response.__init__"].alias


def test_a_relocated_decorator_closure_pairs_on_its_decorated_statement(tmp_path):
    src = "from dep import bp\n@bp.route('/items')\ndef list_items():\n    return 1\n"
    old = [ev("dep.scaffold.decorator", {"f": {"__callable__": "app.list_items"}},
              result={"__callable__": "app.list_items"}, line=3, func="<module>")]
    new = [ev("dep.sansio.scaffold.Scaffold.route.<locals>.decorator",
              {"f": {"__callable__": "app.list_items"}},
              result={"__callable__": "app.list_items"}, line=2, func="<module>")]
    d = run(tmp_path, old, new, src)
    (e,) = d.entries
    assert "attribution-moved" in e.notes and not e.fails


ABORT_SRC = "from dep import abort\ndef admin():\n    abort(403)\n"


def test_a_moved_callee_binding_its_argument_differently_pairs(tmp_path):
    old = [ev("werkzeug.exceptions.abort", {"status": 403}, raised="dep.Forbidden")]
    new = [ev("flask.helpers.abort", {"code": 403}, raised="dep.Forbidden")]
    d = run(tmp_path, old, new, ABORT_SRC)
    (e,) = d.entries
    assert not e.fails and e.new_symbol == "flask.helpers.abort"
    assert d.verdict is VerificationVerdict.PASS


def test_mutation_a_moved_callee_given_another_value_fails(tmp_path):
    old = [ev("werkzeug.exceptions.abort", {"status": 403}, raised="dep.Forbidden")]
    new = [ev("flask.helpers.abort", {"code": 404}, raised="dep.NotFound")]
    assert run(tmp_path, old, new, ABORT_SRC).verdict is VerificationVerdict.FAIL


RNG_SRC = "import numpy as np\ndef churn():\n    rng = np.random.RandomState(0)\n    return rng\n"


def concatenate(values):
    return ev("numpy.core.multiarray.concatenate", {"arrays": {"__list__": values}},
              result={"__list__": values})


def test_a_call_compiled_code_made_on_the_line_is_not_compared(tmp_path):
    rs = ev("numpy.random.mtrand.RandomState", extra_in={"__c_call__": True},
            result={"__opaque__": "unobserved:c-return"})
    rs["in"] = {"__c_call__": True}
    d = run(tmp_path, [concatenate([1, 2])], [concatenate([7, 9]), rs], RNG_SRC)
    assert not any(e.symbol.endswith("concatenate") for e in d.entries)
    assert d.via == 2 and d.absorbed.get("via") == 2
    # (the compiled call itself is new-side only here: the real backends explain that)
    assert [e.symbol for e in d.failures()] == ["numpy.random.mtrand.RandomState"]
    assert "numpy.core.multiarray.concatenate" in d.via_symbols


def test_mutation_an_unnamed_call_with_no_compiled_call_on_its_line_stays(tmp_path):
    d = run(tmp_path, [concatenate([1, 2])], [concatenate([7, 9])], RNG_SRC)
    assert d.via == 0 and d.verdict is not VerificationVerdict.PASS


def test_a_numpy_dispatcher_is_not_compared(tmp_path):
    src = "import numpy as np\ndef f(v):\n    return np.round(v, 6)\n"
    old = [ev("numpy.core.fromnumeric._round_dispatcher", {"a": 1.0}, result={"__tuple__": [1.0]}),
           ev("numpy.core.fromnumeric.round", {"a": 1.0}, result=1.0)]
    new = [ev("numpy._core.fromnumeric._round_dispatcher", {"a": 1.0}, result={"__tuple__": [2.0]}),
           ev("numpy._core.fromnumeric.round", {"a": 1.0}, result=1.0)]
    d = run(tmp_path, old, new, src)
    assert d.via == 2 and d.verdict is VerificationVerdict.PASS
