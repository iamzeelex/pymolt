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
