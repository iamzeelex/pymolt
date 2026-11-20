"""A stdlib function is observed by the profile hook, never wrapped in place.

The discover -> watch switch of a setprofile capture wraps the observed dependency functions.
Wrapping a stdlib function (``logging.Logger.info``, ``unittest.TestCase.assertEqual``) would be
visible to the program: logging's ``findCaller`` reports the wrapper as the call site, a
traceback runs through it, ``inspect.stack`` lists it. Stdlib targets stay under the (narrowed)
profile hook instead.

Runtime-layer test: pure stdlib + pytest, 3.6-compatible syntax."""
import logging
import sys
import types

import pytest

from pymolt.verify._sinks import MemorySink
from pymolt.verify.boundary_tracer import Controller

USER = "vfakestduser"
USER_SRC = '''
import logging

def log_it(logger):
    logger.info("hello")

def check(case):
    case.assertEqual(1, 2)
'''


@pytest.fixture
def user():
    mod = types.ModuleType(USER)
    sys.modules[USER] = mod
    exec(compile(USER_SRC, "<" + USER + ">", "exec"), mod.__dict__)
    yield mod
    sys.modules.pop(USER, None)


class _Grab(logging.Handler):
    def __init__(self):
        logging.Handler.__init__(self)
        self.records = []

    def emit(self, record):
        self.records.append(record)


def test_stdlib_symbols_are_not_wrapped_at_the_switch(user, monkeypatch):
    import unittest

    monkeypatch.setenv("PYMOLT_TRACE_STDLIB", "logging,unittest")
    logger = logging.getLogger("vfakestd")
    logger.setLevel(logging.INFO)
    grab = _Grab()
    logger.addHandler(grab)
    sink = MemorySink()
    ctl = Controller("vfakestd_dep", sink, prefer="setprofile", mode="auto", quiet=1000.0,
                     max_seconds=1000.0, incremental=False, source=(__name__, USER))
    ctl.start()
    try:
        user.log_it(logger)                     # observed during discover
        case = unittest.TestCase()
        with pytest.raises(AssertionError):
            user.check(case)
        assert ctl.switch("manual")
        user.log_it(logger)                     # after the switch
        with pytest.raises(AssertionError) as info:
            user.check(case)
    finally:
        ctl.stop()
        logger.removeHandler(grab)
    for record in grab.records:
        assert record.filename.startswith("<" + USER) or record.pathname == "<" + USER + ">", (
            record.pathname)
        assert record.funcName == "log_it"
    assert len(grab.records) == 2
    frames = []
    tb = info.value.__traceback__
    while tb is not None:
        frames.append(tb.tb_frame.f_code.co_filename)
        tb = tb.tb_next
    assert not any("/pymolt/verify/" in name for name in frames), frames
    assert not hasattr(logging.Logger.info, "__pymolt_wrapped__")
    assert not hasattr(unittest.TestCase.assertEqual, "__pymolt_wrapped__")
    syms = {r.get("sym") or r["qualname"] for r in sink.records.values()}
    assert any(s.startswith("logging.") for s in syms)        # still observed, by the hook


def test_installed_packages_under_a_stdlib_root_are_not_stdlib():
    # A venv's platstdlib (<venv>/lib/pythonX.Y) and a system Python's stdlib
    # (/usr/local/lib/pythonX.Y) contain site-packages: a prefix test alone took every
    # compiled dependency for stdlib and left it unwrapped (pydantic v1 on 3.11 recorded
    # nothing but json calls).
    import os

    from pymolt.verify._wrap import stdlib_file

    root = os.sep + os.path.join("usr", "local", "lib", "python3.8") + os.sep
    assert stdlib_file(root + "lib-dynload" + os.sep + "_json.so", (root,))
    assert stdlib_file(root + "json" + os.sep + "decoder.py", (root,))
    for site in ("site-packages", "dist-packages"):
        assert not stdlib_file(root + site + os.sep + "pydantic" + os.sep + "main.so", (root,))
    assert not stdlib_file("/elsewhere/pydantic/main.so", (root,))
    assert not stdlib_file(root + "x.so", ())
