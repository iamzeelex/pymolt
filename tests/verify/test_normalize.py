"""Normalization: cycles, opaque, nondeterminism markers, non-finite floats, truncation."""
from pymolt.verify import _normalize as nz


def test_safe_scalars_pass_through():
    assert nz.normalize(1) == 1
    assert nz.normalize("x") == "x"
    assert nz.normalize(True) is True
    assert nz.normalize(None) is None


def test_bytes_are_hex_encoded_stably():
    assert nz.normalize(b"\x00\xff") == {"__bytes__": "00ff"}


def test_non_finite_floats_are_opaque():
    assert nz.normalize(float("inf")) == {"__opaque__": "float-nonfinite"}
    assert nz.normalize(float("nan")) == {"__opaque__": "float-nonfinite"}
    assert nz.normalize(1.5) == 1.5  # finite floats pass through


def test_cycle_is_broken_not_infinite():
    d = {}
    d["self"] = d
    out = nz.normalize(d)
    assert out == {"__dict__": [["self", {"__cycle__": "dict"}]]}


def test_opaque_object_uses_typename_not_repr():
    class Widget:
        pass

    out = nz.normalize(Widget())
    assert out == {"__opaque__": out["__opaque__"]}
    assert out["__opaque__"].endswith(".Widget")


def test_depth_limit_collapses_to_opaque():
    deep = cur = {}
    for _ in range(nz.MAX_DEPTH + 2):
        cur["k"] = {}
        cur = cur["k"]
    # somewhere down the chain the value collapses to an opaque marker, not infinite nesting
    assert "__opaque__" in repr(nz.normalize(deep))


def test_sequence_truncation_is_marked():
    out = nz.normalize(list(range(nz.MAX_SEQ + 50)))
    assert out["__truncated__"] is True
    assert len(out["__list__"]) == nz.MAX_SEQ


def test_dicts_are_order_independent():
    a = nz.normalize({"x": 1, "y": 2})
    b = nz.normalize({"y": 2, "x": 1})
    assert a == b  # items are sorted by a stable key


def test_sets_are_normalized_and_sorted():
    assert nz.normalize({3, 1, 2}) == {"__set__": [1, 2, 3]}
