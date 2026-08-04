"""WrapBackend: targeted-wrap observation of named dependency symbols."""
import importlib
import sys
import types

import pytest

from pymolt.verify._sinks import MemorySink
from pymolt.verify._wrap import WrapBackend

_SRC = """
def jsonify(x):
    return {"j": x}

def boom():
    raise ValueError("bad")

VERSION = "1.0"

class Schema:
    def __init__(self, name):
        self.name = name
"""


@pytest.fixture
def dep():
    mod = types.ModuleType("wdep")
    exec(compile(_SRC, "<wdep>", "exec"), mod.__dict__)
    sys.modules["wdep"] = mod
    yield mod
    sys.modules.pop("wdep", None)


def _run(symbols, body, source="tests.verify.test_wrap", **kw):
    sink = MemorySink()
    be = WrapBackend(symbols, sink, source=source, **kw)
    be.start()
    try:
        body()
    finally:
        be.stop()
    return sink, be


def test_wraps_function_and_captures_args_result_site(dep):
    def body():
        dep.jsonify({"a": 1})

    sink, _ = _run(["wdep.jsonify"], body, source=__name__)
    (rec,) = sink.records.values()
    assert rec["qualname"] == "wdep.jsonify"
    assert rec["inputs"] == {"bound": {"x": {"__dict__": [["a", 1]]}}}
    assert rec["result"] == {"__dict__": [["j", {"__dict__": [["a", 1]]}]]}
    assert rec["where"]["file"].endswith("test_wrap.py")


def test_wraps_class_via_init_keeps_class_and_isinstance(dep):
    holder = {}

    def body():
        holder["s"] = dep.Schema("user")

    sink, _ = _run(["wdep.Schema"], body, source=__name__)
    assert isinstance(holder["s"], dep.Schema)        # class stays a class
    (rec,) = sink.records.values()
    assert rec["qualname"] == "wdep.Schema"
    # self binds to the real signature (opaque), named arg captured
    assert rec["inputs"]["bound"]["name"] == "user"
    assert rec["inputs"]["bound"]["self"] == {"__opaque__": "wdep.Schema"}
    assert rec["result"] == {"__opaque__": "wdep.Schema"}  # construction -> instance shape


def test_wraps_python_raise(dep):
    def body():
        with pytest.raises(ValueError):
            dep.boom()

    sink, _ = _run(["wdep.boom"], body, source=__name__)
    (rec,) = sink.records.values()
    assert rec["raised"] == "ValueError" and rec["result"] is None


def test_non_callable_and_missing_are_skipped_not_dropped(dep):
    _, be = _run(["wdep.VERSION", "wdep.NOPE"], lambda: None, source=__name__)
    reasons = dict(be.skipped)
    assert reasons["wdep.VERSION"] == "not-callable"
    assert reasons["wdep.NOPE"] == "attr-missing"


def test_near_filter_records_only_our_code(dep):
    # source names a package that is NOT this test module -> our call is "out of scope"
    def body():
        dep.jsonify(1)

    sink, _ = _run(["wdep.jsonify"], body, source="some.other.pkg")
    assert sink.records == {}                          # caller (test) not in source -> not recorded


def test_restore_on_stop(dep):
    sink, _ = _run(["wdep.jsonify"], lambda: dep.jsonify(1), source=__name__)
    assert not hasattr(dep.jsonify, "__wrapped__")     # original restored


def test_transparent_passthrough_returns_real_value(dep):
    out = {}

    def body():
        out["r"] = dep.jsonify("z")

    _run(["wdep.jsonify"], body, source=__name__)
    assert out["r"] == {"j": "z"}                       # wrapper returns the real result unchanged


_METHODS_SRC = """
class Flask:
    def add_url_rule(self, rule, ep):
        return (rule, ep)

    @staticmethod
    def make(x):
        return x * 2

    @classmethod
    def of(cls, y):
        return (cls.__name__, y)
"""


@pytest.fixture
def mdep():
    mod = types.ModuleType("mdep")
    exec(compile(_METHODS_SRC, "<mdep>", "exec"), mod.__dict__)
    sys.modules["mdep"] = mod
    yield mod
    sys.modules.pop("mdep", None)


def test_wraps_instance_static_and_class_methods(mdep):
    def body():
        mdep.Flask().add_url_rule("/x", "ep")
        mdep.Flask.make(5)
        mdep.Flask.of(9)

    sink, _ = _run(["mdep.Flask.add_url_rule", "mdep.Flask.make", "mdep.Flask.of"],
                   body, source=__name__)
    recs = {r["qualname"]: r for r in sink.records.values()}
    add = recs["mdep.Flask.add_url_rule"]["inputs"]["bound"]
    assert add["rule"] == "/x"
    assert add["self"] == {"__opaque__": "mdep.Flask"}
    assert recs["mdep.Flask.make"]["inputs"]["bound"] == {"x": 5}        # staticmethod: no self
    assert recs["mdep.Flask.of"]["result"] == {"__tuple__": ["Flask", 9]}


def test_method_descriptors_preserved_after_restore(mdep):
    _run(["mdep.Flask.make", "mdep.Flask.of"], lambda: None, source=__name__)
    assert isinstance(mdep.Flask.__dict__["make"], staticmethod)
    assert isinstance(mdep.Flask.__dict__["of"], classmethod)


# ── hybrid ─────────────────────────────────────────────────────────────────────
_HYBRID_SRC = """
def f(x):
    return x + 1

def g(y):
    return y * 2

def boom():
    raise ValueError("e")
"""


@pytest.fixture
def hdep():
    mod = types.ModuleType("hdep")
    exec(compile(_HYBRID_SRC, "<hdep>", "exec"), mod.__dict__)
    sys.modules["hdep"] = mod
    yield mod
    sys.modules.pop("hdep", None)


def test_hybrid_dedups_wrapped_and_completes_with_setprofile(hdep):
    from pymolt.verify._wrap import HybridBackend

    sink = MemorySink()
    be = HybridBackend(["hdep.f", "hdep.boom"], sink, source=__name__)
    be.start()
    try:
        hdep.f(10)                 # wrapped
        hdep.g(5)                  # NOT wrapped -> setprofile completeness
        with pytest.raises(ValueError):
            hdep.boom()            # wrapped + raises
    finally:
        be.stop()
    recs = {r["qualname"]: r for r in sink.records.values()}
    assert recs["hdep.f"]["count"] == 1            # wrapped once, setprofile didn't double it
    assert "hdep.g" in recs                        # setprofile caught the un-wrapped call
    assert recs["hdep.boom"]["raised"] == "ValueError"  # wrap keeps the true raise


def test_import_hook_wraps_module_imported_after_start(tmp_path):
    (tmp_path / "lhd.py").write_text("def greet(n):\n    return 'hi ' + n\n")
    sys.path.insert(0, str(tmp_path))
    sink = MemorySink()
    be = WrapBackend(["lhd.greet"], sink, source=__name__)
    be.start()
    try:
        assert "lhd" not in sys.modules               # not imported yet
        lhd = importlib.import_module("lhd")          # import hook must wrap on load
        assert hasattr(lhd.greet, "__wrapped__")
        lhd.greet("ann")
    finally:
        be.stop()
        sys.modules.pop("lhd", None)
        sys.path.remove(str(tmp_path))
    assert any(r["qualname"] == "lhd.greet" for r in sink.records.values())
