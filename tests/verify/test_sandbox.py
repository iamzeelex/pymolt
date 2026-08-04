import os
import time

import pytest

from pymolt.verify.sandbox import run_isolated

pytestmark = pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")

_SIDE_EFFECT = []


def _raises():
    raise ValueError("boom")


def _mutate_then_return():
    _SIDE_EFFECT.append("child")  # mutates a global — must NOT leak to the parent
    return "ok"


def test_returns_value():
    r = run_isolated(lambda: 42)
    assert r.outcome == "returned"
    assert r.value == 42


def test_captures_raise():
    r = run_isolated(_raises)
    assert r.outcome == "raised"
    assert r.error == "ValueError"


def test_timeout_is_killed():
    r = run_isolated(lambda: time.sleep(5), timeout=0.3)
    assert r.outcome == "timeout"
    assert r.duration < 2.0  # killed promptly, not waited out


def test_hard_crash_is_contained():
    # os._exit bypasses Python exception handling — the child dies without a payload.
    r = run_isolated(lambda: os._exit(7))
    assert r.outcome == "crashed"
    assert r.exit_code == 7


def test_side_effects_stay_in_child():
    assert _SIDE_EFFECT == []
    r = run_isolated(_mutate_then_return)
    assert r.outcome == "returned"
    assert r.value == "ok"
    # The child mutated its own CoW copy; the parent's global is untouched.
    assert _SIDE_EFFECT == []
