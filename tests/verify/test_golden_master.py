"""Golden-master snapshots + value-level diff, sharing the tracer's normalization."""
from pymolt.verify.golden_master import diff_snapshots, snapshot


def test_snapshot_evaluates_callables_and_normalizes():
    snap = snapshot({"a": lambda: [3, 1, 2], "b": 5}, version="1.0")
    assert snap["version"] == "1.0"
    assert snap["values"]["a"] == {"__list__": [3, 1, 2]}
    assert snap["values"]["b"] == 5


def test_snapshot_point_that_raises_becomes_opaque_marker():
    def boom():
        raise RuntimeError("x")

    snap = snapshot({"p": boom}, version="1.0")
    assert snap["values"]["p"] == {"__opaque__": "raised:RuntimeError"}


def test_diff_detects_changed_value():
    old = snapshot({"p": 1}, "1.0")
    new = snapshot({"p": 2}, "2.0")
    d = diff_snapshots(old, new)
    assert d.is_clean() is False
    assert d.changed[0]["point"] == "p"


def test_diff_clean_when_identical():
    old = snapshot({"p": [1, 2]}, "1.0")
    new = snapshot({"p": [1, 2]}, "2.0")
    assert diff_snapshots(old, new).is_clean() is True


def test_diff_reports_missing_point():
    old = snapshot({"p": 1, "q": 2}, "1.0")
    new = snapshot({"p": 1}, "2.0")
    d = diff_snapshots(old, new)
    assert "q" in d.missing


def test_diff_skips_opaque_points():
    old = snapshot({"p": object()}, "1.0")   # opaque
    new = snapshot({"p": object()}, "2.0")
    d = diff_snapshots(old, new)
    assert "p" in d.skipped
    assert d.is_clean() is True


def test_a_snapshot_from_before_dict_order_was_recorded_is_not_a_reorder():
    new = snapshot({"row": {"name": "a", "method": "GET", "age": 1}}, "v2")
    old = {"version": "v1", "values": {"row": {"__dict__": [["age", 1], ["method", "GET"],
                                                            ["name", "a"]]}}}
    assert "__order__" in new["values"]["row"]           # insertion order is recorded now
    assert diff_snapshots(old, new).is_clean()


def test_a_key_order_change_shows_when_both_snapshots_recorded_it():
    old = snapshot({"row": {"name": "a", "method": "GET", "age": 1}}, "v1")
    new = snapshot({"row": {"age": 1, "name": "a", "method": "GET"}}, "v2")
    assert not diff_snapshots(old, new).is_clean()
