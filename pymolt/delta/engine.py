"""Local delta engine: API-change facts for the symbols a project touches.

``compute_deltas(project_dir, migrations)`` probes the touched symbols in the old and the
new interpreter (``pymolt/delta/probe.py``, a stdlib subprocess) and diffs the answers
into ``DeltaReport``s (``pymolt.delta/1``). Flow:

1. interpreters: ``envs.resolve_old`` / ``envs.resolve_new`` (baseline capture → base env →
   throwaway; target env → throwaway built with uv). An unavailable side makes its packages
   INCONCLUSIVE with the reason, never "no changes";
2. touched symbols: given, or derived from the static contact map plus the baseline trace's
   symbols (dynamic-only). The probe spells canonical names the way the identity resolver
   and a profile-backend trace do, so the canonical names join with baseline facts;
3. both probes run in parallel; a second, small probe of the new interpreter answers
   "is the same object reachable elsewhere" (old canonical names, successor hints);
4. :func:`diff` (pure) classifies each touched object: REMOVED | MOVED | SIGNATURE |
   DEFAULT | DEPRECATED | UNCHANGED, with evidence and successor hints in the documented
   order (baseline warning text, old docstring/deprecation prose, curated knowledge, and
   only with ``online=True`` the intermediate-release fetch).

Probe answers and fetched hints are cached under ``.pymolt/cache/delta/``.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import logging
import os
import re
import subprocess
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from packaging.utils import canonicalize_name

from pymolt.adapters.python_runner import RunnerSpec, run_in, to_container
from pymolt.delta import envs as envs_mod
from pymolt.delta import fetch as fetch_mod
from pymolt.delta.models import (
    KIND_ORDER,
    SCHEMA,
    DeltaFact,
    DeltaReport,
    Interpreter,
    RecordedWarning,
    TouchedSymbol,
)
from pymolt.delta.probe import MARK, PROBE_VERSION

log = logging.getLogger(__name__)

CACHE_DIR = Path(".pymolt") / "cache" / "delta"
PROBE_TIMEOUT = 300.0
PER_PATH_TIMEOUT = 15.0
_MSG = 300
_PROBE_SOURCE_PATH = Path(__file__).with_name("probe.py")
_DEPRECATES = re.compile(r"deprecat|will (?:be )?(?:removed|change|default)|in a future version",
                         re.IGNORECASE)


# ── inputs ────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Migration:
    name: str
    from_version: str
    to_version: str


def as_migrations(items: Iterable[Any]) -> list[Migration]:
    """Accept ``DependencyMigration``s, ``(name, from, to)`` tuples or dicts."""
    out: list[Migration] = []
    for m in items or []:
        if isinstance(m, Migration):
            out.append(m)
        elif isinstance(m, Mapping):
            out.append(Migration(
                str(m["name"]), str(m.get("from_version") or m.get("from")),
                str(m.get("to_version") or m.get("to")),
            ))
        elif isinstance(m, (tuple, list)) and len(m) == 3:
            out.append(Migration(str(m[0]), str(m[1]), str(m[2])))
        else:
            out.append(Migration(str(m.name), str(m.from_version), str(m.to_version)))
    return out


def as_touched(symbols: Iterable[str | TouchedSymbol | Mapping]) -> dict[str, TouchedSymbol]:
    out: dict[str, TouchedSymbol] = {}
    for s in symbols:
        if isinstance(s, str):
            t = TouchedSymbol(path=s)
        elif isinstance(s, TouchedSymbol):
            t = s
        else:
            t = TouchedSymbol.model_validate(s)
        t.path = t.path.strip()
        if not t.path:
            continue
        if t.path in out:
            out[t.path].warnings.extend(t.warnings)
        else:
            out[t.path] = t.model_copy(deep=True)
    return out


def _fact_warnings(fact: Any) -> list[RecordedWarning]:
    """Warnings a baseline fact recorded (section 3 of the design), tolerant of shape."""
    raw = getattr(fact, "warnings", None)
    if raw is None and isinstance(fact, Mapping):
        raw = fact.get("warnings")
    out = []
    for w in raw or []:
        if isinstance(w, Mapping):
            msg = w.get("message")
            cat = w.get("category") or "Warning"
            count = w.get("count") or 1
        else:
            msg = getattr(w, "message", None)
            cat = getattr(w, "category", None) or "Warning"
            count = getattr(w, "count", 1) or 1
        if isinstance(msg, str) and msg:
            out.append(RecordedWarning(category=str(cat), message=msg[:_MSG], count=int(count)))
    return out


def derive_symbols(root: Path, trace: Path | None) -> tuple[dict[str, TouchedSymbol], list[str]]:
    """Static contact-map targets plus the baseline trace's symbols (and its warnings)."""
    notes: list[str] = []
    touched: dict[str, TouchedSymbol] = {}
    try:
        from pymolt.verify.contact_map import build_contact_map

        cmap = build_contact_map(root)
        for c in cmap.contacts:
            touched.setdefault(c.target, TouchedSymbol(path=c.target, origin="contact"))
    except Exception as exc:  # noqa: BLE001 — the trace may still supply symbols
        notes.append(f"contact map failed: {exc}")
    for sym, warns in _trace_symbols(root, trace, notes).items():
        t = touched.setdefault(sym, TouchedSymbol(path=sym, origin="trace"))
        t.warnings.extend(warns)
    return touched, notes


def _trace_symbols(root: Path, trace: Path | None,
                   notes: list[str]) -> dict[str, list[RecordedWarning]]:
    if trace is None or not (trace.exists() or Path(str(trace) + ".facts.json").exists()):
        return {}
    try:
        from pymolt.verify.facts import load_facts

        facts = load_facts(trace, root=root)
    except Exception as exc:  # noqa: BLE001
        notes.append(f"baseline trace unreadable ({trace}): {exc}")
        return {}
    out: dict[str, list[RecordedWarning]] = {}
    for f in facts.facts:
        out.setdefault(f.symbol, []).extend(_fact_warnings(f))
    return out


# ── probe subprocess ──────────────────────────────────────────────────────────────────


@dataclass
class ProbeRun:
    meta: dict[str, Any] = field(default_factory=dict)
    results: dict[str, dict] = field(default_factory=dict)
    failure: str | None = None
    seconds: float = 0.0
    from_cache: bool = False

    @property
    def ok(self) -> bool:
        return self.failure is None


def _child_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYMOLT_TRACE")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def _mtime(path: str) -> int | None:
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


def _stamps(executable: str, site_dirs: list[str]) -> dict[str, int | None]:
    real = os.path.realpath(executable)
    stamps = {f"interpreter:{real}": _mtime(real)}
    for d in site_dirs:
        stamps[f"site:{d}"] = _mtime(d)
    return stamps


# Runs inside the runner (possibly Python 3.6): stdlib only, no f-strings.
_REMOTE_STAMPS_SOURCE = r'''
import json, os, shutil, sys
req = json.loads(sys.stdin.read() or "{}")
exe = req.get("exe") or ""
real = os.path.realpath(shutil.which(exe) or exe)
def mtime(p):
    try:
        return os.stat(p).st_mtime_ns
    except OSError:
        return None
out = {"interpreter:" + real: mtime(real)}
for d in req.get("dirs") or []:
    out["site:" + d] = mtime(d)
sys.stdout.write(json.dumps(out))
'''


def _remote_stamps(runner: RunnerSpec, executable: str,
                   site_dirs: list[str]) -> dict[str, int | None] | None:
    """The same validity stamps, read by a tiny stdlib script inside the runner."""
    try:
        proc = run_in(runner, [executable, "-B", "-c", _REMOTE_STAMPS_SOURCE],
                      stdin=json.dumps({"exe": executable, "dirs": site_dirs}), timeout=30.0)
        if proc.returncode != 0:
            return None
        return json.loads(proc.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _cache_file(root: Path, executable: str, request: dict,
                runner: RunnerSpec | None = None) -> Path:
    key = json.dumps({"v": PROBE_VERSION,
                      "exe": executable if runner else os.path.abspath(executable),
                      "container": runner.container_id if runner else None,
                      "image": (runner.image_id or runner.image) if runner else None,
                      "src": hashlib.sha256(_PROBE_SOURCE_PATH.read_bytes()).hexdigest(),
                      "req": request}, sort_keys=True)
    return root / CACHE_DIR / f"probe-{hashlib.sha256(key.encode()).hexdigest()[:20]}.json"


def _parse_probe_output(run: ProbeRun, stdout: str, stderr: str, returncode: int | None) -> None:
    done = False
    for line in (stdout or "").splitlines():
        if not line.startswith(MARK):
            continue
        try:
            payload = json.loads(line[len(MARK):])
        except ValueError:
            continue
        if isinstance(payload.get("meta"), dict):
            run.meta = payload["meta"]
        elif payload.get("done"):
            done = True
        elif isinstance(payload.get("path"), str) and isinstance(payload.get("result"), dict):
            run.results[payload["path"]] = payload["result"]
    if run.failure is None and not done:
        tail = (stderr or "").strip().splitlines()[-1:] or [""]
        run.failure = f"probe stopped before finishing (exit {returncode}) {tail[0][:200]}"


def run_probe(
    executable: str, root: Path, request: dict, *, use_cache: bool = True,
    timeout: float = PROBE_TIMEOUT, runner: RunnerSpec | None = None,
) -> ProbeRun:
    """Run the probe in ``executable`` with the project as cwd. Never raises.

    ``runner``: run it inside that container instead (``docker exec -i``; the request goes
    on stdin, nothing is copied in; ``executable`` is a path inside the container and the
    cwd is the project's mount point there). Timeouts grow ×4 when the container is emulated.
    """
    cache = _cache_file(root, executable, request, runner)

    def stamps_now(site_dirs: list[str]) -> dict[str, int | None] | None:
        if runner is None:
            return _stamps(executable, site_dirs)
        return _remote_stamps(runner, executable, site_dirs)

    if use_cache:
        try:
            payload = json.loads(cache.read_text(encoding="utf-8"))
            now = stamps_now(payload["meta"].get("site_dirs") or [])
            if now is not None and payload.get("stamps") == now:
                return ProbeRun(meta=payload["meta"], results=payload["results"], from_cache=True)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass
    t0 = time.perf_counter()
    run = ProbeRun()
    if runner is not None:
        cwd = to_container(runner, str(root))
        factor = 4 if runner.emulated else 1
        try:
            proc = run_in(runner, [executable, "-B", "-c",
                                   _PROBE_SOURCE_PATH.read_text(encoding="utf-8")],
                          stdin=json.dumps(request), cwd=cwd, timeout=timeout)
        except subprocess.TimeoutExpired:
            run.failure = f"probe timed out after {timeout * factor:g}s in container {runner.name}"
            run.seconds = round(time.perf_counter() - t0, 3)
            return run
        except (OSError, subprocess.SubprocessError) as exc:
            run.failure = f"could not start {executable} in container {runner.name}: {exc}"
            return run
        _parse_probe_output(run, proc.stdout, proc.stderr, proc.returncode)
    else:
        stdout = stderr = ""
        try:
            proc = subprocess.Popen(
                [executable, "-B", "-c", _PROBE_SOURCE_PATH.read_text(encoding="utf-8")],
                cwd=str(root), env=_child_env(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
            )
        except OSError as exc:
            run.failure = f"could not start {executable}: {exc}"
            return run
        try:
            stdout, stderr = proc.communicate(json.dumps(request), timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            run.failure = f"probe timed out after {timeout:g}s"
        _parse_probe_output(run, stdout, stderr, proc.returncode)
    run.seconds = round(time.perf_counter() - t0, 3)
    if run.ok and run.meta:
        site_dirs = run.meta.get("site_dirs") or []
        stamps = stamps_now(site_dirs)
        if stamps is not None:
            try:
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(json.dumps({
                    "stamps": stamps, "meta": run.meta, "results": run.results,
                }), encoding="utf-8")
            except OSError as exc:
                log.debug("delta probe cache not written: %s", exc)
    return run


# ── diff (pure) ───────────────────────────────────────────────────────────────────────

_SENTINEL = re.compile(r"<(no_default|NoDefault\.no_default|_NoDefault\.no_default|object object)>")
_VAR = ("VAR_POSITIONAL", "VAR_KEYWORD")
_POSITIONAL = ("POSITIONAL_ONLY", "POSITIONAL_OR_KEYWORD")


def _default(d: str | None) -> str | None:
    return None if d is None else _SENTINEL.sub("<sentinel>", d)


@dataclass(frozen=True)
class ParamChange:
    kind: str  # SIGNATURE | DEFAULT
    param: str | None
    old: str | None
    new: str | None
    note: str


def param_changes(old: list | None, new: list | None) -> list[ParamChange]:
    """Parameter-level differences between two ``[name, kind, default-repr]`` shapes.

    A removed parameter is not reported when the new callable takes ``**kwargs`` (the call
    still binds); a removed positional parameter replaced at the same index by a new one of
    the same kind is a rename.
    """
    if not old or not new:
        return []
    o = {p[0]: p for p in old}
    n = {p[0]: p for p in new}
    new_has_varkw = any(p[1] == "VAR_KEYWORD" for p in new)
    old_pos = [p for p in old if p[1] in _POSITIONAL]
    new_pos = [p for p in new if p[1] in _POSITIONAL]
    renamed: dict[str, str] = {}
    for i, p in enumerate(old_pos):
        if p[0] in n or i >= len(new_pos):
            continue
        q = new_pos[i]
        if q[0] not in o and q[1] == p[1]:
            renamed[p[0]] = q[0]
    out: list[ParamChange] = []
    for name, (_, kind, default) in o.items():
        if kind in _VAR:
            if not any(p[1] == kind for p in new):
                star = "*" if kind == "VAR_POSITIONAL" else "**"
                out.append(ParamChange("SIGNATURE", name, kind, None, f"{star}{name} removed"))
            continue
        if name in renamed:
            out.append(ParamChange("SIGNATURE", name, name, renamed[name],
                                   f"param {name} renamed to {renamed[name]}"))
            continue
        if name not in n:
            if not new_has_varkw:
                out.append(ParamChange("SIGNATURE", name, kind, None, f"param {name} removed"))
            continue
        _, nkind, ndefault = n[name]
        if nkind != kind:
            out.append(ParamChange("SIGNATURE", name, kind, nkind,
                                   f"param {name} kind {kind}->{nkind}"))
        if _default(default) != _default(ndefault):
            out.append(ParamChange("DEFAULT", name, default, ndefault,
                                   f"param {name} default {default}->{ndefault}"))
    added = set(renamed.values())
    for name, (_, kind, default) in n.items():
        if name not in o and name not in added and default is None and kind not in _VAR:
            out.append(ParamChange("SIGNATURE", name, None, f"{kind} (required)",
                                   f"param {name} added as required"))
    return out


def bases_change(old: dict, new: dict) -> ParamChange | None:
    if old.get("kind") != "class" or new.get("kind") != "class":
        return None
    lo = [b.rsplit(".", 1)[-1] for b in old.get("bases") or []]
    ln = [b.rsplit(".", 1)[-1] for b in new.get("bases") or []]
    if lo == ln:
        return None
    return ParamChange("SIGNATURE", None, ", ".join(old.get("bases") or []),
                       ", ".join(new.get("bases") or []), f"bases {lo} -> {ln}")


@dataclass
class Spell:
    """One dependency-owned spelling (``flask.request.headers``) in one environment."""

    spelling: str
    step: dict
    module: str
    index: int  # 0 = an attribute of the imported module (``from module import attr``)
    paths: set[str] = field(default_factory=set)
    final_for: set[str] = field(default_factory=set)
    runtime_warnings: list[dict] = field(default_factory=list)
    module_file: str | None = None
    # A touched symbol in its own right: the end of a touched path, or a prefix of a path
    # the project spells (``flask.request`` of ``flask.request.headers.get``). A prefix of a
    # defining name from the trace (``flask.scaffold.Scaffold`` of
    # ``flask.scaffold.Scaffold.route``) is not: the project never names it.
    unit: bool = False

    @property
    def exists(self) -> bool:
        return bool(self.step.get("exists"))

    @property
    def canonical(self) -> str:
        return self.step.get("canonical") or self.step.get("owner") or self.spelling


def spellings(results: Mapping[str, dict],
              expand: Callable[[str], bool] | None = None) -> dict[str, Spell]:
    """Every dependency-owned prefix of every probed path, by spelling.

    ``expand(path)`` says whether the path's prefixes are touched symbols too (default
    yes); its final spelling always is.
    """
    out: dict[str, Spell] = {}
    for path, res in results.items():
        prefixes_are_units = expand(path) if expand is not None else True
        module = res.get("module")
        if not module:
            continue
        dep_steps = []
        for st in res.get("steps") or []:
            if not st.get("dep"):
                break
            dep_steps.append(st)
            if not st.get("exists"):
                break
        prefix = module
        for k, st in enumerate(dep_steps):
            spelling = prefix + "." + st["attr"]
            prefix = spelling
            final = k == len(dep_steps) - 1
            sp = out.get(spelling)
            if sp is None or (not sp.exists and st.get("exists")):
                prev = sp
                sp = Spell(spelling, st, module, k, module_file=res.get("module_file"))
                if prev is not None:
                    sp.paths |= prev.paths
                    sp.final_for |= prev.final_for
                    sp.unit = prev.unit
                out[spelling] = sp
            sp.paths.add(path)
            if final or prefixes_are_units:
                sp.unit = True
            if final:
                sp.final_for.add(path)
                if res.get("runtime_warnings") and not sp.runtime_warnings:
                    sp.runtime_warnings = list(res["runtime_warnings"])
    return out


def _leaf(name: str) -> str:
    return name.rsplit(".", 1)[-1]


def _mentions(msg: str, word: str) -> bool:
    w = word.strip("_")
    return bool(w) and re.search(r"(?<![A-Za-z0-9_])" + re.escape(w) + r"(?![A-Za-z0-9_])",
                                 msg) is not None


def _trim(msg: str) -> str:
    msg = " ".join((msg or "").split())
    return msg if len(msg) <= _MSG else msg[: _MSG - 1] + "…"


@dataclass
class _Evidence:
    """Deprecation evidence for one object, split by how unconditional it is."""

    object_level: list[str] = field(default_factory=list)  # evidence strings
    baseline: list[RecordedWarning] = field(default_factory=list)
    static: list[str] = field(default_factory=list)  # warn() prose in its body (conditional)
    doc_mentions: list[str] = field(default_factory=list)


def _side_deprecation(step: dict, leaf: str, runtime: list[dict], prefix: str) -> list[str]:
    out = []
    for w in step.get("getattr_warns") or []:
        if _mentions(w.get("message", ""), leaf):
            out.append(f"{prefix}getattr: {_trim(w['message'])}")
    for w in runtime or []:
        out.append(f"{prefix}access-warning: {w.get('category')}: {_trim(w.get('message', ''))}")
    if step.get("deprecated_attr"):
        out.append(f"{prefix}__deprecated__: {_trim(step['deprecated_attr'])}")
    for line in (step.get("doc") or {}).get("directive") or []:
        out.append(f"{prefix}docstring: {_trim(line)}")
    return out


def _hint_bucket2(step: dict, leaf: str) -> list[str]:
    """Old-version deprecation prose (docstring, __deprecated__, warn() text)."""
    msgs = list((step.get("doc") or {}).get("directive") or [])
    msgs += list((step.get("doc") or {}).get("mentions") or [])
    if step.get("deprecated_attr"):
        msgs.append(step["deprecated_attr"])
    msgs += [w.get("message", "") for w in step.get("getattr_warns") or []
             if _mentions(w.get("message", ""), leaf)]
    warns = step.get("warns") or []
    if step.get("kind") == "class":
        warns = [w for w in warns if _mentions(w.get("message", ""), leaf)]
    msgs += [w.get("message", "") for w in warns]
    return msgs


@dataclass
class DiffInput:
    old: Mapping[str, dict]
    new: Mapping[str, dict]
    touched: Mapping[str, TouchedSymbol]
    root_to_package: Mapping[str, str]
    # paths -> results in the new interpreter, resolved without the dependency-root limit
    resolve_new: Callable[[list[str]], Mapping[str, dict]] | None = None
    # (package, spellings) -> (successor, source) from curated knowledge
    knowledge_hint: Callable[[str, list[str]], tuple[str, str] | None] | None = None
    # new-side paths whose import raised (not "no such module"): never counted as removed
    import_failed: frozenset[str] = frozenset()


def diff(inp: DiffInput) -> dict[str, list[DeltaFact]]:
    """Classify every touched object; facts grouped by migration package name."""
    def expand(path: str) -> bool:
        t = inp.touched.get(path)
        return t is None or t.origin != "trace"

    sp_old = spellings(inp.old, expand)
    sp_new = spellings(inp.new, expand)

    # baseline warnings, by the old spelling the touched path ends at
    baseline: dict[str, list[RecordedWarning]] = {}
    for path, t in inp.touched.items():
        if not t.warnings:
            continue
        ends = [s for s in sp_old.values() if path in s.final_for]
        for s in ends or ([sp_old[path]] if path in sp_old else []):
            baseline.setdefault(s.spelling, []).extend(t.warnings)

    def import_failed(name: str) -> bool:
        return any(name == p or name.startswith(p + ".") or p.startswith(name + ".")
                   for p in inp.import_failed)

    groups: dict[str, list[str]] = {}
    for s in sp_old.values():
        if not s.unit or not s.exists or s.step.get("kind") == "module":
            continue
        if import_failed(s.spelling):  # raised on import in the new interpreter: not "removed"
            continue
        groups.setdefault(s.canonical, []).append(s.spelling)

    # what the second pass must answer: old canonical names that are not touched spellings,
    # when a member vanished or now names something else; and successor hints to check.
    hints: dict[str, tuple[str | None, str | None]] = {}
    need: set[str] = set()
    for canon, members in groups.items():
        vanished = [m for m in members if not (m in sp_new and sp_new[m].exists)]
        renamed = [m for m in members if m in sp_new and sp_new[m].exists
                   and sp_new[m].canonical != canon]
        if canon not in sp_old and (vanished or renamed):
            need.add(canon)
        if vanished:
            for m in members:  # the same object under another spelling, probed on the old side
                need.update(sp_old[m].step.get("same_object_as") or [])
            hints[canon] = _local_hint(canon, members, sp_old, baseline, inp)
            succ = hints[canon][0]
            if succ:
                need.update(_successor_candidates(succ, canon))
    extra: dict[str, Spell] = {}
    todo = sorted(p for p in need if p not in sp_new)
    if todo and inp.resolve_new is not None:
        extra = spellings(inp.resolve_new(todo))

    def new_of(name: str) -> Spell | None:
        s = sp_new.get(name) or extra.get(name)
        return s if s is not None and s.exists else None

    def new_error(name: str) -> str | None:
        parts = name.split(".")
        for i in range(len(parts), 1, -1):
            s = sp_new.get(".".join(parts[:i])) or extra.get(".".join(parts[:i]))
            if s is not None:
                return None if s.exists else s.step.get("error")
        return None

    facts: dict[str, list[DeltaFact]] = {}

    def add(fact: DeltaFact) -> None:
        pkg = _package_for(fact, inp.root_to_package)
        if pkg is not None:
            facts.setdefault(pkg, []).append(fact)

    for canon, members in sorted(groups.items()):
        for fact in _group_facts(canon, sorted(members), sp_old, new_of, new_error, baseline,
                                 hints.get(canon, (None, None))):
            add(fact)
    for pkg in facts:
        facts[pkg] = _dedupe(facts[pkg])
    return facts


def _successor_candidates(succ: str, canon: str) -> list[str]:
    root = canon.split(".")[0]
    out = [succ]
    if not succ.startswith(root + "."):
        out.append(f"{root}.{succ}")
    return out


def _local_hint(canon: str, members: list[str], sp_old: Mapping[str, Spell],
                baseline: Mapping[str, list[RecordedWarning]],
                inp: DiffInput) -> tuple[str | None, str | None]:
    """Successor hint from sources 1-3: baseline warning, old deprecation prose, knowledge."""
    leaf = _leaf(canon)
    warn_msgs = [w.message for m in members for w in baseline.get(m, [])]
    succ = fetch_mod.successor_from(warn_msgs)
    if succ:
        return succ, "warning"
    prose: list[str] = []
    for m in members:
        prose += _hint_bucket2(sp_old[m].step, leaf)
    succ = fetch_mod.successor_from(prose)
    if succ:
        return succ, "docstring"
    if inp.knowledge_hint is not None:
        pkg = None
        for name in [canon, *members]:
            pkg = inp.root_to_package.get(name.split(".")[0])
            if pkg:
                break
        if pkg:
            found = inp.knowledge_hint(pkg, [canon, *members])
            if found:
                return found
    return None, None


def _group_facts(
    canon: str, members: list[str], sp_old: Mapping[str, Spell],
    new_of: Callable[[str], Spell | None], new_error: Callable[[str], str | None],
    baseline: Mapping[str, list[RecordedWarning]], hint: tuple[str | None, str | None],
) -> list[DeltaFact]:
    leaf = _leaf(canon)
    old_steps = {m: sp_old[m] for m in members}
    surviving = [m for m in members if new_of(m) is not None]
    vanished = [m for m in members if new_of(m) is None]
    c_new = new_of(canon)
    any_old = old_steps[members[0]].step

    # old-side deprecation evidence (object-level, unconditional) and baseline warnings
    ev = _Evidence()
    for m in members:
        s = old_steps[m]
        runtime = s.runtime_warnings if s.final_for else []
        ev.object_level += _side_deprecation(s.step, leaf, runtime, "")
        if s.step.get("kind") != "class":  # a class body's warn() calls are about anything
            ev.static += [w.get("message", "") for w in s.step.get("warns") or []]
        ev.doc_mentions += list((s.step.get("doc") or {}).get("mentions") or [])
        ev.baseline += baseline.get(m, [])
    ev.object_level = sorted(set(ev.object_level))
    old_deprecated = bool(ev.object_level or ev.baseline)
    baseline_ev = sorted({f"warning:{w.text()}" if len(w.text()) <= _MSG
                          else f"warning:{_trim(w.text())}" for w in ev.baseline})

    out: list[DeltaFact] = []

    # ── vanished spellings: MOVED (object reachable elsewhere) or REMOVED ───────────────
    moved: dict[str, list[str]] = {}
    moved_new_canon: dict[str, str] = {}
    removed: list[str] = []
    for m in vanished:
        succ: str | None = None
        new_canon: str | None = None
        cands = [t for t in surviving
                 if _compatible(any_old, new_of(t).step, leaf)  # type: ignore[union-attr]
                 and _leaf(new_of(t).canonical) == leaf]  # type: ignore[union-attr]
        if m != canon and c_new is not None and _compatible(any_old, c_new.step, leaf):
            cands.append(canon)
        if not cands:
            same = _same_object_successor(old_steps[m].step, any_old, leaf, new_of)
            if same is not None:
                moved.setdefault(same.spelling, []).append(m)
                moved_new_canon[same.spelling] = same.canonical
                continue
        if cands:
            best = min(cands, key=_public_first)
            new_canon = new_of(best).canonical  # type: ignore[union-attr]
            # the defining name moved: name its new home; a dropped alias: a live spelling
            succ = new_canon if m == canon else best
        if succ and succ != m:
            moved.setdefault(succ, []).append(m)
            moved_new_canon[succ] = new_canon or succ
        else:
            removed.append(m)

    for succ, spells in sorted(moved.items()):
        symbol = canon if canon in spells else spells[0]
        target = new_of(succ)
        fact = DeltaFact(
            symbol=symbol, aliases=[s for s in spells if s != symbol], kind="MOVED",
            old=canon, new=moved_new_canon[succ], successor=succ,
            deprecated_in_old=old_deprecated,
            evidence=["existence", f"same object reachable as {succ}", *ev.object_level,
                      *baseline_ev],
        )
        fact.detail.update(
            importable=[s for s in spells if old_steps[s].index == 0],
            successor_importable=bool(target is not None and target.index == 0),
            successor_same_signature=True,
        )
        out.append(fact)

    if removed:
        symbol = canon if canon in removed else removed[0]
        succ, source = hint
        error = next((e for e in (new_error(m) for m in removed) if e), None)
        evidence = ["existence"]
        if error:
            evidence.append(f"absent in new: {error}")
        evidence += [*ev.object_level, *baseline_ev]
        if succ:
            evidence.append(f"successor:{source}")
        fact = DeltaFact(
            symbol=symbol, aliases=[s for s in removed if s != symbol], kind="REMOVED",
            old=canon, successor=succ, deprecated_in_old=old_deprecated, evidence=evidence,
        )
        resolved = None
        if succ:
            for cand in _successor_candidates(succ, canon):
                if new_of(cand) is not None:
                    resolved = cand
                    break
        if resolved and resolved != succ:
            fact.successor = resolved
        target = new_of(resolved) if resolved else None
        old_owner = old_steps[symbol].step
        fact.detail.update(
            importable=[s for s in removed if old_steps[s].index == 0],
            successor_importable=bool(target is not None and target.index == 0),
            successor_same_signature=bool(
                target is not None and any_old.get("shape") is not None
                and target.step.get("shape") == any_old.get("shape")
            ),
            online_query=_online_query(old_steps[symbol], old_owner),
            hint_source=source,
        )
        out.append(fact)

    # ── surviving spellings: object-level comparison ─────────────────────────────────────
    changes: dict[tuple, set[str]] = {}
    notes: dict[tuple, list[str]] = {}
    new_dep: dict[str, set[str]] = {}
    for m in surviving:
        o = old_steps[m].step
        ns = new_of(m)
        n = ns.step  # type: ignore[union-attr]
        symbol = canon
        rebound = None
        cn = ns.canonical  # type: ignore[union-attr]
        if cn != canon:
            ref = c_new
            if ref is not None and ref.canonical != cn:
                symbol, rebound = m, f"rebound: {canon} -> {cn}"
        for ch in param_changes(o.get("shape"), n.get("shape")):
            key = (symbol, ch.kind, ch.param, ch.old, ch.new)
            changes.setdefault(key, set()).add(m)
            notes.setdefault(key, ["signature", *([rebound] if rebound else [])])
        bc = bases_change(o, n)
        if bc is not None:
            key = (symbol, bc.kind, None, bc.old, bc.new)
            changes.setdefault(key, set()).add(m)
            notes.setdefault(key, ["bases", bc.note])
        runtime = ns.runtime_warnings if ns.final_for else []  # type: ignore[union-attr]
        dep_new = _side_deprecation(n, leaf, runtime, "deprecated-in-new:")
        if dep_new:
            new_dep.setdefault(symbol, set()).update(dep_new)
            changes.setdefault((symbol, "DEPRECATED", None, None, None), set()).add(m)

    param_facts = []
    for key, spells in sorted(changes.items(), key=lambda kv: tuple(str(x) for x in kv[0])):
        symbol, kind, param, old, new = key
        evidence = list(notes.get(key, []))
        deprecated = old_deprecated if param is None else False
        if param is not None:
            words = [param]
            related = [w for w in ev.baseline if _mentions(w.message, param)]
            if related:
                deprecated = True
                evidence += [f"warning:{_trim(w.text())}" for w in related]
            prose = [m for m in ev.static + ev.doc_mentions
                     if any(_mentions(m, w) for w in words) and _DEPRECATES.search(m)]
            if prose:
                deprecated = True
                evidence += [f"old-prose: {_trim(m)}" for m in prose[:2]]
            if ev.object_level:
                deprecated = True
        if kind == "DEPRECATED":
            evidence = sorted(new_dep.get(symbol, set()))
        if param is None:
            evidence += [*ev.object_level, *baseline_ev]
        fact = DeltaFact(
            symbol=symbol, aliases=sorted(s for s in spells if s != symbol), kind=kind,
            param=param, old=old, new=new, deprecated_in_old=deprecated,
            evidence=_unique(evidence),
        )
        if kind == "DEPRECATED":
            succ = fetch_mod.successor_from([e.split(": ", 1)[-1] for e in evidence])
            fact.successor = succ
        param_facts.append(fact)
    out += param_facts

    # baseline warnings no fact carries yet: the object warned in the old version
    carried = {e for f in out for e in f.evidence if e.startswith("warning:")}
    loose = [e for e in baseline_ev if e not in carried]
    if surviving and (loose or (ev.object_level and not out)):
        symbol = canon if canon in surviving or canon not in members else surviving[0]
        fact = DeltaFact(
            symbol=symbol, aliases=[s for s in surviving if s != symbol], kind="DEPRECATED",
            deprecated_in_old=True, evidence=_unique([*ev.object_level, *loose]),
            successor=fetch_mod.successor_from([w.message for w in ev.baseline]
                                               + [e.split(": ", 1)[-1] for e in ev.object_level]),
        )
        out.append(fact)

    if not out and surviving:
        symbol = canon
        out.append(DeltaFact(symbol=symbol, aliases=[s for s in members if s != symbol],
                             kind="UNCHANGED", evidence=["existence", "signature"]))
    return out


def _same_object_successor(step: dict, any_old: dict, leaf: str,
                           new_of: Callable[[str], Spell | None]) -> Spell | None:
    """A spelling the old interpreter proved to be the same object (``is``) that is live and
    not itself deprecated on the new side; the shortest public one."""
    live = []
    for cand in step.get("same_object_as") or []:
        target = new_of(cand)
        if target is None or not _compatible(any_old, target.step, leaf):
            continue
        runtime = target.runtime_warnings if target.final_for else []
        if _side_deprecation(target.step, _leaf(cand), runtime, ""):
            continue
        live.append(target)
    return min(live, key=lambda t: _public_first(t.spelling)) if live else None


def _public_first(name: str) -> tuple:
    private = any(part.startswith("_") and not part.startswith("__")
                  for part in name.split("."))
    return (private, name.count("."), name)


def _compatible(old: dict, new: dict, leaf: str) -> bool:
    ok = old.get("kind") == new.get("kind")
    return ok or {old.get("kind"), new.get("kind")} <= {"callable", "class"}


def _online_query(spell: Spell, step: dict) -> dict | None:
    """What the intermediate-release fetch needs: the wheel member and the name chain."""
    owner = step.get("owner") or spell.spelling
    owner_module = step.get("owner_module") or spell.module
    member = step.get("owner_file") or spell.module_file
    if not member or not owner.startswith(owner_module + "."):
        return None
    chain = owner[len(owner_module) + 1:].split(".")
    return {"member": member, "chain": chain}


def _unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for i in items:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def _package_for(fact: DeltaFact, root_to_package: Mapping[str, str]) -> str | None:
    for name in fact.spellings():
        pkg = root_to_package.get(name.split(".")[0])
        if pkg is not None:
            return pkg
    return None


def _dedupe(facts: list[DeltaFact]) -> list[DeltaFact]:
    seen: dict[tuple, DeltaFact] = {}
    for f in facts:
        key = (f.symbol, f.kind, f.param, f.old, f.new)
        if key in seen:
            prev = seen[key]
            prev.aliases = sorted(set(prev.aliases) | set(f.aliases))
            prev.evidence = _unique(prev.evidence + f.evidence)
            prev.deprecated_in_old = prev.deprecated_in_old or f.deprecated_in_old
            prev.detail.update({k: v for k, v in f.detail.items() if k not in prev.detail})
        else:
            seen[key] = f
    return sorted(seen.values(), key=lambda f: (KIND_ORDER[f.kind], f.symbol, f.param or ""))


# ── the run ───────────────────────────────────────────────────────────────────────────


class _Run:
    def __init__(self, root: Path, migrations: list[Migration], *, online: bool,
                 use_cache: bool) -> None:
        self.root = root
        self.migrations = migrations
        self.online = online
        self.use_cache = use_cache
        self.notes: list[str] = []
        self.stats: dict[str, Any] = {}

    def execute(self, symbols) -> list[DeltaReport]:
        t_start = time.perf_counter()
        wants_old = envs_mod.wants_for(self.migrations, "old")
        wants_new = envs_mod.wants_for(self.migrations, "new")
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            f_old = pool.submit(envs_mod.resolve_old, self.root, wants_old, self.online)
            f_new = pool.submit(envs_mod.resolve_new, self.root, wants_new, self.online)
            old_env, new_env = f_old.result(), f_new.result()
        self.stats["env_resolve_s"] = round(time.perf_counter() - t_start, 3)
        self.stats["env_build_s"] = round(
            (old_env.built_seconds or 0) + (new_env.built_seconds or 0), 2)

        trace = envs_mod._baseline_trace(self.root)
        if symbols is None:
            touched, notes = derive_symbols(self.root, trace)
            self.notes += notes
        else:
            touched = as_touched(symbols)
            for sym, warns in _trace_symbols(self.root, trace, self.notes).items():
                if sym in touched:
                    touched[sym].warnings.extend(warns)
        self.stats["symbols"] = len(touched)

        from pymolt import knowledge

        contexts = [
            c.code for c in knowledge._contexts()
            if any(canonicalize_name(c.package) == canonicalize_name(m.name)
                   for m in self.migrations)
        ]
        request = {
            "paths": sorted(touched),
            "dists": sorted({m.name for m in self.migrations}),
            "contexts": contexts,
            "per_path_timeout": PER_PATH_TIMEOUT,
        }

        def request_for(env: Interpreter) -> dict:
            if env.runner is not None and env.runner.emulated:
                return {**request, "per_path_timeout": PER_PATH_TIMEOUT * 4}
            return request

        t_probe = time.perf_counter()
        runs: dict[str, ProbeRun | None] = {"old": None, "new": None}
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = {
                side: pool.submit(run_probe, env.executable, self.root, request_for(env),
                                  use_cache=self.use_cache,
                                  **({"runner": env.runner} if env.runner is not None else {}))
                for side, env in (("old", old_env), ("new", new_env)) if env.available
            }
            for side, fut in futures.items():
                runs[side] = fut.result()
        self.stats["probe_s"] = round(time.perf_counter() - t_probe, 3)
        for side in ("old", "new"):
            run = runs[side]
            if run is not None:
                self.stats[f"probe_{side}_s"] = run.seconds
                self.stats[f"probe_{side}_cached"] = run.from_cache

        root_to_package: dict[str, str] = {}
        for run in runs.values():
            for name, info in ((run.meta.get("dists") if run else None) or {}).items():
                for r in info.get("roots") or []:
                    root_to_package.setdefault(r, self._migration(name).name)

        reports: dict[str, DeltaReport] = {}
        usable: dict[str, bool] = {}
        for m in self.migrations:
            report = DeltaReport(package=m.name, from_version=m.from_version,
                                 to_version=m.to_version, quality="complete")
            reports[m.name] = report
            usable[m.name] = self._side_ok(report, "old", old_env, runs["old"], m.from_version) \
                & self._side_ok(report, "new", new_env, runs["new"], m.to_version)
            if not usable[m.name]:
                report.quality = "inconclusive"

        if runs["old"] is not None and runs["old"].ok and runs["new"] is not None \
                and runs["new"].ok and any(usable.values()):
            new_exe = new_env.executable
            new_runner = new_env.runner

            def resolve_new(paths: list[str]) -> Mapping[str, dict]:
                t2 = time.perf_counter()
                run2 = run_probe(new_exe, self.root,  # type: ignore[arg-type]
                                 {"paths": paths, "dists": request["dists"], "any_root": True,
                                  "contexts": [], "per_path_timeout": PER_PATH_TIMEOUT},
                                 use_cache=self.use_cache,
                                 **({"runner": new_runner} if new_runner is not None else {}))
                self.stats["second_pass_s"] = round(time.perf_counter() - t2, 3)
                self.stats["second_pass_paths"] = len(paths)
                return run2.results if run2.ok else {}

            t_diff = time.perf_counter()
            by_pkg = diff(DiffInput(
                old=runs["old"].results, new=runs["new"].results, touched=touched,
                root_to_package=root_to_package, resolve_new=resolve_new,
                knowledge_hint=self._knowledge_hint,
                import_failed=frozenset(
                    path for path, res in runs["new"].results.items()
                    if res.get("import_errors")),
            ))
            self.stats["diff_s"] = round(time.perf_counter() - t_diff
                                         - self.stats.get("second_pass_s", 0), 3)
            for name, facts in by_pkg.items():
                if usable.get(name):
                    reports[name].facts = facts
            self._transient_notes(reports, runs, root_to_package, touched)
            self._online_hints(reports)
            for name, report in reports.items():
                if usable.get(name) and not report.facts:
                    report.notes.append(f"the touched symbols include nothing from {name}")

        self._record_import_errors(reports, runs, root_to_package)
        for report in reports.values():
            report.notes = _unique(report.notes + self.notes)
        self.stats["total_s"] = round(time.perf_counter() - t_start, 3)
        out = []
        for m in self.migrations:
            r = reports[m.name]
            r._stats = dict(self.stats)
            out.append(r)
        return out

    def _record_import_errors(self, reports, runs, root_to_package) -> None:
        """Modules that raised on import: reported as such, never as missing names."""
        for side in ("old", "new"):
            run = runs[side]
            if run is None:
                continue
            for res in run.results.values():
                for ie in res.get("import_errors") or []:
                    pkg = root_to_package.get(str(ie.get("module", "")).split(".")[0])
                    if pkg not in reports:
                        continue
                    entry = {**ie, "side": side}
                    if entry not in reports[pkg].import_errors:
                        reports[pkg].import_errors.append(entry)

    def _migration(self, dist: str) -> Migration:
        key = canonicalize_name(dist)
        for m in self.migrations:
            if canonicalize_name(m.name) == key:
                return m
        return Migration(dist, "", "")

    def _side_ok(self, report: DeltaReport, side: str, env: Interpreter,
                 run: ProbeRun | None, expected: str) -> bool:
        label = "old" if side == "old" else "new"
        if not env.available:
            report.notes.append(f"no {label} interpreter: {env.reason}")
            return False
        if run is None or not run.ok:
            report.notes.append(f"{label} probe failed ({env.executable}): "
                                f"{run.failure if run else 'not run'}")
            return False
        py = run.meta.get("python")
        if env.python and py and not envs_mod.same_minor(py, env.python):
            report.notes.append(f"the {label} interpreter ({env.executable}) reported Python "
                                f"{env.python} but the probe ran on {py}")
            return False
        if side == "old":
            report.old_python = py
        else:
            report.new_python = py
        info = (run.meta.get("dists") or {}).get(report.package) or {}
        version = info.get("version")
        if version is None:
            report.notes.append(f"{report.package} is not installed in the {label} interpreter "
                                f"({env.executable})")
            return False
        if not envs_mod.same_version(version, expected):
            report.notes.append(f"the {label} interpreter ({env.executable}) has "
                                f"{report.package} {version}, not {expected}")
            return False
        if env.note:
            report.notes.append(f"{label}: {env.note}")
        return True

    def _knowledge_hint(self, package: str, names: list[str]) -> tuple[str, str] | None:
        from pymolt import knowledge

        m = self._migration(package)
        found = knowledge.successor_hint(package, names, m.from_version, m.to_version)
        if found:
            return found[0], f"knowledge:{found[1].id}"
        return None

    def _transient_notes(self, reports, runs, root_to_package, touched) -> None:
        for side in ("old", "new"):
            run = runs[side]
            for path, res in run.results.items():
                if not res.get("transient"):
                    continue
                pkg = root_to_package.get(path.split(".")[0])
                if pkg in reports:
                    reports[pkg].notes.append(f"{side} probe could not resolve {path}: "
                                              f"{res.get('error')}")
                    if reports[pkg].quality == "complete":
                        reports[pkg].quality = "partial"

    def _online_hints(self, reports: dict[str, DeltaReport]) -> None:
        """Source 4: intermediate releases (online only; cached results are reused offline)."""
        cache_path = self.root / CACHE_DIR / "hints.json"
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8")) if self.use_cache else {}
        except (OSError, ValueError):
            cache = {}
        fetcher = None
        changed = False
        t0 = time.perf_counter()
        try:
            for report in reports.values():
                for fact in report.facts:
                    if fact.kind != "REMOVED" or fact.successor:
                        continue
                    query = fact.detail.get("online_query")
                    if not query:
                        continue
                    key = "|".join([canonicalize_name(report.package), report.from_version,
                                    report.to_version, fact.symbol])
                    result = cache.get(key)
                    source = "cached"
                    if result is None:
                        if not self.online:
                            continue
                        if fetcher is None:
                            fetcher = fetch_mod.Fetcher(self.root / CACHE_DIR,
                                                        use_cache=self.use_cache)
                        try:
                            result = fetch_mod.intermediate_hint(
                                fetcher, report.package, query["member"], query["chain"],
                                report.from_version, report.to_version)
                        except Exception as exc:  # noqa: BLE001 — a hint, never a crash
                            report.notes.append(f"intermediate fetch for {fact.symbol} failed: "
                                                f"{type(exc).__name__}: {exc}")
                            continue
                        cache[key] = result
                        changed = True
                        source = "fetched"
                    if result.get("hint"):
                        fact.successor = result["hint"]
                        fact.evidence.append(
                            f"successor:intermediate:{result.get('last_version_with_symbol')} "
                            f"({source})")
                        for msg in result.get("messages", [])[:1]:
                            fact.evidence.append(f"intermediate-prose: {_trim(msg)}")
                    fact.detail["intermediate"] = result
        finally:
            if fetcher is not None:
                self.stats["fetch_bytes"] = fetcher.bytes
                self.stats["fetch_requests"] = fetcher.requests
                fetcher.close()
            self.stats["fetch_s"] = round(time.perf_counter() - t0, 3)
        if changed:
            try:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(json.dumps(cache, indent=1, sort_keys=True),
                                      encoding="utf-8")
            except OSError as exc:
                log.debug("delta hint cache not written: %s", exc)


def compute_deltas(
    project_dir: str | Path,
    migrations: Iterable[Any],
    *,
    symbols: Iterable[str | TouchedSymbol] | None = None,
    online: bool = False,
    use_cache: bool = True,
) -> list[DeltaReport]:
    """API-change facts for each dependency migration, scoped to the touched symbols.

    ``migrations``: ``DependencyMigration``s (or ``(name, from, to)`` tuples / dicts).
    ``symbols``: the touched symbols (strings or :class:`TouchedSymbol` with recorded
    warnings); ``None`` derives them from the contact map plus the baseline trace.
    ``online``: allow the intermediate-release fetch (package names and versions only).
    Never raises for an environment problem: the affected reports are ``inconclusive``
    and their ``notes`` say why.
    """
    root = Path(project_dir).resolve()
    migs = as_migrations(migrations)
    if not migs:
        return []
    return _Run(root, migs, online=online, use_cache=use_cache).execute(symbols)


def run_stats(report: DeltaReport) -> dict[str, Any]:
    """Timing and cache counters of the run that produced ``report`` (bench/diagnostics)."""
    return dict(getattr(report, "_stats", {}) or {})


__all__ = ["SCHEMA", "DiffInput", "Migration", "ParamChange", "ProbeRun", "Spell",
           "as_migrations", "as_touched", "bases_change", "compute_deltas", "derive_symbols",
           "diff", "param_changes", "run_probe", "run_stats", "spellings"]
