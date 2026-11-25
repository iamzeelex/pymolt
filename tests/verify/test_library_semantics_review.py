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
