"""Interaction contract: compare by shape (type/structure), ignore concrete values."""
import json

from pymolt.verify.contract import build_contract_diff, shape_of, shape_str


def test_shape_of_reduces_values_to_types():
    assert shape_of(1) == "int"
    assert shape_of("x") == "str"
    assert shape_of(True) == "bool"
    assert shape_of(None) == "null"
    assert shape_of({"__bytes__": "00ff"}) == "bytes"
    assert shape_of({"__opaque__": "flask.Request"}) == {"__opaque__": "flask.Request"}


def test_shape_of_containers_keep_structure_drop_leaf_values():
    # list collapses to a union of element shapes (values gone)
    assert shape_str(shape_of({"__list__": [1, 2, 3]})) == "list[int]"
    # dict keeps field NAMES, values become shapes
    s = shape_of({"__dict__": [["a", 1], ["b", "x"]]})
    assert shape_str(s) == "dict{a:int,b:str}"
    # tuple keeps arity + positions
    assert shape_str(shape_of({"__tuple__": [1, "x"]})) == "tuple(int,str)"


def _jsonl(tmp_path, name, events):
    p = tmp_path / name
    with p.open("w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")
    return str(p)


def _ret(q, inputs, result):
    return {"t": "return", "q": q, "in": inputs, "result": result}


def test_value_change_with_same_shape_is_NOT_a_contract_change(tmp_path):
    # the headline: returns a str in both versions; only the string itself differs -> stable
    q, in_ = "flask.get_root_path", {"bound": {"x": 1}}
    old = _jsonl(tmp_path, "o.jsonl", [_ret(q, in_, "/old/path")])
    new = _jsonl(tmp_path, "n.jsonl", [_ret(q, in_, "/new/path")])
    d = build_contract_diff(old, new)
    assert d.is_clean() is True
    assert d.counts()["result_changed"] == 0


def test_type_change_is_a_contract_change(tmp_path):
    old = _jsonl(tmp_path, "o.jsonl", [_ret("dep.f", {"bound": {"x": 1}}, 42)])       # int
    new = _jsonl(tmp_path, "n.jsonl", [_ret("dep.f", {"bound": {"x": 1}}, "42")])      # str
    d = build_contract_diff(old, new)
    assert d.counts()["result_changed"] == 1
    assert d.result_changed[0]["old"] == "int" and d.result_changed[0]["new"] == "str"


def test_structure_change_is_a_contract_change(tmp_path):
    old = _jsonl(tmp_path, "o.jsonl",
                 [_ret("dep.f", {"bound": {}}, {"__dict__": [["a", 1]]})])
    new = _jsonl(tmp_path, "n.jsonl",
                 [_ret("dep.f", {"bound": {}}, {"__dict__": [["a", 1], ["b", 2]]})])
    d = build_contract_diff(old, new)
    assert d.counts()["result_changed"] == 1  # dict{a:int} -> dict{a:int,b:int}


def test_return_to_raise_is_raise_changed(tmp_path):
    old = _jsonl(tmp_path, "o.jsonl", [_ret("dep.f", {"bound": {}}, 1)])
    new = _jsonl(tmp_path, "n.jsonl",
                 [{"t": "raise", "q": "dep.f", "in": {"bound": {}}, "raised": "ValueError"}])
    d = build_contract_diff(old, new)
    assert d.counts()["raise_changed"] == 1


def test_same_input_shape_different_concrete_inputs_aggregate(tmp_path):
    # f(1) and f(2) share input-shape "int" -> one contract; same output shape -> stable
    old = _jsonl(tmp_path, "o.jsonl", [
        _ret("dep.f", {"bound": {"x": 1}}, 10),
        _ret("dep.f", {"bound": {"x": 2}}, 20),
    ])
    new = _jsonl(tmp_path, "n.jsonl", [_ret("dep.f", {"bound": {"x": 9}}, 90)])
    d = build_contract_diff(old, new)
    assert d.is_clean() is True


def test_disappeared_and_appeared_by_contract(tmp_path):
    old = _jsonl(tmp_path, "o.jsonl", [_ret("dep.gone", {"bound": {}}, 1)])
    new = _jsonl(tmp_path, "n.jsonl", [_ret("dep.new", {"bound": {}}, 1)])
    d = build_contract_diff(old, new)
    assert d.counts()["disappeared"] == 1 and d.counts()["appeared"] == 1
