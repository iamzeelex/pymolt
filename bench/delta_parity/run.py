#!/usr/bin/env python3
"""Delta parity: a local, scoped API delta versus Axiom Graph's cached checkpoints.

Question: can PyMolt generate the API-change facts a migration needs on the spot, from
the two environments it already builds and scoped to the symbols the project touches,
at least as well as the Axiom Graph service? See README.md for the decision rule.

Per pair:
  1. build (or reuse) the old and new environments with uv;
  2. take the touched symbols. For flask, use the static contact map of the flasgger
     fixture (`pymolt contract map --json`) plus the instance methods flasgger calls.
     For pandas, use the e2e toy app's baseline facts;
  3. LOCAL: run probe.py in both interpreters (existence, owner, signature, bases,
     warnings, forwarding) and diff the results. Add the warnings the baseline run
     emits (what tier 1's baseline capture sees);
  4. GROUND TRUTH: run calls_<pair>.py in both interpreters, which exercises each symbol
     the way the project uses it, plus the introspected API diff;
  5. AXIOM: match the cached checkpoint changes (~/Library/Caches/axiom_graph/checkpoints)
     to the same symbols through every path spelling (public, defining, receiver);
  6. INTERMEDIATE: for each symbol removed without a local successor hint, bisect the PyPI
     releases between old and new. It range-requests only the one module file out of each
     wheel, sending versions only, and mines its deprecation prose;
  7. score precision and recall per category and write results/<pair>_parity.json.

Run:  .venv/bin/python bench/delta_parity/run.py [--pair flask|pandas|all] [--offline]
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import struct
import subprocess
import sys
import time
import zlib
from pathlib import Path

from packaging.version import InvalidVersion, Version

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
CACHE = HERE / ".cache"
RESULTS = HERE / "results"
PYMOLT = REPO / ".venv" / "bin" / "pymolt"
AXIOM = Path(
    os.environ.get(
        "AXIOM_CHECKPOINTS", Path.home() / "Library" / "Caches" / "axiom_graph" / "checkpoints"
    )
)
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
        # Exactly the version pairs Axiom's checkpoints cover.
        "axiom": {"flask": ("2.0.3", "3.0.0"), "werkzeug": ("2.0.0", "3.0.0")},
        "axiom_from": None,
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
        # The only cached pandas checkpoint is 1.3.5 -> 2.0.0; keep the steps from 1.5.3 on.
        "axiom": {"pandas": ("1.3.5", "2.0.0")},
        "axiom_from": "1.5.3",
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

MAX_INTERMEDIATE_PROBES = 8
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
    data, secs = run_json([str(PYMOLT), "contract", "map", str(pair["fixture"]), "--json"])
    roots = set(pair["deps"])
    syms = sorted({c["target"] for c in data["contacts"] if c["target"].split(".")[0] in roots})
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


# ── local prediction (probes + baseline-run warnings; never sees new-env use) ──────────

_SUCCESSOR_RES = [
    re.compile(r"[Ii]mport\s+['\"`]?([A-Za-z_][\w\.]*)['\"`]?\s+instead"),
    re.compile(r"[Uu]se\s+['\"`]?([A-Za-z_][\w\.\(\)]*)['\"`]?\s+(?:instead|to )"),
    re.compile(r"[Ii]mport (?:them|it) directly from\s+['\"`]?([\w\.]+)"),
    re.compile(r"[Ss]ubclass(?:ing)?\s+['\"`]?([A-Za-z_][\w\.]*)['\"`]?"),
]


def successor_from(messages: list[str]) -> str | None:
    for msg in messages:
        for rx in _SUCCESSOR_RES:
            m = rx.search(msg)
            if m:
                return m.group(1).rstrip(".")
    return None


def local_predict(units: dict, calls_o: dict, rule: str = "strict") -> dict[str, dict]:
    """``strict`` (the shipped rule): DEPRECATED only from unconditional evidence, i.e. a
    PEP 562 ``__getattr__`` branch for the name or a warning raised on access. ``any``
    also counts every ``warnings.warn`` in the new body/class that names the symbol,
    which are mostly argument-conditional; reported for transparency."""
    out = {}
    for unit, u in units.items():
        leaf = unit.rsplit(".", 1)[-1]
        cats = {}
        o, n = u["old"], u["new"]
        old_msgs = (
            o.get("warns", [])
            if o.get("kind") != "class"
            else _mentions_d(o.get("warns", []), leaf)
        )
        old_msgs = [m["message"] for m in old_msgs] + _mentions(o.get("getattr_warns", []), leaf)
        base_warn = [w for k, v in _calls_for(unit, calls_o) for w in v["warnings"]]
        if u["exists_o"] and not u["exists_n"]:
            cats["REMOVED"] = ["absent in new"]
        if u["exists_o"] and u["exists_n"]:
            sig = signature_changes(o.get("shape"), n.get("shape"))
            if sig:
                cats["SIGNATURE"] = sig
            bases = bases_changed(o, n)
            if bases:
                cats["BASES"] = bases
            new_static = _mentions(n.get("getattr_warns", []), leaf)
            if rule == "any":
                new_static += [m["message"] for m in _mentions_d(n.get("warns", []), leaf)]
            new_rt = [w["message"] for w in u["runtime_warnings_n"]]
            if new_static or new_rt:
                cats["DEPRECATED"] = sorted(set(new_static + new_rt))
            if base_warn:  # the baseline run warned at this call: predicts a behaviour change
                cats["BEHAVIOR"] = sorted(set(base_warn))
        hint_msgs = old_msgs + base_warn
        out[unit] = {
            "categories": cats,
            "hint": successor_from(hint_msgs),
            "hint_source": "old env (static/baseline run)" if successor_from(hint_msgs) else None,
            "forwards_to_old": o.get("forwards_to"),
        }
    return out


def _mentions_d(warns: list[dict], leaf: str) -> list[dict]:
    return [w for w in warns if leaf.strip("_") in w.get("message", "")]


# ── Axiom checkpoints ─────────────────────────────────────────────────────────────────

_AXIOM_KIND = {
    "Public object was removed": "REMOVED",
    "object-removed": "REMOVED",
    "Parameter was removed": "SIGNATURE",
    "Parameter was added as required": "SIGNATURE",
    "Positional parameter was moved": "SIGNATURE",
    "Parameter kind was changed": "SIGNATURE",
    "Parameter default was changed": "SIGNATURE",
    "Public object points to a different kind of object": "SIGNATURE",
    "Base class was removed": "BASES",
}


def load_axiom(pair: dict) -> tuple[list[dict], dict]:
    rows, meta = [], {}
    for pkg, (fv, tv) in pair["axiom"].items():
        f = AXIOM / f"{pkg}__{fv}__{tv}.jsonl"
        if not f.exists():
            raise SystemExit(f"Axiom checkpoint missing: {f} (set AXIOM_CHECKPOINTS)")
        steps = [json.loads(line) for line in f.read_text().splitlines() if line.strip()]
        seen, empty, kept = set(), 0, 0
        for st in steps:
            if pair["axiom_from"] and Version(st["from_version"]) < Version(pair["axiom_from"]):
                continue
            key = (st["from_version"], st["to_version"])
            if key in seen:
                continue  # resumed runs append duplicate steps
            seen.add(key)
            kept += 1
            if not st.get("changes"):
                empty += 1
            for c in st.get("changes") or []:
                rows.append(
                    {**c, "step": f"{st['from_version']}->{st['to_version']}", "package": pkg}
                )
            for t in st.get("transitions") or []:
                rows.append(
                    {
                        "kind": "transition",
                        "path": t.get("from_path"),
                        "to_path": t.get("to_path"),
                        "step": f"{st['from_version']}->{st['to_version']}",
                        "package": pkg,
                    }
                )
        meta[pkg] = {
            "file": f.name,
            "steps": kept,
            "empty_steps": empty,
            "changes": sum(1 for r in rows if r["package"] == pkg and r["kind"] != "transition"),
        }
    return rows, meta


def axiom_predict(units: dict, rows: list[dict]) -> dict[str, dict]:
    by_path: dict[str, list[dict]] = {}
    for r in rows:
        by_path.setdefault(r["path"], []).append(r)
    out = {}
    for unit, u in units.items():
        keys, init_keys = {unit}, set()
        for side in (u["old"], u["new"]):
            for k in ("owner", "receiver_path", "defined_as"):
                if side.get(k):
                    keys.add(side[k])
            # Generous to Axiom: also accept changes recorded under the callee a public
            # method forwards to (pandas 1.5 DataFrame.mean -> NDFrame.mean). The join
            # itself needs the local probe's forwarding fact.
            fwd = side.get("forwards_to") or ""
            if fwd.startswith("self.") and side.get("owner"):
                keys.add(side["owner"].rsplit(".", 1)[0] + "." + fwd[5:])
            elif fwd.count(".") == 1 and side.get("defined_as"):
                keys.add(side["defined_as"].split(".<locals>")[0].rsplit(".", 2)[0] + "." + fwd)
            if side.get("kind") == "class" and side.get("defined_as"):
                init_keys.add(side["defined_as"] + ".__init__")
        if unit.count(".") >= 1:
            init_keys.add(unit + ".__init__")
        cats, hits, hint_msgs, transitions = {}, [], [], []
        for key in keys | init_keys:
            for r in by_path.get(key, []):
                if r["kind"] == "transition":
                    transitions.append(r["to_path"])
                    continue
                cat = _AXIOM_KIND.get(r["kind"])
                if cat == "REMOVED" and key in init_keys:
                    cat = "SIGNATURE"  # "X.__init__ removed" is a constructor claim
                if r.get("deprecation_hint") or r.get("state") == "deprecated":
                    hint_msgs.append(r.get("deprecation_hint") or "")
                    if u["exists_n"]:
                        cats.setdefault("DEPRECATED", []).append(f"{r['step']} {r['kind']} {key}")
                if cat:
                    cats.setdefault(cat, []).append(f"{r['step']} {r['kind']} {key}")
                hits.append(f"{r['step']} {r['state']} {r['kind']} {key}")
        hint = successor_from(hint_msgs) or (transitions[0] if transitions else None)
        out[unit] = {
            "categories": {k: sorted(set(v)) for k, v in cats.items()},
            "hint": hint,
            "hint_text": [h[:160] for h in hint_msgs if h],
            "hits": sorted(set(hits)),
        }
    return out


# ── intermediate release fetch (versions only; one module file per wheel) ─────────────


class Fetcher:
    def __init__(self, offline: bool):
        import httpx

        self.offline = offline
        self.client = httpx.Client(
            timeout=30, follow_redirects=True, headers={"User-Agent": "delta-parity-bench/0.1"}
        )
        self.bytes = 0
        self.requests = 0
        self.dir = CACHE / "pypi"
        self.dir.mkdir(parents=True, exist_ok=True)

    def releases(self, dist: str) -> dict:
        f = self.dir / f"{dist}.json"
        if not f.exists():
            r = self.client.get(f"https://pypi.org/pypi/{dist}/json")
            r.raise_for_status()
            self.bytes += len(r.content)
            self.requests += 1
            f.write_bytes(r.content)
        return json.loads(f.read_text())["releases"]

    def member(self, dist: str, version: str, files: list[dict], member: str) -> str | None:
        cached = self.dir / f"{dist}-{version}-{member.replace('/', '__')}"
        if cached.exists():
            return cached.read_text()
        wheels = [x for x in files if x["filename"].endswith(".whl") and not x.get("yanked")]
        wheels.sort(
            key=lambda x: 0 if "py3-none-any" in x["filename"] or "py2.py3" in x["filename"] else 1
        )
        if not wheels:
            return None
        text = self._range_member(wheels[0]["url"], member)
        if text is not None:
            cached.write_text(text)
        return text

    def _get(self, url: str, start: int, end: int) -> bytes:
        r = self.client.get(url, headers={"Range": f"bytes={start}-{end}"})
        if r.status_code != 206:
            raise RuntimeError(f"range request not honoured ({r.status_code})")
        self.bytes += len(r.content)
        self.requests += 1
        return r.content

    def _range_member(self, url: str, member: str) -> str | None:
        head = self.client.head(url)
        self.requests += 1
        size = int(head.headers["content-length"])
        tail_start = max(0, size - 65536)
        tail = self._get(url, tail_start, size - 1)
        eocd = tail.rfind(b"PK\x05\x06")
        _, _, _, _, _, cd_size, cd_off, _ = struct.unpack("<IHHHHIIH", tail[eocd : eocd + 22])
        if cd_off >= tail_start:
            cd = tail[cd_off - tail_start : cd_off - tail_start + cd_size]
        else:
            cd = self._get(url, cd_off, cd_off + cd_size - 1)
        pos = 0
        while pos + 46 <= len(cd) and cd[pos : pos + 4] == b"PK\x01\x02":
            (_, _, _, _, method, _, _, _, csize, _, nlen, elen, clen, _, _, _, lho) = struct.unpack(
                "<IHHHHHHIIIHHHHHII", cd[pos : pos + 46]
            )
            name = cd[pos + 46 : pos + 46 + nlen].decode("utf-8", "replace")
            if name == member:
                hdr = self._get(url, lho, lho + 30 + nlen + 512 + csize)
                lnlen, lelen = struct.unpack("<HH", hdr[26:30])
                start = 30 + lnlen + lelen
                data = hdr[start : start + csize]
                if len(data) < csize:
                    data = self._get(url, lho + start, lho + start + csize - 1)
                raw = zlib.decompress(data, -15) if method == 8 else data
                return raw.decode("utf-8", "replace")
            pos += 46 + nlen + elen + clen
        return None


def _present(src: str, leaf: str) -> bool:
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return False
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name == leaf:
            return True
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if any((a.asname or a.name.split(".")[-1]) == leaf for a in node.names):
                return True
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", None) == leaf for t in node.targets
        ):
            return True
        if isinstance(node, ast.FunctionDef) and node.name == "__getattr__":
            if any(isinstance(c, ast.Constant) and c.value == leaf for c in ast.walk(node)):
                return True
    return False


def _module_warnings(src: str, leaf: str) -> list[str]:
    """warnings.warn messages that belong to ``leaf``: its own def/class body, the
    ``__getattr__`` branch serving it, or any module warning that names it."""
    tree = ast.parse(src)
    msgs = []

    def text(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.JoinedStr):
            return "".join(text(v) or "{...}" for v in node.values)
        if isinstance(node, ast.BinOp):
            return (text(node.left) or "") + (text(node.right) or "")
        return None

    def warns(scope):
        for n in ast.walk(scope):
            if (
                isinstance(n, ast.Call)
                and getattr(n.func, "attr", getattr(n.func, "id", "")) == "warn"
            ):
                t = text(n.args[0]) if n.args else None
                if t:
                    msgs.append(t)

    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name == leaf:
            warns(node)
        if isinstance(node, ast.FunctionDef) and node.name == "__getattr__":
            for n in ast.walk(node):
                if isinstance(n, ast.If) and any(
                    isinstance(c, ast.Constant) and c.value == leaf for c in ast.walk(n.test)
                ):
                    warns(ast.Module(body=n.body, type_ignores=[]))
    if not msgs:
        before = len(msgs)
        warns(tree)
        msgs[before:] = [m for m in msgs[before:] if leaf in m]
    return msgs


def intermediate_hint(
    fetcher: Fetcher, pair: dict, unit: str, module_file: str, old_v: str, new_v: str
) -> dict:
    root = unit.split(".")[0]
    dist = pair["dist"][root]
    parts = unit.split(".")
    module_parts = module_file[:-3].replace("/__init__", "").split("/")
    leaf = parts[len(module_parts)] if len(parts) > len(module_parts) else parts[-1]
    t0 = time.perf_counter()
    b0, r0 = fetcher.bytes, fetcher.requests
    releases = fetcher.releases(dist)
    cands = []
    for v, files in releases.items():
        try:
            pv = Version(v)
        except InvalidVersion:
            continue
        if pv.is_prerelease or not (Version(old_v) < pv < Version(new_v)):
            continue
        if files and not all(f.get("yanked") for f in files):
            cands.append((pv, v, files))
    cands.sort()
    # Newest first: the last release that still has the symbol carries its final
    # deprecation prose. Bisection is wrong here: histories are not monotonic (flask 2.3.0
    # dropped `flask.Markup` by accident and 2.3.1 restored it as a deprecated alias).
    # Try the newest patch of each minor series first, then every release, newest first.
    newest_per_minor, seen = [], set()
    for pv, v, files in reversed(cands):
        if (pv.major, pv.minor) not in seen:
            seen.add((pv.major, pv.minor))
            newest_per_minor.append((pv, v, files))
    order = newest_per_minor + [c for c in reversed(cands) if c not in newest_per_minor]
    probes, found = [], None
    for _, v, files in order[:MAX_INTERMEDIATE_PROBES]:
        src = fetcher.member(dist, v, files, module_file)
        ok = bool(src) and _present(src, leaf)
        probes.append([v, ok])
        if ok:
            found = (v, src)
            break
    result = {"unit": unit, "module_file": module_file, "candidates": len(cands), "probes": probes}
    if found:
        msgs = _module_warnings(found[1], leaf)
        result.update(
            {"last_version_with_symbol": found[0], "messages": msgs, "hint": successor_from(msgs)}
        )
    else:
        result.update({"last_version_with_symbol": None, "messages": [], "hint": None})
    result.update(
        {
            "seconds": round(time.perf_counter() - t0, 2),
            "bytes": fetcher.bytes - b0,
            "requests": fetcher.requests - r0,
        }
    )
    return result


def module_file_for(unit: str, env: Path, py: str) -> str | None:
    sp = next(env.glob("lib/python*/site-packages"))
    parts = unit.split(".")
    for i in range(len(parts) - 1, 0, -1):
        rel = Path(*parts[:i])
        if (sp / rel / "__init__.py").exists():
            return str(rel / "__init__.py")
        if (sp / rel.with_suffix(".py")).exists():
            return str(rel.with_suffix(".py"))
    return None


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


def run_pair(name: str, offline: bool) -> dict:
    pair = PAIRS[name]
    timing = {}
    for side in ("old", "new"):
        built = ensure_env(pair[side])
        if built is not None:
            timing[f"env_build_{side}_s"] = round(built, 2)
    symbols, map_s = touched_symbols(name, pair)
    timing["contact_map_s"] = round(map_s, 2)
    sym_file = RESULTS / f"{name}_symbols.json"
    sym_file.write_text(json.dumps(symbols, indent=1))
    probes, calls = {}, {}
    for side in ("old", "new"):
        py = str(pair[side]["env"] / "bin" / "python")
        probes[side], secs = run_json(
            [
                py,
                "-I",
                str(HERE / "probe.py"),
                str(sym_file),
                f"--deps={','.join(pair['deps'])}",
                *pair["probe_args"],
            ]
        )
        timing[f"probe_{side}_wall_s"] = round(secs, 3)
        timing[f"probe_{side}_in_process_s"] = probes[side]["probe_s"]
        calls[side], secs = run_json([py, "-I", str(HERE / pair["calls"])])
        timing[f"use_calls_{side}_wall_s"] = round(secs, 3)
    t0 = time.perf_counter()
    units = build_units(symbols, probes["old"], probes["new"])
    gt = ground_truth(units, calls["old"]["calls"], calls["new"]["calls"])
    local = local_predict(units, calls["old"]["calls"])
    local_any = local_predict(units, calls["old"]["calls"], rule="any")
    timing["diff_s"] = round(time.perf_counter() - t0, 3)
    rows, axiom_meta = load_axiom(pair)
    axiom = axiom_predict(units, rows)

    old_v = probes["old"]["versions"]
    new_v = probes["new"]["versions"]
    intermediates = []
    if not offline:
        fetcher = Fetcher(offline)
        for unit, g in gt.items():
            if "REMOVED" not in g["categories"]:
                continue
            expected = pair["successors"].get(unit, [])
            if hint_ok(local[unit]["hint"], expected):
                continue
            root = unit.split(".")[0]
            mf = module_file_for(unit, pair["old"]["env"], pair["old"]["python"])
            if not mf:
                continue
            try:
                res = intermediate_hint(fetcher, pair, unit, mf, old_v[root], new_v[root])
            except Exception as exc:  # noqa: BLE001 — record, never crash the bench
                res = {"unit": unit, "error": f"{type(exc).__name__}: {exc}"}
            intermediates.append(res)
            if res.get("hint"):
                local[unit]["hint"] = res["hint"]
                local[unit]["hint_source"] = (
                    f"intermediate {res['last_version_with_symbol']} (range-fetched)"
                )
        timing["intermediate_total_s"] = round(sum(r.get("seconds", 0) for r in intermediates), 2)
        timing["intermediate_bytes"] = sum(r.get("bytes", 0) for r in intermediates)

    scored = {
        "local": {"api": score(gt, local), "use": score(gt, local, USE_IMPACT)},
        "local_any_static_warn": {
            "api": score(gt, local_any),
            "use": score(gt, local_any, USE_IMPACT),
        },
        "axiom": {"api": score(gt, axiom), "use": score(gt, axiom, USE_IMPACT)},
    }
    hints = {}
    for unit, g in gt.items():
        if "REMOVED" in g["categories"]:
            exp = pair["successors"].get(unit, [])
            hints[unit] = {
                "expected": exp,
                "local": [
                    local[unit]["hint"],
                    local[unit]["hint_source"],
                    hint_ok(local[unit]["hint"], exp),
                ],
                "axiom": [
                    axiom[unit]["hint"],
                    axiom[unit]["hint_text"][:1],
                    hint_ok(axiom[unit]["hint"], exp),
                ],
            }
    local_total = sum(
        v
        for k, v in timing.items()
        if k.startswith(("probe_", "use_calls_old")) and k.endswith("wall_s")
    ) + timing.get("diff_s", 0)
    timing["local_total_excl_env_build_s"] = round(
        local_total + timing.get("intermediate_total_s", 0), 2
    )
    report = {
        "pair": name,
        "versions": {
            "old": old_v,
            "new": new_v,
            "python": [probes["old"]["python"], probes["new"]["python"]],
        },
        "symbols": len(symbols),
        "units": len(units),
        "timing": timing,
        "axiom_meta": axiom_meta,
        "score": scored,
        "hints": hints,
        "intermediates": intermediates,
        "per_unit": {
            u: {
                "ground_truth": gt[u],
                "local": local[u],
                "axiom": axiom[u],
                "contacts": units[u]["contacts"],
            }
            for u in sorted(units)
        },
    }
    (RESULTS / f"{name}_parity.json").write_text(json.dumps(report, indent=1, default=str))
    return report


def print_report(rep: dict) -> None:
    print(f"\n=== {rep['pair']}  {rep['versions']}  symbols={rep['symbols']} units={rep['units']}")
    print("axiom checkpoints:", rep["axiom_meta"])
    for who in ("local", "local_any_static_warn", "axiom"):
        for view in ("api", "use"):
            s = rep["score"][who][view]
            p = "n/a" if s["precision"] is None else f"{s['precision']:.2f}"
            r = "n/a" if s["recall"] is None else f"{s['recall']:.2f}"
            counts = f"TP={s['tp']:>2} FP={s['fp']:>2} FN={s['fn']:>2}"
            print(f"  {who:<22} {view:<4} {counts}  P={p} R={r}")
    print("  hints:", json.dumps(rep["hints"]))
    print("  timing:", rep["timing"])
    print(f"  {'unit':<52}{'ground truth':<34}{'local':<34}axiom")
    for u, d in rep["per_unit"].items():
        g = ",".join(sorted(d["ground_truth"]["categories"])) or "-"
        lo = ",".join(sorted(d["local"]["categories"])) or "-"
        ax = ",".join(sorted(d["axiom"]["categories"])) or "-"
        if g != "-" or lo != "-" or ax != "-":
            print(f"  {u:<52}{g:<34}{lo:<34}{ax}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pair", default="all", choices=["all", *PAIRS])
    ap.add_argument("--offline", action="store_true", help="skip the intermediate-release fetch")
    args = ap.parse_args()
    RESULTS.mkdir(parents=True, exist_ok=True)
    names = list(PAIRS) if args.pair == "all" else [args.pair]
    for n in names:
        print_report(run_pair(n, args.offline))
    return 0


if __name__ == "__main__":
    sys.exit(main())
