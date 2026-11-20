#!/usr/bin/env python3
"""In-process microbenchmark of the sys.monitoring backend's modes (3.12+).

    .venv/bin/python bench/load/microbench_monitoring.py [--calls 60000] [--repeat 3]

Times one workload (boundary calls into a library-shaped fake dependency, each making many
internal calls) with no tracer and under the monitoring backend in each mode: discover,
incremental, incremental after the settle (tool B off), incremental re-opened by a new line
of ours, and watch. Every boundary call must be counted in every mode. Exit 0 when the
expected cost order holds (incremental < discover, settled < incremental, settled <
re-opened), 1 when it does not.

Timing, not correctness: run it on a quiet host. It needs the repo importable (the repo
venv's python, or PYTHONPATH set to the repo root).
"""
import argparse
import statistics
import sys
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pymolt.verify import _monitoring  # noqa: E402
from pymolt.verify._backend import HAS_MONITORING  # noqa: E402

DEP_SRC = '''
class _Node:
    def __init__(self, v):
        self.v = v

    def __len__(self):
        return 1

def _helper(x):
    return x + 1

def api(x, scale=2):
    acc = 0
    for i in range(20):              # library-shaped: many internal calls per boundary call
        acc += _helper(i)
    node = _Node(x)                  # an internal construction
    return acc + len(node) + scale   # an internal dunder (tool B's worst case)
'''

# Our code, in a module of its own: the driver below is not our code, so its lines never
# turn tool B on again after the settle (a timed line of ours that is new would).
USER_SRC = '''
def _bench_local(x):
    return x * 2

def _bench_workload(api, n):
    acc = 0
    for i in range(n):
        acc += api(i)
        for _ in range(20):
            acc += _bench_local(i)
    return acc
'''

MODES = ("none", "discover", "incremental", "incremental-settled", "incremental-reopened",
         "watch")


class TallySink:
    """Steady state of a novelty-first sink: values unsampled, every call tallied."""

    def __init__(self):
        self.n = 0

    def admit(self, force=False):
        return False

    def on_call(self, qualname, inputs, site=None):
        pass

    def on_return(self, *args, **kwargs):
        pass

    def on_raise(self, *args, **kwargs):
        pass

    def tally(self, qualname, sym, site, kind, raised=None, dur=None):
        self.n += 1


def _module(name, src):
    mod = types.ModuleType(name)
    sys.modules[name] = mod
    exec(compile(src, "<" + name + ">", "exec"), mod.__dict__)
    return mod


def _make(mode, dep, user):
    if mode == "none":
        return None
    be = _monitoring.MonitoringBackend(
        "vfakebench", TallySink(), source="vfakebench_user",
        mode="discover" if mode in ("discover", "watch") else "incremental")
    if mode in ("incremental-settled", "incremental-reopened"):
        orig_start = be.start

        def start_settled():
            orig_start()
            user._bench_workload(dep.api, 1)     # arm, then tool B off
            be.settle("bench")
            # Steady state: past the de-duplication window after the settle. Busy, not
            # asleep: an idle second lets the OS park the thread on a slow core.
            until = time.monotonic() + _monitoring._SETTLE_GRACE + 0.05
            while time.monotonic() < until:
                user._bench_workload(dep.api, 1)
            if mode == "incremental-reopened":
                be._reopen()                     # what a new line of ours does
            be.sink.n = 0
        be.start = start_settled
    if mode == "watch":
        orig_watch = be.start

        def start_watch():
            orig_watch()
            user._bench_workload(dep.api, 1)     # observe, then watch the observed set
            be.enter_watch()
            be.sink.n = 0
        be.start = start_watch
    return be


def run(calls, repeat):
    dep = _module("vfakebench", DEP_SRC)
    user = _module("vfakebench_user", USER_SRC)
    results = {}
    try:
        for mode in MODES:
            times = []
            for _ in range(repeat):
                be = _make(mode, dep, user)
                if be is not None:
                    be.start()
                t0 = time.perf_counter()
                user._bench_workload(dep.api, calls)
                times.append(time.perf_counter() - t0)
                if be is not None:
                    be.stop()
                    if be.sink.n != calls:
                        raise SystemExit(f"{mode}: counted {be.sink.n} of {calls} boundary calls")
            results[mode] = statistics.median(times)
    finally:
        sys.modules.pop("vfakebench", None)
        sys.modules.pop("vfakebench_user", None)
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--calls", type=int, default=60000)
    parser.add_argument("--repeat", type=int, default=3)
    args = parser.parse_args(argv)
    if not HAS_MONITORING:
        print("sys.monitoring is 3.12+", file=sys.stderr)
        return 2
    results = run(args.calls, args.repeat)
    base = results["none"]
    print(f"{args.calls} boundary calls, 22 dependency-internal + 20 local calls each")
    for mode, t in results.items():
        print(f"  {mode:<20} {t:.3f}s  {t / base:.2f}x")
    checks = [
        ("incremental < discover", results["incremental"] < results["discover"]),
        ("incremental-settled < incremental",
         results["incremental-settled"] < results["incremental"]),
        ("incremental-settled < incremental-reopened",
         results["incremental-settled"] < results["incremental-reopened"]),
    ]
    for label, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'}: {label}")
    return 0 if all(ok for _label, ok in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
