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
