"""BoundaryDiff: category classification + JSONL<->JSON fold equivalence."""
from pymolt.verify.diff import build_boundary_diff


def _rec(qual, inputs, result=None, raised=None, count=1):
    return {"qualname": qual, "inputs": inputs, "result": result, "raised": raised, "count": count}


def test_categorizes_all_change_kinds(write_aggregated):
    old = write_aggregated("old.json", [
        _rec("flask.f", {"bound": {"x": 1}}, result=1),
        _rec("flask.g", {"bound": {}}, result=2),
        _rec("flask.gone", {"bound": {}}, result=9),
    ])
    new = write_aggregated("new.json", [
        _rec("flask.f", {"bound": {"x": 1}}, result=99),                 # result_changed
        _rec("flask.g", {"bound": {}}, result=None, raised="ValueError"),  # raise_changed
        _rec("flask.new", {"bound": {}}, result=3),                       # appeared
    ])
    d = build_boundary_diff(old, new)
    assert d.counts() == {"disappeared": 1, "result_changed": 1, "raise_changed": 1,
                          "appeared": 1, "skipped_opaque": 0}
    assert d.is_clean() is False


def test_clean_when_identical(write_aggregated):
    recs = [_rec("flask.f", {"bound": {"x": 1}}, result=1)]
    old = write_aggregated("old.json", recs)
    new = write_aggregated("new.json", recs)
    d = build_boundary_diff(old, new)
    assert d.is_clean() is True


def test_opaque_and_nondeterministic_are_skipped_not_compared(write_aggregated):
    old = write_aggregated("old.json", [
        _rec("flask.f", {"bound": {}}, result={"__nondeterministic__": True}),
        _rec("flask.h", {"bound": {}}, result={"__opaque__": "socket.socket"}),
    ])
    new = write_aggregated("new.json", [
        _rec("flask.f", {"bound": {}}, result=1),
        _rec("flask.h", {"bound": {}}, result={"__opaque__": "socket.socket"}),
    ])
    d = build_boundary_diff(old, new)
    assert len(d.skipped_opaque) == 2
    assert d.is_clean() is True  # skipped is an honesty marker, not clean-blocking


def test_jsonl_and_json_fold_to_same_diff(write_aggregated, write_jsonl):
    old = write_aggregated("old.json", [
        _rec("flask.f", {"bound": {"x": 1}}, result=1),
        _rec("flask.g", {"bound": {}}, result=2),
    ])
    # the SAME 'new' recording, once as aggregated JSON and once as streaming JSONL
    new_json = write_aggregated("new.json", [
        _rec("flask.f", {"bound": {"x": 1}}, result=99),
        _rec("flask.g", {"bound": {}}, result=2),
    ])
    new_jsonl = write_jsonl("new.jsonl", [
        {"t": "return", "q": "flask.f", "in": {"bound": {"x": 1}}, "result": 99},
        {"t": "return", "q": "flask.g", "in": {"bound": {}}, "result": 2},
    ])
    from_json = build_boundary_diff(old, new_json).counts()
    from_jsonl = build_boundary_diff(old, new_jsonl).counts()
    assert from_json == from_jsonl


def test_jsonl_folds_repeated_events_and_detects_nondeterminism(write_aggregated, write_jsonl):
    old = write_aggregated("old.json", [_rec("flask.f", {"bound": {}}, result=1)])
    new = write_jsonl("new.jsonl", [
        {"t": "return", "q": "flask.f", "in": {"bound": {}}, "result": 1},
        {"t": "return", "q": "flask.f", "in": {"bound": {}}, "result": 2},  # same inputs differ
    ])
    d = build_boundary_diff(old, new)
    # folded 'new' result is nondeterministic -> comparison declined
    assert len(d.skipped_opaque) == 1
