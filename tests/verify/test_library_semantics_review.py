"""Regression tests for the two reviews of verify's Stage 1 semantics: each reproduces a
confirmed defect (most of them a false PASS) and asserts the honest verdict."""

from _semantics import ev, one, run

from pymolt.verify.models import VerificationVerdict

PASS, FAIL = VerificationVerdict.PASS, VerificationVerdict.FAIL


# ── 1. a src layout's own records are values ──────────────────────────────────────────────

def test_a_src_layout_project_record_is_a_value(tmp_path):
    models = "from dataclasses import dataclass\n@dataclass\nclass Order:\n    id: int\n    total: float\n"
    app = "import dep\ndef main(raw):\n    o = dep.parse(raw)\n    return o.id\n"
    src = {"src/mypkg/__init__.py": "", "src/mypkg/models.py": models, "src/mypkg/app.py": app}

    def order(total):
        return {"__dataclass__": "mypkg.models.Order", "fields": [["id", 1], ["total", total]]}

    d = run(tmp_path, [ev("dep.parse", {"raw": "x"}, result=order(10.0), file="src/mypkg/app.py")],
            [ev("dep.parse", {"raw": "x"}, result=order(99.0), file="src/mypkg/app.py")], src)
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
    d = run(tmp_path, [query("dep.models.JSONResponse")], [query("dep.models.StreamingPage")], src)
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
