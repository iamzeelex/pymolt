"""The observer filter: warnings raised from C (or through an unwrapped ``warn_explicit``)
reach a call in flight without the program seeing any difference, whatever its filters say.

The observer is a probe at the front of ``warnings.filters`` that never matches, so every
filter after it decides as before. These tests compare what the program can observe (shown
warnings, raised errors, the ``__warningregistry__``) traced and untraced, for each filter
action."""
import sys
import types
import warnings

import pytest

from pymolt.verify import _backend
from pymolt.verify._backend import (
    HAS_MONITORING,
    SetProfileBackend,
    install_warning_capture,
    remove_warning_capture,
)
from pymolt.verify._sinks import MemorySink

MOD = "vobsdep"

_SRC = '''
import datetime
import warnings


def c_level(registry):
    # warn_explicit is C and unwrapped: the same road a PyErr_WarnEx takes to the filters.
    warnings.warn_explicit("old thing", DeprecationWarning, "dep.py", 7, module="dep",
                           registry=registry)
    return 1


def c_level_runtime(registry):
    warnings.warn_explicit("runtime thing", RuntimeWarning, "dep.py", 8, module="dep",
                           registry=registry)
    return 1


def utcnow():
    return datetime.datetime.utcnow()


def python_level(x):
    warnings.warn("py level", DeprecationWarning, stacklevel=2)
    return x


def mixed(registry):
    warnings.warn("py level", DeprecationWarning, stacklevel=2)
    warnings.warn_explicit("c level", DeprecationWarning, "dep.py", 9, module="dep",
                           registry=registry)


def load(x):
    warnings.warn("use safe_load", RuntimeWarning, stacklevel=2)
    return x


def own_business(x):
    warnings.warn("internals", UserWarning)
    warnings.warn("closed", ResourceWarning, stacklevel=2)
    return x
'''


@pytest.fixture
def dep(tmp_path):
    path = tmp_path / "site-packages" / (MOD + ".py")
    path.parent.mkdir()
    path.write_text(_SRC)
    mod = types.ModuleType(MOD)
    mod.__file__ = str(path)
    exec(compile(_SRC, str(path), "exec"), mod.__dict__)
    sys.modules[MOD] = mod
    yield mod
    sys.modules.pop(MOD, None)


def _backends():
    names = ["setprofile"]
    if HAS_MONITORING:
        names.append("monitoring")
    return names


def _make(name, sink):
    if name == "setprofile":
        return SetProfileBackend(MOD, sink, source=__name__)
    from pymolt.verify._monitoring import MonitoringBackend
    return MonitoringBackend(MOD, sink, source=__name__, mode="discover")


def _traced(name, sink, body):
    be = _make(name, sink)
    be.start()
    try:
        return body()
    finally:
        be.stop()


def _warns(sink):
    out = {}
    for rec in sink.records.values():
        for w in rec.get("warnings") or ():
            k = (rec["qualname"].rsplit(".", 1)[-1], w["category"], w["message"])
            out[k] = out.get(k, 0) + w["count"]
    return out


ACTIONS = ("ignore", "default", "once", "module", "always", "error")


def _scenario(dep, action, calls=3):
    """What a program sees when it runs ``calls`` C-level warnings under ``action``: the
    shown warnings, the exception types raised, and the registry it passed in."""
    registry = {}
    seen = []
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter(action)
        for _ in range(calls):
            try:
                dep.c_level(registry)
            except Warning as exc:
                seen.append(type(exc).__name__)
    shown = [(type(w.message).__name__, str(w.message), w.filename, w.lineno) for w in caught]
    reg = sorted((k if isinstance(k, str) else tuple(k)) for k in registry if k != "version")
    return shown, seen, reg


# What each action shows untraced, out of 3 occurrences at one location: the actions really
# differ, so comparing traced with untraced means something.
SHOWN = {"always": 3, "default": 1, "once": 1, "module": 1, "ignore": 0, "error": 0}


@pytest.mark.parametrize("backend", _backends())
@pytest.mark.parametrize("action", ACTIONS)
def test_every_filter_action_behaves_exactly_as_untraced(dep, backend, action):
    untraced = _scenario(dep, action)
    shown, seen, _reg = untraced
    assert len(shown) == SHOWN[action]
    assert (len(seen) == 3) == (action == "error")
    sink = MemorySink()
    traced = _traced(backend, sink, lambda: _scenario(dep, action))
    assert traced == untraced
    # ... and the first one is recorded whatever the action did with it (error and ignore too).
    assert _warns(sink)[("c_level", "DeprecationWarning", "old thing")] >= 1


@pytest.mark.parametrize("backend", _backends())
def test_always_records_every_occurrence(dep, backend):
    sink = MemorySink()
    _traced(backend, sink, lambda: _scenario(dep, "always"))
    assert _warns(sink)[("c_level", "DeprecationWarning", "old thing")] == 3


@pytest.mark.parametrize("backend", _backends())
def test_a_program_filter_in_front_of_ours_cannot_hide_the_warning(dep, backend):
    sink = MemorySink()

    def body():
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("ignore")       # inserted at the front, ahead of the probe
            dep.c_level({})
            warnings.resetwarnings()
            warnings.filterwarnings("ignore", category=DeprecationWarning)
            dep.c_level({})
        return caught

    assert _traced(backend, sink, body) == []
    assert _warns(sink)[("c_level", "DeprecationWarning", "old thing")] == 2


def test_the_observer_filter_is_first_while_capturing_and_gone_after(dep):
    be = SetProfileBackend(MOD, MemorySink(), source=__name__)
    before = list(warnings.filters)
    simple, filt = warnings.simplefilter, warnings.filterwarnings
    be.start()
    try:
        assert warnings.filters[0] is _backend._OBSERVER_FILTER
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            assert warnings.filters[0] is _backend._OBSERVER_FILTER
            warnings.filterwarnings("error", message="x")
            assert warnings.filters[0] is _backend._OBSERVER_FILTER
            warnings.resetwarnings()
            warnings.filters.insert(0, ("ignore", None, Warning, None, 0))
            # not through a function: the next warn() puts it back (one identity test)
            warnings.warn("restore", UserWarning)
            assert warnings.filters[0] is _backend._OBSERVER_FILTER
    finally:
        be.stop()
    assert _backend._OBSERVER_FILTER not in warnings.filters
    assert warnings.filters == before
    assert warnings.simplefilter is simple and warnings.filterwarnings is filt


def test_two_owners_share_one_observer(dep):
    a, b = SetProfileBackend(MOD, MemorySink()), SetProfileBackend(MOD, MemorySink())
    install_warning_capture(a)
    install_warning_capture(b)
    assert sum(f is _backend._OBSERVER_FILTER for f in warnings.filters) == 1
    remove_warning_capture(a)
    assert _backend._OBSERVER_FILTER in warnings.filters
    remove_warning_capture(b)
    assert _backend._OBSERVER_FILTER not in warnings.filters


def test_switching_warnings_off_installs_no_observer(dep, monkeypatch):
    monkeypatch.setenv("PYMOLT_TRACE_WARNINGS", "0")
    be = SetProfileBackend(MOD, MemorySink(), source=__name__)
    be.start()
    try:
        assert _backend._OBSERVER_FILTER not in warnings.filters
    finally:
        be.stop()


# ── no double counting ───────────────────────────────────────────────────────


@pytest.mark.parametrize("backend", _backends())
def test_a_python_level_warning_is_counted_once(dep, backend):
    sink = MemorySink()

    def body():
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            dep.python_level(1)
            dep.python_level(2)

    _traced(backend, sink, body)
    assert _warns(sink)[("python_level", "DeprecationWarning", "py level")] == 2


@pytest.mark.parametrize("backend", _backends())
def test_python_and_c_level_warnings_in_one_call_each_count_once(dep, backend):
    sink = MemorySink()

    def body():
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            dep.mixed({})

    _traced(backend, sink, body)
    got = _warns(sink)
    assert got[("mixed", "DeprecationWarning", "py level")] == 1
    assert got[("mixed", "DeprecationWarning", "c level")] == 1


@pytest.mark.parametrize("backend", _backends())
def test_warnings_outside_a_recorded_call_are_not_attributed(dep, backend):
    sink = MemorySink()

    def body():
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            warnings.warn_explicit("ours", DeprecationWarning, "app.py", 1, module="app")
            warnings.warn("ours too", DeprecationWarning)

    _traced(backend, sink, body)
    assert _warns(sink) == {}


# ── a real C-level warning ───────────────────────────────────────────────────


@pytest.mark.skipif(sys.version_info < (3, 12), reason="utcnow() warns from C on 3.12+")
@pytest.mark.parametrize("backend", _backends())
def test_datetime_utcnow_is_recorded_on_the_call_in_flight(dep, backend):
    sink = MemorySink()

    def body():
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            dep.utcnow()
        return caught

    caught = _traced(backend, sink, body)
    assert len(caught) == 1 and caught[0].category is DeprecationWarning     # still shown
    ((_sym, category, message),) = _warns(sink)
    assert category == "DeprecationWarning" and "utcnow" in message


@pytest.mark.skipif(sys.version_info < (3, 12), reason="utcnow() warns from C on 3.12+")
def test_the_default_filters_still_decide_for_a_c_level_warning(dep):
    """Untouched interpreter filters: the DeprecationWarning is ignored outside __main__, and
    nothing is shown or logged with the observer in place either."""
    sink = MemorySink()
    with warnings.catch_warnings(record=True) as caught:
        warnings.resetwarnings()
        for f in (("default", None, DeprecationWarning, "__main__", 0),
                  ("ignore", None, DeprecationWarning, None, 0)):
            warnings.filters.append(f)
        _traced("setprofile", sink, dep.utcnow)
    assert caught == []
    # ... but it was recorded, once, on the call in flight
    (((sym, category, message), count),) = _warns(sink).items()
    assert (sym, category, count) == ("utcnow", "DeprecationWarning", 1)
    assert "utcnow" in message


# ── what is recorded: the family rule ────────────────────────────────────────


@pytest.mark.parametrize("backend", _backends())
def test_a_warning_that_points_at_the_project_is_recorded_whatever_its_category(dep, backend):
    """PyYAML 5.4's ``YAMLLoadWarning`` is a RuntimeWarning raised with ``stacklevel=3`` at
    the caller's ``yaml.load(s)``: a notice to the project, whatever its category."""
    sink = MemorySink()

    def body():
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            dep.load(1)                       # stacklevel=2: points at this (project) frame
            dep.own_business(1)               # UserWarning at the dependency; ResourceWarning
            dep.c_level_runtime({})           # unwrapped: points at the dependency's frame

    _traced(backend, sink, body)
    got = _warns(sink)
    assert got == {("load", "RuntimeWarning", "use safe_load"): 1}


def test_warning_key_broad_reads_other_categories_but_not_unrelated_ones():
    from pymolt.verify._backend import warning_key
    assert warning_key("m", RuntimeWarning) is None
    assert warning_key("m", RuntimeWarning, broad=True) == ("RuntimeWarning", "m")
    assert warning_key("m", ResourceWarning, broad=True) is None
    assert warning_key("m", int, broad=True) is None
    assert warning_key(UserWarning("u"), broad=True) == ("UserWarning", "u")


# ── recording never changes the program ──────────────────────────────────────


def test_a_failing_recorder_does_not_change_or_raise_from_the_warning(dep, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("recorder bug")

    sink = MemorySink()
    be = SetProfileBackend(MOD, sink, source=__name__)
    be.start()
    monkeypatch.setattr(_backend, "_capture_warning", boom)
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            dep.c_level({})
            dep.python_level(1)
    finally:
        be.stop()
    assert [str(w.message) for w in caught] == ["old thing", "py level"]


def test_the_observer_category_is_never_a_subclass_of_anything_real(dep):
    cat = _backend._ObserverCategory
    assert not issubclass(DeprecationWarning, cat) and not issubclass(Warning, cat)
    assert issubclass(cat, Warning)    # introspecting code still sees a Warning class
