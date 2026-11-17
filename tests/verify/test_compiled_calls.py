"""Compiled (C / Cython) dependency calls and binding failures (Wave G, G1 and G4).

Small real C extensions from the stdlib stand in for a compiled dependency: ``_json`` /
``math`` / ``_datetime`` are made "the dependency" through a test matcher. Runtime-layer test:
pure stdlib + pytest, 3.6-compatible syntax.
"""
import collections.abc
import datetime
import gc
import itertools
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


def test_a_wrapper_called_from_dependency_code_calls_through_without_recording():
    mod = _fake_compiled_module()
    sink = MemorySink()
    wrap = CompiledWrap(sink, PrefixMatcher(["vfakecw"]), source=__name__)
    wrap._suffixes = (".so",)
    wrap.scan(mod)
    dep = types.ModuleType("vfakecwcaller")
    exec("def use(f, x):\n    return f(x)\n", dep.__dict__)
    try:
        assert dep.use(mod.helper, 0) == 0           # decides the dependency's code once
        assert [code.co_name for code in wrap._not_near.values()] == ["use"]
        reached = []
        wrap._safe_record = lambda *a, **kw: reached.append(a)
        assert [dep.use(mod.helper, i) for i in range(1, 3)] == [1, 2]
        assert reached == []                         # later calls skip the record path
        del wrap._safe_record
        assert "vfakecw.helper" not in _by_name(sink)
        assert mod.helper(5) == 5                    # a call from our code is still a contact
        (rec,) = _by_name(sink)["vfakecw.helper"]
        assert rec["inputs"]["bound"]["x"] == 5
        model = mod.Model()
        assert dep.use(model.load, 4) is model       # a bound wrapped method calls through too
        assert [r["count"] for r in _by_name(sink)["vfakecw.helper"]] == [1]
    finally:
        wrap.stop()


MODES = ["discover", "watch", "incremental"]


def _registered_compiled_module(name):
    """A fake compiled dependency module in ``sys.modules``, found by ``CompiledWrap.start``."""
    import importlib.machinery as machinery

    mod = types.ModuleType(name)
    mod.__file__ = "/nowhere/" + name + machinery.EXTENSION_SUFFIXES[-1]
    holder = type("Model", (), {"__module__": name})
    load = _CyFn()
    load.__module__ = name
    load.__qualname__ = "Model.load"
    holder.load = load
    mod.Model = holder
    helper = _CyFn()
    helper.__module__ = name
    helper.__qualname__ = "helper"
    mod.helper = helper
    sys.modules[name] = mod
    return mod


def _monitored(mod, body, **kw):
    sink = MemorySink()
    be = MonitoringBackend(PrefixMatcher([mod.__name__]), sink, source=__name__, **kw)
    be.start()
    try:
        body()
    finally:
        be.stop()
        del sys.modules[mod.__name__]
    return be, sink


def _wrapper_code(code):
    from pymolt.verify._wrap import _WRAPPER_CODES

    return any(code is c for c in _WRAPPER_CODES)


@needs_monitoring
@pytest.mark.parametrize("mode", MODES)
def test_monitoring_wraps_cython_functions_and_records_each_call_once(mode):
    mod = _registered_compiled_module("vfakecwm")
    original = mod.helper

    def body():
        assert mod.helper is not original           # wrapped while the backend runs
        for i in range(3):
            mod.helper(i)
        mod.Model().load(5)

    _be, sink = _monitored(mod, body, mode=mode)
    assert mod.helper is original                    # restored by stop
    names = _by_name(sink)
    helper = names["vfakecwm.helper"]
    assert sum(r["count"] for r in helper) == 3      # once per call: no __c_call__ twin
    assert sorted(r["inputs"]["bound"]["x"] for r in helper) == [0, 1, 2]
    assert sorted(r["result"] for r in helper) == [0, 1, 2]
    assert not any(r["inputs"].get("__c_call__") for rs in names.values() for r in rs)
    assert sum(r["count"] for r in names["vfakecwm.Model.load"]) == 1


@needs_monitoring
@pytest.mark.parametrize("mode", ["discover", "incremental"])
def test_a_cython_callable_bound_before_the_wrap_stays_a_compiled_call(mode):
    mod = _registered_compiled_module("vfakecwe")
    early = mod.helper                               # ``from dep import helper`` before start

    def body():
        early(9)
        mod.helper(1)

    _be, sink = _monitored(mod, body, mode=mode)
    recs = _by_name(sink)["vfakecwe.helper"]
    shapes = sorted((bool(r["inputs"].get("__c_call__")), r["count"]) for r in recs)
    assert shapes == [(False, 1), (True, 1)]
    (compiled,) = [r for r in recs if r["inputs"].get("__c_call__")]
    assert compiled["inputs"]["arg0"] == 9
    assert compiled["result"] == {"__opaque__": "unobserved:c-return"}


@needs_monitoring
def test_the_wrapper_code_is_never_armed_by_either_incremental_tool():
    mod = _registered_compiled_module("vfakecwi")

    def body():
        for i in range(4):
            mod.helper(i)
            mod.Model().load(i)

    be, sink = _monitored(mod, body, mode="incremental")
    assert sum(r["count"] for r in _by_name(sink)["vfakecwi.helper"]) == 4
    assert not any(_wrapper_code(code) for code in be._watch_codes.values())
    assert be.armed == 0                             # tool A armed nothing for the wrapper
    implicit = [ent for ent in be._implicit.values() if _wrapper_code(ent[0])]
    assert implicit and not any(ent[1] for ent in implicit)    # tool B: DISABLEd, not owned
    assert be._local == {}                           # released by stop (nothing left armed)


@needs_monitoring
def test_discover_disables_the_wrapper_code():
    mod = _registered_compiled_module("vfakecwd")
    be, sink = _monitored(mod, lambda: [mod.helper(i) for i in range(3)], mode="discover")
    assert sum(r["count"] for r in _by_name(sink)["vfakecwd.helper"]) == 3
    ents = [ent for ent in be._callee.values() if _wrapper_code(ent[0])]
    assert ents and not any(ent[1] for ent in ents)
    assert all(ent[6] is be._mon.DISABLE for ent in ents)
    assert not any(_wrapper_code(code) for code in be.observed.values())


@needs_monitoring
@pytest.mark.parametrize("mode", MODES)
def test_an_impact_target_that_is_a_wrapped_cython_callable_is_recorded_once(mode):
    mod = _registered_compiled_module("vfakecwt")

    def body():
        for i in range(3):
            mod.helper(i)

    be, sink = _monitored(mod, body, mode=mode, watch_targets=["vfakecwt.helper"])
    assert sum(r["count"] for r in _by_name(sink)["vfakecwt.helper"]) == 3
    assert be.skipped == []                          # observed (by its wrapper), not skipped
    assert not any(_wrapper_code(code) for code in be._impact_codes.values())


@needs_monitoring
def test_include_internal_does_not_record_the_wrapper_calling_what_it_wraps():
    mod = _registered_compiled_module("vfakecwn")

    _be, sink = _monitored(mod, lambda: [mod.helper(i) for i in range(3)],
                           include_internal=True)
    recs = _by_name(sink)["vfakecwn.helper"]
    assert sum(r["count"] for r in recs) == 3
    assert not any(r["inputs"].get("__c_call__") for r in recs)


@needs_monitoring
def test_compiled_wrap_can_be_turned_off_under_monitoring():
    mod = _registered_compiled_module("vfakecwo")
    sink = MemorySink()
    be = MonitoringBackend(PrefixMatcher(["vfakecwo"]), sink, source=__name__)
    be.compiled_wrap = False
    be.start()
    try:
        original = mod.helper
        mod.helper(2)
        assert mod.helper is original
    finally:
        be.stop()
        del sys.modules["vfakecwo"]
    (rec,) = _by_name(sink)["vfakecwo.helper"]
    assert rec["inputs"] == {"__c_call__": True, "arg0": 2}


def _real_cython_function():
    """``yaml._yaml.get_version``: a real Cython function (PyYAML's libyaml binding)."""
    _yaml = pytest.importorskip("yaml._yaml")
    if type(_yaml.get_version).__name__ != "cython_function_or_method":
        pytest.skip("yaml._yaml is not built with Cython function binding")
    return _yaml


@needs_monitoring
@pytest.mark.parametrize("mode", MODES)
def test_a_real_cython_call_is_recorded_with_its_result_like_setprofile_records_it(mode):
    _yaml = _real_cython_function()
    expected = list(_yaml.get_version())

    def body():
        for _ in range(2):
            _yaml.get_version()

    recs = {}
    for cls, kw in ((MonitoringBackend, {"mode": mode}), (SetProfileBackend, {})):
        sink = MemorySink()
        be = cls(PrefixMatcher(["yaml"]), sink, source=__name__, **kw)
        be.start()
        try:
            body()
        finally:
            be.stop()
        recs[cls.name] = _by_name(sink)["yaml._yaml.get_version"]
    (mon,) = recs["monitoring"]
    (sp,) = recs["setprofile"]
    assert mon["count"] == 2
    assert "__c_call__" not in mon["inputs"]
    assert mon["inputs"] == sp["inputs"]
    assert mon["result"] == sp["result"] == {"__tuple__": expected}


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


def _hot_loop(cls, n, targets=("collections",), **kw):
    import collections

    d = collections.deque(range(5))
    be = cls(PrefixMatcher(list(targets)), MemorySink(), source=__name__, **kw)
    be.start()
    try:
        for _ in range(n):
            d.append(d.popleft())
    finally:
        be.stop()
    return be


@pytest.mark.parametrize("cls", BACKENDS)
def test_compiled_calls_past_the_site_cap_are_counted_and_their_site_reported(cls):
    be = _hot_loop(cls, 3000)
    acct = be.c_cap_account()
    assert acct["max"] == 2000 and acct["exact"] is True
    assert acct["muted"] == 2 * 1000            # popleft and append, 1,000 each past the cap
    assert acct["sites_omitted"] == 0 and acct["unsited"] == 0
    by_sym = dict((s["sym"], s) for s in acct["sites"])
    site = by_sym["collections.deque.popleft"]
    assert site["muted"] == 1000 and site["exact"] is True
    assert site["file"] == __file__ and site["func"] == "_hot_loop"
    assert isinstance(site["line"], int)


@pytest.mark.parametrize("cls", BACKENDS)
def test_no_account_when_no_site_reached_the_cap(cls):
    assert _hot_loop(cls, 10).c_cap_account() is None


@needs_monitoring
@pytest.mark.parametrize("mode", ["discover", "incremental"])
def test_monitoring_stops_counting_a_site_after_the_mute_bound(monkeypatch, mode):
    import pymolt.verify._monitoring as mon

    monkeypatch.setattr(mon, "_C_MUTE_MAX", 50)
    be = _hot_loop(MonitoringBackend, 3000, mode=mode)
    acct = be.c_cap_account()
    site = dict((s["sym"], s) for s in acct["sites"])["collections.deque.popleft"]
    assert site["muted"] >= 50 and site["exact"] is False       # a lower bound from here
    assert acct["exact"] is False


@pytest.mark.parametrize("cls", BACKENDS)
def test_a_forked_child_starts_a_fresh_cap_account(cls):
    be = _hot_loop(cls, 2500)
    assert be.c_cap_account()["muted"] == 1000
    be.after_fork_child()
    assert be.c_cap_account() is None and be._ccount == {}


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


# ── callees only some receivers make a dependency's ──────────────────────────
# Under 3.12 ``d.items()`` is a CALL of the unbound ``dict.items`` with ``d`` as its first
# argument, and ``m.get(k)`` one of ``Mapping.get`` with ``m``: whether it is a dependency's
# call depends on the receiver's class, so a call site must not settle (DISABLE) on the
# callee it saw twice. setprofile has no call-site settling: it is the reference.

RECV_CASES = [(SetProfileBackend, {})]
if HAS_MONITORING:
    RECV_CASES += [(MonitoringBackend, {"mode": "discover"}),
                   (MonitoringBackend, {"mode": "incremental"})]
RECV_IDS = ["setprofile", "discover", "incremental"][:len(RECV_CASES)]


def _run_with(cls, kw, targets, body):
    sink = MemorySink()
    be = cls(PrefixMatcher(targets), sink, source=__name__, **kw)
    be.start()
    try:
        body()
    finally:
        be.stop()
    return sink, be


def _counts(sink):
    out = {}
    for rec in sink.records.values():
        sym = rec.get("sym") or rec["qualname"]
        out[sym] = out.get(sym, 0) + rec["count"]
    return out


def _fake_module(name, src):
    """A module that exists once its classes do, as after an import."""
    mod = types.ModuleType(name)
    exec(compile(src, "<" + name + ">", "exec"), mod.__dict__)
    sys.modules[name] = mod
    return mod


def _summary(d):
    from operator import itemgetter

    return json.dumps(dict(sorted(d.items(), key=itemgetter(0)))), sum(d.values())


@pytest.mark.parametrize("cls,kw", RECV_CASES, ids=RECV_IDS)
def test_a_builtin_method_site_reached_by_plain_receivers_first_sees_dependency_ones(cls, kw):
    # locust runners.py:1402-1403: the same lines summarise plain dicts and defaultdicts.
    import collections

    def body():
        for _ in range(3):
            _summary({"a": 1, "b": 2})
        for _ in range(3):
            dd = collections.defaultdict(int)
            dd["a"] += 1
            _summary(dd)

    sink, _be = _run_with(cls, kw, ["collections", "json"], body)
    counts = _counts(sink)
    assert counts.get("collections.defaultdict.items") == 3, counts
    assert counts.get("collections.defaultdict.values") == 3, counts
    assert counts.get("json.dumps") == 6, counts


def _bound_get(d):
    get = d.get             # a builtin method bound to the receiver, not a descriptor call
    return get("a")


@pytest.mark.parametrize("cls,kw", RECV_CASES, ids=RECV_IDS)
def test_a_bound_builtin_method_site_reached_by_plain_receivers_first_sees_dependency_ones(
        cls, kw):
    import collections

    def body():
        for _ in range(3):
            _bound_get({"a": 1})
        for _ in range(3):
            _bound_get(collections.defaultdict(int, a=1))

    sink, _be = _run_with(cls, kw, ["collections"], body)
    assert _counts(sink).get("collections.defaultdict.get") == 3, _counts(sink)


RECV_SRC = '''
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
'''


class _OurMapping(collections.abc.Mapping):     # our code's own Mapping
    def __init__(self, d):
        self._d = dict(d)

    def __getitem__(self, k):
        return self._d[k]

    def __iter__(self):
        return iter(self._d)

    def __len__(self):
        return len(self._d)


def _get_a(m):
    return m.get("a")


@pytest.mark.parametrize("cls,kw", RECV_CASES, ids=RECV_IDS)
def test_a_stdlib_method_site_reached_by_our_receivers_first_sees_dependency_ones(cls, kw):
    # ``Mapping.get`` is a dependency's call only on a dependency class inheriting it.
    dep = _fake_module("vfakerecv", RECV_SRC)
    try:
        def body():
            for _ in range(3):
                _get_a(_OurMapping({"a": 1}))
            for _ in range(3):
                _get_a(dep.Hdrs({"a": 1}))

        sink, _be = _run_with(cls, kw, ["vfakerecv"], body)
    finally:
        del sys.modules["vfakerecv"]
    counts = _counts(sink)
    assert counts.get("vfakerecv.Hdrs.get") == 3, counts


def _items(d):
    return d.items()


def _items_hits(targets, mode, body):
    """How often ``_items``'s ``dict.items`` reached the CALL callback: a settled site stops
    at 2."""
    hits = []

    class Counting(MonitoringBackend):
        def _on_call(self, code, offset, callable_, arg0):
            if callable_ is dict.items and code is _items.__code__:
                hits.append(arg0)
            return MonitoringBackend._on_call(self, code, offset, callable_, arg0)

    _run_with(Counting, {"mode": mode}, targets, body)
    return len(hits)


@needs_monitoring
@pytest.mark.parametrize("mode", ["discover", "incremental"])
def test_a_builtin_method_site_settles_when_no_dependency_class_inherits_it(mode):
    def body():
        for _ in range(6):
            _items({"a": 1})

    assert _items_hits(["json"], mode, body) == 2           # no json class inherits dict.items
    assert _items_hits(["collections"], mode, body) == 6    # collections.defaultdict does


LATE_SRC = '''
import collections.abc

class LateDict(dict):
    pass

class LateMap(collections.abc.Mapping):
    def __init__(self, d):
        self._d = dict(d)
    def __getitem__(self, k):
        return self._d[k]
    def __iter__(self):
        return iter(self._d)
    def __len__(self):
        return len(self._d)
'''


_UNIQUE = itertools.count()


def _unique(prefix):
    """A module name of its own per test case: an earlier case's classes (garbage not yet
    collected) are still subclasses of ``dict``, and would make the callee open from the
    start."""
    return prefix + str(next(_UNIQUE))


def _closed_now(be, callee):
    """Assert the callee was decided closed (none of the target classes inherits it yet), so
    a case exercises the re-decision; setprofile has no settling to check."""
    closed = getattr(be, "_rd_closed", None)
    if closed is not None:
        assert id(callee) in closed, "the case must start with a closed callee"


def _late(cls, kw, call, callee, plain, make):
    """``call`` settles on plain receivers while no class of the target exists, then the
    target module is imported and its class reaches the same site."""
    name = _unique("vfakelate")
    sink = MemorySink()
    be = cls(PrefixMatcher([name]), sink, source=__name__, **kw)
    be.start()
    try:
        for _ in range(3):
            call(plain())
        _closed_now(be, callee)
        mod = _fake_module(name, LATE_SRC)
        for _ in range(3):
            call(make(mod))
    finally:
        be.stop()
        sys.modules.pop(name, None)
        gc.collect()
    return name, _counts(sink)


@pytest.mark.parametrize("cls,kw", RECV_CASES, ids=RECV_IDS)
def test_a_settled_builtin_method_site_reopens_when_an_import_brings_an_heir(cls, kw):
    name, counts = _late(cls, kw, _items, dict.items, lambda: {"a": 1},
                         lambda m: m.LateDict(a=1))
    assert counts.get(name + ".LateDict.items") == 3, counts


@pytest.mark.parametrize("cls,kw", RECV_CASES, ids=RECV_IDS)
def test_a_settled_stdlib_method_site_reopens_when_an_import_brings_an_heir(cls, kw):
    name, counts = _late(cls, kw, _get_a, collections.abc.Mapping.get.__code__,
                         lambda: _OurMapping({"a": 1}), lambda m: m.LateMap({"a": 1}))
    assert counts.get(name + ".LateMap.get") == 3, counts


def _keys(d):
    return d.keys()


@pytest.mark.parametrize("cls,kw", RECV_CASES, ids=RECV_IDS)
def test_a_class_made_at_run_time_reopens_a_settled_site_once_another_call_shows_it(cls, kw):
    # No import tells the runtime about it; a call elsewhere with that receiver does.
    name = _unique("vfakert")
    sys.modules[name] = types.ModuleType(name)
    sink = MemorySink()
    be = cls(PrefixMatcher([name]), sink, source=__name__, **kw)
    be.start()
    try:
        for _ in range(3):
            _keys({"a": 1})                     # settles: no class of the target inherits it
        _closed_now(be, dict.keys)
        made = type("Made", (dict,), {"__module__": name})
        list(made(a=1).keys())                  # another site: the receiver shows the heir
        for _ in range(3):
            _keys(made(a=1))
    finally:
        be.stop()
        del sys.modules[name]
        gc.collect()
    assert _counts(sink).get(name + ".Made.keys") == 1 + 3, _counts(sink)


@pytest.mark.parametrize("cls,kw", RECV_CASES, ids=RECV_IDS)
def test_an_open_site_answers_each_receiver_class_on_every_call(cls, kw):
    # The open site's kept answer (plain dict) must not swallow a defaultdict call after it.
    import collections

    def body():
        for _ in range(4):
            _items({"a": 1})
            _items(collections.defaultdict(int, a=1))
            _items({"a": 1})

    sink, _be = _run_with(cls, kw, ["collections"], body)
    assert _counts(sink).get("collections.defaultdict.items") == 4, _counts(sink)


KIN_BASE_SRC = '''
class Base(object):
    def __new__(cls):
        return object.__new__(cls)
'''


def _make(c, base):
    return base.__new__(c)


@pytest.mark.parametrize("cls,kw", RECV_CASES, ids=RECV_IDS)
def test_an_open_site_with_class_receivers_decides_each_class(cls, kw):
    # ``Base.__new__(c)``: the receiver is the class itself, so every receiver has the same
    # type (``type``) and the open site's kept answer cannot be keyed by it.
    base_name, kid_name = _unique("vfakekinbase"), _unique("vfakekin")
    base = _fake_module(base_name, KIN_BASE_SRC)
    kid = _fake_module(kid_name, "from " + base_name + " import Base\n"
                                 "class Kid(Base):\n    pass\n")
    ours = type("Ours", (base.Base,), {})
    try:
        def body():
            for _ in range(3):
                _make(ours, base.Base)
            for _ in range(3):
                _make(kid.Kid, base.Base)

        sink, _be = _run_with(cls, kw, [kid_name], body)
    finally:
        del sys.modules[base_name], sys.modules[kid_name]
        gc.collect()
    assert _counts(sink).get(kid_name + ".Kid.__new__") == 3, _counts(sink)


WRAPPED_SRC = '''
import functools

def deco(f):
    @functools.wraps(f)
    def wrapper(self):
        return f(self)
    return wrapper

class Third(object):
    @deco
    def go(self):
        return 1
'''


@needs_monitoring
@pytest.mark.parametrize("mode", ["discover", "incremental"])
def test_a_method_no_class_holds_under_its_code_name_settles(mode):
    # ``Third.go`` is the ``wrapper`` code: inherited calls are named by the code's name, so
    # none can be recorded through it, and its site settles (and is never decided again).
    name = _unique("vfakewrapped")
    mod = _fake_module(name, WRAPPED_SRC)
    obj = mod.Third()
    hits = []

    class Counting(MonitoringBackend):
        def _on_call(self, code, offset, callable_, arg0):
            if callable_ is mod.Third.go:
                hits.append(1)
            return MonitoringBackend._on_call(self, code, offset, callable_, arg0)

    def body():
        for _ in range(6):
            obj.go()

    try:
        _sink, be = _run_with(Counting, {"mode": mode}, ["collections"], body)
    finally:
        del sys.modules[name]
    assert len(hits) == 2
    assert be._rd[id(mod.Third.go.__code__)][1] is False
    assert id(mod.Third.go.__code__) not in be._rd_closed


class _Ours:
    def ping(self):
        return 1


@needs_monitoring
@pytest.mark.parametrize("mode", ["discover", "incremental"])
def test_our_own_methods_settle_and_are_never_decided_again(mode):
    obj = _Ours()

    def body():
        for _ in range(5):
            obj.ping()

    _sink, be = _run_with(MonitoringBackend, {"mode": mode}, ["collections"], body)
    code = _Ours.ping.__code__
    assert be._rd[id(code)][1] is False             # no dependency class inherits our code
    assert id(code) not in be._rd_closed            # so imports never re-decide it
