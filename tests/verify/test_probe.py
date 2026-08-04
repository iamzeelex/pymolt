import json
import sys

from pymolt.verify.probe import _reconstruct, probe_trace

PY = sys.executable


def _trace(tmp_path, records):
    p = tmp_path / "trace.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    return p


def test_reconstruct_inverts_normalization():
    assert _reconstruct("x") == ("x", True)
    assert _reconstruct({"__list__": [1, 2]}) == ([1, 2], True)
    assert _reconstruct({"__dict__": [["a", 1]]}) == ({"a": 1}, True)
    assert _reconstruct({"__opaque__": "function"}) == (None, False)


def test_probe_stable(tmp_path):
    trace = _trace(tmp_path, [{
        "q": "json.dumps", "in": {"bound": {"obj": {"__dict__": [["a", 1]]}}},
        "result": '{"a": 1}', "t": "return",
    }])
    res = probe_trace(trace, PY)
    assert res["json.dumps"].status == "stable"
    assert res["json.dumps"].probed_contacts == 1


def test_probe_changed(tmp_path):
    trace = _trace(tmp_path, [{
        "q": "json.dumps", "in": {"bound": {"obj": {"__dict__": [["a", 1]]}}},
        "result": "OLD-OUTPUT", "t": "return",   # captured result differs from re-invoke
    }])
    res = probe_trace(trace, PY)
    assert res["json.dumps"].status == "changed"


def test_probe_opaque_inputs_skipped(tmp_path):
    trace = _trace(tmp_path, [{
        "q": "some.func", "in": {"bound": {"f": {"__opaque__": "function"}}},
        "result": None, "t": "return",
    }])
    res = probe_trace(trace, PY)
    assert res["some.func"].status == "opaque-inputs"
    assert res["some.func"].probed_contacts == 0
