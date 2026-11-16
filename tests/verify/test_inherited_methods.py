"""Methods a dependency class inherits from the stdlib (FU6): ``HTTPHeaderDict.get`` is
``Mapping.get``, so the callee's code is outside every target, yet the call from our code
is a boundary contact, named after the receiver's class.

Runtime-layer test: pure stdlib + pytest, 3.6-compatible syntax."""
import sys
import types

import pytest

from pymolt.verify._backend import (
    HAS_MONITORING,
    PrefixMatcher,
    SetProfileBackend,
)
from pymolt.verify._sinks import MemorySink

if HAS_MONITORING:
    from pymolt.verify._monitoring import MonitoringBackend

MOD = "vfakeinh"
SRC = '''
import collections.abc

class Hdrs(collections.abc.Mapping):
    def __init__(self, d):
        self._d = dict(d)
    def __getitem__(self, k):
        return self._d[k]
    def __iter__(self):
        return iter(self._d)
    def __len__(self):
        return len(self._d)

class Own(Hdrs):
    def get(self, k, default=None):
        return "own"

class MyDict(dict):
    pass
'''

BACKENDS = [SetProfileBackend]
if HAS_MONITORING:
    BACKENDS.append(MonitoringBackend)


@pytest.fixture
def dep():
    mod = types.ModuleType(MOD)
    exec(compile(SRC, "<" + MOD + ">", "exec"), mod.__dict__)
    sys.modules[MOD] = mod
    yield mod
    sys.modules.pop(MOD, None)


def _run(cls, body, target=MOD):
    sink = MemorySink()
    be = cls(PrefixMatcher([target]), sink, source=__name__)
    be.start()
    try:
        body()
    finally:
        be.stop()
    return sink


def _syms(sink):
    return sorted({r["qualname"] if not r.get("sym") else r["sym"] for r in sink.records.values()})


@pytest.mark.parametrize("cls", BACKENDS)
def test_inherited_python_method_is_named_by_the_receiver_class(cls, dep):
    h = dep.Hdrs({"a": 1})

    def body():
        for _ in range(5):
            h.get("a")

    sink = _run(cls, body)
    syms = _syms(sink)
    assert MOD + ".Hdrs.get" in syms
    rec = [r for r in sink.records.values() if r.get("sym") == MOD + ".Hdrs.get"][0]
    assert rec["result"] == 1
    assert rec["where"]["func"] == "body"


@pytest.mark.parametrize("cls", BACKENDS)
def test_other_receivers_of_the_same_stdlib_method_are_not_contacts(cls, dep):
    import collections

    chain = collections.ChainMap({"a": 1})
    plain = {"a": 1}
    other = types.SimpleNamespace(get=plain.get)

    def body():
        chain.get("a")
        plain.get("a")
        other.get("a")

    sink = _run(cls, body)
    assert not any(".get" in (r.get("sym") or "") for r in sink.records.values())


@pytest.mark.parametrize("cls", BACKENDS)
def test_a_subclass_override_is_its_own_python_method(cls, dep):
    o = dep.Own({"a": 1})
    sink = _run(cls, lambda: o.get("a"))
    assert MOD + ".Own.get" in _syms(sink)


@pytest.mark.parametrize("cls", BACKENDS)
def test_builtin_method_on_a_dependency_subclass_of_a_builtin(cls, dep):
    d = dep.MyDict(a=1)
    sink = _run(cls, lambda: d.get("a"))
    assert MOD + ".MyDict.get" in _syms(sink)


@pytest.mark.parametrize("cls", BACKENDS)
def test_calls_from_dependency_code_are_not_the_boundary(cls, dep):
    h = dep.Hdrs({"a": 1})
    sink = _run(cls, lambda: list(h.items()))   # Mapping.items runs Mapping code, ItemsView
    assert MOD + ".Hdrs.items" in _syms(sink)
    assert MOD + ".Hdrs.__getitem__" not in _syms(sink)  # reached from stdlib, not from us


@pytest.mark.parametrize("cls", BACKENDS)
def test_no_double_record_when_the_stdlib_module_is_a_target(cls, dep):
    h = dep.Hdrs({"a": 1})
    sink = MemorySink()
    be = cls(PrefixMatcher([MOD, "_collections_abc"]), sink, source=__name__)
    be.start()
    try:
        h.get("a")
    finally:
        be.stop()
    gets = [r for r in sink.records.values() if (r.get("sym") or "").endswith(".get")]
    assert len(gets) == 1       # one record, not one per rule
