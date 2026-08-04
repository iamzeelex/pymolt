"""SetProfileBackend over an in-process fake target module + stdlib-purity guard.

The target module is built with ``types.ModuleType`` so functions defined in it carry
``f_globals['__name__'] == "<prefix>"`` — exactly what the prefix filter keys on — without
touching the filesystem.
"""
import subprocess
import sys
import types

import pytest

from pymolt.verify._backend import SetProfileBackend, select_backend
from pymolt.verify._sinks import MemorySink

PREFIX = "vfaketgt"

TARGET_SRC = """
def greet(name, times=1):
    return ("hi " + name) * times

def use_builtin(xs):
    return sorted(xs)        # C calls inside the target -> c_call/c_return

def boom():
    raise ValueError("kaboom")
"""


@pytest.fixture
def target():
    mod = types.ModuleType(PREFIX)
    exec(compile(TARGET_SRC, "<%s>" % PREFIX, "exec"), mod.__dict__)
    sys.modules[PREFIX] = mod
    yield mod
    sys.modules.pop(PREFIX, None)


def _trace(target_mod, body):
    sink = MemorySink()
    be = SetProfileBackend(PREFIX, sink)
    be.start()
    try:
        body(target_mod)
    finally:
        be.stop()
    return sink, be


def test_records_call_with_bound_args_and_normalized_return(target):
    sink, _ = _trace(target, lambda m: m.greet("ann", times=2))
    (rec,) = [r for r in sink.records.values() if r["qualname"] == PREFIX + ".greet"]
    assert rec["inputs"] == {"bound": {"name": "ann", "times": 2}}
    assert rec["result"] == "hi annhi ann"


def test_aggregates_repeated_identical_calls(target):
    sink, _ = _trace(target, lambda m: (m.greet("x"), m.greet("x")))
    (rec,) = [r for r in sink.records.values() if r["qualname"] == PREFIX + ".greet"]
    assert rec["count"] == 2


def test_out_of_prefix_frames_are_rejected(target):
    def out_of_target():  # __name__ is this test module, not the prefix
        return 123

    def body(m):
        out_of_target()
        m.greet("z")

    sink, _ = _trace(target, body)
    quals = {r["qualname"] for r in sink.records.values()}
    assert PREFIX + ".greet" in quals
    assert not any("out_of_target" in q for q in quals)


def test_c_level_traffic_is_classified_not_crashing(target):
    sink, be = _trace(target, lambda m: m.use_builtin([3, 1, 2]))
    (rec,) = [r for r in sink.records.values() if r["qualname"] == PREFIX + ".use_builtin"]
    assert rec["result"] == {"__list__": [1, 2, 3]}
    assert be.c_event_count > 0  # builtins within target counted, hook survived


def test_module_body_frame_is_skipped(target):
    # Re-exec the module body under tracing; the "<module>" frame must not be recorded.
    def body(m):
        exec(compile("x = greet('q')", "<%s>" % PREFIX, "exec"), m.__dict__)

    sink, _ = _trace(target, body)
    # greet() called from the module body IS a contact; the <module> frame itself is not.
    assert not any(r["qualname"].endswith(".<module>") for r in sink.records.values())


def test_hook_never_swallows_target_exceptions_and_clears_pending(target):
    sink = MemorySink()
    be = SetProfileBackend(PREFIX, sink)
    be.start()
    try:
        with pytest.raises(ValueError):
            target.boom()          # exception must still propagate through the hook
    finally:
        be.stop()
    assert be._pending == {}        # stop() drains pending frames
    # Under setprofile a Python raise is delivered as 'return' (documented limitation):
    # boom() is recorded as a contact, just not as a raise.
    assert any(r["qualname"] == PREFIX + ".boom" for r in sink.records.values())


def test_records_capture_the_call_site_in_our_code(target):
    def caller(m):
        return m.greet("z")          # this line is the call site into the dependency

    sink, _ = _trace(target, caller)
    (rec,) = [r for r in sink.records.values() if r["qualname"] == PREFIX + ".greet"]
    where = rec["where"]
    assert where["func"] == "caller"
    assert where["file"].endswith("test_backend.py")
    assert isinstance(where["line"], int)


def test_select_backend_defaults_to_setprofile():
    be = select_backend("x", MemorySink())
    assert be.name == "setprofile"
    assert select_backend("x", MemorySink(), prefer="monitoring").name == "monitoring"


def test_build_matcher_dispatch():
    from pymolt.verify._backend import PrefixMatcher, SitePackagesMatcher, build_matcher

    assert isinstance(build_matcher("all"), SitePackagesMatcher)
    assert isinstance(build_matcher("*"), SitePackagesMatcher)
    assert isinstance(build_matcher("flask"), PrefixMatcher)
    m = build_matcher("flask,werkzeug")
    assert isinstance(m, PrefixMatcher) and m.prefixes == ("flask", "werkzeug")
    assert isinstance(build_matcher(["a", "b"]), PrefixMatcher)
    # an existing matcher passes through unchanged
    assert build_matcher(m) is m


def test_prefix_matcher_multi_and_exclude():
    from pymolt.verify._backend import build_matcher

    m = build_matcher("flask,werkzeug", exclude=("werkzeug",))
    assert m.matches("flask.app") is True
    assert m.matches("flask") is True
    assert m.matches("werkzeug.routing") is False   # excluded top-level
    assert m.matches("jinja2") is False             # not a target prefix


def test_multi_prefix_backend_traces_each_listed_dependency():
    mods = {}
    for name in ("mtgtA", "mtgtB", "mtgtC"):
        mod = types.ModuleType(name)
        exec(compile("def f():\n return 1\n", "<%s>" % name, "exec"), mod.__dict__)
        sys.modules[name] = mod
        mods[name] = mod
    try:
        sink = MemorySink()
        be = SetProfileBackend("mtgtA,mtgtB", sink)  # C is NOT a target
        be.start()
        try:
            mods["mtgtA"].f()
            mods["mtgtB"].f()
            mods["mtgtC"].f()
        finally:
            be.stop()
        traced = {r["qualname"] for r in sink.records.values()}
        assert "mtgtA.f" in traced and "mtgtB.f" in traced
        assert "mtgtC.f" not in traced
    finally:
        for name in mods:
            sys.modules.pop(name, None)


def _frame_with_file(path):
    """Return a live frame whose co_filename is exactly ``path`` (for AppMatcher tests)."""
    g = {}
    exec(compile("import sys\ndef f():\n    return sys._getframe()\n", path, "exec"), g)
    return g["f"]()


def test_app_matcher_explicit_source_by_prefix():
    from pymolt.verify._backend import AppMatcher

    am = AppMatcher(source=("flasgger",))
    assert am.matches("flasgger.utils", None) is True
    assert am.matches("flask", None) is False


def test_app_matcher_by_elimination_excludes_deps_and_stdlib():
    from pymolt.verify._backend import AppMatcher

    am = AppMatcher()
    am._dep_roots = ("/x/site-packages/", "/usr/lib/python3.6/")  # deps + stdlib
    am._cache = {}
    assert am.matches("dep", _frame_with_file("/x/site-packages/dep/m.py")) is False
    assert am.matches("std", _frame_with_file("/usr/lib/python3.6/json/__init__.py")) is False
    assert am.matches("app", _frame_with_file("/home/me/proj/app.py")) is True


def _modules(*names):
    made = {}
    for n in names:
        m = types.ModuleType(n)
        exec(compile("def call(cb):\n return cb()\n", "<%s>" % n, "exec"), m.__dict__)
        sys.modules[n] = m
        made[n] = m
    return made


def test_boundary_records_only_our_code_to_dependency():
    dep = types.ModuleType("depX")
    exec(compile("def f():\n return 1\n", "<depX>", "exec"), dep.__dict__)
    sys.modules["depX"] = dep
    callers = _modules("myapp", "otherdep")
    try:
        sink = MemorySink()
        be = SetProfileBackend("depX", sink, source="myapp")  # near side = myapp only
        be.start()
        try:
            callers["myapp"].call(dep.f)      # our code -> dep : RECORDED
            callers["otherdep"].call(dep.f)   # other -> dep : NOT our boundary
        finally:
            be.stop()
        quals = {r["qualname"] for r in sink.records.values()}
        assert "depX.f" in quals
        # only one contact (from myapp), not two
        assert sum(r["count"] for r in sink.records.values() if r["qualname"] == "depX.f") == 1
    finally:
        for n in ("depX", "myapp", "otherdep"):
            sys.modules.pop(n, None)


def test_include_internal_records_regardless_of_caller():
    dep = types.ModuleType("depY")
    exec(compile("def f():\n return 1\n", "<depY>", "exec"), dep.__dict__)
    sys.modules["depY"] = dep
    callers = _modules("appA", "appB")
    try:
        sink = MemorySink()
        be = SetProfileBackend("depY", sink, source="appA", include_internal=True)
        be.start()
        try:
            callers["appA"].call(dep.f)
            callers["appB"].call(dep.f)
        finally:
            be.stop()
        total = sum(r["count"] for r in sink.records.values() if r["qualname"] == "depY.f")
        assert total == 2  # both callers recorded — boundary filter is off
    finally:
        for n in ("depY", "appA", "appB"):
            sys.modules.pop(n, None)


class _Raiser(object):
    """Stand-in for a C callable: `__module__` decides whether it is the dependency."""

    def __init__(self, module, name="boom"):
        if module is not None:
            self.__module__ = module
        else:                       # builtin method descriptors often have none at all
            try:
                del self.__module__
            except AttributeError:
                pass
        self.__qualname__ = name


def _c_raise(backend, cfunc, module="tests.verify.test_backend"):
    """Drive one c_exception event through the hook with a caller frame in `module`."""
    frame = sys._getframe()
    saved = frame.f_globals.get("__name__")
    try:
        backend._hook(frame, "c_exception", cfunc)
    finally:
        if saved is not None:
            frame.f_globals["__name__"] = saved


def test_c_raise_outside_the_target_is_not_a_contact():
    """Regression: gating C events on the caller alone recorded every C raise our
    code provoked — `_io.BufferedReader.seek` during an import landed in a
    `--target requests` recording as a contact of requests."""
    import _io

    sink = MemorySink()
    be = SetProfileBackend("requests", sink, source=__name__)
    _c_raise(be, _io.BufferedReader.seek)          # no __module__ at all
    _c_raise(be, _Raiser("zipimport", "read"))     # attributable, but not the target
    assert sink.records == {}


def test_c_raise_inside_the_target_is_still_recorded():
    sink = MemorySink()
    be = SetProfileBackend("mydep", sink, source=__name__)
    _c_raise(be, _Raiser("mydep.speedups", "parse"))
    (rec,) = sink.records.values()
    assert rec["qualname"] == "mydep.speedups.parse"
    assert rec["raised"] == "Exception"


def test_frozen_interpreter_frames_are_not_our_code():
    """`<frozen importlib._bootstrap>` has no real file, so the site-packages/stdlib
    path test cannot reject it — left alone it masqueraded as the project."""
    from pymolt.verify._backend import AppMatcher

    am = AppMatcher()
    am._dep_roots = ("/x/site-packages/",)
    am._cache = {}
    assert am.matches("zipimport", _frame_with_file("<frozen zipimport>")) is False
    assert am.matches("app", _frame_with_file("/home/me/proj/app.py")) is True


def test_injected_runtime_is_pure_stdlib():
    """Loading the injected modules must not import pydantic or pymolt.core (they run inside
    an old target interpreter that may have neither). Checked in a clean subprocess."""
    code = (
        "import sys;"
        "import pymolt.verify.boundary_tracer, pymolt.verify._backend,"
        " pymolt.verify._sinks, pymolt.verify._normalize;"
        "bad=[m for m in sys.modules if m.startswith('pydantic') or m=='pymolt.core'"
        " or m.startswith('pymolt.core.')];"
        "assert not bad, bad"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
