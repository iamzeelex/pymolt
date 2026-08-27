#!/usr/bin/env python3
"""Aggregate bench/results/mcp_*.json into per-task A/B tables."""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

RESULTS = Path(__file__).resolve().parent / "results"

METRICS = [
    ("total_tokens", "total tok"),
    ("billed_input_total", "input tok"),
    ("output_tokens", "out tok"),
    ("num_turns", "turns"),
    ("wall_ms", "wall s"),
    ("total_cost_usd", "cost $"),
]


def med(runs, key):
    vals = [r[key] for r in runs if r.get(key) is not None]
    return statistics.median(vals) if vals else None


def main(paths: list[str]) -> int:
    files = [Path(p) for p in paths] if paths else sorted(RESULTS.glob("mcp_*.json"))
    for f in files:
        d = json.loads(f.read_text())
        model = d["meta"]["model"]
        runs = [r for r in d["runs"] if "error" not in r]
        errs = [r for r in d["runs"] if "error" in r]
        print(f"\n{'='*84}\n{f.name}  model={model}  ok={len(runs)} err={len(errs)}  "
              f"complete={d['meta'].get('complete')}\n{'='*84}")
        tasks = sorted({r["task"] for r in runs})
        print(f"{'task':<12}{'arm':<8}{'n':>3}{'total tok':>11}{'input tok':>11}{'out tok':>9}"
              f"{'turns':>7}{'wall s':>9}{'cost $':>9}")
        for t in tasks:
            rows = {}
            for arm in ("bare", "mcp_only", "plugin"):
                rs = [r for r in runs if r["task"] == t and r["arm"] == arm]
                if not rs:
                    continue
                rows[arm] = rs
                print(f"{t:<12}{arm:<8}{len(rs):>3}"
                      f"{med(rs,'total_tokens'):>11,.0f}"
                      f"{med(rs,'billed_input_total'):>11,.0f}"
                      f"{med(rs,'output_tokens'):>9,.0f}"
                      f"{med(rs,'num_turns'):>7.0f}"
                      f"{med(rs,'wall_ms')/1000:>9.1f}"
                      f"{med(rs,'total_cost_usd'):>9.4f}")
            if "bare" in rows and len(rows) > 1:
                b = rows["bare"]
                for arm_name, p in rows.items():
                    if arm_name == "bare":
                        continue
                    bt, pt = med(b, "total_tokens"), med(p, "total_tokens")
                    bw, pw = med(b, "wall_ms"), med(p, "wall_ms")
                    bn, pn = med(b, "num_turns"), med(p, "num_turns")
                    print(f"{'':<12}{'Δ vs bare':<8}{'':>3}{pt-bt:>+11,.0f}"
                          f"{med(p,'billed_input_total')-med(b,'billed_input_total'):>+11,.0f}"
                          f"{med(p,'output_tokens')-med(b,'output_tokens'):>+9,.0f}"
                          f"{pn-bn:>+7.0f}{(pw-bw)/1000:>+9.1f}"
                          f"{med(p,'total_cost_usd')-med(b,'total_cost_usd'):>+9.4f}"
                          f"   [{arm_name}]")
                    print(f"{'':<12}{'':<8}   {arm_name}: {pt/bt*100:.0f}% of bare tokens"
                          f" ({bt/pt:.2f}x), {pw/bw*100:.0f}% of the time")
        for e in errs:
            print(f"  ERROR {e['task']}/{e['arm']}#{e.get('repeat')}: {e['error']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
