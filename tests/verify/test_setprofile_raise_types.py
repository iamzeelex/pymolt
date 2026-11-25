"""setprofile (3.6-3.11) records the TYPE of a raise.

A profile hook never sees the exception (CPython fetches it away before the unwinding
``return`` / ``c_exception`` event), so ``SetProfileBackend`` parks a raised call. Below 3.12
it arms the frame that receives the exception for one ``exception`` trace event, which
carries it; otherwise (3.12+, or another tool owning ``sys.settrace``) it types the raise at
the thread's next event, from ``sys.exc_info()``. Either way the type counts only if a
traceback passes through the call's frame. The scenarios below run on the host (3.12,
setprofile forced: never armed) and, when uv has them, on real 3.8 and 3.11 interpreters:
the runtime is pure stdlib, 3.6 syntax.
"""
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest

from pymolt.verify import _backend
from pymolt.verify._backend import UNTYPED_RAISE, SetProfileBackend, raised_through
from pymolt.verify._sinks import MemorySink

REPO = Path(__file__).resolve().parents[2]

# The scenario script: python 3.6 syntax, stdlib only. It prints {scenario: [outcome, ...]},
# an outcome being the raised type label, "<unknown>", "return" or "ambiguous".
SCENARIOS = r'''
import json, math, sys, threading, types

from pymolt.verify._backend import PrefixMatcher, SetProfileBackend
from pymolt.verify._sinks import MemorySink


class EventSink(MemorySink):
    """Every completion, in order (MemorySink folds equal calls into one record)."""

    def __init__(self):
        MemorySink.__init__(self)
        self.events = []

    def on_return(self, qualname, inputs, result_obj, site=None, admitted=False, sym=None,
                  symq=None, own=None, dur=None, tid=None, warnings=None):
        ambiguous = getattr(result_obj, "label", None) == "ambiguous:return-or-raise"
        self.events.append("ambiguous" if ambiguous else "return")

    def on_raise(self, qualname, inputs, exc_typename, site=None, admitted=False, sym=None,
                 symq=None, own=None, dur=None, tid=None, warnings=None, binding=False):
        self.events.append(exc_typename)

DEP_SRC = """
class Err(Exception):
    pass

def raises(t):
    raise t("boom")

def raises_in_with(lock):
    with lock:
        raise KeyError("inside a with block")

def none_in_with(lock):
    with lock:
        return None

def reraise_other():
    try:
        raise KeyError("inner")
    except KeyError:
        raise ValueError("outer")

def caught_inside():
    try:
        raise KeyError("inner")
    except KeyError:
        return 1

def finally_swallow_then_reraise():
    try:
        raise ValueError("first")
    finally:
        try:
            raise KeyError("in finally")
        except KeyError:
            pass

def calls_back(cb):
    return cb()

def gen():
    yield 1
    raise ValueError("in gen")

class It(object):
    def __init__(self):
        self.n = 0
    def __iter__(self):
        return self
    def __next__(self):
        self.n += 1
        if self.n > 1:
            raise StopIteration
        return self.n

class P(object):
    @property
    def bad(self):
        raise AttributeError("nope")

class Ends(object):
    def __next__(self):
        raise StopIteration

def bad_key(x):
    raise TypeError("bad key")
"""

dep = types.ModuleType("vfaketyped")
exec(compile(DEP_SRC, "<vfaketyped>", "exec"), dep.__dict__)
sys.modules["vfaketyped"] = dep


class Ctx(object):          # what pytest.raises / assertRaises do: __exit__ sees the exception
    def __enter__(self):
        return self

    def __exit__(self, t, v, tb):
        return t is not None


def s_context_manager():
    with Ctx():
        dep.raises(ValueError)


def s_except_with_a_call():
    try:
        dep.raises(TypeError)
    except TypeError:
        len("x")


def s_except_pass():
    try:
        dep.raises(TypeError)
    except TypeError:
        pass


def s_through_two_near_frames():
    def helper():
        dep.raises(KeyError)
    try:
        helper()
    except KeyError:
        len("x")


def s_wrapped_by_the_caller():
    try:
        try:
            dep.raises(KeyError)
        except KeyError:
            raise RuntimeError("wrapped")      # no call in this handler
    except RuntimeError:
        len("x")


def s_reraise_other_inside():
    try:
        dep.reraise_other()
    except ValueError:
        len("x")


def s_caught_inside_then_raise():
    dep.caught_inside()
    try:
        dep.raises(dep.Err)
    except Exception:
        len("x")


def s_finally_swallow_inside():
    try:
        dep.finally_swallow_then_reraise()
    except ValueError:
        len("x")


def s_finally_with_a_call():
    try:
        try:
            dep.raises(OSError)
        finally:
            len("x")
    except OSError:
        pass


def s_callback_then_raise():
    def cb():
        dep.raises(ValueError)
    try:
        dep.calls_back(cb)
    except ValueError:
        len("x")


def s_generator():
    try:
        for _ in dep.gen():
            pass
    except ValueError:
        len("x")


def s_for_stopiteration():
    for _ in dep.It():
        pass


def s_hasattr():
    hasattr(dep.P(), "bad")


def s_sorted_key():
    try:
        sorted([1, 2], key=dep.bad_key)
    except TypeError:
        len("x")


def s_c_raise():
    try:
        math.sqrt(-1)
    except ValueError:
        len("x")
    for _ in range(2):
        try:
            math.sqrt(-1)
        except ValueError:
            pass


def s_loop_except_pass():
    for _ in range(3):
        try:
            dep.raises(ValueError)
        except ValueError:
            pass


def s_thread():
    def run():
        try:
            dep.raises(LookupError)
        except LookupError:
            len("x")
    t = threading.Thread(target=run)
    t.start()
    t.join()


def s_raise_in_with_block():
    try:
        dep.raises_in_with(threading.Lock())
    except KeyError:
        len("x")


def s_none_in_with_block():
    dep.none_in_with(threading.Lock())


class Holder(object):
    pass


def s_except_as_store():                 # Flask's match_request: the handler makes no call
    h = Holder()
    try:
        dep.raises(KeyError)
    except KeyError as e:
        h.err = e


def s_except_tuple_pass():
    try:
        dep.raises(ValueError)
    except (KeyError, ValueError):
        pass


def s_except_pass_one_line():
    try: dep.raises(TypeError)
    except TypeError: pass


def s_through_two_near_frames_then_pass():
    def helper():
        dep.raises(KeyError)
    try:
        helper()
    except KeyError:
        pass


def s_sorted_key_pass():
    try:
        sorted([1, 2], key=dep.bad_key)
    except TypeError:
        pass


def s_raise_in_with_block_pass():        # <=3.7: "return or raise" until the exception shows
    try:
        dep.raises_in_with(threading.Lock())
    except KeyError:
        pass


def s_getattr_default():
    getattr(dep.P(), "bad", None)


def s_next_default():
    next(dep.Ends(), None)


def s_list_of_iterator():
    list(dep.It())


def s_swallowed_then_another_raise():
    try:
        (hasattr(dep.P(), "bad"), 1 / 0)
    except ZeroDivisionError:
        pass


def s_program_never_sees_the_trace_hook():
    try:
        dep.raises(ValueError)
    except ValueError:
        PROGRAM_SAW.append(sys.gettrace())
    PROGRAM_SAW.append((hasattr(dep.P(), "bad"), sys.gettrace())[1])


def _user_traced():
    seen = []

    def tracer(frame, event, arg):
        seen.append((frame.f_code.co_name, event, frame.f_lineno))
        return tracer
    sys.settrace(tracer)
    try:
        try:
            dep.raises(TypeError)
        except TypeError:
            pass
        still_owner = sys.gettrace() is tracer
    finally:
        sys.settrace(None)
    return seen, still_owner


TRACER_ALONE = _user_traced()              # the reference: no backend running


def s_user_tracer_present():             # coverage, a debugger: never displaced
    USER_TRACER_UNTOUCHED.append(_user_traced() == TRACER_ALONE)


PROGRAM_SAW = []
USER_TRACER_UNTOUCHED = []


report = {"version": list(sys.version_info[:2])}
for name, fn in sorted(globals().items()):
    if not name.startswith("s_"):
        continue
    sink = EventSink()
    be = SetProfileBackend(PrefixMatcher(["vfaketyped", "math"]), sink, source=["__main__"])
    be.compiled_wrap = False
    be.start()
    try:
        fn()
    finally:
        be.stop()
    report[name[2:]] = sink.events
report["program_saw"] = PROGRAM_SAW
report["user_tracer_untouched"] = USER_TRACER_UNTOUCHED
print(json.dumps(report, sort_keys=True))
'''

# What 3.6-3.11 record. Every raise that reaches a Python frame is typed; a raise C code
# swallows stays untyped, and so does one while another tool owns sys.settrace if its handler
# makes no call.
EXPECTED = {
    "c_raise": ["ValueError"] * 3,
    "callback_then_raise": ["ValueError", "ValueError"],
    "caught_inside_then_raise": ["return", "vfaketyped.Err"],
    "context_manager": ["ValueError"],
    "except_as_store": ["KeyError"],
    "except_pass": ["TypeError"],
    "except_pass_one_line": ["TypeError"],
    "except_tuple_pass": ["ValueError"],
    "except_with_a_call": ["TypeError"],
    "finally_swallow_inside": ["ValueError"],
    "finally_with_a_call": ["OSError"],
    # __init__, __iter__, __next__, then the StopIteration the ``for`` loop reports and swallows
    "for_stopiteration": ["return", "return", "return", "StopIteration"],
    "generator": ["return"],             # a generator call is complete at its first start
    "getattr_default": [UNTYPED_RAISE],  # swallowed in C (getattr, next, list, hasattr)
    "hasattr": [UNTYPED_RAISE],
    "list_of_iterator": ["return", "return", "return", UNTYPED_RAISE],
    "loop_except_pass": ["ValueError"] * 3,
    "next_default": [UNTYPED_RAISE],
    "program_never_sees_the_trace_hook": ["ValueError", UNTYPED_RAISE],
    "raise_in_with_block": ["KeyError"],
    "raise_in_with_block_pass": ["KeyError"],
    "reraise_other_inside": ["ValueError"],
    "sorted_key": ["TypeError"],         # through ``sorted`` (C) into our handler
    "sorted_key_pass": ["TypeError"],
    "swallowed_then_another_raise": [UNTYPED_RAISE],   # never the ZeroDivisionError's type
    "through_two_near_frames": ["KeyError"],
    "through_two_near_frames_then_pass": ["KeyError"],
    "thread": ["LookupError"],
    "user_tracer_present": [UNTYPED_RAISE],
    "wrapped_by_the_caller": ["KeyError"],
}

# setprofile on 3.12+ (a fallback there) is never armed: only the next profile event types a
# raise, so a raise handled without any call stays untyped.
EXPECTED_312 = dict(
    EXPECTED,
    c_raise=["ValueError", UNTYPED_RAISE, UNTYPED_RAISE],
    except_as_store=[UNTYPED_RAISE],
    except_pass=[UNTYPED_RAISE],
    except_pass_one_line=[UNTYPED_RAISE],
    except_tuple_pass=[UNTYPED_RAISE],
    for_stopiteration=["return", "return", "return", UNTYPED_RAISE],
    loop_except_pass=[UNTYPED_RAISE] * 3,
    raise_in_with_block_pass=[UNTYPED_RAISE],
    sorted_key_pass=[UNTYPED_RAISE],
    through_two_near_frames_then_pass=[UNTYPED_RAISE],
)


def _interpreter(minor):
    uv = shutil.which("uv")
    if uv is None:
        return None
    proc = subprocess.run([uv, "python", "find", "--no-project", minor], stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, universal_newlines=True, timeout=30, cwd="/")
    found = proc.stdout.strip()
    return found if proc.returncode == 0 and found and Path(found).is_file() else None


def _run_scenarios(python, tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYMOLT_", "PYTHON"))}
    env["PYTHONPATH"] = str(REPO)
    script = tmp_path / "scenarios.py"
    script.write_text(SCENARIOS, encoding="utf-8")
    proc = subprocess.run([python, str(script)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          universal_newlines=True, env=env, timeout=120, cwd=str(tmp_path))
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _check(report):
    version = tuple(report.pop("version"))
    # The program never runs while the trace hook is set: not in a handler, not after a swallow.
    assert report.pop("program_saw") == [None, None]
    # A tracer the program owns sees exactly what it sees without pymolt, and stays installed.
    assert report.pop("user_tracer_untouched") == [True]
    none_in_with = report.pop("none_in_with_block")
    # ``return None`` inside a ``with`` block: ambiguous on <=3.7 (no exception shows up, so it
    # stays ambiguous), a plain return from 3.8 on.
    assert none_in_with == (["ambiguous"] if version < (3, 8) else ["return"])
    assert report == (EXPECTED if version < (3, 12) else EXPECTED_312)


@pytest.mark.parametrize("minor", ["3.8", "3.11"])
def test_old_interpreters_type_raises_their_caller_handles(minor, tmp_path):
    python = _interpreter(minor)
    if python is None:
        pytest.skip("needs a uv-managed Python " + minor)
    _check(_run_scenarios(python, tmp_path))


def test_host_setprofile_types_raises_its_caller_handles(tmp_path):
    _check(_run_scenarios(sys.executable, tmp_path))


# ── in-process details ───────────────────────────────────────────────────────

PREFIX = "vfakeparked"


@pytest.fixture
def dep():
    mod = types.ModuleType(PREFIX)
    exec(compile("def boom(x):\n    raise ValueError(x)\n", f"<{PREFIX}>", "exec"), mod.__dict__)
    sys.modules[PREFIX] = mod
    yield mod
    sys.modules.pop(PREFIX, None)


class _Raises(MemorySink):
    def __init__(self):
        MemorySink.__init__(self)
        self.raises = []    # (type label, dur)

    def on_raise(self, qualname, inputs, exc_typename, site=None, admitted=False, sym=None,
                 symq=None, own=None, dur=None, tid=None, warnings=None, binding=False):
        self.raises.append((exc_typename, dur))


@pytest.fixture
def profile_path(monkeypatch):
    """Only the profile-event typing (what 3.12+ and a busy sys.settrace get)."""
    monkeypatch.setattr(_backend, "_ARM_EXCEPTION_EVENT", False)


@pytest.fixture
def armed(monkeypatch):
    """The exception-event typing of 3.6-3.11, forced on the host (sys.settrace still works
    on 3.12, only slower). Needs a free sys.settrace, so not under coverage or a debugger."""
    if sys.gettrace() is not None:
        pytest.skip("sys.settrace is taken (coverage or a debugger runs the suite)")
    monkeypatch.setattr(_backend, "_ARM_EXCEPTION_EVENT", True)
    yield
    assert sys.gettrace() is None


def _trace(body):
    sink = _Raises()
    be = SetProfileBackend(PREFIX, sink)
    be.start()
    try:
        body()
    finally:
        be.stop()
    return sink


def _spin(n):
    t = 0
    while t < n:         # pure bytecode: no profile event until it ends
        t += 1
    return t


def test_a_raise_keeps_the_duration_it_had_when_its_frame_unwound(dep, profile_path):
    t0 = time.perf_counter()
    _spin(3_000_000)
    spin = time.perf_counter() - t0

    def body():
        try:
            dep.boom(1)
        except ValueError:
            pass
        _spin(3_000_000)  # the call is completed (untyped) only at the next event

    ((raised, dur),) = _trace(body).raises
    assert raised == UNTYPED_RAISE
    assert dur < spin / 4


def test_stop_inside_the_handler_types_the_parked_raise(dep, profile_path):
    sink = _Raises()
    be = SetProfileBackend(PREFIX, sink)
    be.start()
    try:
        dep.boom(1)
    except ValueError:
        be.stop()           # stop is called inside the handler: that call event types it
    assert [r[0] for r in sink.raises] == ["ValueError"]


def test_a_raise_still_parked_at_stop_is_recorded_untyped(dep, profile_path):
    sink = _Raises()
    be = SetProfileBackend(PREFIX, sink)
    info = ("code", "q", {}, None, "q", None, None, time.perf_counter(), 0, True)
    be._park(info, sys._getframe(), None, _backend.OUTCOME_RAISE, None)
    be.stop()               # no event ever came (the thread ended, the hook was replaced)
    assert [r[0] for r in sink.raises] == [UNTYPED_RAISE]
    assert not be._parked


def test_parked_raises_are_bounded_per_thread(dep, monkeypatch):
    monkeypatch.setattr(_backend, "_PARKED_MAX", 2)
    sink = MemorySink()
    be = SetProfileBackend(PREFIX, sink)
    frame = sys._getframe()
    info = ("code", "q", None, None, "q", None, None, None, 0, False)
    for _ in range(5):
        be._park(info, frame, None, _backend.OUTCOME_RAISE, None)
    assert len(be._parked[_backend._get_ident()]) == 2


def test_a_forked_child_drops_the_other_threads_parked_raises(armed):
    be = SetProfileBackend(PREFIX, _Raises())
    frame = sys._getframe()
    info = ("code", "q", None, None, "q", None, None, None, 0, False)
    be._park(info, frame, None, _backend.OUTCOME_RAISE, None)
    be._parked[-1] = [be._parked[_backend._get_ident()][0]]   # another thread's
    be._armed[-1] = frame
    be.after_fork_child()
    assert list(be._parked) == [_backend._get_ident()]
    assert list(be._armed) == [_backend._get_ident()]
    be.stop()
    assert sys.gettrace() is None and frame.f_back.f_trace is None


def test_raised_through_follows_the_traceback_and_the_chain():
    def inner():
        raise KeyError("k")

    def outer():
        try:
            inner()
        except KeyError:
            raise RuntimeError("wrapped")

    frames = []

    def spy():
        frames.append(sys._getframe(1))
        inner()

    try:
        outer()
    except RuntimeError as e:
        tb = e.__context__.__traceback__
        while tb.tb_next is not None:
            tb = tb.tb_next
        assert raised_through(e, tb.tb_frame) == "KeyError"      # via __context__
        assert raised_through(e, sys._getframe()) == "RuntimeError"
        assert raised_through(e, sys._getframe(), lasti=-1) is None
    assert raised_through(None, sys._getframe()) is None


# ── the exception event (3.6-3.11), forced on the host ───────────────────────

PREFIX_X = "vfakearmed"

DEP_X = """
def boom(x):
    raise ValueError(x)

class Ends(object):
    def __iter__(self):
        return self
    def __next__(self):
        raise StopIteration
"""


@pytest.fixture
def depx():
    mod = types.ModuleType(PREFIX_X)
    exec(compile(DEP_X, f"<{PREFIX_X}>", "exec"), mod.__dict__)
    sys.modules[PREFIX_X] = mod
    yield mod
    sys.modules.pop(PREFIX_X, None)


def _trace_x(body):
    sink = _Raises()
    be = SetProfileBackend(PREFIX_X, sink)
    be.start()
    try:
        body()
    finally:
        be.stop()
    return [r[0] for r in sink.raises]


def test_the_exception_event_types_a_raise_handled_without_a_call(depx, armed):
    seen = []

    def body():
        try:
            depx.boom(1)
        except ValueError:
            pass
        for _ in depx.Ends():                       # the StopIteration the loop swallows
            pass
        seen.append(sys.gettrace())                 # never the backend's hook

    assert _trace_x(body) == ["ValueError", "StopIteration"]
    assert seen == [None]


def test_without_the_exception_event_the_same_raises_stay_untyped(depx, profile_path):
    def body():
        try:
            depx.boom(1)
        except ValueError:
            pass
        for _ in depx.Ends():
            pass

    assert _trace_x(body) == [UNTYPED_RAISE, UNTYPED_RAISE]


def test_an_existing_settrace_owner_is_never_displaced(depx, armed):
    events, owner = [], []

    def tracer(frame, event, arg):
        events.append((frame.f_code.co_name, event))
        return tracer

    def handled():
        try:
            depx.boom(1)
        except ValueError:
            pass

    def body():
        sys.settrace(tracer)
        try:
            handled()
            owner.append(sys.gettrace() is tracer)
        finally:
            sys.settrace(None)

    body()                                          # the reference: no backend
    alone = list(events)
    del events[:]
    assert _trace_x(body) == [UNTYPED_RAISE]        # its handler makes no call
    assert events == alone and owner == [True, True]


def test_coverage_is_never_displaced(depx, armed):
    coverage = pytest.importorskip("coverage")

    def handled():
        try:
            depx.boom(1)
        except ValueError:
            pass

    def measure(trace):
        cov = coverage.Coverage(data_file=None)
        cov.start()
        try:
            tracer = sys.gettrace()
            raised = _trace_x(handled) if trace else handled()
            still = sys.gettrace() is tracer
        finally:
            cov.stop()
        return tracer, raised, still, set(cov.get_data().lines(__file__) or ())

    tracer, _, _, alone = measure(trace=False)
    if tracer is None:
        pytest.skip("coverage measures with sys.monitoring here")
    _, raised, still, lines = measure(trace=True)
    assert raised == [UNTYPED_RAISE] and still
    assert alone <= lines                           # the traced run only adds _trace_x's lines


def test_greenlets_type_their_raises(depx, armed):
    greenlet = pytest.importorskip("greenlet")
    main = greenlet.getcurrent()

    def handles():
        try:
            depx.boom(1)
        except ValueError:
            pass
        main.switch()

    def dies():
        depx.boom(2)                                # thrown into the parent

    def switches_in_handler():
        try:
            depx.boom(3)
        except ValueError:
            main.switch()

    def body():
        greenlet.greenlet(handles).switch()
        try:
            greenlet.greenlet(dies).switch()
        except ValueError:
            pass
        g = greenlet.greenlet(switches_in_handler)
        g.switch()
        g.switch()

    assert _trace_x(body) == ["ValueError"] * 3


def test_stop_leaves_no_trace_hook_in_an_armed_thread(armed):
    be = SetProfileBackend(PREFIX_X, _Raises())
    info = ("code", "q", None, None, "q", None, None, None, 0, False)
    parked, stopped, after = threading.Event(), threading.Event(), []

    def noop():
        return None

    def run():
        be._park(info, sys._getframe(), None, _backend.OUTCOME_RAISE, None)   # arms its caller
        parked.set()
        stopped.wait(10)
        noop()                      # its call event: the hook sees the thread disarmed
        after.append(sys.gettrace())

    t = threading.Thread(target=run)
    t.start()
    parked.wait(10)
    be.stop()                       # from another thread: flushes, unarms run()'s frame
    stopped.set()
    t.join(10)
    assert after == [None] and not be._armed and not be._parked
