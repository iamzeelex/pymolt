import pickle
import sys

from pymolt.verify.reinvoke import reinvoke

PY = sys.executable


def test_reinvoke_returns_normalized_value():
    r = reinvoke(PY, "os.path.join", args=["a", "b"])
    assert r.outcome == "returned"
    assert r.value == "a/b"


def test_reinvoke_captures_raise():
    r = reinvoke(PY, "json.loads", args=["{not json"])
    assert r.outcome == "raised"
    assert r.error == "JSONDecodeError"


def test_reinvoke_resolves_dotted_qualname():
    r = reinvoke(PY, "json.dumps", args=[{"a": 1}])
    assert r.outcome == "returned"
    assert r.value == '{"a": 1}'


def test_reinvoke_from_pickle_blob(tmp_path):
    blob = tmp_path / "args.pkl"
    with open(blob, "wb") as f:
        pickle.dump({"args": [[3, 1, 2]]}, f)
    r = reinvoke(PY, "builtins.sorted", args_pickle=blob)
    assert r.outcome == "returned"
    # normalized form (same normalizer as the boundary tracer; lists are wrapped)
    assert r.value == {"__list__": [1, 2, 3]}


def test_reinvoke_timeout():
    r = reinvoke(PY, "time.sleep", args=[5], timeout=0.5)
    assert r.outcome == "timeout"
