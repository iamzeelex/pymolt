"""A callable is wrapped only when the wrapper keeps what the program can do with it.

The wrapper pymolt installs (``WrapBackend``, and ``CompiledWrap`` for the Cython functions of
dependency modules) is a plain function. It replaced scipy's fused Cython function
``gaussian_kernel_estimate``, which ``gaussian_kde`` indexes (``gaussian_kernel_estimate[spec]``):
``TypeError: 'function' object is not subscriptable`` under capture, on 3.11 and 3.12, for any
project that reaches it (seaborn's ``kdeplot``). The same kind of change hid a coroutine,
generator or async generator function from ``inspect`` and ``asyncio`` (frameworks call those
differently), dropped the methods of a callable object (``lru_cache``'s ``cache_clear``) and
made a callable that does not bind in a class namespace bind the instance.

Each such callable now keeps its own object; ``skipped`` lists it with the reason.

Runtime-layer test: pure stdlib + pytest, 3.6-compatible syntax.
"""
import importlib
import importlib.util
import inspect
import os
import subprocess
import sys
import types

import pytest

from pymolt.verify._backend import PrefixMatcher
from pymolt.verify._sinks import MemorySink
from pymolt.verify._wrap import _WRAP_MARK, CompiledWrap, WrapBackend


def _by_name(sink):
    out = {}
    for rec in sink.records.values():
        out.setdefault(rec.get("sym") or rec["qualname"], []).append(rec)
    return out


# ── stand-ins for Cython functions (same type names, as CompiledWrap tells them) ─

class _Fn(object):
    def __call__(self, x=None, *rest):
        return x

    def __get__(self, obj, owner=None):         # a binding Cython function
        return self if obj is None else types.MethodType(self, obj)

    __signature__ = inspect.signature(__call__.__get__(object()))


_CyFn = type("cython_function_or_method", (_Fn,), {"__module__": "builtins"})


def _specialise(self, key):
    return self


_FusedFn = type("fused_cython_function", (_CyFn,),
                {"__module__": "builtins", "__getitem__": _specialise})


def _static_get(self, obj, owner=None):
    return self                                 # a Cython function marked static


_StaticCyFn = type("cython_function_or_method", (_Fn,),
                   {"__module__": "builtins", "__get__": _static_get})


async def _coro(x=None):
    return x


def _gen(x=None):
    yield x


async def _agen(x=None):
    yield x


def _cy(cls, module, qualname, code=None):
    fn = cls()
    fn.__module__ = module
    fn.__qualname__ = qualname
    if code is not None:
        fn.__code__ = code                      # what Cython 3 sets for async def / yield
    return fn


def _compiled_module(name):
    mod = types.ModuleType(name)
    mod.__file__ = "/nowhere/" + name + ".so"
    mod.plain = _cy(_CyFn, name, "plain")
    mod.kernel = _cy(_FusedFn, name, "kernel")
    mod.acoro = _cy(_CyFn, name, "acoro", _coro.__code__)
    mod.gen = _cy(_CyFn, name, "gen", _gen.__code__)
    mod.agen = _cy(_CyFn, name, "agen", _agen.__code__)
    holder = type("Model", (), {"__module__": name})
    holder.load = _cy(_CyFn, name, "Model.load")
    holder.evaluate = _cy(_FusedFn, name, "Model.evaluate")
    holder.make = _cy(_StaticCyFn, name, "Model.make")
    mod.Model = holder
    return mod


def _scanned(mod):
    sink = MemorySink()
    wrap = CompiledWrap(sink, PrefixMatcher([mod.__name__]), source=__name__)
    wrap._suffixes = (".so",)
    wrap.scan(mod)
    return wrap, sink


def test_compiled_wrap_leaves_what_a_function_wrapper_cannot_stand_in_for():
    mod = _compiled_module("vfakeproto")
    originals = dict(vars(mod))
    model_ns = dict(vars(mod.Model))
    wrap, sink = _scanned(mod)
    try:
        assert getattr(mod.plain, _WRAP_MARK, False)            # the rest is wrapped as before
        assert getattr(mod.Model.__dict__["load"], _WRAP_MARK, False)
        for name in ("kernel", "acoro", "gen", "agen"):
            assert mod.__dict__[name] is originals[name], name
        for name in ("evaluate", "make"):
            assert mod.Model.__dict__[name] is model_ns[name], name
        assert mod.kernel["double"] is originals["kernel"]       # still subscriptable
        assert mod.Model.make(3) == 3                            # still static
        assert sorted(wrap.skipped) == [
            ("vfakeproto.Model.evaluate", "fused"),
            ("vfakeproto.Model.make", "binding"),
            ("vfakeproto.acoro", "coroutine"),
            ("vfakeproto.agen", "async-generator"),
            ("vfakeproto.gen", "generator"),
            ("vfakeproto.kernel", "fused"),
        ]
        assert mod.plain(5) == 5
        assert [r["count"] for r in _by_name(sink)["vfakeproto.plain"]] == [1]
    finally:
        wrap.stop()
    assert mod.plain is originals["plain"]


WDEP_SRC = '''
import functools


async def fetch(x):
    return x


async def stream(n):
    yield n


def rows(n):
    yield n


@functools.lru_cache(maxsize=None)
def settings():
    return {"debug": False}


def plain(x):
    return x


class Command(object):
    def __call__(self, x):
        return x

    def describe(self):
        return "command"


command = Command()
partial_plain = functools.partial(plain, 1)


class Base(object):
    @classmethod
    def build(cls):
        return cls

    @staticmethod
    def helper(x):
        return x

    def method(self):
        return 1


class Child(Base):
    pass


class Holder(object):
    pass


Holder.length = len         # a builtin in a class namespace: it does not bind
'''

SYMBOLS = ["vwdepproto.fetch", "vwdepproto.stream", "vwdepproto.rows", "vwdepproto.settings",
           "vwdepproto.plain", "vwdepproto.command", "vwdepproto.partial_plain",
           "vwdepproto.Child.build", "vwdepproto.Child.helper", "vwdepproto.Child.method",
           "vwdepproto.Holder.length"]


@pytest.fixture
def wdep():
    mod = types.ModuleType("vwdepproto")
    exec(compile(WDEP_SRC, "<vwdepproto>", "exec"), mod.__dict__)
    sys.modules["vwdepproto"] = mod
    yield mod
    sys.modules.pop("vwdepproto", None)


def test_wrap_backend_leaves_what_a_function_wrapper_cannot_stand_in_for(wdep):
    sink = MemorySink()
    be = WrapBackend(SYMBOLS, sink, source=__name__)
    seen = {}
    be.start()
    try:
        seen["coroutine"] = inspect.iscoroutinefunction(wdep.fetch)
        seen["async-generator"] = inspect.isasyncgenfunction(wdep.stream)
        seen["generator"] = inspect.isgeneratorfunction(wdep.rows)
        wdep.settings()
        wdep.settings.cache_clear()                 # an lru_cache wrapper keeps its methods
        seen["describe"] = wdep.command.describe()
        seen["partial"] = wdep.partial_plain.func is wdep.plain.__wrapped__
        seen["child"] = (wdep.Child.build() is wdep.Child, wdep.Child().build() is wdep.Child,
                         wdep.Child().helper(2), wdep.Child().method())
        seen["length"] = wdep.Holder().length([1, 2])
        seen["plain"] = (getattr(wdep.plain, _WRAP_MARK, False), wdep.plain(4))
    finally:
        be.stop()
    assert seen == {"coroutine": True, "async-generator": True, "generator": True,
                    "describe": "command", "partial": True, "child": (True, True, 2, 1),
                    "length": 2, "plain": (True, 4)}
    assert sorted(be.skipped) == [
        ("vwdepproto.Child.build", "binding"),      # inherited: a wrapper on Child would bind
        ("vwdepproto.Child.helper", "binding"),
        ("vwdepproto.Holder.length", "binding"),
        ("vwdepproto.command", "callable-object"),
        ("vwdepproto.fetch", "coroutine"),
        ("vwdepproto.partial_plain", "callable-object"),
        ("vwdepproto.rows", "generator"),
        ("vwdepproto.settings", "callable-object"),
        ("vwdepproto.stream", "async-generator"),
    ]
    by = _by_name(sink)
    assert [r["count"] for r in by["vwdepproto.plain"]] == [1]
    assert [r["count"] for r in by["vwdepproto.Base.method"]] == [1]


def test_a_real_fused_cython_function_stays_subscriptable():
    """pandas ships fused Cython functions (``pandas._libs.algos.pad``) in every release."""
    pytest.importorskip("pandas")
    algos = importlib.import_module("pandas._libs.algos")
    fused = [(k, v) for k, v in sorted(vars(algos).items())
             if type(v).__name__ == "fused_cython_function" and not k.startswith("_")]
    if not fused:
        pytest.skip("no public fused function in pandas._libs.algos")
    name, original = fused[0]
    key = next(iter(original.__signatures__))
    wrap = CompiledWrap(MemorySink(), PrefixMatcher(["pandas"]), source=__name__)
    wrap.start()                                    # scans the loaded compiled modules
    try:
        assert vars(algos)[name] is original
        assert callable(algos.__dict__[name][key])         # a specialisation, by signature
        assert ("pandas._libs.algos." + name, "fused") in wrap.skipped
    finally:
        wrap.stop()


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
KDE_PROGRAM = (
    "from scipy.stats import gaussian_kde\n"
    "print(float(gaussian_kde([1.0, 2.0, 2.5, 4.0])(2.0)[0]) > 0)\n"
)
CAPTURE_BACKENDS = ["setprofile"] + (["monitoring"] if sys.version_info >= (3, 12) else [])


@pytest.mark.parametrize("backend", CAPTURE_BACKENDS)
def test_scipy_gaussian_kde_runs_under_capture(tmp_path, backend):
    """The QA repro: seaborn's kdeplot reaches ``gaussian_kde``, which indexes a fused Cython
    function. The tracer is active before scipy is imported, as under ``pymolt capture``."""
    if importlib.util.find_spec("scipy") is None:
        pytest.skip("needs scipy")
    (tmp_path / "program.py").write_text(KDE_PROGRAM)
    env = dict((k, v) for k, v in os.environ.items() if not k.startswith(("PYMOLT_", "PYTHON")))
    env.update(PYTHONPATH=os.pathsep.join([ROOT, str(tmp_path)]), PYMOLT_TRACE_TARGET="scipy",
               PYMOLT_TRACE_SOURCE="program", PYMOLT_TRACE_BACKEND=backend,
               PYMOLT_TRACE_MODE="discover", PYMOLT_TRACE_OUT=str(tmp_path / "t-{pid}.jsonl"))
    activate = ("from pymolt.verify import boundary_tracer\n"
                "boundary_tracer.activate()\n"
                "import runpy\n"
                "runpy.run_path('program.py', run_name='program')\n")
    proc = subprocess.run([sys.executable, "-c", activate], env=env, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, universal_newlines=True, timeout=300,
                          cwd=str(tmp_path))
    assert proc.returncode == 0 and proc.stdout.strip() == "True", proc.stdout + proc.stderr
    (trace,) = tmp_path.glob("t-*.jsonl")
    assert "gaussian_kde" in trace.read_text()


CALLBACK_SRC = '''
import threading

done = threading.Event()
seen = []


def callback(x):
    seen.append(x)
    done.set()


def use(f, x):
    return f(x)
'''


def test_a_wrapper_called_with_no_python_caller_calls_through():
    """C may call a wrapped function with no Python frame above it: an ``atexit`` callback
    (pyarrow's ``_unregister_py_extension_types`` at shutdown), a thread's target. The wrapper
    looked its caller up with ``sys._getframe`` and raised ``ValueError: call stack is not deep
    enough`` into the program instead of calling the function."""
    import _thread

    mod = types.ModuleType("vwdepcb")
    exec(compile(CALLBACK_SRC, "<vwdepcb>", "exec"), mod.__dict__)
    sys.modules["vwdepcb"] = mod
    be = WrapBackend(["vwdepcb.callback"], MemorySink(), source=__name__)
    be.start()
    try:
        mod.use(mod.callback, 0)            # a dependency's caller: decided "not ours" once
        assert be._not_near
        mod.done.clear()
        _thread.start_new_thread(mod.callback, (1,))     # the wrapper is the thread's first frame
        assert mod.done.wait(10)
    finally:
        be.stop()
        sys.modules.pop("vwdepcb", None)
    assert mod.seen == [0, 1]
