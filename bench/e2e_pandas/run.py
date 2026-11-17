#!/usr/bin/env python3
"""End-to-end acceptance check: a real pandas 1.5 -> 2.2 migration under pymolt.

A toy app (requests + pandas, with an unpinned requirements.txt) is captured under Python
3.11 + pandas 1.5.3 (baseline), planned offline against ``pandas==2.2.3`` (the plan must
take pandas 1.5.3 from the baseline capture, propose ``Series.append`` -> ``pd.concat`` and
predict the scenario's break), applied with ``pymolt apply``, and captured under Python 3.12
+ pandas 2.2.3 (post-migration). Each scenario has a fixed expected verdict, so this script
is a pass/fail acceptance test for the plan -> apply -> verify pipeline, not just a demo:

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


def check_plan(root: Path, change: bool) -> tuple[bool, str]:
    """The offline plan's from-side, rewrite and predictions (acceptance criterion 1)."""
    try:
        plan = json.loads((root / ".pymolt" / "migration_plan.json").read_text())
    except (OSError, ValueError):
        return False, "no plan"
    deps = {d.get("name"): d.get("from_version") for d in plan.get("dependencies", [])}
    rewrites = [c.get("path") for c in plan.get("changes", [])]
    risks = (plan.get("risks") or {}).get("risks", [])
    append = [r for r in risks if r.get("symbol", "").endswith("Series.append")]
    ok = deps.get("pandas") == "1.5.3" and rewrites == ["toyapp/core.py"] \
        and bool(append) and append[0].get("tier") == "certain" \
        and append[0].get("fixed_by_plan") is True
    if change:
        mean = [r for r in risks if r.get("symbol", "").endswith("DataFrame.mean")]
        ok = ok and bool(mean) and mean[0].get("tier") == "high"
    return ok, f"plan from pandas {deps.get('pandas')}, rewrites {rewrites}"


def run_scenario(name: str, base: Path) -> dict:
    change, upgrade, expected = SCENARIOS[name]
    root = base / name
    make_project(root, change)
    # Explicit versions: without them setup assumes the Python pymolt itself runs on, so the
    # scenario would silently become a migration to whatever interpreter the dev venv has.
    pymolt(root, "setup", ".", "--base-python", OLD["python"], "--target-python", NEW["python"],
           "--target-env", str(ENVS / NEW["python"]), log="setup.log")
    rc_base = capture(root, "baseline", OLD)
    (root / "requirements-target.txt").write_text("pandas==2.2.3\nrequests\n")
    rc_plan = pymolt(root, "plan", ".", "--target-manifest", "requirements-target.txt",
                     log="plan.log")
    plan_ok, plan_detail = check_plan(root, change)
    rc_apply = pymolt(root, "apply", ".", log="apply.log")
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
    ok = rc_base == 0 and rc_plan == 0 and plan_ok and rc_apply == 0 and rc_post == 0 \
        and verdict == expected
    detail = plan_detail
    calibration = rep.get("calibration") or {}
    # The rewrite the plan made kept the behaviour: a prevented risk, never a miss.
    append = [r for r in calibration.get("risks", [])
              if r.get("symbol", "").endswith("Series.append")]
    ok = ok and bool(append) and append[0].get("result") == "prevented"
    if change:
        mean = [e for e in changed if e.get("symbol", "").endswith("DataFrame.mean")]
        ok = ok and len(changed) == 1 and bool(mean)
        if mean:
            new = mean[0].get("new") or [{}]
            detail += f"; mean -> {new[0].get('kind')} {new[0].get('value')}"
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
            "plan_rc": rc_plan, "apply_rc": rc_apply,
            "calibration": {k: calibration.get(k) for k in
                            ("source", "predicted", "hit", "prevented", "miss",
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


# ── README example ────────────────────────────────────────────────────────────────────────────
# The example in README.md is generated from a real run of the `change` scenario, never typed by
# hand: `run.py readme-example` formats verify's JSON report and rewrites the block between the
# markers below.

README = REPO / "README.md"
EXAMPLE_START = "<!-- example:verify:start -->"
EXAMPLE_END = "<!-- example:verify:end -->"


def _short_type(name: str) -> str:
    return name.rsplit(".", 1)[-1]


def _value_text(value) -> str:
    if isinstance(value, dict):
        if "__structure__" in value:
            dtype = value.get("dtype")
            return f"{_short_type(value['__structure__'])}({dtype})" if dtype else \
                _short_type(value["__structure__"])
        if "__dict__" in value:
            return json.dumps({str(k): v for k, v in value["__dict__"]})
        if "__opaque__" in value:
            return _short_type(value["__opaque__"])
    return json.dumps(value)


def _outcome_text(outcomes) -> str:
    """Every distinct outcome of the fact, e.g. `returns 2.0 or raises KeyError`."""
    if not outcomes:
        return "—"
    texts = []
    for o in outcomes:
        if o.get("kind") == "raise":
            texts.append(f"raises {_short_type(str(o.get('value')))}")
        else:
            texts.append(f"returns {_value_text(o.get('value'))}")
    return " or ".join(dict.fromkeys(texts))


def _drift_text(entry) -> str:
    old = set((entry.get("old_defaulted") or [[]])[0])
    new = set((entry.get("new_defaulted") or [[]])[0])
    parts = [f"default `{p}` removed" for p in sorted(old - new)]
    parts += [f"default `{p}` added" for p in sorted(new - old)]
    return ", ".join(parts) or "defaults changed"


def example_block(report: dict) -> str:
    """The README example: verify's headline numbers and its non-equal facts."""
    c = report.get("contract") or {}
    facts = c.get("fact_diff") or {}
    counts = ", ".join(f"{k} {v}" for k, v in (facts.get("counts") or {}).items() if v)
    verdict = str(report.get("verdict", "?"))
    # The same lines, in the same words, as `pymolt verify` prints (verify_cmd.py).
    head = [
        f"VERDICT: {verdict.upper()}",
        f"Static symbols: {c.get('static_targets')}   confirmed: {c.get('confirmed')}   "
        f"BLIND: {c.get('blind')}   dynamic-only: {c.get('dynamic_only')}",
        f"Contract trust: {round((c.get('trust') or 0) * 100)}%  "
        f"({c.get('trust_confirmed')}/{c.get('trust_static')} {c.get('trust_basis')})",
    ]
    if c.get("impact_filter_active"):
        head.append(f"Impact focus: {len(c.get('impact_paths') or [])} changed API path(s); "
                    f"on them {c.get('impacted_static_targets')} static, "
                    f"{c.get('impacted_confirmed')} confirmed, {c.get('impacted_blind')} BLIND, "
                    f"{c.get('impacted_dynamic_only')} dynamic-only")
    said = {"fail": "behavior changed", "pass": "no observed change on the covered surface"}
    head.append(f"Version diff: {said.get(verdict, 'inconclusive')}  ({counts})")
    rows = [("Change", "Dependency call", "Call site (your code)", "Before → after")]
    order = ["changed", "disappeared", "appeared", "rewritten", "drift", "uncomparable"]
    entries = [e for e in facts.get("entries", []) if e.get("category") in order]
    entries.sort(key=lambda e: order.index(e["category"]))
    for e in entries:
        site = e.get("old_site") or e.get("new_site") or {}
        where = f"{site.get('file')}:{site.get('line')} {site.get('func')}()"
        if e["category"] == "drift":
            change = _drift_text(e)
        elif e["category"] == "rewritten" and e.get("comparison") == "equal":
            change = "equal result"
        else:
            change = f"{_outcome_text(e.get('old'))} → {_outcome_text(e.get('new'))}"
        rows.append((e["category"].upper(), e["symbol"], where, change))
        if e.get("new_symbol"):
            rows.append(("", f"→ {e['new_symbol']}", "", ""))
    widths = [max(len(r[i]) for r in rows) for i in range(3)]
    lines = [*head, ""]
    lines += ["  ".join(r[i].ljust(widths[i]) for i in range(3)) + "  " + r[3] for r in rows]
    return "```text\n" + "\n".join(line.rstrip() for line in lines) + "\n```"


def readme_example(results: Path | None) -> int:
    text = README.read_text()
    if EXAMPLE_START not in text or EXAMPLE_END not in text:
        print(f"README.md lacks the {EXAMPLE_START} / {EXAMPLE_END} markers")
        return 2
    if results is None:
        if not env_python(OLD).exists() or not env_python(NEW).exists():
            print("environments missing: run `python bench/e2e_pandas/run.py setup` first")
            return 2
        base = RESULTS / time.strftime("%Y%m%d-%H%M%S")
        base.mkdir(parents=True)
        row = run_scenario("change", base)
        if not row["ok"]:
            print(f"the change scenario did not meet its expectations: {row}")
            return 1
        results = base / "change"
    try:
        report = json.loads((results / "report.json").read_text())
    except (OSError, ValueError) as exc:
        print(f"no usable verify report in {results}: {exc}")
        return 2
    # The README's prose describes a failing run; a stale or broken result must not replace it.
    if report.get("verdict") != "fail":
        print(f"the report in {results} is not the expected FAIL (verdict: {report.get('verdict')})")
        return 1
    block = example_block(report)
    start = text.index(EXAMPLE_START)
    end = text.index(EXAMPLE_END, start)
    README.write_text(text[: start + len(EXAMPLE_START)] + "\n" + block + "\n" + text[end:])
    print(block)
    print(f"README example updated from {results}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup")
    ex_p = sub.add_parser("readme-example", help="rewrite README.md's example from a real run")
    ex_p.add_argument("--results", type=Path, default=None,
                      help="an existing change-scenario result dir (default: run it now)")
    run_p = sub.add_parser("run")
    run_p.add_argument("scenarios", nargs="*", help=f"any of {', '.join(SCENARIOS)} (default: all)")
    args = parser.parse_args()
    unknown = [name for name in getattr(args, "scenarios", []) if name not in SCENARIOS]
    if unknown:
        parser.error(f"unknown scenario(s): {', '.join(unknown)}")
    if args.cmd == "readme-example":
        return readme_example(args.results)
    if args.cmd == "setup":
        if shutil.which("uv") is None:
            print("uv is required")
            return 2
        setup()
        return 0
    return run(args.scenarios or list(SCENARIOS))


if __name__ == "__main__":
    sys.exit(main())
