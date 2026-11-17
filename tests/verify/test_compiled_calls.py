"""Compiled (C / Cython) dependency calls and binding failures (Wave G, G1 and G4).

Small real C extensions from the stdlib stand in for a compiled dependency: ``_json`` /
``math`` / ``_datetime`` are made "the dependency" through a test matcher. Runtime-layer test:
pure stdlib + pytest, 3.6-compatible syntax.
"""
import datetime
import json
import math
import sys
import types

import pytest

from pymolt.verify._backend import (
    HAS_MONITORING,
    UNTYPED_RAISE,
    PrefixMatcher,
    SetProfileBackend,
    is_compiled_callable,
)
from pymolt.verify._sinks import JsonlSink, MemorySink
from pymolt.verify._wrap import CompiledWrap, WrapBackend

if HAS_MONITORING:
    from pymolt.verify._monitoring import MonitoringBackend

BACKENDS = [SetProfileBackend]
if HAS_MONITORING:
    BACKENDS.append(MonitoringBackend)
needs_monitoring = pytest.mark.skipif(not HAS_MONITORING, reason="sys.monitoring is 3.12+")


def _run(backend_cls, targets, body):
    sink = MemorySink()
    be = backend_cls(PrefixMatcher(targets), sink, source=__name__)
    be.start()
    try:
        body()
    finally:
        be.stop()
    return sink


def _by_name(sink):
    out = {}
    for rec in sink.records.values():
        out.setdefault(rec.get("sym") or rec["qualname"], []).append(rec)
    return out


@pytest.mark.parametrize("cls", BACKENDS)
def test_c_function_of_a_dependency_is_recorded(cls):
    sink = _run(cls, ["_json"], lambda: json.decoder.scanstring('"abc"', 1))
    (rec,) = _by_name(sink)["_json.scanstring"]
    assert rec["inputs"]["__c_call__"] is True
    assert rec["raised"] is None
    assert rec["result"] == {"__opaque__": "unobserved:c-return"}
    assert rec["where"]["func"] == "<lambda>"


@needs_monitoring
def test_monitoring_gives_arg0():
    sink = _run(MonitoringBackend, ["math"], lambda: math.sqrt(4))
    (rec,) = _by_name(sink)["math.sqrt"]
    assert rec["inputs"] == {"__c_call__": True, "arg0": 4}


@needs_monitoring
def test_monitoring_types_a_c_raise():
    def body():
        with pytest.raises(ValueError):
            math.sqrt(-1)

    sink = _run(MonitoringBackend, ["math"], body)
    (rec,) = _by_name(sink)["math.sqrt"]
    assert rec["raised"] == "ValueError"


def test_setprofile_records_a_c_raise_untyped():
    def body():
        with pytest.raises(ValueError):
            math.sqrt(-1)

    sink = _run(SetProfileBackend, ["math"], body)
    (rec,) = _by_name(sink)["math.sqrt"]
    assert rec["raised"] == UNTYPED_RAISE


@pytest.mark.parametrize("cls", BACKENDS)
def test_methods_of_a_c_type_are_named_by_their_type(cls):
    stamp = datetime.datetime(2020, 1, 2)
    sink = _run(cls, ["datetime"], lambda: (stamp.isoformat(), datetime.datetime.now()))
    names = _by_name(sink)
    assert "datetime.datetime.isoformat" in names
    assert "datetime.datetime.now" in names


@pytest.mark.parametrize("cls", BACKENDS)
def test_builtins_and_other_modules_are_not_dependencies(cls):
    def body():
        len([1])
        isinstance(1, int)
        math.sqrt(4)           # a stdlib C module that is not the target

    sink = _run(cls, ["_json"], body)
    assert sink.records == {}


@pytest.mark.parametrize("cls", BACKENDS)
def test_calls_from_a_dependency_are_not_the_boundary(cls):
    # json.loads runs C code (_json) from its own Python frames, never from ours.
    sink = _run(cls, ["_json"], lambda: json.loads("[1, 2]"))
    assert "_json.scanstring" not in _by_name(sink)
    assert not any(r["inputs"].get("__c_call__") for r in sink.records.values())


class _Fn:
    """Stands in for a Cython function: same type name, owned by a fake package."""

    def __call__(self, x=None, *rest):
        return x


_CyFn = type("cython_function_or_method", (_Fn,), {"__module__": "builtins"})


def _cython_like():
    fn = _CyFn()
    fn.__module__ = "vfakecy.speedups"
    fn.__qualname__ = "parse"
    return fn


def test_cython_like_function_is_a_compiled_callable():
    assert is_compiled_callable(_cython_like())
    assert not is_compiled_callable(len.__class__)


@needs_monitoring
def test_cython_like_function_is_recorded_under_monitoring():
    fn = _cython_like()
    sink = _run(MonitoringBackend, ["vfakecy"], lambda: fn(3))
    (rec,) = _by_name(sink)["vfakecy.speedups.parse"]
    assert rec["inputs"]["__c_call__"] is True


def _fake_compiled_module():
    mod = types.ModuleType("vfakecw")
    mod.__file__ = "/nowhere/vfakecw.cpython-3x-fake.so"
    holder = type("Model", (), {"__module__": "vfakecw"})
    fn = _CyFn()
    fn.__module__ = "vfakecw"
    fn.__qualname__ = "Model.load"
    holder.load = fn           # a "compiled method": instances bind it like a function
    mod.Model = holder
    free = _CyFn()
    free.__module__ = "vfakecw"
    free.__qualname__ = "helper"
    mod.helper = free
    return mod


def test_compiled_wrap_wraps_module_functions_and_class_methods():
    import importlib.machinery as machinery

    sink = MemorySink()
    mod = _fake_compiled_module()
    original = mod.helper
    wrap = CompiledWrap(sink, PrefixMatcher(["vfakecw"]), source=__name__)
    wrap._suffixes = tuple(machinery.EXTENSION_SUFFIXES) + (".so",)
    wrap.scan(mod)
    assert mod.helper is not original and mod.helper.__wrapped__ is original
    assert mod.helper(7) == 7
    assert "vfakecw.helper" in _by_name(sink)
    wrap.stop()
    assert mod.helper is original


@needs_monitoring
def test_binding_failure_is_recorded_as_a_raise():
    dep = types.ModuleType("vfakebind")
    exec("def load(stream, Loader):\n    return stream\n", dep.__dict__)
    sys.modules["vfakebind"] = dep

    def call():
        dep.load("a: 1")                    # Loader is required: fails before the frame

    def body():
        with pytest.raises(TypeError):
            call()

    try:
        sink = _run(MonitoringBackend, ["vfakebind"], body)
    finally:
        del sys.modules["vfakebind"]
    (rec,) = _by_name(sink)["vfakebind.load"]
    assert rec["raised"] == "TypeError"
    assert rec["binding"] is True
    assert rec["where"]["func"] == "call"


@needs_monitoring
def test_a_typeerror_inside_the_callee_is_not_a_binding_failure():
    dep = types.ModuleType("vfakebind2")
    exec("def load(x):\n    return x + 'a'\n", dep.__dict__)
    sys.modules["vfakebind2"] = dep

    def call():
        dep.load(1)

    def body():
        with pytest.raises(TypeError):
            call()

    try:
        sink = _run(MonitoringBackend, ["vfakebind2"], body)
    finally:
        del sys.modules["vfakebind2"]
    recs = _by_name(sink)["vfakebind2.load"]
    assert [r["raised"] for r in recs] == ["TypeError"]
    assert not any(r.get("binding") for r in recs)


@needs_monitoring
def test_a_raise_statement_after_a_call_is_not_a_binding_failure():
    dep = types.ModuleType("vfakebind3")
    exec("def load(x):\n    return x\n", dep.__dict__)
    sys.modules["vfakebind3"] = dep

    def call():
        dep.load(1)
        raise TypeError("mine")

    def body():
        with pytest.raises(TypeError):
            call()

    try:
        sink = _run(MonitoringBackend, ["vfakebind3"], body)
    finally:
        del sys.modules["vfakebind3"]
    assert not any(r.get("binding") for r in sink.records.values())


def test_wrap_marks_a_binding_failure():
    dep = types.ModuleType("vfakewrapbind")
    exec("def load(stream, Loader):\n    return stream\n"
         "def inner(x):\n    return x + 'a'\n", dep.__dict__)
    sys.modules["vfakewrapbind"] = dep
    sink = MemorySink()
    be = WrapBackend(["vfakewrapbind.load", "vfakewrapbind.inner"], sink, source=__name__)
    be.start()
    try:
        with pytest.raises(TypeError):
            dep.load("a")
        with pytest.raises(TypeError):
            dep.inner(1)
    finally:
        be.stop()
        del sys.modules["vfakewrapbind"]
    names = _by_name(sink)
    assert names["vfakewrapbind.load"][0]["binding"] is True
    assert "binding" not in names["vfakewrapbind.inner"][0]


def test_jsonl_sink_carries_the_binding_flag(tmp_path):
    out = tmp_path / "t.jsonl"
    sink = JsonlSink(str(out))
    sink.on_raise("m.f", {"__c_call__": True}, "TypeError", {"file": "a.py", "line": 1,
                                                              "func": "g"}, binding=True)
    sink.close()
    events = [json.loads(line) for line in out.read_text().splitlines() if line.strip()]
    (ev,) = [e for e in events if e.get("t") == "raise"]
    assert ev["binding"] is True


def test_compiled_wrap_ignores_calls_mediated_by_c_not_made_by_a_call():
    sink = MemorySink()
    holder = type("Model", (), {"__module__": "vfakecw"})
    mod = types.ModuleType("vfakecw")
    mod.__file__ = "/nowhere/vfakecw.fake.so"
    mod.Model = holder
    fn = _CyFn()
    fn.__module__ = "vfakecw"
    fn.__qualname__ = "Model.dict"
    holder.dict = fn
    wrap = CompiledWrap(sink, PrefixMatcher(["vfakecw"]), source=__name__)
    wrap._suffixes = (".so",)
    wrap.scan(mod)
    instance = holder()

    # a wrapper reached by ``==`` (COMPARE_OP) is not a call from this line
    wrapped = holder.dict
    holder.__eq__ = wrapped
    instance == holder()                         # noqa: B015 - C-mediated, no call opcode
    assert "vfakecw.Model.dict" not in _by_name(sink)
    instance.dict()                              # a direct call is a contact
    assert "vfakecw.Model.dict" in _by_name(sink)
    wrap.stop()


def test_finalizers_are_never_recorded():
    dep = types.ModuleType("vfakedel")
    exec("class K(object):\n    def __del__(self):\n        pass\n"
         "    def use(self):\n        return 1\n", dep.__dict__)
    sys.modules["vfakedel"] = dep

    def body():
        k = dep.K()
        k.use()
        del k

    try:
        for cls in BACKENDS:
            sink = _run(cls, ["vfakedel"], body)
            assert "vfakedel.K.__del__" not in _by_name(sink)
            assert "vfakedel.K.use" in _by_name(sink)
    finally:
        del sys.modules["vfakedel"]


def test_import_machinery_is_never_a_target():
    from pymolt.verify._backend import never_target

    assert never_target("importlib._bootstrap")
    assert never_target("importlib._bootstrap_external")
    assert never_target("_frozen_importlib")
    assert not never_target("importlib.metadata")


MEMO_COMMON = (
    "import functools\n"
    "def memoize_when_activated(fun):\n"
    "    @functools.wraps(fun)\n"
    "    def wrapper(self):\n"
    "        return fun(self)\n"
    "    return wrapper\n"
)
MEMO_MAIN = (
    "from vfakememo_common import memoize_when_activated\n"
    "class Process(object):\n"
    "    @memoize_when_activated\n"
    "    def memory_info(self):\n"
    "        return 1\n"
    "    @memoize_when_activated\n"
    "    def cpu_times(self):\n"
    "        return 2\n"
)


@pytest.mark.parametrize("cls", BACKENDS)
def test_a_decorator_wrapper_is_named_after_what_it_wraps_on_every_call(cls):
    # psutil's ``memoize_when_activated``: one ``wrapper(self)`` code serves every memoized
    # method. Named by that code, memory_info and cpu_times would merge; named by the CALL
    # event (3.12 only), the name changed once the call site settled (locust: 2 calls of
    # 6,572 were ``Process.memory_info``, the rest the wrapper's closure name).
    mods = {}
    for name, src in (("vfakememo_common", MEMO_COMMON), ("vfakememo", MEMO_MAIN)):
        mod = types.ModuleType(name)
        sys.modules[name] = mod
        exec(compile(src, "<" + name + ">", "exec"), mod.__dict__)
        mods[name] = mod
    proc = mods["vfakememo"].Process()

    def body():
        for _ in range(6):
            proc.memory_info()
            proc.cpu_times()

    try:
        sink = _run(cls, ["vfakememo", "vfakememo_common"], body)
    finally:
        for name in mods:
            del sys.modules[name]
    by = _by_name(sink)
    assert set(by) == {"vfakememo.Process.memory_info", "vfakememo.Process.cpu_times"}, set(by)
    for recs in by.values():
        assert sum(r["count"] for r in recs) == 6
        assert all(r.get("symq") != "fallback" for r in recs)


@needs_monitoring
def test_decorator_wrapper_is_named_by_the_declared_callable():
    dep = types.ModuleType("vfakedeco")
    exec("import functools\n"
         "def deco(f):\n"
         "    @functools.wraps(f)\n"
         "    def wrapper(*a, **k):\n"
         "        return f(*a, **k)\n"
         "    return wrapper\n"
         "def f(x):\n    return x\n"
         "g = deco(f)\n"
         "def h(x):\n    return x\n", dep.__dict__)
    dep.g.__qualname__ = "declared_g"
    sys.modules["vfakedeco"] = dep
    try:
        # more calls than it takes a call site to settle: every one keeps the declared name
        sink = _run(MonitoringBackend, ["vfakedeco"], lambda: [dep.g(1) for _ in range(5)])
    finally:
        del sys.modules["vfakedeco"]
    by = _by_name(sink)
    assert set(by) == {"vfakedeco.declared_g"}, set(by)
    assert sum(r["count"] for r in by["vfakedeco.declared_g"]) == 5
    assert any(r["qualname"].endswith(".wrapper") for r in sink.records.values())


@pytest.mark.parametrize("cls", BACKENDS)
def test_a_generic_wrapper_keeps_its_name_after_the_call_site_settles(cls):
    dep = types.ModuleType("vfakedeco")
    exec("import functools\n"
         "def deco(f):\n"
         "    @functools.wraps(f)\n"
         "    def wrapper(*a, **k):\n"
         "        return f(*a, **k)\n"
         "    return wrapper\n"
         "def f(x):\n    return x\n"
         "g = deco(f)\n", dep.__dict__)
    sys.modules["vfakedeco"] = dep
    try:
        sink = _run(cls, ["vfakedeco"], lambda: [dep.g(1) for _ in range(5)])
    finally:
        del sys.modules["vfakedeco"]
    by = _by_name(sink)
    assert set(by) == {"vfakedeco.f"}, set(by)
    assert sum(r["count"] for r in by["vfakedeco.f"]) == 5


def test_static_c_types_render_opaque_not_structurally():
    from pymolt.verify._normalize import normalize

    def gen():
        yield 1

    g = gen()
    assert normalize(g) == {"__opaque__": "generator"}
    assert normalize(g.gi_code) == {"__opaque__": "code"}
    assert normalize(sys._getframe()) == {"__opaque__": "frame"}


@pytest.mark.parametrize("cls", BACKENDS)
def test_a_hot_loop_of_compiled_calls_is_witnessed_not_replayed(cls):
    import collections

    d = collections.deque(range(5))

    def body():
        for _ in range(3000):
            d.append(d.popleft())

    sink = _run(cls, ["collections"], body)
    recs = _by_name(sink)["collections.deque.popleft"]
    assert sum(r["count"] for r in recs) == 2000        # _C_SITE_MAX at that line


def test_bound_builtin_methods_are_decided_once_per_owner_class():
    import collections

    be = SetProfileBackend(PrefixMatcher(["collections"]), MemorySink(), source=__name__)
    a, b = collections.deque(), collections.deque()
    assert be._c_sym(a.append) == be._c_sym(b.append) == "collections.deque.append"
    assert len(be._ccache) == 0 and len(be._cmeth) == 1     # no entry per bound object


def test_the_runtime_writer_module_is_never_the_project_nor_a_dependency():
    from pymolt.verify import _rawthreads
    from pymolt.verify._backend import AppMatcher, never_target

    assert never_target(_rawthreads.__name__)
    assert not AppMatcher().matches(_rawthreads.__name__, sys._getframe())
