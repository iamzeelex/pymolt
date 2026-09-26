from __future__ import annotations

import json

import pytest

from pymolt.verify.comparators import ComparatorError, compare_traces
from pymolt.verify.models import BoundaryDiff, VerificationVerdict
from pymolt.verify.report import build_contract_report


def _trace(path, result):
    path.write_text(
        json.dumps({
            "t": "return",
            "q": "pkg.api.read",
            "in": {"value": 1},
            "result": result,
            "where": {"file": "app.py", "line": 4},
        })
        + "\n",
        encoding="utf-8",
    )


def test_exact_and_shape_profiles_make_the_tradeoff_explicit(tmp_path):
    old = tmp_path / "old.jsonl"
    new = tmp_path / "new.jsonl"
    _trace(old, {"value": 1})
    _trace(new, {"value": 2})

    assert compare_traces(str(old), str(new), profile="exact").verdict is (
        VerificationVerdict.FAIL
    )
    assert compare_traces(str(old), str(new), profile="shape").verdict is (
        VerificationVerdict.PASS
    )


def test_custom_profile_uses_the_common_boundary_diff_contract(tmp_path):
    old = tmp_path / "old.jsonl"
    new = tmp_path / "new.jsonl"
    _trace(old, 1)
    _trace(new, 2)

    def tolerate_values(old_path: str, new_path: str):
        assert old_path.endswith("old.jsonl")
        assert new_path.endswith("new.jsonl")
        return {"appeared": [{"qualname": "pkg.api.extra"}]}

    result = compare_traces(
        str(old), str(new), profile="custom", custom=tolerate_values
    )

    assert isinstance(result, BoundaryDiff)
    assert result.verdict is VerificationVerdict.PASS
    assert result.appeared == [{"qualname": "pkg.api.extra"}]


def test_custom_profile_requires_an_explicit_callable(tmp_path):
    with pytest.raises(ComparatorError, match="requires"):
        compare_traces("old", "new", profile="custom")


def test_invalid_profile_is_actionable():
    with pytest.raises(ComparatorError, match="exact, shape, custom"):
        compare_traces("old", "new", profile="fuzzy")


def test_contract_report_uses_selected_comparator_profile(tmp_path):
    old = tmp_path / "old.jsonl"
    new = tmp_path / "new.jsonl"
    _trace(old, {"value": 1})
    _trace(new, {"value": 2})

    exact = build_contract_report(
        tmp_path, trace=new, against=old, comparator_profile="exact"
    )
    shape = build_contract_report(
        tmp_path, trace=new, against=old, comparator_profile="shape"
    )

    assert exact.diff_clean is False
    assert shape.diff_clean is True
