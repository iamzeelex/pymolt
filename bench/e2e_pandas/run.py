#!/usr/bin/env python3
"""End-to-end acceptance check: a real pandas 1.5 -> 2.2 migration under pymolt.

A toy app (requests + pandas) is captured under Python 3.11 + pandas 1.5.3 (baseline),
migrated (``Series.append`` -> ``pd.concat``), and captured under Python 3.12 + pandas 2.2.3
(post-migration). Each scenario has a fixed expected verdict, so this script is a pass/fail
acceptance test for the verify pipeline, not just a demo:

  equiv            equivalent migration                      -> PASS
  change           + df.mean() on mixed columns, which 1.5 answers and 2.x raises on (the
                     app's weak test stays green)            -> FAIL, CHANGED DataFrame.mean
  equiv-upgrade    equiv, and requirements.txt pins bumped between the captures -> PASS
  change-upgrade   change, with the same manifest upgrade    -> FAIL

Usage:
  python bench/e2e_pandas/run.py setup            # build the two interpreters' envs (uv)
  python bench/e2e_pandas/run.py run [SCENARIO...]  # default: all; exit 1 on any mismatch

Environments live in bench/e2e_pandas/.envs/ and results in bench/e2e_pandas/results/
(both gitignored).
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
ENVS = HERE / ".envs"
RESULTS = HERE / "results"
PYMOLT = REPO / ".venv" / "bin" / "pymolt"

OLD = {"python": "3.11", "packages": ["pandas==1.5.3", "numpy<2", "requests", "pytest"]}
NEW = {"python": "3.12", "packages": ["pandas==2.2.3", "requests", "pytest"]}

CORE = '''\
import pandas as pd
import requests


def build_url(q):
    req = requests.Request("GET", "http://x.test/a", params={"q": q})
    return req.prepare().url


def combine(a, b):
    s1 = pd.Series(a)
    s2 = pd.Series(b)
    return s1.append(s2, ignore_index=True)


def summarize(rows):
    df = pd.DataFrame(rows)
    return df.groupby("k")["v"].sum().to_dict()
'''

AVERAGE = '''

def average(rows):
    df = pd.DataFrame(rows)
    try:
        return df.mean().to_dict()
    except TypeError:
        return None
'''

TESTS = '''\
from toyapp.core import {imports}


def test_build_url():
    assert build_url("hi there") == "http://x.test/a?q=hi+there"


def test_combine():
    assert list(combine([1, 2], [3])) == [1, 2, 3]


def test_summarize():
    assert summarize([{{"k": "a", "v": 1}}, {{"k": "a", "v": 2}}]) == {{"a": 3}}
'''

TEST_AVERAGE = '''

def test_average_runs():
    # A weak test, as real suites often are: it only checks the call does not crash.
    average([{"name": "x", "v": 1}, {"name": "y", "v": 3}])
'''

SCENARIOS = {
    # name: (with_behaviour_change, upgrade_manifest, expected_verdict)
    "equiv": (False, False, "pass"),
    "change": (True, False, "fail"),
    "equiv-upgrade": (False, True, "pass"),
    "change-upgrade": (True, True, "fail"),
}


def env_python(spec: dict) -> Path:
    return ENVS / spec["python"] / "bin" / "python"


def setup() -> None:
    for spec in (OLD, NEW):
        target = ENVS / spec["python"]
        if not env_python(spec).exists():
            subprocess.run(["uv", "venv", "-q", "-p", spec["python"], str(target)], check=True)
        subprocess.run(["uv", "pip", "install", "-q", "-p", str(env_python(spec)),
                        *spec["packages"]], check=True)
        out = subprocess.run(
            [str(env_python(spec)), "-c",
             "import sys, pandas; print(sys.version.split()[0], pandas.__version__)"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        print(f"{spec['python']}: python/pandas {out}")


def make_project(root: Path, change: bool) -> None:
    (root / "toyapp").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "toyapp" / "__init__.py").write_text("")
    (root / "toyapp" / "core.py").write_text(CORE + (AVERAGE if change else ""))
    names = ["build_url", "combine", "summarize"] + (["average"] if change else [])
    (root / "tests" / "test_core.py").write_text(
        TESTS.format(imports=", ".join(sorted(names))) + (TEST_AVERAGE if change else "")
    )
    (root / "requirements.txt").write_text("pandas\nrequests\n")


def pymolt(root: Path, *args: str, log: str) -> int:
    with open(root / log, "w") as fh:
        return subprocess.run([str(PYMOLT), *args], cwd=root, stdout=fh,
                              stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL).returncode


def capture(root: Path, when: str, spec: dict) -> int:
    return pymolt(root, "capture", when, "--mode", "tests",
                  "--project-dir", ".", "--force", "--",
                  str(env_python(spec)), "-m", "pytest", "-q", "-p", "no:cacheprovider",
                  log=f"capture_{when}.log")


def run_scenario(name: str, base: Path) -> dict:
    change, upgrade, expected = SCENARIOS[name]
    root = base / name
    make_project(root, change)
    pymolt(root, "setup", ".", log="setup.log")
    rc_base = capture(root, "baseline", OLD)
    core = root / "toyapp" / "core.py"
    core.write_text(core.read_text().replace(
        "return s1.append(s2, ignore_index=True)",
        "return pd.concat([s1, s2], ignore_index=True)",
    ))
    if upgrade:
        (root / "requirements.txt").write_text("pandas==2.2.3\nrequests\n")
    rc_post = capture(root, "post-migration", NEW)
    report = subprocess.run([str(PYMOLT), "verify", ".", "--json"], cwd=root,
                            capture_output=True, text=True)
    (root / "report.json").write_text(report.stdout)
    try:
        rep = json.loads(report.stdout).get("contract") or {}
    except (ValueError, AttributeError):
        rep = {}
    facts = rep.get("fact_diff") or {}
    counts = {k: v for k, v in (facts.get("counts") or {}).items() if v}
    changed = [e for e in facts.get("entries", []) if e.get("category") == "changed"]
    verdict = rep.get("verdict")
    ok = rc_base == 0 and rc_post == 0 and verdict == expected
    detail = ""
    calibration = rep.get("calibration") or {}
    if change:
        mean = [e for e in changed if e.get("symbol", "").endswith("DataFrame.mean")]
        ok = ok and len(changed) == 1 and bool(mean)
        if mean:
            new = mean[0].get("new") or [{}]
            detail = f"mean -> {new[0].get('kind')} {new[0].get('value')}"
        # The baseline's FutureWarning predicted the break: DataFrame.mean at core.py:24 is a
        # high-tier prediction, and verify scores it a hit.
        predicted = [r for r in calibration.get("risks", [])
                     if r.get("symbol", "").endswith("DataFrame.mean")
                     and (r.get("site") or {}).get("line") == 24]
        ok = ok and bool(predicted) and predicted[0].get("tier") == "high" \
            and predicted[0].get("result") == "hit"
        if predicted:
            detail += f"; predicted {predicted[0].get('tier')} -> {predicted[0].get('result')}"
    return {"scenario": name, "expected": expected, "verdict": verdict, "ok": ok,
            "facts": counts, "trust": rep.get("trust"), "reasons": rep.get("verdict_reasons"),
            "detail": detail, "captures_rc": [rc_base, rc_post],
            "calibration": {k: calibration.get(k) for k in
                            ("source", "predicted", "hit", "miss",
                             "not_exercised")}}


def run(names: list[str]) -> int:
    if not env_python(OLD).exists() or not env_python(NEW).exists():
        print("environments missing: run `python bench/e2e_pandas/run.py setup` first")
        return 2
    base = RESULTS / time.strftime("%Y%m%d-%H%M%S")
    base.mkdir(parents=True)
    rows = [run_scenario(name, base) for name in names]
    (base / "summary.json").write_text(json.dumps(rows, indent=2))
    print(f"{'scenario':<16}{'expected':<10}{'verdict':<14}{'ok':<5}facts / detail")
    for r in rows:
        print(f"{r['scenario']:<16}{r['expected']:<10}{str(r['verdict']):<14}"
              f"{'yes' if r['ok'] else 'NO':<5}{r['facts']} {r['detail']}")
        if not r["ok"] and r["reasons"]:
            print(f"{'':<16}reasons: {r['reasons']}")
    print(f"results: {base}")
    return 0 if all(r["ok"] for r in rows) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup")
    run_p = sub.add_parser("run")
    run_p.add_argument("scenarios", nargs="*", help=f"any of {', '.join(SCENARIOS)} (default: all)")
    args = parser.parse_args()
    unknown = [name for name in getattr(args, "scenarios", []) if name not in SCENARIOS]
    if unknown:
        parser.error(f"unknown scenario(s): {', '.join(unknown)}")
    if args.cmd == "setup":
        if shutil.which("uv") is None:
            print("uv is required")
            return 2
        setup()
        return 0
    return run(args.scenarios or list(SCENARIOS))


if __name__ == "__main__":
    sys.exit(main())
