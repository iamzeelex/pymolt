#!/usr/bin/env python3
"""Delta accuracy: the local API-change engine against ground truth.

Question: how well does PyMolt generate the API-change facts a migration needs on the
spot, from the two environments it already builds and scoped to the symbols the project
touches? See README.md for the method.

Per pair:
  1. build (or reuse) the old and new environments with uv;
  2. take the touched symbols. For flask, use the static contact map of the flasgger
     fixture (pymolt's static contact map) plus the instance methods flasgger calls.
     For pandas, use the e2e toy app's baseline facts;
  3. GROUND TRUTH: run this directory's probe.py in both interpreters (the units and the
     introspected API diff) and calls_<pair>.py, which exercises each symbol the way the
     project uses it;
  4. LOCAL: `pymolt.delta.compute_deltas` on a scratch project whose baseline capture
     points at the old interpreter and whose target environment is the new one, with the
     warnings the baseline run emits (calls_<pair>.py in the OLD env only: what the
     baseline capture records). Without --offline it may fetch intermediate releases
     (versions only). The engine's facts are mapped back onto the units;
  5. score precision and recall per category and write results/<pair>_accuracy.json.

Run:  .venv/bin/python bench/delta_accuracy/run.py [--pair flask|pandas|all] [--offline] [--warm]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO))
sys.path.append(str(HERE.parent))  # bench/, for _paths

from _paths import scrub  # noqa: E402

from pymolt.delta import RecordedWarning, TouchedSymbol, compute_deltas, run_stats  # noqa: E402

CACHE = HERE / ".cache"
RESULTS = HERE / "results"
PYMOLT = REPO / ".venv" / "bin" / "pymolt"
E2E_ENVS = REPO / "bench" / "e2e_pandas" / ".envs"

PAIRS = {
    "flask": {
        "fixture": REPO / "tests" / "artifacts" / "flasgger",
        "deps": ["flask", "werkzeug"],
        "old": {
            "env": CACHE / "envs" / "flask-old",
            "python": "3.9",
            "packages": ["flask==2.0.3", "werkzeug==2.0.0"],
            "exclude_newer": "2022-02-16",
        },
        "new": {
            "env": CACHE / "envs" / "flask-new",
            "python": "3.12",
            "packages": ["flask==3.0.0", "werkzeug==3.0.0"],
        },
        "calls": "calls_flask.py",
        "probe_args": ["--flask-context"],
        # Instance methods / overrides flasgger uses that a static map cannot type
        # (grep of tests/artifacts/flasgger/flasgger/*.py: blueprint.add_url_rule x4,
        # app.route, app.register_blueprint, app.app_context; SwaggerView overrides
        # MethodView.dispatch_request; LazyJSONEncoder overrides DefaultJSONProvider.default).
        "extra_symbols": [
            "flask.Flask.route",
            "flask.Flask.register_blueprint",
            "flask.Flask.app_context",
            "flask.Blueprint.add_url_rule",
            "flask.views.MethodView.dispatch_request",
            "flask.json.provider.DefaultJSONProvider.default",
        ],
        "successors": {
            "flask.Markup": ["markupsafe.Markup"],
            "flask.json.JSONEncoder": ["DefaultJSONProvider", "Flask.json", "app.json"],
        },
        "dist": {"flask": "flask", "werkzeug": "werkzeug"},
    },
    "pandas": {
        "deps": ["pandas"],
        "old": {
            "env": E2E_ENVS / "3.11",
            "python": "3.11",
            "packages": ["pandas==1.5.3", "numpy<2"],
        },
        "new": {"env": E2E_ENVS / "3.12", "python": "3.12", "packages": ["pandas==2.2.3"]},
        "calls": "calls_pandas.py",
        "probe_args": [],
        # Baseline fact symbols of the toy app (bench/e2e_pandas, scenario "change").
        "symbols": [
            "pandas.DataFrame",
            "pandas.Series",
            "pandas.concat",
            "pandas.core.base.IndexOpsMixin.__iter__",
            "pandas.core.frame.DataFrame.__init__",
            "pandas.core.frame.DataFrame.groupby",
            "pandas.core.frame.DataFrame.mean",
            "pandas.core.groupby.generic.DataFrameGroupBy.__getitem__",
            "pandas.core.groupby.groupby.GroupBy.sum",
            "pandas.core.reshape.concat.concat",
            "pandas.core.series.Series.__init__",
            "pandas.core.series.Series.__len__",
            "pandas.core.series.Series.append",
            "pandas.core.series.Series.to_dict",
        ],
        "extra_symbols": [],
        "successors": {"pandas.core.series.Series.append": ["pandas.concat", "concat"]},
        "dist": {"pandas": "pandas"},
    },
}

CATEGORIES = ("REMOVED", "SIGNATURE", "BASES", "DEPRECATED", "BEHAVIOR")
USE_IMPACT = ("REMOVED", "DEPRECATED", "BEHAVIOR")


# ── environments ──────────────────────────────────────────────────────────────────────


def ensure_env(spec: dict) -> float | None:
    py = spec["env"] / "bin" / "python"
    if py.exists():
        return None
    t0 = time.perf_counter()
    spec["env"].parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["uv", "venv", "-q", "-p", spec["python"], str(spec["env"])], check=True)
    cmd = ["uv", "pip", "install", "-q", "-p", str(py), *spec["packages"]]
    if spec.get("exclude_newer"):
        cmd += ["--exclude-newer", spec["exclude_newer"]]
    subprocess.run(cmd, check=True)
    return time.perf_counter() - t0


def run_json(cmd: list[str], cwd: Path | None = None) -> tuple[dict, float]:
    t0 = time.perf_counter()
    out = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=True).stdout
    return json.loads(out), time.perf_counter() - t0


# ── touched symbols ───────────────────────────────────────────────────────────────────


def touched_symbols(name: str, pair: dict) -> tuple[list[str], float]:
    if pair.get("symbols"):
        return list(pair["symbols"]), 0.0
    from pymolt.verify.contact_map import build_contact_map  # the map `pymolt scan` builds

    t0 = time.perf_counter()
    cmap = build_contact_map(pair["fixture"])
    secs = time.perf_counter() - t0
    roots = set(pair["deps"])
    syms = sorted({c.target for c in cmap.contacts if c.target.split(".")[0] in roots})
    return sorted(set(syms) | set(pair["extra_symbols"])), secs


# ── units: a contact path truncated at its last dependency-owned step ──────────────────


def _side(path: str, fact: dict) -> tuple[list[str], dict]:
    """(dependency-owned public prefix of ``path``, step by prefix length) for one env."""
    parts = path.split(".")
    if not fact.get("module"):
        return [], {}
    mod_len = len(fact["module"].split("."))
    by_len: dict[int, dict] = {mod_len: {"exists": True, "kind": "module", "owner": fact["module"]}}
    prefix = parts[:mod_len]
    for k, step in enumerate(fact.get("steps", [])):
        if not step.get("dep"):
            break
        by_len[mod_len + k + 1] = step
        prefix = parts[: mod_len + k + 1]
    return prefix, by_len


def build_units(symbols: list[str], old: dict, new: dict) -> dict[str, dict]:
    """One unit per dependency-owned prefix of every touched path: ``flask.current_app``,
    ``flask.current_app.url_map`` and ``flask.current_app.url_map.iter_rules`` are three
    touched dependency symbols (the trailing ``str.lower`` of ``request.method.lower`` is not)."""
    units: dict[str, dict] = {}
    for path in symbols:
        fo, fn = old["facts"][path], new["facts"][path]
        po, so = _side(path, fo)
        pn, sn = _side(path, fn)
        prefix = po if len(po) >= len(pn) else pn
        for length in range(2, len(prefix) + 1):
            lo, ln = so.get(length, {}), sn.get(length, {})
            if "module" in (lo.get("kind"), ln.get("kind")):
                continue  # a submodule, not an API symbol
            if not lo.get("exists") and not ln.get("exists"):
                continue  # absent in both: project-owned attribute or a dead legacy fallback
            unit = ".".join(prefix[:length])
            prev = units.get(unit)
            if prev and (prev["old"] or prev["new"]):
                prev["contacts"] = sorted(set(prev["contacts"]) | {path})
                continue
            units[unit] = {
                "unit": unit,
                "contacts": [path],
                "old": lo,
                "new": ln,
                "exists_o": bool(lo.get("exists")),
                "exists_n": bool(ln.get("exists")),
                "runtime_warnings_o": fo.get("runtime_warnings", [])
                if length == len(prefix)
                else [],
                "runtime_warnings_n": fn.get("runtime_warnings", [])
                if length == len(prefix)
                else [],
            }
    return units


# ── diffs shared by ground truth and local ────────────────────────────────────────────

_SENTINEL = re.compile(r"<(no_default|NoDefault\.no_default|_NoDefault\.no_default|object object)>")


def _default(d):
    return None if d is None else _SENTINEL.sub("<sentinel>", d)


def signature_changes(old: list | None, new: list | None) -> list[str]:
    if not old or not new:
        return []
    o = {p[0]: p for p in old}
    n = {p[0]: p for p in new}
    new_has_varkw = any(p[1] == "VAR_KEYWORD" for p in new)
    out = []
    for name, (_, kind, default) in o.items():
        if kind in ("VAR_POSITIONAL", "VAR_KEYWORD"):
            if not any(p[1] == kind for p in new):
                out.append(f"{'*' if kind == 'VAR_POSITIONAL' else '**'}{name} removed")
            continue
        if name not in n:
            if not new_has_varkw:
                out.append(f"param {name} removed")
            continue
        _, nkind, ndefault = n[name]
        if nkind != kind:
            out.append(f"param {name} kind {kind}->{nkind}")
        if _default(default) != _default(ndefault):
            out.append(f"param {name} default {default}->{ndefault}")
    for name, (_, kind, default) in n.items():
        if name not in o and default is None and kind not in ("VAR_POSITIONAL", "VAR_KEYWORD"):
            out.append(f"param {name} added as required")
    return out


def bases_changed(old: dict, new: dict) -> list[str]:
    if old.get("kind") != "class" or new.get("kind") != "class":
        return []
    lo = [b.rsplit(".", 1)[-1] for b in old.get("bases", [])]
    ln = [b.rsplit(".", 1)[-1] for b in new.get("bases", [])]
    return [f"bases {lo} -> {ln}"] if lo != ln else []


def _mentions(messages: list[dict], leaf: str) -> list[str]:
    return [m["message"] for m in messages if leaf.strip("_") in m.get("message", "")]


# ── ground truth (introspection + use) ────────────────────────────────────────────────


def _calls_for(unit: str, calls: dict) -> list[tuple[str, dict]]:
    return [(k, v) for k, v in calls.items() if re.sub(r"\[.*\]$", "", k) == unit]


def ground_truth(units: dict, calls_o: dict, calls_n: dict) -> dict[str, dict]:
    gt = {}
    for unit, u in units.items():
        cats, notes = {}, []
        pairs = [(k, calls_o.get(k), calls_n.get(k)) for k, _ in _calls_for(unit, calls_n)]
        removed_by_use = any(
            o
            and n
            and o["outcome"][0] == "ok"
            and n["outcome"][0] == "raise"
            and n["outcome"][1] in ("AttributeError", "ImportError", "ModuleNotFoundError")
            for _, o, n in pairs
        )
        if u["exists_o"] and not u["exists_n"]:
            if pairs and not removed_by_use:
                notes.append("introspection says removed but use still works: NOT counted")
            else:
                cats["REMOVED"] = [
                    "absent in new" + (" (confirmed by use)" if removed_by_use else "")
                ]
        if u["exists_o"] and u["exists_n"]:
            sig = signature_changes(u["old"].get("shape"), u["new"].get("shape"))
            if sig:
                cats["SIGNATURE"] = sig
            bases = bases_changed(u["old"], u["new"])
            if bases:
                cats["BASES"] = bases
            warned = [w for _, _, n in pairs if n for w in n["warnings"]]
            warned += [w["message"] for w in u["runtime_warnings_n"]]
            if warned:
                cats["DEPRECATED"] = sorted(set(warned))
            changed = [
                f"{k}: {o['outcome']} -> {n['outcome']}"
                for k, o, n in pairs
                if o and n and o["outcome"] != n["outcome"]
            ]
            if changed:
                cats["BEHAVIOR"] = changed
        if not u["exists_o"] and u["exists_n"]:
            notes.append("added in new (not a change for code written against old)")
        gt[unit] = {
            "categories": cats,
            "notes": notes,
            "use": {k: [o and o["outcome"], n and n["outcome"]] for k, o, n in pairs},
        }
    return gt


# ── local: pymolt.delta (never sees new-env use) ───────────────────────────────────────


def _pins(pair: dict, side: str) -> dict[str, str]:
    out = {}
    for spec in pair[side]["packages"]:
        if "==" in spec:
            name, version = spec.split("==", 1)
            out[name.strip().lower()] = version.strip()
    return out


def _scratch_project(name: str, pair: dict) -> Path:
    """A project whose baseline capture ran in the old env and whose target env is the
    new one: compute_deltas resolves both interpreters the way it does for a real project."""
    from pymolt.ingestion.config import EnvConfig
    from pymolt.verify.models import CaptureMode, ContractSlot, ContractState

    root = CACHE / "projects" / name
    (root / ".pymolt").mkdir(parents=True, exist_ok=True)
    EnvConfig(target_env_path=str(pair["new"]["env"])).save(root / ".pymolt" / "env_config.json")
    trace = root / ".pymolt" / "baseline.jsonl"
    trace.write_text("", encoding="utf-8")
    Path(str(trace) + ".meta.json").write_text(json.dumps({"processes": [{
        "executable": str(pair["old"]["env"] / "bin" / "python"),
        "counters": {"events_written": 1}}]}), encoding="utf-8")
    ContractState(baseline=ContractSlot(
        trace_path=str(trace), captured_at="2026-11-15T00:00:00+00:00",
        mode=CaptureMode.TEST_SUITE,
    )).save(root / ".pymolt" / "contract_state.json")
    return root


def _baseline_warnings(symbols: list[str], calls_o: dict) -> list[TouchedSymbol]:
    """The touched symbols, each with the warnings the old env's baseline run emitted."""
    out = []
    for sym in symbols:
        warns = []
        for key, call in calls_o.items():
            if re.sub(r"\[.*\]$", "", key) != sym:
                continue
            for text in call.get("warnings", []):
                category, _, message = text.partition(": ")
                warns.append(RecordedWarning(category=category, message=message or text))
        out.append(TouchedSymbol(path=sym, origin="given", warnings=warns))
    return out


def engine_predict(units: dict, reports: list) -> dict[str, dict]:
    """The engine's facts mapped onto the bench units (every spelling a fact names).

    REMOVED/MOVED → REMOVED (the spelling no longer works); SIGNATURE/DEFAULT, and a deprecated
    parameter → SIGNATURE, or BASES for a bases change; DEPRECATED with new-side evidence →
    DEPRECATED; a baseline
    runtime warning on a symbol that still exists → BEHAVIOR (it predicts the break).
    """
    by_spelling: dict[str, list] = {}
    for report in reports:
        for fact in report.facts:
            for spelling in fact.spellings():
                by_spelling.setdefault(spelling, []).append(fact)
    out = {}
    for unit in units:
        cats: dict[str, list[str]] = {}
        hint = source = None
        facts = by_spelling.get(unit, [])
        for f in facts:
            label = f"{f.kind} {f.symbol}" + (f" {f.param}: {f.old} -> {f.new}" if f.param else "")
            if f.kind in ("REMOVED", "MOVED"):
                cats.setdefault("REMOVED", []).append(label)
                if f.successor:
                    hint = f.successor
                    source = ("same object (MOVED)" if f.kind == "MOVED" else
                              next((e[len("successor:"):] for e in f.evidence
                                    if e.startswith("successor:")), None))
            elif f.kind in ("SIGNATURE", "DEFAULT"):
                cats.setdefault("BASES" if "bases" in f.evidence else "SIGNATURE", []).append(label)
            elif f.kind == "DEPRECATED" and f.param:
                # a deprecated parameter (the object is not): a parameter-level change
                cats.setdefault("SIGNATURE", []).append(label)
            elif f.kind == "DEPRECATED" and any(e.startswith("deprecated-in-new:")
                                                for e in f.evidence):
                cats.setdefault("DEPRECATED", []).append(label)
            if f.kind not in ("REMOVED", "MOVED") and any(e.startswith("warning:")
                                                          for e in f.evidence):
                cats.setdefault("BEHAVIOR", []).append(
                    next(e for e in f.evidence if e.startswith("warning:")))
        out[unit] = {"categories": {k: sorted(set(v)) for k, v in cats.items()},
                     "hint": hint, "hint_source": source,
                     "facts": [f.model_dump() for f in facts if f.kind != "UNCHANGED"]}
    return out


# ── scoring ───────────────────────────────────────────────────────────────────────────


def score(gt: dict, pred: dict, cats=CATEGORIES) -> dict:
    tp = fp = fn = 0
    per = {c: {"tp": [], "fp": [], "fn": []} for c in cats}
    for unit in gt:
        g = set(gt[unit]["categories"]) & set(cats)
        p = set(pred.get(unit, {}).get("categories", {})) & set(cats)
        for c in g & p:
            per[c]["tp"].append(unit)
        for c in p - g:
            per[c]["fp"].append(unit)
        for c in g - p:
            per[c]["fn"].append(unit)
        tp += len(g & p)
        fp += len(p - g)
        fn += len(g - p)
    prec = tp / (tp + fp) if tp + fp else None
    rec = tp / (tp + fn) if tp + fn else None
    return {"tp": tp, "fp": fp, "fn": fn, "precision": prec, "recall": rec, "per_category": per}


def hint_ok(hint: str | None, expected: list[str]) -> bool:
    return bool(hint) and any(e in hint for e in expected)


# ── driver ────────────────────────────────────────────────────────────────────────────


def run_pair(name: str, offline: bool, warm: bool = False) -> dict:
    pair = PAIRS[name]
    timing = {}
    for side in ("old", "new"):
        built = ensure_env(pair[side])
        if built is not None:
            timing[f"env_build_{side}_s"] = round(built, 2)
    symbols, map_s = touched_symbols(name, pair)
    timing["contact_map_s"] = round(map_s, 2)
    sym_file = RESULTS / f"{name}_symbols.json"
    sym_file.write_text(json.dumps(scrub(symbols), indent=1))

    # ground truth: introspection + use, independent of pymolt.delta
    probes, calls = {}, {}
    for side in ("old", "new"):
        py = str(pair[side]["env"] / "bin" / "python")
        probes[side], _ = run_json(
            [py, "-I", str(HERE / "probe.py"), str(sym_file),
             f"--deps={','.join(pair['deps'])}", *pair["probe_args"]]
        )
        calls[side], secs = run_json([py, "-I", str(HERE / pair["calls"])])
        if side == "old":
            timing["baseline_run_old_s"] = round(secs, 3)
    units = build_units(symbols, probes["old"], probes["new"])
    gt = ground_truth(units, calls["old"]["calls"], calls["new"]["calls"])

    # local: the shipped engine
    old_pins, new_pins = _pins(pair, "old"), _pins(pair, "new")
    migrations = [(dep, old_pins[dep], new_pins[dep]) for dep in pair["deps"]]
    project = _scratch_project(name, pair)
    t0 = time.perf_counter()
    reports = compute_deltas(project, migrations,
                             symbols=_baseline_warnings(symbols, calls["old"]["calls"]),
                             online=not offline, use_cache=warm)
    wall = time.perf_counter() - t0
    stats = run_stats(reports[0]) if reports else {}
    timing["engine"] = stats
    timing["engine_wall_s"] = round(wall, 3)
    timing["local_total_excl_env_build_s"] = round(wall - stats.get("env_build_s", 0), 2)
    local = engine_predict(units, reports)

    scored = {"local": {"api": score(gt, local), "use": score(gt, local, USE_IMPACT)}}
    hints = {}
    for unit, g in gt.items():
        if "REMOVED" in g["categories"]:
            exp = pair["successors"].get(unit, [])
            hints[unit] = {
                "expected": exp,
                "local": [local[unit]["hint"], local[unit]["hint_source"],
                          hint_ok(local[unit]["hint"], exp)],
            }
    report = {
        "pair": name,
        "versions": {"old": probes["old"]["versions"], "new": probes["new"]["versions"],
                     "python": [probes["old"]["python"], probes["new"]["python"]]},
        "symbols": len(symbols),
        "units": len(units),
        "timing": timing,
        "score": scored,
        "hints": hints,
        "delta_reports": [r.model_dump() for r in reports],
        "per_unit": {
            u: {"ground_truth": gt[u], "local": local[u], "contacts": units[u]["contacts"]}
            for u in sorted(units)
        },
    }
    (RESULTS / f"{name}_accuracy.json").write_text(
        json.dumps(scrub(report), indent=1, default=str)
    )
    return report


def print_report(rep: dict) -> None:
    print(f"\n=== {rep['pair']}  {rep['versions']}  symbols={rep['symbols']} units={rep['units']}")
    for r in rep["delta_reports"]:
        print(f"  pymolt.delta {r['package']} {r['from']} -> {r['to']}: {r['quality']}, "
              f"{sum(1 for f in r['facts'] if f['kind'] != 'UNCHANGED')} change fact(s)")
    for view in ("api", "use"):
        s = rep["score"]["local"][view]
        p = "n/a" if s["precision"] is None else f"{s['precision']:.2f}"
        r = "n/a" if s["recall"] is None else f"{s['recall']:.2f}"
        counts = f"TP={s['tp']:>2} FP={s['fp']:>2} FN={s['fn']:>2}"
        print(f"  {'local':<22} {view:<4} {counts}  P={p} R={r}")
    print("  hints:", json.dumps(rep["hints"]))
    t = rep["timing"]
    print(f"  engine: {t['local_total_excl_env_build_s']} s excl. env builds "
          f"(wall {t['engine_wall_s']} s)  {t['engine']}")
    print(f"  {'unit':<52}{'ground truth':<34}local")
    for u, d in rep["per_unit"].items():
        g = ",".join(sorted(d["ground_truth"]["categories"])) or "-"
        lo = ",".join(sorted(d["local"]["categories"])) or "-"
        if g != "-" or lo != "-":
            print(f"  {u:<52}{g:<34}{lo}")


def print_pooled(reps: list[dict]) -> None:
    print("\n=== pooled")
    for view in ("api", "use"):
        tp = sum(r["score"]["local"][view]["tp"] for r in reps)
        fp = sum(r["score"]["local"][view]["fp"] for r in reps)
        fn = sum(r["score"]["local"][view]["fn"] for r in reps)
        p = f"{tp / (tp + fp):.2f}" if tp + fp else "n/a"
        rc = f"{tp / (tp + fn):.2f}" if tp + fn else "n/a"
        print(f"  {'local':<6} {view:<4} TP={tp:>2} FP={fp:>2} FN={fn:>2}  P={p} R={rc}")
    for r in reps:
        print(f"  {r['pair']}: engine {r['timing']['local_total_excl_env_build_s']} s "
              "excl. env builds")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pair", default="all", choices=["all", *PAIRS])
    ap.add_argument("--offline", action="store_true", help="skip the intermediate-release fetch")
    ap.add_argument("--warm", action="store_true",
                    help="let the engine reuse its probe/hint caches (default: cold)")
    args = ap.parse_args()
    RESULTS.mkdir(parents=True, exist_ok=True)
    names = list(PAIRS) if args.pair == "all" else [args.pair]
    reps = [run_pair(n, args.offline, args.warm) for n in names]
    for rep in reps:
        print_report(rep)
    if len(reps) > 1:
        print_pooled(reps)
    return 0


if __name__ == "__main__":
    sys.exit(main())
