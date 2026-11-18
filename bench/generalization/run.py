#!/usr/bin/env python3
"""Generalization matrix runner: does pymolt work outside the pandas/numpy world?

Each toy under bench/generalization/toys/<name>/toy.json is a small app plus a verified
ground truth (see SPEC.md). For every toy and scenario (equiv, change) the runner drives
setup -> capture baseline -> plan -> apply -> manual migration -> capture post-migration
-> verify in a fresh scratch copy, then scores metrics A-F against the ground truth.

Usage:
  python bench/generalization/run.py envs [TOY...]
  python bench/generalization/run.py run [TOY...] --out DIR [--parallel N] [--record]
  python bench/generalization/run.py score DIR          # re-score an existing run
  python bench/generalization/run.py diff DIR_A DIR_B   # compare two scorecards

``--record`` (stage 5's guard) wraps the manual migration in ``apply --step ID --record --agent``
/ ``--record --done --agent``, one step at a time, each edit under the plan step it matches (an
edit no step matches stays a hand edit), and adds three must-FAIL scenarios per toy with manual
edits: one manual edit's call gets a bogus keyword (a new raise), recorded as ``preserve``
(``mut-preserve``: every record passes ``--claim preserve``, which tightens a step whose claim is
``intended``), recorded under the step's claim and then rewritten to ``intended`` with ``any`` in
the ledger as an agent could (``mut-intended``), or left unrecorded beside the recorded edits
(``mut-beside``). None may PASS.
It writes ``record_summary.json`` next to the scorecard.

Stdlib only. See README.md for what each metric means.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
TOYS = HERE / "toys"
ENVS = HERE / ".envs"
DEFAULT_PYMOLT = REPO / ".venv" / "bin" / "pymolt"
KINDS = {"REMOVED", "MOVED", "SIGNATURE", "DEFAULT", "DEPRECATED", "BEHAVIOR"}
SCENARIOS = ("equiv", "change")
SKEW_MARGIN = 0.2


# ── toy loading and validation ────────────────────────────────────────────────────────────


def validate(toy: object) -> list[str]:
    """Problems with a parsed toy.json against the SPEC format (empty when valid)."""
    errs: list[str] = []
    if not isinstance(toy, dict):
        return ["toy.json is not an object"]

    def need(key: str, typ: type) -> object:
        val = toy.get(key)
        if not isinstance(val, typ):
            errs.append(f"'{key}' missing or not {typ.__name__}")
            return typ()
        return val

    if not isinstance(toy.get("name"), str) or not toy.get("name"):
        errs.append("'name' missing")
    for side in ("old", "new"):
        spec = toy.get(side)
        if not isinstance(spec, dict) or not isinstance(spec.get("python"), str) \
                or not isinstance(spec.get("requirements"), list):
            errs.append(f"'{side}' needs 'python' (str) and 'requirements' (list)")
    need("manifest", list)
    need("target_manifest", list)
    files = need("files", dict)
    if not any(p.startswith("tests/") or "/tests/" in p or Path(p).name.startswith("test_")
               for p in files):
        errs.append("'files' has no test file")
    for i, e in enumerate(toy.get("manual_migration", []) or []):
        if not (isinstance(e, dict) and {"file", "old", "new"} <= set(e)):
            errs.append(f"manual_migration[{i}] needs file/old/new")
    scen = toy.get("scenarios")
    if not isinstance(scen, dict):
        errs.append("'scenarios' missing")
    else:
        for name in SCENARIOS:
            s = scen.get(name)
            if not isinstance(s, dict) or not isinstance(s.get("edits"), list) \
                    or not isinstance(s.get("expected_verdict"), str):
                errs.append(f"scenarios.{name} needs 'edits' (list) and 'expected_verdict'")
                continue
            for i, e in enumerate(s["edits"]):
                if not (isinstance(e, dict) and "file" in e
                        and ("append" in e or {"old", "new"} <= set(e) or "content" in e)):
                    errs.append(f"scenarios.{name}.edits[{i}] needs file and an edit")
    gt = toy.get("ground_truth")
    if not isinstance(gt, list):
        errs.append("'ground_truth' missing or not a list")
    else:
        for i, g in enumerate(gt):
            if not isinstance(g, dict) or not isinstance(g.get("symbol"), str) \
                    or g.get("kind") not in KINDS:
                errs.append(f"ground_truth[{i}] needs 'symbol' and 'kind' in {sorted(KINDS)}")
    for i, w in enumerate(toy.get("expected_warnings", []) or []):
        if not (isinstance(w, dict) and "symbol" in w and "category" in w):
            errs.append(f"expected_warnings[{i}] needs symbol/category")
    return errs


def load_toys(names: list[str]) -> dict[str, dict]:
    found: dict[str, dict] = {}
    dirs = sorted(p for p in TOYS.iterdir() if p.is_dir()) if TOYS.is_dir() else []
    wanted = set(names)
    for name in sorted(wanted - {d.name for d in dirs}):
        print(f"skip {name}: no such toy directory")
    for d in dirs:
        if wanted and d.name not in wanted:
            continue
        path = d / "toy.json"
        if not path.exists():
            print(f"skip {d.name}: no toy.json")
            continue
        try:
            toy = json.loads(path.read_text())
        except ValueError as exc:
            print(f"skip {d.name}: toy.json is not valid JSON ({exc})")
            continue
        errs = validate(toy)
        if errs:
            print(f"skip {d.name}: invalid toy.json: " + "; ".join(errs))
            continue
        toy["_key"] = d.name
        found[d.name] = toy
    return found


# ── process helpers ───────────────────────────────────────────────────────────────────────


def sh(cmd: list[str], cwd: Path, log: Path, *, stdout: Path | None = None,
       timeout: int = 1800, env_extra: dict | None = None) -> tuple[int, float]:
    """Run ``cmd``; the log keeps the command line, stdout (unless redirected) and stderr."""
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    env.update(env_extra or {})
    start = time.time()
    with open(log, "w") as lf:
        lf.write("$ " + " ".join(cmd) + "\n")
        lf.flush()
        out = open(stdout, "w") if stdout else lf
        try:
            rc = subprocess.run(cmd, cwd=cwd, stdout=out, stderr=lf, stdin=subprocess.DEVNULL,
                                timeout=timeout, env=env).returncode
        except subprocess.TimeoutExpired:
            lf.write(f"\n[runner] timed out after {timeout}s\n")
            rc = 124
        except OSError as exc:
            lf.write(f"\n[runner] {exc}\n")
            rc = 127
        finally:
            if stdout:
                out.close()
    return rc, time.time() - start


def env_dir(key: str, side: str) -> Path:
    return ENVS / key / side


def env_python(key: str, side: str) -> Path:
    return env_dir(key, side) / "bin" / "python"


def build_env(toy: dict, side: str) -> tuple[bool, str]:
    """Create ENVS/<toy>/<side> with uv, idempotently (a hash of the spec marks it built)."""
    key = toy["_key"]
    spec = toy[side]
    target = env_dir(key, side)
    stamp = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:16]
    ok_file = target / ".gen_ok"
    if ok_file.exists() and ok_file.read_text().strip() == stamp and env_python(key, side).exists():
        return True, "cached"
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    log = target.parent / f"build_{side}.log"
    rc, _ = sh(["uv", "venv", "-q", "-p", spec["python"], str(target)], target.parent, log)
    if rc == 0:
        extra = None
        if spec.get("build_constraints"):
            cfile = target.parent / f"build_constraints_{side}.txt"
            cfile.write_text("\n".join(spec["build_constraints"]) + "\n")
            extra = {"UV_BUILD_CONSTRAINT": str(cfile)}
        rc, _ = sh(["uv", "pip", "install", "-q", "-p", str(env_python(key, side)),
                    *spec["requirements"]], target.parent, target.parent / f"install_{side}.log",
                   env_extra=extra)
        log = target.parent / f"install_{side}.log"
    if rc != 0:
        return False, f"see {log}"
    ok_file.write_text(stamp)
    return True, "built"


def ensure_envs(toy: dict) -> tuple[bool, str]:
    notes = []
    for side in ("old", "new"):
        ok, note = build_env(toy, side)
        notes.append(f"{side}: {note}")
        if not ok:
            return False, "; ".join(notes)
    return True, "; ".join(notes)


# ── a scenario run ────────────────────────────────────────────────────────────────────────


def write_files(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)


def apply_edit(root: Path, edit: dict, manual: bool = False) -> str | None:
    """Apply one edit; returns a note when it did nothing.

    Manual-migration entries replace the first occurrence of ``old`` (every one with
    ``replace_all``); a missing ``old`` is a note, never fatal (``if_present`` just says the
    entry is optional)."""
    p = root / edit["file"]
    if "content" in edit:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(edit["content"])
        return None
    if not p.exists():
        return f"{edit['file']}: no such file"
    text = p.read_text()
    if "append" in edit:
        p.write_text(text + edit["append"])
        return None
    if edit["old"] in text:
        count = -1 if (edit.get("replace_all") or not manual) else 1
        p.write_text(text.replace(edit["old"], edit["new"], count))
        return None
    if edit.get("if_present") or edit["new"] in text:
        return None  # already in the wanted shape (the plan fixed it)
    return f"{edit['file']}: old text not found: {edit['old'][:60]!r}"


def pytest_summary(log: Path) -> str:
    try:
        lines = [x for x in log.read_text().splitlines() if x.strip()]
    except OSError:
        return ""
    for line in reversed(lines):
        if re.search(r"\b(passed|failed|error|errors|no tests ran)\b", line):
            return line.strip(" =")
    return lines[-1] if lines else ""


MUTATIONS = ("mut-preserve", "mut-intended", "mut-beside")
_KEYWORD = "pymolt_mutation=1"


def _call_span(text: str) -> tuple[int, int] | None:
    """``(open paren, close paren)`` of the first dotted call in ``text`` outside a ``def``,
    ``class``, decorator or import line (the call a mutation gives a bogus keyword)."""
    pos = 0
    for line in text.splitlines(keepends=True):
        bare = line.strip()
        if bare.startswith(("def ", "class ", "@", "import ", "from ", "#")):
            pos += len(line)
            continue
        for m in re.finditer(r"[A-Za-z_][\w]*(?:\.[A-Za-z_]\w*)+\(", line):
            start = pos + m.end() - 1
            depth, i, quote = 0, start, None
            while i < len(text):
                c = text[i]
                if quote:
                    quote = None if c == quote else quote
                elif c in "'\"":
                    quote = c
                elif c == "(":
                    depth += 1
                elif c == ")":
                    depth -= 1
                    if depth == 0:
                        return start, i
                i += 1
        pos += len(line)
    return None


def mutate(text: str) -> str | None:
    """``text`` with its first dotted call given a bogus keyword (it now raises TypeError)."""
    span = _call_span(text)
    if span is None:
        return None
    start, end = span
    inner = text[start + 1:end].strip()
    return text[:end] + (f", {_KEYWORD}" if inner else _KEYWORD) + text[end:]


def _read(path: Path) -> str:
    try:
        return path.read_text()
    except OSError:
        return ""


def _plan_steps(root: Path) -> list[dict]:
    return jload(root / ".pymolt" / "migration_plan.json").get("steps") or []


def match_step(steps: list[dict], edit: dict) -> str | None:
    """The agent or person step a manual edit makes: the first, in plan order, with a site in
    the edit's file whose symbol's last name is in the text the edit replaces, else one whose
    site's line is in it; None (a hand edit) otherwise."""
    old = edit.get("old") or ""
    mine = [s for s in steps if s.get("executor") != "pymolt" and s.get("phase") != "env"
            and any(a.get("file") == edit.get("file") for a in s.get("where") or ())]
    for s in mine:
        names = [(s.get("symbol") or {}).get("old"), *(a.get("symbol") for a in s["where"])]
        if any(n and n.rsplit(".", 1)[-1] in old for n in names):
            return s["id"]
    for s in mine:
        if any(a.get("file") == edit.get("file") and (a.get("site_text") or "~") in old
               for a in s["where"]):
            return s["id"]
    return None


def run_manual(root: Path, toy: dict, step, warnings: list[str], pm: str, *,
               record: bool, variant: str | None) -> dict:
    """The manual migration, under records in record mode (module docstring). Returns
    ``{"records": [(step, [edit index])], "mutated": index or None, "unrecorded": [index]}``."""
    edits = list(toy.get("manual_migration") or [])
    info: dict = {"records": [], "mutated": None, "unrecorded": []}
    steps = _plan_steps(root) if record else []
    matched = [match_step(steps, e) if record else None for e in edits]
    target = None
    if variant:
        # Not in a file with coroutines: a raise there can leave a test awaiting forever.
        target = next((i for i, e in enumerate(edits) if matched[i] and "content" not in e
                       and "async def" not in _read(root / e["file"])
                       and mutate(e.get("new") or "") is not None), None)
        if target is not None:
            edits[target] = {**edits[target], "new": mutate(edits[target]["new"])}
            info["mutated"] = target
    open_step = None
    by_id = {s["id"]: s for s in steps}

    def close() -> None:
        nonlocal open_step
        if open_step is not None:
            pairs = record_pairs(toy, by_id.get(open_step) or {})
            info.setdefault("pairs", {})[open_step] = pairs
            tighten = ["--claim", "preserve"] if variant == "mut-preserve" else []
            step(f"done_{open_step}_{len(info['records'])}",
                 [pm, "apply", ".", "--step", open_step, "--record", "--done", "--agent",
                  *tighten, *(x for pair in pairs for x in ("--pair", pair))])
            open_step = None

    later = []
    for i, edit in enumerate(edits):
        sid = matched[i]
        if variant == "mut-beside" and i == target:
            later.append(i)                    # applied after the records close, unrecorded
            continue
        if sid != open_step:
            close()
            if sid is not None:
                step(f"record_{sid}_{len(info['records'])}",
                     [pm, "apply", ".", "--step", sid, "--record", "--agent"])
                open_step = sid
                info["records"].append([sid, []])
        if sid is not None:
            info["records"][-1][1].append(i)
        else:
            info["unrecorded"].append(i)
        w = apply_edit(root, edit, manual=True)
        if w:
            warnings.append(f"manual_migration: {w}")
    close()
    for i in later:
        info["unrecorded"].append(i)
        w = apply_edit(root, edits[i], manual=True)
        if w:
            warnings.append(f"manual_migration: {w}")
    if variant == "mut-intended" and target is not None:
        _rewrite_claim(root)
    return info


def record_pairs(toy: dict, plan_step: dict) -> list[str]:
    """The pairing anchors an agent adds to its record (``--pair OLD=NEW``) when the step names
    no successor: the step's old symbol and the qualified name, in the target environment, of
    the ground truth's dotted successor for it (what the agent wrote; it can look the name
    up)."""
    old = (plan_step.get("symbol") or {}).get("old")
    if not old or (plan_step.get("symbol") or {}).get("new"):
        return []
    out = []
    for g in toy.get("ground_truth") or ():
        succ = g.get("successor") or ""
        if leaf(g.get("symbol")) != leaf(old) or not re.fullmatch(r"[A-Za-z_][\w.]*", succ) \
                or "." not in succ:
            continue
        new = resolve_object(toy["_key"], "new", succ)
        if new and new != old:
            out.append(f"{old}={new}")
    return list(dict.fromkeys(out))


def _rewrite_claim(root: Path) -> None:
    """What an agent with a shell could do: rewrite the claim of the record that holds the
    mutation to ``intended`` (unattested) in the ledger."""
    path = root / ".pymolt" / "apply_ledger.json"
    data = jload(path)
    epochs = {e.get("epoch") for e in data.get("entries") or ()
              if _KEYWORD in ((e.get("anchor_new") or {}).get("stmt") or "")}
    for e in data.get("entries") or ():
        if e.get("epoch") in epochs:
            e["claim"], e["expect"] = "intended", "any"
    path.write_text(json.dumps(data, indent=2))


def run_scenario(toy: dict, scn: str, out: Path, pymolt: Path, timeout: int,
                 online: bool = False, record: bool = False,
                 variant: str | None = None) -> dict:
    key = toy["_key"]
    root = out / key / (variant or scn)
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    steps: dict[str, dict] = {}
    warnings: list[str] = []

    def step(name: str, cmd: list[str], *, stdout: str | None = None, to: int | None = None) -> int:
        rc, secs = sh(cmd, root, root / f"{name}.log",
                      stdout=(root / stdout) if stdout else None, timeout=to or timeout)
        steps[name] = {"rc": rc, "seconds": round(secs, 1)}
        return rc

    spec = toy["scenarios"][scn]
    write_files(root, toy["files"])
    for edit in spec["edits"]:  # in place before the baseline: both captures must see them
        w = apply_edit(root, edit)
        if w:
            warnings.append(f"edit: {w}")
    (root / "requirements.txt").write_text("\n".join(toy["manifest"]) + "\n")
    old_py, new_py = env_python(key, "old"), env_python(key, "new")
    pm = str(pymolt)
    pytest_cmd = ["-m", "pytest", "-q", "-p", "no:cacheprovider"]

    step("setup", [pm, "setup", ".", "--manifest", "requirements.txt", "--tool", "uv",
                   "--base-python", toy["old"]["python"], "--target-python", "3.12"])
    step("capture_baseline", [pm, "capture", "baseline", "--mode", "tests", "--project-dir",
                              ".", "--force", "--", str(old_py), *pytest_cmd])
    (root / "requirements-target.txt").write_text("\n".join(toy["target_manifest"]) + "\n")
    step("plan", [pm, "plan", ".", "--target-manifest", "requirements-target.txt", "--agent"],
         stdout="plan_agent.json")
    plan_file = root / ".pymolt" / "migration_plan.json"
    if online:  # keep the offline plan to report both; the online one is the one applied
        if plan_file.exists():
            shutil.copy(plan_file, root / "migration_plan_offline.json")
        step("plan_online", [pm, "plan", ".", "--target-manifest", "requirements-target.txt",
                             "--agent", "--online"], stdout="plan_agent_online.json")
    step("apply", [pm, "apply", "."])
    manual = run_manual(root, toy, step, warnings, pm, record=record, variant=variant)
    short = min(timeout, 300) if variant else None    # a mutation must not hang the matrix
    step("tests_new", [str(new_py), *pytest_cmd], to=short)
    step("capture_post", [pm, "capture", "post-migration", "--mode", "tests", "--project-dir",
                          ".", "--force", *(["--accept-failing"] if variant else []), "--",
                          str(new_py), *pytest_cmd], to=short)
    step("verify", [pm, "verify", ".", "--json"], stdout="verify.json")
    result = {"toy": key, "scenario": scn, "steps": steps, "warnings": warnings,
              "tests_new": pytest_summary(root / "tests_new.log"),
              "seconds": round(sum(s["seconds"] for s in steps.values()), 1)}
    if record:
        result.update({"record": True, "variant": variant, "manual": manual})
    (root / "result.json").write_text(json.dumps(result, indent=2))
    return result


# ── reading a scenario's artifacts ────────────────────────────────────────────────────────


def jload(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def events(path: Path) -> list[dict]:
    out = []
    try:
        for line in path.read_text().splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if isinstance(ev, dict):
                out.append(ev)
    except OSError:
        pass
    return out


def sym_match(gt: str, cand: str | None) -> bool:
    """A ground-truth symbol against a spelled symbol: equal, or the same leaf under a
    matching parent (``pandas.Series.append`` ~ ``pandas.core.series.Series.append``)."""
    if not gt or not cand:
        return False
    if gt == cand or cand.endswith("." + gt) or gt.endswith("." + cand):
        return True
    g, c = gt.split("."), cand.split(".")
    if g[-1] != c[-1]:
        return False
    return len(g) == 1 or g[-2].lower() in [x.lower() for x in c[:-1]]


def read_scenario(root: Path) -> dict:
    verify = jload(root / "verify.json")
    return {
        "root": root,
        "result": jload(root / "result.json"),
        "plan": jload(root / ".pymolt" / "migration_plan.json"),
        "plan_offline": jload(root / "migration_plan_offline.json"),
        "verify": verify,
        "contract": verify.get("contract") or {},
        "baseline": events(root / ".pymolt" / "contract_traces" / "baseline.jsonl"),
        "post": events(root / ".pymolt" / "contract_traces" / "post_migration.jsonl"),
    }


def plan_candidates(plan: dict) -> list[dict]:
    """Every API fact the plan names: ``impacts`` plus the facts behind its risks."""
    cands = []
    for imp in plan.get("impacts") or []:
        if imp.get("kind") == "INCONCLUSIVE":
            continue
        cands.append({"symbol": imp.get("path"), "aliases": imp.get("aliases") or [],
                      "kind": imp.get("kind"), "successor": imp.get("replacement_path"),
                      "package": imp.get("package"), "risk": imp.get("risk"), "src": "impact"})
    for risk in (plan.get("risks") or {}).get("risks") or []:
        for fact in [risk.get("fact") or {}, *(risk.get("facts") or [])]:
            if fact.get("symbol"):
                cands.append({"symbol": fact["symbol"], "aliases": fact.get("aliases") or [],
                              "kind": fact.get("kind"), "successor": fact.get("successor"),
                              "package": risk.get("package"), "risk": risk.get("tier"),
                              "src": "risk"})
    return cands


def spellings(c: dict) -> list[str]:
    return [c["symbol"], *c.get("aliases", [])]


def leaf(s: str | None) -> str:
    return (s or "").rsplit(".", 1)[-1]


# ── metrics ───────────────────────────────────────────────────────────────────────────────


def metric_a(toy: dict, scens: dict[str, dict], plan_key: str = "plan") -> dict:
    cands = [dict(c, scn=n) for n, s in scens.items() for c in plan_candidates(s[plan_key])]
    per, matched_idx = [], set()
    for g in toy["ground_truth"]:
        canon = None
        if g["kind"] != "BEHAVIOR":
            canon = resolve_object(toy["_key"], "old", g["symbol"])

        def names(sp: str, g: dict = g, canon: str | None = canon) -> bool:
            """The same symbol: by spelling, by the module it lives in when that module is
            removed (a member-level REMOVED under a removed module), or by the resolved object
            (``asyncio.queues.Queue.__init__`` is a use of ``asyncio.Queue``)."""
            if sym_match(g["symbol"], sp) or sym_match(sp, g["symbol"]):
                return True
            if g["kind"] == "REMOVED" and sp.startswith(g["symbol"] + "."):
                return True
            base = sp.rsplit(".", 1)[0] if sp.rsplit(".", 1)[-1] in _CONSTRUCTORS else None
            return bool(canon and base == canon)

        hits = [(i, c) for i, c in enumerate(cands)
                if (not g.get("scenario") or c["scn"] == g["scenario"])
                and any(names(sp) for sp in spellings(c))]
        exact = [(i, c) for i, c in hits if c["kind"] == g["kind"]]
        pick = (exact or hits or [(None, None)])[0]
        matched_idx.update(i for i, _ in hits)
        c = pick[1]
        succ = g.get("successor")
        succ_ok = None
        if succ or (c and c.get("successor")):
            succ_ok = bool(c and succ and c.get("successor")
                           and leaf(succ) == leaf(c["successor"]))
        per.append({"symbol": g["symbol"], "kind": g["kind"],
                    "sites": g.get("sites") or ([g["site"]] if g.get("site") else []),
                    "scenario": g.get("scenario"), "fixable": g.get("fixable"),
                    "status": "detected" if exact else ("other_kind" if hits else "missed"),
                    "plan_kind": c["kind"] if c else None, "src": c["src"] if c else None,
                    "successor": succ, "plan_successor": c.get("successor") if c else None,
                    "successor_ok": succ_ok})
    fp, seen = [], set()
    for i, c in enumerate(cands):
        if i not in matched_idx and (c["symbol"], c["kind"]) not in seen and c["src"] == "impact":
            seen.add((c["symbol"], c["kind"]))
            fp.append(f"{c['symbol']} {c['kind']}")
    # BEHAVIOR entries belong to verify (the delta cannot see them): they stay in per_entry and
    # are reported on their own, but are left out of the recall.
    behavior = [p for p in per if p["kind"] == "BEHAVIOR"]
    scored = [p for p in per if p["kind"] != "BEHAVIOR"]
    n = len(scored)
    det = sum(p["status"] == "detected" for p in scored)
    anyk = sum(p["status"] != "missed" for p in scored)
    with_succ = [p for p in scored if p["successor"]]
    return {"total": n, "detected": det, "detected_any_kind": anyk,
            "recall": det / n if n else None, "recall_any_kind": anyk / n if n else None,
            "behavior_excluded": {"total": len(behavior),
                                  "seen_by_plan": sum(p["status"] != "missed" for p in behavior),
                                  "symbols": [p["symbol"] for p in behavior]},
            "successors_expected": len(with_succ),
            "successors_correct": sum(bool(p["successor_ok"]) for p in with_succ),
            "fp_candidates": fp, "per_entry": per}


def risks_for(plan: dict, symbol: str) -> list[dict]:
    return [r for r in (plan.get("risks") or {}).get("risks") or []
            if sym_match(symbol, r.get("symbol")) or sym_match(r.get("symbol") or "", symbol)]


TIER_ORDER = {"certain": 4, "high": 3, "medium": 2, "low": 1}


def _whole_diff(s: dict) -> dict:
    """The scenario's fact diff over the whole surface (``fact_diff`` is narrowed to the
    plan's impacted symbols when an impact filter is active)."""
    c = s["contract"]
    return c.get("all_fact_diff") or c.get("fact_diff") or {}


def metric_b(toy: dict, scens: dict[str, dict]) -> dict:
    change = scens.get("change")
    expected = (toy["scenarios"]["change"].get("expected_changed") or [])
    per = []
    for sym in expected:
        entry = {"symbol": sym, "tier": None, "result": None, "fixed_by_plan": None}
        if change:
            rs = risks_for(change["plan"], sym)
            if rs:
                best = max(rs, key=lambda r: TIER_ORDER.get(r.get("tier"), 0))
                entry["tier"], entry["fixed_by_plan"] = best.get("tier"), best.get("fixed_by_plan")
            cal = (change["contract"].get("calibration") or {}).get("risks") or []
            results = [c.get("result") for c in cal if sym_match(sym, c.get("symbol"))]
            for want in ("hit", "prevented", "miss", "not_exercised"):
                if want in results:
                    entry["result"] = want
                    break
        per.append(entry)
    # calibration over both scenarios, and changed facts nobody predicted
    totals: dict[str, int] = {}
    unpredicted = []
    for name, s in scens.items():
        cal = s["contract"].get("calibration") or {}
        for k in ("predicted", "hit", "prevented", "miss", "not_exercised"):
            totals[k] = totals.get(k, 0) + int(cal.get(k) or 0)
        for e in _whole_diff(s).get("entries") or []:
            if e.get("category") == "changed" and not any(
                    sym_match(e.get("symbol") or "", r.get("symbol"))
                    or sym_match(r.get("symbol") or "", e.get("symbol") or "")
                    for r in (s["plan"].get("risks") or {}).get("risks") or []):
                unpredicted.append(f"{name}: {e.get('symbol')}")
    hits = sum(p["result"] == "hit" for p in per)
    return {"expected_changed": per, "hit_rate": hits / len(per) if per else None,
            "calibration": totals, "unpredicted_changes": unpredicted}


def _line_set(text: str) -> set[str]:
    return {ln.strip() for ln in (text or "").splitlines() if ln.strip()}


def attribute_rewrites(s: dict) -> list[dict]:
    """The post-migration capture's facts at the sites pymolt rewrote: a REWRITTEN fact whose
    new line is one of the plan's own output lines (so a manual edit is not blamed on pymolt).
    equal = correct, changed = wrong, anything else unknown."""
    plan_lines: dict[str, set[str]] = {}
    for c in s["plan"].get("changes") or []:
        plan_lines[c.get("path") or ""] = _line_set(c.get("new_source") or "")
    out = []
    for e in _whole_diff(s).get("entries") or []:
        if e.get("category") != "rewritten":
            continue
        site = e.get("new_site") or {}
        file = site.get("file") or ""
        key = next((k for k in plan_lines if k and (file == k or file.endswith("/" + k))), None)
        if key is None or (site.get("text") or "").strip() not in plan_lines[key]:
            continue
        cmp_ = e.get("comparison")
        out.append({"symbol": e.get("symbol"), "new_symbol": e.get("new_symbol"),
                    "site": f"{file}:{site.get('line')}",
                    "result": "correct" if cmp_ == "equal"
                    else "wrong" if cmp_ == "changed" else "unknown"})
    return out


def metric_c(scens: dict[str, dict], expected_rewrites: int = 0) -> dict:
    out = {"proposed": 0, "correct": None, "wrong": None, "tests_after_apply": None,
           "expected": expected_rewrites, "attribution": "suite"}
    proposed = 0
    for s in scens.values():
        proposed = max(proposed, sum(int(c.get("sites") or 0)
                                     for c in s["plan"].get("changes") or []))
    out["proposed"] = proposed
    eq = scens.get("equiv")
    if eq:
        rc = (eq["result"].get("steps") or {}).get("tests_new", {}).get("rc")
        out["tests_after_apply"] = eq["result"].get("tests_new")
        if rc is not None:
            out["correct"] = proposed if rc == 0 else 0
            out["wrong"] = 0 if rc == 0 else proposed  # unattributed: any red test blames all
        per = attribute_rewrites(eq)
        if per:  # per rewrite, from the post capture's facts at the rewritten sites
            out["attribution"] = "per-rewrite"
            out["attributed"] = len(per)
            out["correct"] = sum(p["result"] == "correct" for p in per)
            out["wrong"] = sum(p["result"] == "wrong" for p in per)
            out["unknown"] = sum(p["result"] == "unknown" for p in per)
            out["per_rewrite"] = per
    return out


def opaque_share(evs: list[dict]) -> float | None:
    opaque = total = 0
    for ev in evs:
        res = ev.get("result")
        if res is None or ev.get("t") != "return":
            continue
        total += 1
        if isinstance(res, dict) and "__opaque__" in res:
            opaque += 1
    return opaque / total if total else None


def detection_of(symbol: str, entries: list[dict], root: Path) -> str | None:
    """How verify saw an expected-changed symbol: ``changed`` (a CHANGED fact), ``raise``
    (its outcome at a site is now a raise, e.g. a binding TypeError) or ``disappeared`` at a
    site whose line is still in the file (the call could not complete, or was not traced);
    None when none of these."""
    def hit(e: dict) -> bool:
        # either side's name: a decorator wrapper is named by its code on one interpreter
        # (``django.utils.functional.wrapper``) and by the function it wraps on another
        return any(sym_match(symbol, name) or sym_match(name, symbol)
                   for name in (e.get("symbol"), e.get("new_symbol")) if name)

    mine = [e for e in entries if hit(e)]
    if any(e.get("category") == "changed" for e in mine):
        return "changed"
    for e in mine:
        if any(o.get("kind") == "raise" for o in e.get("new") or []) and not any(
                o.get("kind") == "raise" for o in e.get("old") or []):
            return "raise"
    for e in mine:
        if e.get("category") != "disappeared":
            continue
        site = e.get("old_site") or {}
        try:
            text = (root / site.get("file", "")).read_text()
        except OSError:
            continue
        if (site.get("text") or "").strip() in _line_set(text):
            return "disappeared"
    return None


def verdict_ok(scenario: str, verdict: str | None, expected: str,
               fact_diff: dict) -> tuple[bool, str]:
    """Whether a scenario's verdict is the expected one. The equivalent scenario is expected to
    be "not fail" (round 3): PASS, or INCONCLUSIVE with no changed entry and no failing edited
    site, i.e. only review items or unverified edited sites. Any other scenario must match its
    expected verdict exactly."""
    v = (verdict or "").lower()
    if scenario == "equiv" and expected.lower() == "pass":
        if v == "pass":
            return True, "not fail"
        counts = fact_diff.get("counts") or {}
        failed = (fact_diff.get("edited_sites") or {}).get("failed") or 0
        fact_ok = (fact_diff.get("verdict") or "").lower() != "fail"
        return v == "inconclusive" and fact_ok and not counts.get("changed") and not failed, \
            "not fail"
    return v == expected.lower(), expected


def metric_d(toy: dict, scens: dict[str, dict]) -> dict:
    out: dict = {"scenarios": {}}
    lv = {"exact": 0, "type": 0}
    for name, s in scens.items():
        spec = toy["scenarios"][name]
        verdict = s["verify"].get("verdict") or s["contract"].get("verdict")
        # the whole surface: an impact filter narrows ``fact_diff`` to the plan's symbols, and
        # a change nobody predicted (``unpredicted_changes``) lives only outside it
        fd = _whole_diff(s)
        entries = fd.get("entries") or []
        changed = [e.get("symbol") for e in entries if e.get("category") == "changed"]
        for e in entries:
            if e.get("level") in lv:
                lv[e["level"]] += 1
        exp_changed = spec.get("expected_changed") or []
        by = {x: detection_of(x, entries, s["root"]) for x in exp_changed}
        found = [x for x in exp_changed if by[x]]
        ok, expected = verdict_ok(name, verdict, spec["expected_verdict"], fd)
        out["scenarios"][name] = {
            "expected_changed_by": {x: v for x, v in by.items() if v},
            "verdict": verdict, "expected": expected,
            "edited_sites": fd.get("edited_sites"), "edited_summary": fd.get("edited_summary"),
            "ok": ok,
            "changed": changed, "expected_changed_found": found,
            "expected_changed_missing": [x for x in exp_changed if x not in found],
            "counts": {k: v for k, v in (fd.get("counts") or {}).items() if v}}
    tot = lv["exact"] + lv["type"]
    out["levels"] = lv
    out["exact_share"] = lv["exact"] / tot if tot else None
    shares = [x for x in (opaque_share(s["baseline"]) for s in scens.values()) if x is not None]
    out["opaque_share"] = sum(shares) / len(shares) if shares else None
    return out


_RESOLVE_SRC = """
import importlib, json, sys
sys.path[:] = [p for p in sys.path if p not in ("", ".")]
def find(spelling):
    parts = spelling.split(".")
    obj, k = None, len(parts)
    while k > 0 and obj is None:
        try:
            obj = importlib.import_module(".".join(parts[:k]))
        except Exception:
            k -= 1
    for attr in parts[k:]:
        obj = getattr(obj, attr)
    return obj
out = {}
for sp in json.loads(sys.stdin.read()):
    try:
        o = find(sp)
        mod, qual = getattr(o, "__module__", None), getattr(o, "__qualname__", None)
        out[sp] = mod + "." + qual if isinstance(mod, str) and isinstance(qual, str) else None
    except BaseException:
        out[sp] = None
print(json.dumps(out))
"""
_RESOLVED: dict[tuple[str, str, str], str | None] = {}


def resolve_object(key: str, side: str, spelling: str) -> str | None:
    """``module.qualname`` the spelling names in that side's interpreter (``asyncio.Queue`` is
    ``asyncio.queues.Queue``), or None when it does not resolve there."""
    ck = (key, side, spelling)
    if ck not in _RESOLVED:
        _RESOLVED[ck] = None
        py = env_python(key, side)
        if py.exists():
            try:
                proc = subprocess.run([str(py), "-I", "-W", "ignore", "-c", _RESOLVE_SRC],
                                      input=json.dumps([spelling]), capture_output=True,
                                      text=True, timeout=60)
                _RESOLVED[ck] = json.loads(proc.stdout or "{}").get(spelling)
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
    return _RESOLVED[ck]


_CONSTRUCTORS = ("__init__", "__new__", "__call__")


def warned(evs: list[dict], symbol: str, category: str, resolve=None) -> bool:
    """A warning of ``category`` on ``symbol``: by spelling, else by the object it resolves to
    (a call of ``asyncio.queues.Queue.__init__`` is a call of ``asyncio.Queue``), else a warning
    whose text names the symbol's leaf on a member of the same resolved class
    (``assertEqual`` warning "assertEquals is deprecated")."""
    canon = resolve(symbol) if resolve else None
    parent = symbol.rsplit(".", 1)[0] if "." in symbol else None
    parent_canon = resolve(parent) if resolve and parent else None
    name = leaf(symbol)
    for ev in evs:
        sym = ev.get("sym") or ""
        by_spelling = sym_match(symbol, sym)
        by_object = bool(canon) and (sym == canon or (
            sym.startswith(canon + ".") and sym[len(canon) + 1:] in _CONSTRUCTORS))
        for w in ev.get("warnings") or []:
            if leaf(w.get("category")) != leaf(category):
                continue
            if by_spelling or by_object:
                return True
            if parent_canon and sym.startswith(parent_canon + ".") and re.search(
                    r"(?<![\w])" + re.escape(name) + r"(?![\w])", w.get("message") or ""):
                return True
    return False


def metric_e(toy: dict, scens: dict[str, dict]) -> dict:
    base = [ev for s in scens.values() for ev in s["baseline"]]
    post = [ev for s in scens.values() for ev in s["post"]]
    per, grp = [], {"python": [0, 0], "c": [0, 0]}
    post_only = []
    for w in (toy.get("expected_warnings_new") or []):
        post_only.append(dict(w, side="new"))
    old_ws = []
    for w in toy.get("expected_warnings") or []:
        (post_only if str(w.get("side", "old")).startswith("new") else old_ws).append(w)
    key = toy["_key"]
    res_old = lambda sp: resolve_object(key, "old", sp)  # noqa: E731
    res_new = lambda sp: resolve_object(key, "new", sp)  # noqa: E731
    obs = [{"symbol": w["symbol"], "category": w["category"], "from_c": bool(w.get("from_c")),
            "observed_post": warned(post, w["symbol"].split()[0], w["category"], res_new)}
           for w in post_only]
    for w in old_ws:
        found = warned(base, w["symbol"].split()[0], w["category"], res_old)
        g = grp["c" if w.get("from_c") else "python"]
        g[1] += 1
        g[0] += found
        per.append({"symbol": w["symbol"], "category": w["category"],
                    "from_c": bool(w.get("from_c")), "found_in_baseline": found,
                    "found_in_post": warned(post, w["symbol"].split()[0], w["category"],
                                                res_new)})
    return {"total": sum(g[1] for g in grp.values()), "found": sum(g[0] for g in grp.values()),
            "python_level": {"found": grp["python"][0], "total": grp["python"][1]},
            "c_level": {"found": grp["c"][0], "total": grp["c"][1]},
            "post_migration": obs,
            "missed": [f"{p['symbol']} {p['category']}" for p in per
                       if not p["found_in_baseline"]], "per_entry": per}


def find_import_errors(node: object, acc: list) -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "import_errors" and isinstance(v, list):
                acc.extend(x for x in v if isinstance(x, dict))
            else:
                find_import_errors(v, acc)
    elif isinstance(node, list):
        for x in node:
            find_import_errors(x, acc)


def metric_f(scens: dict[str, dict]) -> dict:
    inconclusive: set[str] = set()
    partial: set[str] = set()
    errs, seen = [], set()
    for s in scens.values():
        for d in (s["plan"].get("risks") or {}).get("deltas") or []:
            if d.get("quality") == "inconclusive":
                inconclusive.add(f"{d.get('package')}")
            elif d.get("quality") == "partial":
                partial.add(f"{d.get('package')}")
        for imp in s["plan"].get("impacts") or []:
            if imp.get("kind") == "INCONCLUSIVE":
                inconclusive.add(str(imp.get("path")))
        acc: list = []
        for f in sorted((s["root"] / ".pymolt" / "cache" / "delta").glob("probe-*.json")):
            find_import_errors(jload(f), acc)
        for e in acc:
            k = (e.get("module"), e.get("side"), str(e.get("error"))[:80])
            if k not in seen:
                seen.add(k)
                errs.append({"module": e.get("module"), "side": e.get("side"),
                             "error": str(e.get("error"))[:120]})
    sides = sorted({f"{d.get('package')} {n}" for s in scens.values()
                    for d in (s["plan"].get("risks") or {}).get("deltas") or []
                    for n in d.get("notes") or [] if str(n).startswith(("old:", "new:"))})
    built = [x for x in sides if "throwaway" in x and " old:" in x]
    return {"delta_sides": sides, "delta_old_side_built": built,
            "inconclusive": sorted(inconclusive), "partial": sorted(partial),
            "import_errors": len(errs), "import_error_samples": errs[:10]}


def step_counts(plan: dict) -> dict | None:
    """The plan's work order: steps in all, by executor and by type (None: a plan with no
    steps, e.g. one saved before plan schema 2)."""
    steps = (plan or {}).get("steps")
    if steps is None:
        return None
    out: dict = {"total": len(steps), "pymolt": 0, "agent": 0, "person": 0, "by_type": {}}
    for step in steps:
        out[step.get("executor")] = out.get(step.get("executor"), 0) + 1
        kind = step.get("type")
        out["by_type"][kind] = out["by_type"].get(kind, 0) + 1
    return out


def steps_cell(counts: dict | None) -> str:
    if not counts:
        return "-"
    return f"{counts['total']} ({counts['pymolt']}/{counts['agent']}/{counts['person']})"


def score_toy(toy: dict, out: Path) -> dict:
    key = toy["_key"]
    scens = {n: read_scenario(out / key / n) for n in SCENARIOS if (out / key / n).is_dir()}
    row: dict = {"toy": key, "ecosystem": toy.get("ecosystem"),
                 "old_python": toy["old"]["python"], "status": "ok"}
    if not scens:
        row["status"] = "not run"
        return row
    failed = [f"{n}:{k}" for n, s in scens.items()
              for k, v in (s["result"].get("steps") or {}).items()
              if v.get("rc") not in (0, None) and k in ("setup", "capture_baseline", "plan",
                                                         "apply", "capture_post")]
    if failed:
        row["status"] = "step failures: " + ", ".join(failed)
    row["warnings"] = [w for s in scens.values() for w in s["result"].get("warnings") or []]
    row["A"] = metric_a(toy, scens)
    if any(s.get("plan_offline") for s in scens.values()):  # an --online run: both numbers
        offline = metric_a(toy, {n: dict(s, plan_offline=s["plan_offline"] or s["plan"])
                                 for n, s in scens.items()}, "plan_offline")
        row["A"]["offline"] = {k: offline[k] for k in
                               ("successors_expected", "successors_correct", "recall")}
    row["B"] = metric_b(toy, scens)
    row["C"] = metric_c(scens, sum(
        len(g.get("sites") or [1]) for g in toy["ground_truth"]
        if g.get("fixable") in ("auto", "codemod", "plan")))
    row["D"] = metric_d(toy, scens)
    row["E"] = metric_e(toy, scens)
    row["F"] = metric_f(scens)
    row["steps"] = {n: step_counts(s["plan"]) for n, s in scens.items()}
    row["seconds"] = {n: s["result"].get("seconds") for n, s in scens.items()}
    row["phase_seconds"] = {
        n: {k: v["seconds"] for k, v in (s["result"].get("steps") or {}).items()}
        for n, s in scens.items()}
    return row


# ── scorecard ─────────────────────────────────────────────────────────────────────────────


def pct(x: float | None) -> str:
    return "-" if x is None else f"{round(100 * x)}%"


def frac(a: int | None, b: int | None) -> str:
    return "-" if a is None or b is None else f"{a}/{b}"


def skew_signals(rows: list[dict]) -> list[str]:
    ref = next((r for r in rows if r["toy"] == "pandas" and "A" in r), None)
    out: list[str] = []
    if ref is None:
        out.append("No pandas reference row in this run; only absolute flags are shown.")

    def low(r: dict, label: str, val: float | None, refval: float | None) -> None:
        if val is None:
            return
        if refval is not None and val < refval - SKEW_MARGIN:
            out.append(f"{r['toy']}: {label} {pct(val)} is well below pandas {pct(refval)}")
        elif refval is None and val < 0.5:
            out.append(f"{r['toy']}: {label} {pct(val)} is below 50%")

    for r in rows:
        if "A" not in r or r is ref:
            continue
        low(r, "A recall", r["A"]["recall"], ref["A"]["recall"] if ref else None)
        low(r, "B hit rate", r["B"]["hit_rate"], ref["B"]["hit_rate"] if ref else None)
        low(r, "D exact share", r["D"]["exact_share"], ref["D"]["exact_share"] if ref else None)
        ro = r["D"].get("opaque_share")
        ref_opaque = ref["D"].get("opaque_share") if ref else None
        if ro is not None and ro > ((ref_opaque or 0) + SKEW_MARGIN):
            out.append(f"{r['toy']}: {pct(ro)} of returned values are opaque (type-only) vs "
                       f"{pct(ref_opaque)} for pandas (S1)")
        bad = [n for n, d in r["D"]["scenarios"].items() if not d["ok"]]
        if bad:
            out.append(f"{r['toy']}: verdict differs from expected in {', '.join(bad)}")
        if r["E"]["c_level"]["total"] and r["E"]["c_level"]["found"] < r["E"]["c_level"]["total"]:
            out.append(f"{r['toy']}: C-level warnings found "
                       f"{frac(r['E']['c_level']['found'], r['E']['c_level']['total'])} (S2)")
        if r["F"]["inconclusive"] or r["F"]["import_errors"]:
            out.append(f"{r['toy']}: inconclusive {r['F']['inconclusive']}, "
                       f"{r['F']['import_errors']} import error(s) (S3)")
        if r["C"].get("wrong"):
            out.append(f"{r['toy']}: {r['C']['wrong']} rewrite(s) left the equiv tests red")
        if r["status"] != "ok":
            out.append(f"{r['toy']}: {r['status']}")
    return out


def write_scorecard(out: Path, rows: list[dict], meta: dict) -> None:
    (out / "scorecard.json").write_text(json.dumps({"meta": meta, "rows": rows}, indent=2))
    lines = ["# Generalization scorecard", "",
             f"run: {meta.get('when')}  |  pymolt: `{meta.get('pymolt')}`", "",
             "| toy | A recall | B hit | C rewrites ok/prop | D equiv | D change | D exact | "
             "E found (C-lvl) | F incon/imp.err | steps equiv / change (pymolt/agent/person) "
             "| secs |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        if "A" not in r:
            lines.append(f"| {r['toy']} | {r['status']} |  |  |  |  |  |  |  |  |  |")
            continue
        steps = r.get("steps") or {}
        ds = r["D"]["scenarios"]

        def v(n: str) -> str:
            d = ds.get(n)
            if not d:
                return "-"
            return str(d["verdict"]) + ("" if d["ok"] else " (exp " + d["expected"] + ")")

        e = r["E"]
        lines.append(
            f"| {r['toy']} | {frac(r['A']['detected'], r['A']['total'])} ({pct(r['A']['recall'])}) "
            f"| {pct(r['B']['hit_rate'])} "
            f"| {frac(r['C']['correct'], r['C'].get('attributed') or r['C']['proposed'])} "
            f"| {v('equiv')} | {v('change')} | {pct(r['D']['exact_share'])} "
            f"| {frac(e['found'], e['total'])} "
            f"({frac(e['c_level']['found'], e['c_level']['total'])}) "
            f"| {len(r['F']['inconclusive'])}/{r['F']['import_errors']} "
            f"| {steps_cell(steps.get('equiv'))} / {steps_cell(steps.get('change'))} "
            f"| {sum(x for x in r['seconds'].values() if x)} |")
    lines += ["", "## Skew signals", ""]
    sig = skew_signals(rows)
    lines += [f"- {s}" for s in sig] or ["- none"]
    lines += ["", "## Per toy detail", ""]
    for r in rows:
        if "A" not in r:
            continue
        lines.append(f"### {r['toy']} ({r.get('ecosystem')}, old python {r['old_python']})")
        lines.append(f"- status: {r['status']}")
        for p in r["A"]["per_entry"]:
            lines.append(f"- A {p['symbol']} {p['kind']}: {p['status']}"
                         + (f" (plan kind {p['plan_kind']})" if p["status"] == "other_kind" else "")
                         + (f", successor {'ok' if p['successor_ok'] else 'wrong/absent'}"
                            if p["successor"] else ""))
        off = r["A"].get("offline")
        corr = frac(r["A"]["successors_correct"], r["A"]["successors_expected"])
        lines.append(f"- A successors correct {corr}"
                     + (f" online, {frac(off['successors_correct'], off['successors_expected'])}"
                        " offline" if off else ""))
        if r["A"]["fp_candidates"]:
            lines.append(f"- A fp candidates ({len(r['A']['fp_candidates'])}): "
                         + "; ".join(r["A"]["fp_candidates"][:8]))
        for p in r["B"]["expected_changed"]:
            lines.append(f"- B {p['symbol']}: tier {p['tier']}, calibration {p['result']}")
        lines.append(f"- B calibration totals {r['B']['calibration']}; "
                     f"unpredicted changes {r['B']['unpredicted_changes']}")
        beh = r["A"].get("behavior_excluded") or {}
        if beh.get("total"):
            lines.append(f"- A BEHAVIOR entries left out of recall (verify's): {beh['total']}, "
                         f"seen by the plan {beh['seen_by_plan']}: {beh['symbols']}")
        lines.append(f"- C tests after apply (equiv): {r['C']['tests_after_apply']}; "
                     f"attribution {r['C'].get('attribution')}"
                     + (f": {r['C']['correct']} correct, {r['C']['wrong']} wrong, "
                        f"{r['C'].get('unknown')} unknown of {r['C']['attributed']} rewrites"
                        if r["C"].get("attribution") == "per-rewrite" else ""))
        for pr in r["C"].get("per_rewrite") or []:
            if pr["result"] != "correct":
                lines.append(f"  - C {pr['symbol']} at {pr['site']}: {pr['result']}")
        for n, d in r["D"]["scenarios"].items():
            if d.get("edited_summary"):
                lines.append(f"- D {n} {d['edited_summary']}")
            lines.append(f"- D {n}: {d['verdict']} (expected {d['expected']}), "
                         f"counts {d['counts']}, missing changed {d['expected_changed_missing']}, "
                         f"detected by {d.get('expected_changed_by')}")
        lines.append(f"- D levels {r['D']['levels']}, opaque share {pct(r['D']['opaque_share'])}")
        for o in r["E"].get("post_migration", []):
            lines.append(f"- E post-migration (new side) {o['symbol']} {o['category']}"
                         f"{' [C-level]' if o['from_c'] else ''}: "
                         f"{'observed' if o['observed_post'] else 'not observed'}")
        lines.append(f"- C expected rewrites {r['C'].get('expected')}, "
                     f"proposed {r['C']['proposed']}")
        lines.append(f"- F delta sides {r['F']['delta_sides']}")
        if r["E"]["missed"]:
            lines.append(f"- E missed: {r['E']['missed']}")
        lines.append(f"- F inconclusive {r['F']['inconclusive']}, partial {r['F']['partial']}, "
                     f"import errors {r['F']['import_error_samples']}")
        if r.get("warnings"):
            lines.append(f"- runner warnings: {r['warnings']}")
        lines.append("")
    (out / "scorecard.md").write_text("\n".join(lines) + "\n")


# ── commands ──────────────────────────────────────────────────────────────────────────────


def cmd_envs(args: argparse.Namespace) -> int:
    if shutil.which("uv") is None:
        print("uv is required")
        return 2
    toys = load_toys(args.toys)
    rc = 0
    with cf.ThreadPoolExecutor(max_workers=max(1, args.parallel)) as pool:
        futs = {pool.submit(ensure_envs, t): k for k, t in toys.items()}
        for fut in cf.as_completed(futs):
            ok, note = fut.result()
            print(f"{futs[fut]:<14}{'ok' if ok else 'FAILED':<8}{note}")
            rc |= 0 if ok else 1
    return rc


def cmd_run(args: argparse.Namespace) -> int:
    pymolt = Path(args.pymolt)
    if not pymolt.exists():
        print(f"pymolt not found: {pymolt}")
        return 2
    if shutil.which("uv") is None:
        print("uv is required")
        return 2
    toys = load_toys(args.toys)
    if not toys:
        print("no valid toys to run")
        return 2
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    start = time.time()
    ready: dict[str, dict] = {}
    with cf.ThreadPoolExecutor(max_workers=max(1, args.parallel)) as pool:
        futs = {pool.submit(ensure_envs, t): k for k, t in toys.items()}
        for fut in cf.as_completed(futs):
            ok, note = fut.result()
            print(f"envs {futs[fut]}: {note}", flush=True)
            if ok:
                ready[futs[fut]] = toys[futs[fut]]
    env_secs = time.time() - start
    jobs = [(t, s, None) for t in ready.values() for s in SCENARIOS]
    if args.record:
        jobs += [(t, "equiv", v) for t in ready.values() if t.get("manual_migration")
                 for v in MUTATIONS]
    with cf.ThreadPoolExecutor(max_workers=max(1, args.parallel)) as pool:
        futs2 = {pool.submit(run_scenario, t, s, out, pymolt, args.timeout,
                             args.online, args.record, v): (t["_key"], v or s)
                 for t, s, v in jobs}
        for fut in cf.as_completed(futs2):
            k, s = futs2[fut]
            try:
                res = fut.result()
                print(f"done {k}/{s} in {res['seconds']}s", flush=True)
            except Exception as exc:  # noqa: BLE001 - one toy must not sink the matrix
                print(f"ERROR {k}/{s}: {type(exc).__name__}: {exc}", flush=True)
    rows = []
    for k, toy in toys.items():
        rows.append(score_toy(toy, out) if k in ready else
                    {"toy": k, "ecosystem": toy.get("ecosystem"),
                     "old_python": toy["old"]["python"],
                     "status": f"env build failed (see .envs/{k}/*.log)"})
    meta = {"when": time.strftime("%Y-%m-%d %H:%M:%S"), "pymolt": str(pymolt),
            "wall_seconds": round(time.time() - start, 1), "env_seconds": round(env_secs, 1),
            "parallel": args.parallel, "online": args.online}
    meta["record"] = bool(args.record)
    write_scorecard(out, rows, meta)
    print((out / "scorecard.md").read_text())
    if args.record:
        summary = record_summary(toys, rows, out)
        (out / "record_summary.json").write_text(json.dumps(summary, indent=2))
        print(json.dumps(summary["totals"], indent=2))
    print(f"wall time {meta['wall_seconds']}s; scorecard: {out / 'scorecard.md'}")
    return 0


def record_summary(toys: dict, rows: list[dict], out: Path) -> dict:
    """Record mode's guard: edited sites over the equiv scenarios, every change scenario's
    verdict on its expected symbol, and the must-FAIL mutations (none may PASS)."""
    totals = {"equiv_sites": 0, "equiv_unverified": 0, "change_fail_ok": 0, "change_total": 0,
              "mutations": 0, "mutations_pass": 0, "mutations_not_mutated": 0}
    per: dict = {}
    for row in rows:
        key = row.get("toy")
        scen = (row.get("D") or {}).get("scenarios") or {}
        es = (scen.get("equiv") or {}).get("edited_sites") or {}
        totals["equiv_sites"] += es.get("sites") or 0
        totals["equiv_unverified"] += es.get("unverified") or 0
        change = scen.get("change") or {}
        if change:
            totals["change_total"] += 1
            totals["change_fail_ok"] += bool(change.get("ok")
                                             and not change.get("expected_changed_missing"))
        per[key] = {"equiv": scen.get("equiv", {}).get("verdict"),
                    "change": change.get("verdict"), "equiv_edited": es,
                    "change_missing": change.get("expected_changed_missing")}
        for v in MUTATIONS:
            root = out / key / v
            if not root.is_dir():
                continue
            verify = jload(root / "verify.json")
            fd = (verify.get("contract") or {}).get("all_fact_diff") or {}
            result = jload(root / "result.json")
            mutated = (result.get("manual") or {}).get("mutated")
            totals["mutations"] += 1
            totals["mutations_pass"] += verify.get("verdict") == "pass"
            totals["mutations_not_mutated"] += mutated is None
            per[key][v] = {"verdict": verify.get("verdict"), "fact_verdict": fd.get("verdict"),
                           "mutated_edit": mutated, "edited": fd.get("edited_sites"),
                           "records": (result.get("manual") or {}).get("records")}
    totals["equiv_unverified_share"] = (f"{totals['equiv_unverified']} of "
                                        f"{totals['equiv_sites']}")
    return {"totals": totals, "toys": per}


def cmd_score(args: argparse.Namespace) -> int:
    out = Path(args.dir).resolve()
    toys = load_toys(args.toys)
    rows = [score_toy(t, out) for t in toys.values()]
    meta = jload(out / "scorecard.json").get("meta") or {"when": "rescored"}
    write_scorecard(out, rows, meta)
    print((out / "scorecard.md").read_text())
    return 0


def flat(row: dict) -> dict:
    keys = {
        "A.recall": row.get("A", {}).get("recall"),
        "A.recall_any_kind": row.get("A", {}).get("recall_any_kind"),
        "A.fp": len(row.get("A", {}).get("fp_candidates", [])),
        "B.hit_rate": row.get("B", {}).get("hit_rate"),
        "C.proposed": row.get("C", {}).get("proposed"),
        "C.correct": row.get("C", {}).get("correct"),
        "D.exact_share": row.get("D", {}).get("exact_share"),
        "D.opaque_share": row.get("D", {}).get("opaque_share"),
        "E.found": row.get("E", {}).get("found"),
        "F.import_errors": row.get("F", {}).get("import_errors"),
        "F.inconclusive": ",".join(row.get("F", {}).get("inconclusive", [])),
        "status": row.get("status"),
    }
    for n, d in row.get("D", {}).get("scenarios", {}).items():
        keys[f"D.{n}.verdict"] = d["verdict"]
    for n, counts in (row.get("steps") or {}).items():
        keys[f"steps.{n}"] = steps_cell(counts)
    return keys


def fmt_val(v: object) -> str:
    return f"{v:.2f}" if isinstance(v, float) else str(v)


def cmd_diff(args: argparse.Namespace) -> int:
    a = {r["toy"]: r for r in jload(Path(args.a) / "scorecard.json").get("rows", [])}
    b = {r["toy"]: r for r in jload(Path(args.b) / "scorecard.json").get("rows", [])}
    if not a or not b:
        print("scorecard.json missing in one of the directories")
        return 2
    changed = 0
    for toy in sorted(set(a) | set(b)):
        if toy not in a or toy not in b:
            print(f"{toy}: only in {'B' if toy in b else 'A'}")
            changed += 1
            continue
        fa, fb = flat(a[toy]), flat(b[toy])
        diffs = [(k, fa[k], fb[k]) for k in fa if fa[k] != fb[k]]
        if diffs:
            changed += 1
            print(toy)
            for k, x, y in diffs:
                print(f"  {k}: {fmt_val(x)} -> {fmt_val(y)}")
    if not changed:
        print("no differences")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("envs", help="build the old and new envs per toy")
    p.add_argument("toys", nargs="*")
    p.add_argument("--parallel", type=int, default=2)
    p = sub.add_parser("run", help="run the matrix")
    p.add_argument("toys", nargs="*")
    p.add_argument("--out", required=True)
    p.add_argument("--parallel", type=int, default=2)
    p.add_argument("--pymolt", default=str(DEFAULT_PYMOLT))
    p.add_argument("--timeout", type=int, default=1800, help="seconds per pymolt/pytest step")
    p.add_argument("--online", action="store_true",
                   help="also run `plan --online` (intermediate-release successor hints); the "
                        "scorecard reports A successors offline and online")
    p.add_argument("--record", action="store_true",
                   help="record the manual migration as steps (`apply --record`), and run the "
                        "must-FAIL mutations")
    p = sub.add_parser("score", help="re-score an existing run directory")
    p.add_argument("dir")
    p.add_argument("toys", nargs="*")
    p = sub.add_parser("diff", help="compare two scorecards")
    p.add_argument("a")
    p.add_argument("b")
    args = parser.parse_args()
    return {"envs": cmd_envs, "run": cmd_run, "score": cmd_score, "diff": cmd_diff}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
