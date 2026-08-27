#!/usr/bin/env python3
"""Score each benchmark answer against ground truth.

Tokens are only meaningful next to correctness: a wrong answer is cheap.
Each task gets a 0..1 score plus the raw claim, so a cheap-but-wrong run is
visible rather than counted as a win.
"""
from __future__ import annotations

import collections
import json
import re
import statistics
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parent
GT = BENCH / "ground_truth"
RESULTS = BENCH / "results"


def load_truth() -> dict:
    scan = json.loads((GT / "flasgger_scan.json").read_text())
    amap = json.loads((GT / "flasgger_map.json").read_text())
    assess = json.loads((GT / "flasgger_assess.json").read_text())
    roots = {r["path"] for r in scan["surfaces"]["project_roots"]}
    contacts = amap["contacts"]
    per_dep = collections.Counter(c["dep"] for c in contacts)
    changed = {p["name"]: (p.get("baseline_version"), p.get("target_version"))
               for p in assess["packages"]
               if p.get("baseline_version") != p.get("target_version")}
    return {
        "roots": roots,
        "root_count": len(roots),
        "contact_points": len(contacts),
        "distinct_symbols": len({c["target"] for c in contacts}),
        "top_modules": [m for m, _ in per_dep.most_common(5)],
        "top_symbols": [m for m, _ in collections.Counter(
            {d: len({c["target"] for c in contacts if c["dep"] == d}) for d in per_dep}
        ).most_common(5)],
        "per_dep": dict(per_dep),
        "upgrades": changed,
    }


def extract_json(text: str):
    if not text:
        return None
    blocks = re.findall(r"```(?:json)?\s*(.*?)```", text, re.S)
    for b in reversed(blocks):
        try:
            return json.loads(b.strip())
        except json.JSONDecodeError:
            continue
    for m in re.finditer(r"\{.*\}", text, re.S):
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            continue
    return None


def norm_root(p: str) -> str:
    p = (p or "").strip().strip("/").replace("./", "")
    return "." if p in ("", ".") else p


def score(task: str, ans, T: dict) -> tuple[float, str]:
    if ans is None:
        return 0.0, "no parseable JSON answer"
    if task == "T1_recon":
        got = {norm_root(r.get("path", "")) for r in ans.get("roots") or []}
        hit = len(got & T["roots"])
        prec = hit / len(got) if got else 0
        rec = hit / len(T["roots"])
        f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0
        vers = json.dumps(ans)
        found36 = "3.6" in vers
        s = 0.8 * f1 + 0.2 * found36
        return s, f"roots {sorted(got)} vs {sorted(T['roots'])}; 3.6 evidence={found36}"
    if task == "T2_upgrade":
        ups = ans.get("changed") or ans.get("upgrades") or []
        def nm(x):
            return str(x.get("name", "")).lower().replace("_", "-")
        named = {nm(u) for u in ups if isinstance(u, dict)}
        exact = {(nm(u), str(u.get("py312") if "py312" in u else u.get("to")))
                 for u in ups if isinstance(u, dict)}
        truth = {(k.lower().replace("_", "-"), str(v[1])) for k, v in T["upgrades"].items()}
        truth_names = {k for k, _ in truth}
        name_hits = named & truth_names
        ver_hits = exact & truth
        rec = len(name_hits) / len(truth_names) if truth_names else 0
        prec = len(name_hits) / len(named) if named else 0
        f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
        # half the credit for naming the right packages, half for the right versions
        vrec = len(ver_hits) / len(truth) if truth else 0
        return 0.5 * f1 + 0.5 * vrec, \
               f"{len(named)} claimed, {len(name_hits)}/{len(truth_names)} right pkg, " \
               f"{len(ver_hits)}/{len(truth)} right pkg+version"

    if task == "T3_contact":
        n = ans.get("total_contact_points")
        # The question ("every distinct symbol ... count them") admits two correct
        # readings: 227 call sites or 55 distinct symbols. Credit whichever is closer.
        acc = 0.0
        if isinstance(n, (int, float)) and n > 0:
            acc = max(0.0, 1 - min(abs(n - T["contact_points"]) / T["contact_points"],
                                   abs(n - T["distinct_symbols"]) / T["distinct_symbols"]))
        got_top = [str(m.get("module", "")).lower() for m in (ans.get("top_modules") or [])][:5]
        best = 0.0
        for truth_top in (T["top_modules"], T["top_symbols"]):
            ov = len(set(got_top) & set(truth_top)) / 5
            t1 = 1.0 if got_top and got_top[0] == truth_top[0] else 0.0
            best = max(best, 0.6 * ov + 0.4 * t1)
        return 0.5 * acc + 0.5 * best, \
               f"claimed {n} (truth {T['contact_points']} call sites / {T['distinct_symbols']} symbols); top={got_top}"

    return float("nan"), "n/a"


def main(paths: list[str]) -> int:
    T = load_truth()
    print(f"ground truth: {T['root_count']} roots, {T['contact_points']} contact points, "
          f"{T['distinct_symbols']} distinct symbols, {len(T['upgrades'])} version changes")
    files = [Path(p) for p in paths] or sorted(RESULTS.glob("mcp_*.json"))
    for f in files:
        d = json.loads(f.read_text())
        if not d["runs"]:
            continue
        print(f"\n{'='*96}\n{f.name}  model={d['meta']['model']}\n{'='*96}")
        rows = collections.defaultdict(list)
        for r in d["runs"]:
            if "error" in r or r["task"] == "T0_noop":
                continue
            s, why = score(r["task"], extract_json(r.get("result_text", "")), T)
            r["score"] = round(s, 3)
            r["score_detail"] = why
            rows[(r["task"], r["arm"])].append(r)
        f.write_text(json.dumps(d, indent=2))
        for task in sorted({k[0] for k in rows}):
            for arm in ("bare", "mcp_only", "plugin"):
                rs = rows.get((task, arm))
                if not rs:
                    continue
                scores = [x["score"] for x in rs]
                print(f"  {task:<12}{arm:<10} score median={statistics.median(scores):.2f} "
                      f"all={[f'{s:.2f}' for s in scores]}")
                for x in rs:
                    print(f"      #{x['repeat']}: {x['score_detail']}")
            print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
