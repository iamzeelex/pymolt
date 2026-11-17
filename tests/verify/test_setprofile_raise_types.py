"""setprofile (3.6-3.11) records the TYPE of a raise once the caller handles it.

A profile hook never sees the exception (CPython fetches it away before the unwinding
``return`` / ``c_exception`` event), so ``SetProfileBackend`` parks a raised call and types it
at the thread's next event, from ``sys.exc_info()`` and a traceback that passes through the
call's frame. The scenarios below run on the host (3.12, setprofile forced) and, when uv has
them, on real 3.8 and 3.11 interpreters: the runtime is pure stdlib, 3.6 syntax.
"""
import json
import os
import shutil
import subprocess
import sys
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
print(json.dumps(report, sort_keys=True))
'''

# What every interpreter records. A raise handled with no call at all, or swallowed by C code,
# stays untyped; everything else is typed.
EXPECTED = {
    "c_raise": ["ValueError", UNTYPED_RAISE, UNTYPED_RAISE],
    "callback_then_raise": ["ValueError", "ValueError"],
    "caught_inside_then_raise": ["return", "vfaketyped.Err"],
    "context_manager": ["ValueError"],
    "except_pass": [UNTYPED_RAISE],
    "except_with_a_call": ["TypeError"],
    "finally_swallow_inside": ["ValueError"],
    "finally_with_a_call": ["OSError"],
    # __init__, __iter__, __next__, then the StopIteration a ``for`` loop swallows in C
    "for_stopiteration": ["return", "return", "return", UNTYPED_RAISE],
    "generator": ["return"],             # a generator call is complete at its first start
    "hasattr": [UNTYPED_RAISE],          # the property's AttributeError, swallowed in C
    "loop_except_pass": [UNTYPED_RAISE] * 3,
    "raise_in_with_block": ["KeyError"],
    "reraise_other_inside": ["ValueError"],
    "sorted_key": ["TypeError"],         # through ``sorted`` (C) into our handler
    "through_two_near_frames": ["KeyError"],
    "thread": ["LookupError"],
    "wrapped_by_the_caller": ["KeyError"],
}


def _interpreter(minor):
    uv = shutil.which("uv")
    if uv is None:
        return None
    proc = subprocess.run([uv, "python", "find", "--no-project", minor], capture_output=True,
                          text=True, timeout=30, cwd="/")
    found = proc.stdout.strip()
    return found if proc.returncode == 0 and found and Path(found).is_file() else None


def _run_scenarios(python, tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYMOLT_", "PYTHON"))}
    env["PYTHONPATH"] = str(REPO)
    script = tmp_path / "scenarios.py"
    script.write_text(SCENARIOS, encoding="utf-8")
    proc = subprocess.run([python, str(script)], capture_output=True, text=True, env=env,
                          timeout=120, cwd=str(tmp_path))
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _check(report):
    version = tuple(report.pop("version"))
    none_in_with = report.pop("none_in_with_block")
    # ``return None`` inside a ``with`` block: ambiguous on <=3.7 (no exception shows up, so it
    # stays ambiguous), a plain return from 3.8 on.
    assert none_in_with == (["ambiguous"] if version < (3, 8) else ["return"])
    assert report == EXPECTED


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


def test_a_raise_keeps_the_duration_it_had_when_its_frame_unwound(dep):
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


def test_a_raise_still_parked_at_stop_is_recorded_untyped(dep):
    sink = _Raises()
    be = SetProfileBackend(PREFIX, sink)
    be.start()
    try:
        dep.boom(1)
    except ValueError:
        be.stop()           # stop is called inside the handler: that call event types it
    assert sink.raises[0][0] == "ValueError"

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


def test_a_forked_child_drops_the_other_threads_parked_raises():
    be = SetProfileBackend(PREFIX, _Raises())
    frame = sys._getframe()
    info = ("code", "q", None, None, "q", None, None, None, 0, False)
    be._park(info, frame, None, _backend.OUTCOME_RAISE, None)
    be._parked[-1] = [be._parked[_backend._get_ident()][0]]   # another thread's
    be.after_fork_child()
    assert list(be._parked) == [_backend._get_ident()]


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
