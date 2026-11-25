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
from pymolt.delta import succession as succ_mod
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
from pymolt.delta.static_side import UNEVALUATED, static_probe
from pymolt.state_io import atomic_write_json

log = logging.getLogger(__name__)

CACHE_DIR = Path(".pymolt") / "cache" / "delta"
STATIC_VERSION = 3
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
    """Static contact-map targets plus the baseline trace's symbols (and its warnings), the
    spellings its call sites name that neither records (aliased and inherited attributes), and
    the spellings its calls reach through their receivers (``touched.receiver_spellings``)."""
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
    spelled, receivers = _site_spellings(root, trace, notes)
    for sym in spelled:
        touched.setdefault(sym, TouchedSymbol(path=sym, origin="contact"))
    for sym, cls in receivers.items():
        touched.setdefault(sym, TouchedSymbol(path=sym, origin="receiver", receiver=cls))
    return touched, notes


def _site_spellings(root: Path, trace: Path | None,
                    notes: list[str]) -> tuple[set[str], dict[str, str]]:
    """Spellings the runtime's code names and the contact map cannot see: aliased attributes
    (``G.node`` recorded as ``Graph.nodes``) and attributes inherited through project classes
    (``User.__fields__`` on a ``BaseModel`` subclass); and the receiver spellings of the
    baseline's calls (``{spelling: receiver class}``). Dependency spellings only."""
    from pymolt.delta import touched as touched_mod

    out: set[str] = set()
    receivers: dict[str, str] = {}
    try:
        out |= touched_mod.inherited_spellings(root)
        if trace is not None and (trace.exists()
                                  or Path(str(trace) + ".facts.json").exists()):
            from pymolt.verify.facts import load_facts

            facts = load_facts(trace, root=root).facts
            out |= touched_mod.alias_spellings(facts, root)
            receivers = touched_mod.receiver_spellings(facts, root)
    except Exception as exc:  # noqa: BLE001 — an enrichment, never a failure
        notes.append(f"site spellings unavailable: {type(exc).__name__}: {exc}")
    return ({s for s in out if not touched_mod.is_stdlib(s)},
            {s: c for s, c in receivers.items() if not touched_mod.is_stdlib(s)})


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


MAX_RELOCATION_TYPES = 300
_TYPE_MARKERS = ("__object__", "__type__", "__opaque__")


def baseline_type_names(root: Path, trace: Path | None) -> list[str]:
    """Dotted names of the types the baseline observed: raised exception types and the
    ``__object__`` / ``__type__`` / ``__opaque__`` markers of outcomes (builtins and bare names
    left out). Never raises."""
    if trace is None or not (trace.exists() or Path(str(trace) + ".facts.json").exists()):
        return []
    try:
        from pymolt.verify.facts import load_facts

        facts = load_facts(trace, root=root)
    except Exception:  # noqa: BLE001 — a missing hint, not a failure
        return []
    names: set[str] = set()

    def add(value: Any) -> None:
        if isinstance(value, str) and "." in value and value.replace(".", "").replace(
                "_", "").isalnum():
            names.add(value)

    def walk(value: Any, depth: int = 0) -> None:
        if depth > 6:
            return
        if isinstance(value, dict):
            for k, v in value.items():
                if k in _TYPE_MARKERS:
                    add(v)
                else:
                    walk(v, depth + 1)
        elif isinstance(value, list):
            for v in value[:50]:
                walk(v, depth + 1)

    for f in facts.facts:
        for o in f.outcomes:
            if o.kind == "raise":
                add(o.value)
            else:
                walk(o.value)
    return sorted(names)


def relocation_candidates(name: str) -> list[str]:
    """The public spellings that may still name ``name``: its own dotted path, then each
    enclosing package's export of the same leaf, nearest first, the top-level package last
    (``sqlalchemy.engine.ResultProxy``, ``pydantic.ValidationError``)."""
    from pymolt.delta.touched import public_candidates

    return public_candidates(name)


def _defined_as(result: Mapping[str, Any] | None) -> str | None:
    """``module.qualname`` of the class a probe result resolved to (None if not a class)."""
    steps = (result or {}).get("steps") or []
    if not (result or {}).get("exists") or not steps:
        return None
    last = steps[-1]
    return last.get("defined_as") if last.get("kind") == "class" else None


def compute_type_relocations(old: Mapping[str, dict], new: Mapping[str, dict],
                             names: Iterable[str]) -> list[dict[str, str]]:
    """Same-object evidence only. For each baseline type ``T``: a public spelling that, in the
    old interpreter, resolved to ``T`` and, in the new one, resolves to a different class ``N``.
    ``via`` is that spelling. A name that still resolves to itself is not a relocation."""
    out: list[dict[str, str]] = []
    for name in sorted(set(names)):
        for cand in relocation_candidates(name):
            if _defined_as(old.get(cand)) != name:
                continue
            moved = _defined_as(new.get(cand))
            if moved and moved != name:
                out.append({"old": name, "new": moved, "via": cand})
                break
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
                atomic_write_json(cache, {
                    "stamps": stamps, "meta": run.meta, "results": run.results,
                }, indent=None)
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


_REMOVAL_WORDS = re.compile(r"\b(?:removed|no longer|not supported|unsupported|renamed|"
                            r"deprecated|ignored|invalid)\b", re.IGNORECASE)


def param_changes(old: list | None, new: list | None, new_prose: Iterable[str] = (),
                  kwargs_rejected: bool = False) -> list[ParamChange]:
    """Parameter-level differences between two ``[name, kind, default-repr]`` shapes.

    A removed parameter is not reported when the new callable takes ``**kwargs`` (the call
    still binds) unless the new callable's own prose (``new_prose``: warn()/raise messages,
    docstring) names it as removed or renamed: it then does not bind to anything. A removed
    positional parameter replaced at the same index by a new one of the same kind is a rename.
    A parameter that loses its default is a SIGNATURE change (calls that omitted it now fail).
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
            elif kwargs_rejected:
                out.append(ParamChange(
                    "SIGNATURE", name, kind, None,
                    f"param {name} removed (the new signature's **kwargs only refuses what it "
                    f"collects)"))
            elif _stated_removed(name, new_prose):
                out.append(ParamChange(
                    "SIGNATURE", name, kind, None,
                    f"param {name} removed (the new signature takes **kwargs, but its own "
                    f"prose says {name} is removed)"))
            continue
        _, nkind, ndefault = n[name]
        if nkind != kind:
            out.append(ParamChange("SIGNATURE", name, kind, nkind,
                                   f"param {name} kind {kind}->{nkind}"))
        if default not in (None, UNEVALUATED) and ndefault is None and kind not in _VAR:
            out.append(ParamChange("SIGNATURE", name, default, "(required)",
                                   f"parameter {name} lost its default; now required"))
        elif default != UNEVALUATED and _default(default) != _default(ndefault):
            out.append(ParamChange("DEFAULT", name, default, ndefault,
                                   f"param {name} default {default}->{ndefault}"))
    added = set(renamed.values())
    for name, (_, kind, default) in n.items():
        if name not in o and name not in added and default is None and kind not in _VAR:
            out.append(ParamChange("SIGNATURE", name, None, f"{kind} (required)",
                                   f"param {name} added as required"))
    return out


def _stated_removed(name: str, prose: Iterable[str]) -> bool:
    """Whether a message names ``name`` as removed/renamed/ignored (word-bounded)."""
    word = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])")
    return any(word.search(m) and _REMOVAL_WORDS.search(m) for m in prose or ())


def bases_change(old: dict, new: dict) -> ParamChange | None:
    if old.get("kind") != "class" or new.get("kind") != "class":
        return None
    if old.get("bases") is None or new.get("bases") is None:  # a static side that cannot name them
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
    # paths -> results in the OLD interpreter, one run (object ids compare within it): the
    # identity check of a named successor (``np.NaN is np.nan``)
    resolve_old: Callable[[list[str]], Mapping[str, dict]] | None = None


def _class_step(result: Mapping[str, Any] | None) -> dict | None:
    """The step a receiver spelling (``<class>.<attr>``) reaches its class at: the one before
    the last. None when the path did not resolve that far."""
    steps = (result or {}).get("steps") or []
    if len(steps) < 2 or not steps[-2].get("exists"):
        return None
    return steps[-2]


def _receiver_paths_off(inp: DiffInput) -> set[str]:
    """Receiver spellings that do not reach the recorded receiver class in the old version
    (a candidate public spelling that names another class, or none): no spelling the project
    uses, so they are compared on neither side."""
    off: set[str] = set()
    for path, t in inp.touched.items():
        if t.origin != "receiver" or not t.receiver:
            continue
        step = _class_step(inp.old.get(path))
        if step is None or step.get("kind") != "class" or step.get("defined_as") != t.receiver:
            off.add(path)
    return off


def diff(inp: DiffInput) -> dict[str, list[DeltaFact]]:
    """Classify every touched object; facts grouped by migration package name."""
    def expand(path: str) -> bool:
        t = inp.touched.get(path)
        return t is None or t.origin not in ("trace", "receiver")

    off = _receiver_paths_off(inp)
    old_results = {p: r for p, r in inp.old.items() if p not in off}
    new_results = {p: r for p, r in inp.new.items() if p not in off}
    sp_old = spellings(old_results, expand)
    sp_new = spellings(new_results, expand)

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
    hints: dict[str, list[succ_mod.Candidate]] = {}
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
            errors = [sp_new[m].step.get("error") or "" for m in vanished
                      if m in sp_new and not sp_new[m].exists]
            cands = [*hints[canon], *_error_candidates(errors, canon)]
            for c in cands:
                need.update(_successor_candidates(c.name, canon, members))
        for m in members:  # what a forwarding body calls, to spell it in the new version
            for side in (sp_old, sp_new):
                step = side[m].step if m in side and side[m].exists else {}
                callee = step.get("callee") or {}
                need.update(callee.get("spellings") or ())
                if callee.get("member") and "." in m:
                    need.add(m.rsplit(".", 1)[0] + "." + callee["member"])
        for m in members:  # still there, deprecated: the new version may name its successor
            if m in sp_new and sp_new[m].exists:
                for c in _new_candidates(sp_new[m], _leaf(canon)):
                    need.update(_successor_candidates(c.name, canon, [m]))
                home = sp_new[m].step.get("same_object_home")
                if home and home != m:          # the object's public home (identity)
                    need.add(home)
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

    def vanished_at(name: str) -> str | None:
        """The first prefix of ``name`` that no longer resolves in the new version (None:
        the new probe recorded no step of it, so it cannot say)."""
        parts = name.split(".")
        for i in range(2, len(parts) + 1):
            prefix = ".".join(parts[:i])
            s = sp_new.get(prefix) or extra.get(prefix)
            if s is not None and not s.exists:
                return prefix
        return None

    facts: dict[str, list[DeltaFact]] = {}

    def add(fact: DeltaFact) -> None:
        pkg = _package_for(fact, inp.root_to_package)
        if pkg is not None:
            facts.setdefault(pkg, []).append(fact)

    for canon, members in sorted(groups.items()):
        for fact in _group_facts(canon, sorted(members), sp_old, new_of, new_error, baseline,
                                 hints.get(canon, []), vanished_at):
            add(fact)
    for pkg in facts:
        facts[pkg] = _dedupe(facts[pkg])
    every = [f for fs in facts.values() for f in fs]
    _chains(every, inp.resolve_new, new_of)
    _owner_spellings(every, inp)
    if inp.resolve_old is not None:
        _identity_proofs(every, inp.resolve_old, new_of)
    return facts


def _owner_spellings(facts: list[DeltaFact], inp: DiffInput) -> None:
    """For a proven member: every probed spelling of the class that owns it in the old version
    (``pydantic.BaseModel`` beside ``pydantic.main.BaseModel``), ``succession.old.owners``,
    so a receiver recorded under any of them is one the proof covers."""
    classes: dict[str, set[str]] = {}
    for path, result in inp.old.items():
        step = _class_step(result)
        if step is not None and step.get("kind") == "class" and step.get("defined_as"):
            classes.setdefault(step["defined_as"], set()).add(path.rsplit(".", 1)[0])
    for f in facts:
        rec = f.succession or {}
        if not (rec.get("template") or {}).get("member") or not rec.get("old"):
            continue
        owners: set[str] = set()
        for spelling in [*(rec["old"].get("spellings") or ()), *f.spellings()]:
            step = _class_step(inp.old.get(spelling))
            if step is not None and step.get("defined_as") in classes:
                owners |= classes[step["defined_as"]]
                owners.add(step["defined_as"])
        if owners:
            rec = dict(rec)
            rec["old"] = {**rec["old"], "owners": sorted(owners)}
            f.succession = rec


def _identity_proofs(facts: list[DeltaFact],
                     resolve_old: Callable[[list[str]], Mapping[str, dict]],
                     new_of: Callable[[str], Spell | None]) -> None:
    """A removed name whose named successor was the very same object in the old version
    (``np.NaN is np.nan`` on numpy 1.26): identity, stamped as such. Both are resolved in one
    old-side run, where object ids compare. A small int or string is the same object everywhere
    without being a rename: identity counts for those only beside the text that named it."""
    pairs = [(f, f.symbol, f.successor) for f in facts
             if f.kind == "REMOVED" and f.successor and not f.param
             and (f.succession or {}).get("trust") not in ("identity", "proof")]
    if not pairs:
        return
    try:
        found = resolve_old(sorted({p for _, a, b in pairs for p in (a, b)}))
    except Exception:  # noqa: BLE001 - a check, never a crash
        return

    def oid(path: str) -> int | None:
        steps = (found.get(path) or {}).get("steps") or []
        return steps[-1].get("oid") if steps and steps[-1].get("exists") else None

    for fact, old, new in pairs:
        a, b = oid(old), oid(new)
        if a is None or a != b or new_of(new) is None:
            continue
        rec = dict(fact.succession or {"scope": "object"})
        rec.update(trust="identity", transfer="at-target",
                   proof={"kind": "identity", "locus": f"{old} is {new}", "version": "old",
                          "side": "old"})
        rec["sources"] = list(dict.fromkeys(["identity", *rec.get("sources", [])]))
        fact.succession = rec


def _successor_candidates(succ: str, canon: str, members: Iterable[str] = ()) -> list[str]:
    """Spellings a (possibly bare) successor name may stand for: as written, beside each member
    (``BaseModel.dict`` -> ``BaseModel.model_dump``), then under the package root."""
    root = canon.split(".")[0]
    succ = succ.replace("-", "_") if "-" in succ.split(".")[0] else succ
    out = [succ]
    if "." not in succ:
        for m in members:
            if "." in m:
                out.append(m.rsplit(".", 1)[0] + "." + succ)
    if not succ.startswith(root + "."):
        out.append(f"{root}.{succ}")
        head, _, rest = succ.partition(".")
        if rest and head != root and len(head) <= 4:   # an import alias: ``np.nan``, ``st.rerun``
            out.append(f"{root}.{rest}")
    return list(dict.fromkeys(out))


def _own_prose(step: dict) -> list[str]:
    """Messages of a callable's own source: warn() and raise texts, then its docstring lines
    that say something was removed/renamed."""
    msgs = [w.get("message", "") for w in step.get("warns") or []]
    if step.get("kind") != "class":  # a class's source holds every method's raises
        msgs += [w.get("message", "") for w in step.get("raises") or []]
    doc = step.get("doc") or {}
    msgs += list(doc.get("directive") or []) + list(doc.get("mentions") or [])
    return msgs


def _new_prose(spell: Spell, leaf: str) -> list[str]:
    """Deprecation prose of a spelling that still exists in the new version (``__deprecated__``,
    docstring, access warning, PEP 562 branch, the ``warn()`` in its own body): where a rename
    successor is named. Empty unless the new side marks the object deprecated."""
    step = spell.step
    runtime = spell.runtime_warnings if spell.final_for else []
    out = [e.split(": ", 1)[-1] for e in _side_deprecation(step, leaf, runtime, "")]
    if not out:
        return []
    doc = step.get("doc") or {}
    out += [m for m in list(doc.get("directive") or []) + list(doc.get("mentions") or [])
            if _DEPRECATES.search(m)]
    if step.get("kind") != "class":
        out += [w["message"] for w in step.get("warns") or []
                if _DEPRECATES.search(w.get("message", ""))
                and _mentions(w.get("message", ""), leaf)]
    return out


def _names_of(canon: str) -> list[str]:
    """The names a text about ``canon`` uses: its leaf, and a constructor's class."""
    parts = canon.split(".")
    if len(parts) > 1 and parts[-1] in ("__init__", "__new__", "__call__"):
        return [parts[-2]]
    return [parts[-1]]


def _shape_params(step: Mapping[str, Any] | None) -> list[str]:
    return [p[0] for p in (step or {}).get("shape") or () if p and p[1] not in _VAR]


def _attributed_warnings(members: Iterable[str], sp_old: Mapping[str, Spell],
                         baseline: Mapping[str, list[RecordedWarning]], canon: str
                         ) -> tuple[list[RecordedWarning], list[RecordedWarning]]:
    """``(attributed, pitfalls)``: the baseline warnings recorded at the symbol's sites, split by
    the attribution rule (``succession.attributed``)."""
    names = _names_of(canon)
    params: list[str] = []
    templates: list[str] = []
    for m in members:
        step = sp_old[m].step if m in sp_old else {}
        params += _shape_params(step)
        templates += succ_mod.templates_of(step)
    mine, other = [], []
    for m in members:
        for w in baseline.get(m, []):
            (mine if succ_mod.attributed(w.message, names, params, templates) else other).append(w)
    return mine, other


def _helper_texts(step: Mapping[str, Any], names: Iterable[str]) -> list[str]:
    """The deprecation texts a step's helpers give about the object itself: the structured hints
    of its registered helpers, and the messages of the warning helpers it calls that name it."""
    out = []
    for hf in succ_mod.helper_facts(step):
        if hf.kind in ("rename-object", "prescription", "prose"):
            text = succ_mod.prescription_text(hf)
            if text:
                out.append(text)
    for h in step.get("helper_warns") or ():
        out += [m for m in h.get("messages") or () if any(_mentions(m, n) for n in names)]
    return out


def _local_hint(canon: str, members: list[str], sp_old: Mapping[str, Spell],
                baseline: Mapping[str, list[RecordedWarning]],
                inp: DiffInput) -> list[succ_mod.Candidate]:
    """Successor candidates from the old side: the baseline warnings attributed to the symbol,
    its old deprecation prose and its registered helpers' data, then curated knowledge."""
    names = _names_of(canon)
    mine, _ = _attributed_warnings(members, sp_old, baseline, canon)
    cands = succ_mod.candidates_from([w.message for w in mine], source="warning")
    for m in members:
        step = sp_old[m].step
        cands += succ_mod.candidates_from(_hint_bucket2(step, _leaf(canon)), source="docstring")
        for hf in succ_mod.helper_facts(step):
            if hf.kind in ("rename-object", "prescription"):
                cands += succ_mod.candidates_from([succ_mod.prescription_text(hf) or ""],
                                                  source="helper")
        cands += succ_mod.candidates_from(
            [t for t in _helper_texts(step, names)
             if not any(t == succ_mod.prescription_text(h) for h in succ_mod.helper_facts(step)
                        if h.kind != "prose")], source="prose")
    if not cands and inp.knowledge_hint is not None:
        pkg = None
        for name in [canon, *members]:
            pkg = inp.root_to_package.get(name.split(".")[0])
            if pkg:
                break
        if pkg:
            found = inp.knowledge_hint(pkg, [canon, *members])
            if found:
                cands.append(succ_mod.Candidate(name=found[0], source=found[1], quoted=True))
    return cands


def _error_candidates(errors: Iterable[str], canon: str) -> list[succ_mod.Candidate]:
    """Candidates the new version's error names, when the name is gone and the error is about it
    (a module ``__getattr__``'s "`np.NaN` was removed ... Use `np.nan` instead")."""
    names = _names_of(canon)
    texts = [e.split(": ", 1)[-1] for e in errors if e and any(_mentions(e, n) for n in names)]
    return succ_mod.candidates_from(texts, source="error")


def _new_candidates(spell: Spell, leaf: str) -> list[succ_mod.Candidate]:
    """Candidates the new version names for a spelling that still exists there: its deprecation
    prose (``_new_prose``), and its helpers' texts about it."""
    cands = succ_mod.candidates_from(_new_prose(spell, leaf), source="new-prose")
    for hf in succ_mod.helper_facts(spell.step):
        if hf.kind in ("rename-object", "prescription"):
            cands += succ_mod.candidates_from([succ_mod.prescription_text(hf) or ""],
                                              source="helper")
    cands += succ_mod.candidates_from(_body_deprecations(spell.step, [leaf]), source="new-prose")
    return cands


def _body_deprecations(step: Mapping[str, Any], names: Iterable[str],
                       always: bool | None = None) -> list[str]:
    """Deprecation texts in a callable's own body, or in the warning helpers it calls, that name
    it: a body that warns on every call (``always``), or only on some paths (``pl.count()``
    without columns: the object is not marked deprecated, yet its successor is named there).
    ``always`` None takes both."""
    if step.get("kind") == "class":
        return []
    names = list(names)
    texts = [w.get("message", "") for w in step.get("warns") or ()
             if always is None or bool(w.get("always")) == always]
    if always is not True:
        for h in step.get("helper_warns") or ():
            texts += list(h.get("messages") or ())
    return [t for t in texts if t and _DEPRECATES.search(t)
            and any(_mentions(t, n) for n in names)]


_PARAM_SUBJECT = (r"(?:[`'\"]{P}[`'\"=]|(?<![\w.]){P}\s*=|(?<![\w.]){P}\s+(?:parameter|argument|"
                  r"keyword|kwarg)|(?:parameter|argument|keyword|kwarg)s?\s+[`'\"]?{P}(?![\w])"
                  r"|^\W*{P}\s+(?:is|was|has been|are)\s+(?:now\s+)?deprecated)")


def _names_param(text: str, param: str) -> bool:
    """The text is about the parameter: it quotes it (`` `shade` ``, ``'ci'``), writes it as a
    keyword (``verify_ssl=``) or calls it a parameter or an argument. A name inside a call the
    text shows (``headers.get(name, default)``) is not."""
    rx = _PARAM_SUBJECT.replace("{P}", re.escape(param))
    return re.search(rx, text or "", re.IGNORECASE) is not None


_COMPAT_PARTS = {"v1", "compat", "_compat", "legacy", "deprecated", "_deprecated"}


def _choose(cands: list[succ_mod.Candidate], canon: str, members: list[str],
            new_of: Callable[[str], Spell | None], *, texts: Iterable[str] = (),
            identity: str | None = None) -> tuple[str | None, succ_mod.Succession]:
    """The successor ``cands`` name, if they name exactly one object that exists and is public in
    the new version, and the succession record either way (``succession.Succession``).

    Filters: it exists at the target and is not the old symbol itself; a private name (a
    ``_segment``) or a compatibility namespace (``pydantic.v1``) is never chosen while another
    candidate is; a bare generic word counts only as a member of the old symbol's own class.
    Several distinct objects, or a text that offers a choice, name no successor: the candidates
    are listed, ordered by evidence, for an agent or a person to choose."""
    rec = succ_mod.Succession(scope="member" if _is_member(canon) else "object")
    rec.caveats = succ_mod.caveats(texts)
    rec.equivalent = succ_mod.asserts_equivalence(texts)
    resolved: list[tuple[succ_mod.Candidate, str, str]] = []
    if identity is not None:
        target = new_of(identity)
        if target is not None:
            resolved.append((succ_mod.Candidate(name=identity, source="identity", quoted=True),
                             identity, target.canonical))
    rejected: dict[str, str] = {}
    member_parents = {m.rsplit(".", 1)[0] for m in members if "." in m}
    for c in cands:
        if c.role == "param":
            continue
        options = _successor_candidates(c.name, canon, members)
        spells = [sp for sp in options if new_of(sp) is not None]
        if not spells:
            spells = [sp for sp in options if _instance_attribute(sp, new_of)]
            if spells:
                sp = min(spells, key=_public_first)
                resolved.append((c, sp, sp))
                continue
            rejected.setdefault(c.name, "does not exist in the new version")
            continue
        if _is_member(canon):           # a member's successor is a member of its class first
            beside = [x for x in spells if x.rsplit(".", 1)[0] in member_parents]
            spells = beside or spells
        sp = min(spells, key=_public_first)
        target = new_of(sp)
        if sp in members or (target is not None and target.canonical == canon
                             and sp.rsplit(".", 1)[-1] == canon.rsplit(".", 1)[-1]):
            rejected.setdefault(c.name, "is the old symbol itself")
            continue
        if c.generic and not any(s.rsplit(".", 1)[0] in member_parents for s in spells):
            rejected.setdefault(c.name, "a generic word, neither quoted, qualified nor a member "
                                        "of the old symbol's class")
            continue
        resolved.append((c, sp, target.canonical if target is not None else sp))
    public = [r for r in resolved if not _private(r[1])]
    for c, sp, _ in resolved:
        if _private(sp) and public:
            rejected.setdefault(c.name, "private (a `_` segment)")
    resolved = public or resolved
    forward = [r for r in resolved if not (_COMPAT_PARTS & set(r[1].split(".")))]
    for c, sp, _ in resolved:
        if forward and (_COMPAT_PARTS & set(sp.split("."))):
            rejected.setdefault(c.name, "a compatibility namespace, while the library names a "
                                        "forward successor")
    resolved = forward or resolved
    rec.rejected = [{"name": n, "why": w} for n, w in rejected.items()
                    if not any(r[0].name == n for r in resolved)]
    by_object: dict[str, list[tuple[succ_mod.Candidate, str]]] = {}
    for c, sp, key in resolved:
        by_object.setdefault(key, []).append((c, sp))
    choice = any(c.role == "alternative" for c, _, _ in resolved)
    if len(by_object) == 1 and not choice:
        items = next(iter(by_object.values()))
        sp = min((s for _, s in items), key=_public_first)
        sources = list(dict.fromkeys(c.source for c, _ in items))
        rec.sources = sources
        rec.trust = min((succ_mod.trust_of(src) for src in sources),
                        key=succ_mod.TRUST_ORDER.index)
        return sp, rec
    if by_object:
        order = {src: i for i, src in enumerate(("identity", "helper", "knowledge", "warning",
                                                 "docstring", "new-prose", "prose", "error"))}
        ranked = sorted(((c, s) for items in by_object.values() for c, s in items[:1]),
                        key=lambda cs: (order.get(cs[0].source, 9), _public_first(cs[1])))
        rec.candidates = [{"name": s, "source": c.source} for c, s in ranked]
        rec.trust = None
    return None, rec


def _instance_attribute(spelling: str, new_of: Callable[[str], Spell | None]) -> bool:
    """``Class.attr`` (or ``Class.attr.more``) where ``attr`` is no class attribute but one the
    new version's ``__init__`` assigns on ``self`` (``self.status = status``)."""
    parts = spelling.split(".")
    for i in range(len(parts) - 1, 1, -1):
        owner = new_of(".".join(parts[:i]))
        if owner is not None:
            return owner.step.get("kind") == "class" and \
                parts[i] in (owner.step.get("instance_attrs") or ())
    return False


def _is_member(canon: str) -> bool:
    parts = canon.split(".")
    return len(parts) > 2 and parts[-2][:1].isupper()


def _private(spelling: str) -> bool:
    return any(part.startswith("_") and not part.startswith("__") for part in spelling.split("."))


def _group_facts(
    canon: str, members: list[str], sp_old: Mapping[str, Spell],
    new_of: Callable[[str], Spell | None], new_error: Callable[[str], str | None],
    baseline: Mapping[str, list[RecordedWarning]], hint: list[succ_mod.Candidate],
    vanished_at: Callable[[str], str | None] = lambda name: None,
) -> list[DeltaFact]:
    leaf = _leaf(canon)
    names = _names_of(canon)
    old_steps = {m: sp_old[m] for m in members}
    surviving = [m for m in members if new_of(m) is not None]
    vanished = [m for m in members if new_of(m) is None]
    c_new = new_of(canon)
    any_old = old_steps[members[0]].step

    # old-side deprecation evidence (object-level, unconditional) and the baseline warnings
    # attributed to the symbol (the others are pitfalls of the sites, never its evidence)
    ev = _Evidence()
    for m in members:
        s = old_steps[m]
        runtime = s.runtime_warnings if s.final_for else []
        ev.object_level += _side_deprecation(s.step, leaf, runtime, "")
        if s.step.get("kind") != "class":  # a class body's warn() calls are about anything
            ev.static += [w.get("message", "") for w in s.step.get("warns") or []]
            for h in s.step.get("helper_warns") or ():
                ev.static += list(h.get("messages") or ())
        ev.doc_mentions += list((s.step.get("doc") or {}).get("mentions") or [])
    ev.baseline, _pitfalls = _attributed_warnings(members, sp_old, baseline, canon)
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
                 and _leaf(new_of(t).canonical) == leaf  # type: ignore[union-attr]
                 and _variant_ok(_leaf(m), _leaf(t))]
        if (m != canon and c_new is not None and _compatible(any_old, c_new.step, leaf)
                and _variant_ok(_leaf(m), _leaf(canon))):
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

    def where_vanished(spells: Iterable[str]) -> dict[str, str] | None:
        found = {sp: at for sp in spells if (at := vanished_at(sp)) is not None}
        return found or None

    for succ, spells in sorted(moved.items()):
        symbol = canon if canon in spells else spells[0]
        target = new_of(succ)
        fact = DeltaFact(
            symbol=symbol, aliases=[s for s in spells if s != symbol], kind="MOVED",
            old=canon, new=moved_new_canon[succ], successor=succ,
            deprecated_in_old=old_deprecated,
            evidence=["existence", f"same object reachable as {succ}", *ev.object_level,
                      *baseline_ev],
            vanished_at=where_vanished(spells),
        )
        rec = succ_mod.Succession(scope="member" if _is_member(canon) else "object",
                                  trust="identity", sources=["identity"]).as_dict() or {}
        rec["transfer"] = "at-target"   # the same object, reachable there under the successor
        fact.succession = rec
        fact.detail.update(
            importable=[s for s in spells if old_steps[s].index == 0],
            successor_importable=bool(target is not None and target.index == 0),
            successor_same_signature=True,
        )
        out.append(fact)

    if removed:
        symbol = canon if canon in removed else removed[0]
        errors = [e for e in (new_error(m) for m in removed) if e]
        error = errors[0] if errors else None
        evidence = ["existence"]
        if error:
            evidence.append(f"absent in new: {error}")
        evidence += [*ev.object_level, *baseline_ev]
        cands = [*hint, *_error_candidates(errors, canon)]
        succ, rec = _choose(cands, canon, members, new_of,
                            texts=[c.text for c in cands])
        if succ:
            evidence.append(f"successor:{rec.sources[0]}")
        fact = DeltaFact(
            symbol=symbol, aliases=[s for s in removed if s != symbol], kind="REMOVED",
            old=canon, successor=succ, deprecated_in_old=old_deprecated, evidence=evidence,
            vanished_at=where_vanished(removed),
        )
        fact.succession = rec.as_dict()
        _prove_object(fact, canon, members, any_old, None, new_of)
        succ = fact.successor
        target = new_of(succ) if succ else None
        old_owner = old_steps[symbol].step
        fact.detail.update(
            importable=[s for s in removed if old_steps[s].index == 0],
            successor_importable=bool(target is not None and target.index == 0),
            successor_same_signature=bool(
                target is not None and any_old.get("shape") is not None
                and target.step.get("shape") == any_old.get("shape")
            ),
            online_query=_online_query(old_steps[symbol], old_owner),
            hint_source=rec.sources[0] if succ and rec.sources else None,
            old_shape=any_old.get("shape"),
        )
        out.append(fact)

    # ── surviving spellings: object-level comparison ─────────────────────────────────────
    changes: dict[tuple, set[str]] = {}
    notes: dict[tuple, list[str]] = {}
    new_dep: dict[str, set[str]] = {}
    param_names: dict[tuple, tuple[str | None, succ_mod.Succession]] = {}
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
        for ch, extra in _param_level(o, n, ev, m in sp_old and old_steps[m]):
            key = (symbol, ch.kind, ch.param, ch.old, ch.new)
            changes.setdefault(key, set()).add(m)
            notes.setdefault(key, ["signature", *([rebound] if rebound else []), *extra])
            if ch.param and (symbol, ch.param) not in param_names:
                param_names[(symbol, ch.param)] = _param_successor(ch, o, n, ev)
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
        else:
            always = _body_deprecations(n, names, always=True)
            # a warning on some calls only is about the object only when it names a successor
            # that exists (polars ``count()``: "Please use `pl.len()`"); "passing a BlockManager
            # is deprecated" is about one way of calling it
            some = [t for t in _body_deprecations(n, names, always=False) if t not in always
                    and any(new_of(sp) is not None
                            for c in succ_mod.candidates_from([t]) if c.role != "param"
                            for sp in _successor_candidates(c.name, canon, [m]))]
            if always or some:  # its body warns: on every call, or on some paths only
                changes.setdefault((symbol, "DEPRECATED", None, None, None), set()).add(m)
                new_dep.setdefault(symbol, set()).update(
                    [f"deprecated-in-new:warn: {_trim(t)}" for t in always[:2]]
                    + [f"deprecated-in-new:in its body (some calls): {_trim(t)}"
                       for t in some[:2]])

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
            if any(e.startswith("helper:") and "old version" in e for e in evidence):
                deprecated = True
            if kind == "DEPRECATED" and not any(e.startswith("deprecated-in-new:")
                                                for e in evidence):
                deprecated = True        # only the old version's texts say so
        if kind == "DEPRECATED" and param is None:
            evidence = sorted(new_dep.get(symbol, set()))
        if param is None:
            evidence += [*ev.object_level, *baseline_ev]
        fact = DeltaFact(
            symbol=symbol, aliases=sorted(s for s in spells if s != symbol), kind=kind,
            param=param, old=old, new=new, deprecated_in_old=deprecated,
            evidence=_unique(evidence),
        )
        if param is not None and (symbol, param) in param_names:
            succ, rec = param_names[(symbol, param)]
            fact.successor = succ
            fact.succession = rec.as_dict()
            if succ:
                fact.evidence.append(f"successor:{rec.sources[0] if rec.sources else 'named'}")
                first = sorted(spells)[0]
                n_spell = new_of(first)
                if n_spell is not None:
                    _prove_param(fact, old_steps[first].step, n_spell.step,
                                 [f for f in param_facts if f.symbol == symbol])
        if kind == "DEPRECATED" and param is None:
            pool = [e.split(": ", 1)[-1] for e in evidence]
            cands: list[succ_mod.Candidate] = succ_mod.candidates_from(pool, source="new-prose")
            identity = None
            for sp in sorted(spells):
                target_new = new_of(sp)
                if target_new is not None:
                    pool += _new_prose(target_new, leaf)
                    cands += _new_candidates(target_new, leaf)
                    home = target_new.step.get("same_object_home")
                    if identity is None and home and home not in spells and \
                            _about_access(evidence) and not _private(home):
                        identity = home
            succ, rec = _choose(cands, canon, sorted(spells), new_of, texts=pool,
                                identity=identity)
            fact.successor = succ
            fact.succession = rec.as_dict()
            first = new_of(sorted(spells)[0])
            _prove_object(fact, canon, sorted(spells), any_old,
                          first.step if first is not None else None, new_of)
            succ = fact.successor
            target = new_of(succ) if succ else None
            fact.detail.update(
                importable=[x for x in sorted(spells) if old_steps[x].index == 0],
                successor_importable=bool(target is not None and target.index == 0),
                successor_same_signature=bool(
                    target is not None and any_old.get("shape") is not None
                    and target.step.get("shape") == any_old.get("shape")),
                successor_exists=target is not None,
            )
        param_facts.append(fact)
    out += param_facts

    # baseline warnings no fact carries yet: the object warned in the old version
    carried = {e for f in out for e in f.evidence if e.startswith("warning:")}
    loose = [e for e in baseline_ev if e not in carried]
    if surviving and (loose or (ev.object_level and not out)):
        symbol = canon if canon in surviving or canon not in members else surviving[0]
        texts = [w.message for w in ev.baseline] + [e.split(": ", 1)[-1]
                                                     for e in ev.object_level]
        cands = [*succ_mod.candidates_from([w.message for w in ev.baseline], source="warning"),
                 *succ_mod.candidates_from([e.split(": ", 1)[-1] for e in ev.object_level],
                                           source="docstring"),
                 *[c for c in hint if c.source in ("helper",)]]
        succ, rec = _choose(cands, canon, members, new_of, texts=texts)
        fact = DeltaFact(
            symbol=symbol, aliases=[s for s in surviving if s != symbol], kind="DEPRECATED",
            deprecated_in_old=True, evidence=_unique([*ev.object_level, *loose]),
            successor=succ,
        )
        fact.succession = rec.as_dict()
        here = new_of(symbol)
        _prove_object(fact, canon, members, any_old, here.step if here is not None else None,
                      new_of)
        out.append(fact)

    if not out and surviving:
        symbol = canon
        out.append(DeltaFact(symbol=symbol, aliases=[s for s in members if s != symbol],
                             kind="UNCHANGED", evidence=["existence", "signature"]))
    elif surviving:
        # Spellings that still resolve and changed in nothing, beside facts about the group's
        # other spellings: named, so a reader can tell "checked, unchanged" from "never probed".
        named = {sp for f in out for sp in f.spellings()}
        quiet = [m for m in surviving if m not in named]
        if quiet:
            fact = DeltaFact(symbol=quiet[0], aliases=quiet[1:], kind="UNCHANGED",
                             evidence=["existence", "signature"])
            fact.detail["canonical"] = canon
            out.append(fact)
    return out


# ── proofs (successors v2, stage S2) ─────────────────────────────────────────────────────


def _own_params(shape: list | None) -> list[list]:
    """A shape without its receiver (``self``/``cls``)."""
    params = [list(p) for p in shape or ()]
    if params and params[0][0] in ("self", "cls") and params[0][1] in _POSITIONAL:
        params = params[1:]
    return params


def _pass_through(forward: Mapping[str, Any], shape: list | None) -> bool:
    """The forward hands every parameter on as it came: positional ones positionally in order,
    keyword-only ones by their own name, ``*args`` and ``**kwargs`` through ``*``/``**``."""
    params = _own_params(shape)
    if shape is None:
        return False
    args = list(forward.get("args") or ())
    kwargs = [tuple(k) for k in forward.get("kwargs") or ()]
    want_args: list[dict] = []
    want_kwargs: list[tuple] = []
    for name, kind, _default in params:
        if kind in _POSITIONAL:
            want_args.append({"p": name})
        elif kind == "VAR_POSITIONAL":
            want_args.append({"star": name})
        elif kind == "KEYWORD_ONLY":
            want_kwargs.append((name, {"p": name}))
        elif kind == "VAR_KEYWORD":
            want_kwargs.append(("**", {"dstar": name}))
    if args == want_args and sorted(map(str, kwargs)) == sorted(map(str, want_kwargs)):
        return True
    # positional-or-keyword parameters handed on by keyword (``maintain_order=maintain_order``)
    if len(args) <= len(want_args):
        rest = want_args[len(args):]
        if args == want_args[:len(args)] and all("p" in a for a in rest):
            moved = [(a["p"], a) for a in rest]
            return sorted(map(str, kwargs)) == sorted(map(str, want_kwargs + moved))
    return False


def _forward_of(step: Mapping[str, Any]) -> dict | None:
    return ((step or {}).get("body") or {}).get("forward")


def _callee_spelling(fwd: Mapping[str, Any], callee: Mapping[str, Any] | None, canon: str,
                     members: list[str], new_of: Callable[[str], Spell | None]
                     ) -> tuple[str | None, str | None]:
    """``(spelling, why_not)``: the public spelling, in the new version, of what a forward
    calls; None with the reason when there is none."""
    name = str(fwd.get("callee") or "")
    if fwd.get("attr"):
        name = "self." + str(fwd["attr"])
    if name.startswith(("self.", "cls.")):
        leaf = name.split(".", 1)[1]
        if "." in leaf:
            return None, "it forwards to an attribute of an attribute"
        spells = [m.rsplit(".", 1)[0] + "." + leaf for m in members if "." in m]
        live = [s for s in spells if new_of(s) is not None]
        if not live:
            return None, f"the member {leaf} it forwards to does not exist in the new version"
        best = min(live, key=_public_first)
        return (best, None) if not _private(best) or not leaf.startswith("_") else (
            None, f"it forwards to the private member {leaf}")
    spellings = list((callee or {}).get("spellings") or ())
    public = [s for s in spellings if not _private(s)]
    if spellings and not public:
        return None, f"it forwards to the private {spellings[0]}"
    live = [s for s in public if new_of(s) is not None]
    if not live:
        return None, "what it forwards to has no public spelling in the new version"
    return min(live, key=_public_first), None


def _same_old_body(old_step: Mapping[str, Any] | None, new_step: Mapping[str, Any] | None
                   ) -> bool:
    """The old symbol is one implementation in both versions: its body, warnings aside, hashes
    alike (or it forwards to the same callee there). A proof made in the target then holds for
    the old version's calls too."""
    a = ((old_step or {}).get("body") or {})
    b = ((new_step or {}).get("body") or {})
    if a.get("key") and a.get("key") == b.get("key"):
        return True
    fa, fb = a.get("forward") or {}, b.get("forward") or {}
    return bool(fa) and fa.get("callee") is not None and fa == fb


def _deprecated_at(spell: Spell | None, leaf: str) -> bool:
    if spell is None:
        return False
    runtime = spell.runtime_warnings if spell.final_for else []
    return bool(_side_deprecation(spell.step, leaf, runtime, "")
                or _body_deprecations(spell.step, [leaf], always=True))


def _prove_object(fact: DeltaFact, canon: str, members: list[str],
                  old_step: Mapping[str, Any], new_step: Mapping[str, Any] | None,
                  new_of: Callable[[str], Spell | None]) -> None:
    """A forward, a conditional forward or a twin body that proves a removed or deprecated
    object's successor, recorded in ``fact.succession`` (``proof``, ``transfer``,
    ``template``). A proof that names another object than the text named, or a private one,
    proves nothing: it is recorded as rejected, and the named successor stays named."""
    rec = dict(fact.succession or {"scope": "member" if _is_member(canon) else "object"})
    if rec.get("trust") == "identity":
        # the same object in the target: nothing to prove, the spelling moves
        rec.setdefault("transfer", "at-target")
        fact.succession = rec
        return
    tried: list[tuple[str, Mapping[str, Any] | None, Mapping[str, Any]]] = []
    if new_step is not None:
        tried.append(("new", new_step, new_step))
    tried.append(("old", old_step, old_step))
    named: tuple[str, str, Mapping[str, Any], Mapping[str, Any] | None] | None = None
    for side, step, shape_step in tried:
        body = (step or {}).get("body") or {}
        fwd = body.get("forward")
        cond = body.get("cond_forward")
        if fwd is None and cond is None:
            continue
        fwd_used = fwd or cond["forward"]
        incomplete = _incomplete(body)
        target, why = _callee_spelling(fwd_used, (step or {}).get("callee"), canon, members,
                                       new_of)
        if target is None:
            rec.setdefault("rejected", []).append({"name": fwd_used.get("callee")
                                                   or fwd_used.get("attr"),
                                                   "why": f"no proof: {incomplete or why}"})
            fact.succession = rec
            continue
        if fact.successor and new_of(fact.successor) is not None and \
                new_of(fact.successor).canonical != new_of(target).canonical:
            rec.setdefault("rejected", []).append({
                "name": target, "why": f"the body forwards to it, the text names "
                                       f"{fact.successor}: no proof of either"})
            fact.succession = rec
            return
        if incomplete is not None:
            # The summary does not account for the whole callable: the library's own code
            # names the successor, but proves nothing a rewrite could rest on.
            rec.setdefault("rejected", []).append({"name": target,
                                                   "why": f"no proof: {incomplete}"})
            fact.succession = rec
            if named is None:
                named = (target, side, fwd_used, step)
            continue
        tgt = new_of(target)
        old_shape = (step or {}).get("shape")
        callee_old = ((step or {}).get("callee") or {}).get("shape")
        if side == "new":
            transfer = "at-target"
            callee_old = tgt.step.get("shape") if tgt is not None else None
            if not _same_old_body(old_step, step):
                # The target's own old symbol forwards; the old version's was another
                # implementation (pydantic 1's compiled ``.json()`` wrote other bytes): the proof
                # says nothing about the old version, so it rewrites nothing.
                transfer = "old-changed:the old version's own implementation differs"
        elif _deprecated_at(tgt, _leaf(target)):
            transfer = "successor-deprecated"
        else:
            transfer = "successor-unchanged" if callee_old is not None and \
                callee_old == (tgt.step.get("shape") if tgt else None) else "per-site"
        # a chained call on the result (``new(a).ravel()``) is part of the forward: no rename
        kind = "rename" if fwd is not None and _pass_through(fwd_used, old_shape) and \
            not body.get("materialize") and not fwd_used.get("chain") else "call"
        rec.update(trust="proof", proof={
            "kind": "forward" if fwd is not None else "conditional-forward",
            "locus": canon, "version": "new" if side == "new" else "old", "side": side},
            transfer=transfer, sources=list(dict.fromkeys(["proof", *rec.get("sources", [])])))
        rec["template"] = {
            "kind": kind, "callee": target, "member": fwd_used.get("callee", "").startswith(
                ("self.", "cls.")) or bool(fwd_used.get("attr")),
            "attr": bool(fwd_used.get("attr")),
            "args": fwd_used.get("args") or [], "kwargs": fwd_used.get("kwargs") or [],
            "chain": fwd_used.get("chain") or [], "materialize": body.get("materialize") or {},
            "cond": cond.get("cond") if cond and fwd is None else None,
            "old_shape": old_shape, "callee_old_shape": callee_old,
            "callee_new_shape": tgt.step.get("shape") if tgt is not None else None}
        rec["old"] = {"spelling": fact.symbol, "canonical": canon, "spellings": members,
                      "wrappers": _wrapper_symbols(old_step)}
        rec["new"] = {"spelling": target, "canonical": tgt.canonical if tgt else target}
        hop = _deprecated_hop(fwd_used, callee_old, tgt) if side == "old" and tgt else None
        if hop is not None:
            # The forward hands on a parameter the successor deprecates in the target: one more
            # hop, named by the successor's own text (resolved after the facts, ``_chains``).
            rec["chain"] = [hop]
            rec["transfer"] = f"successor-changed:{target} deprecates {hop['param']}"
        fact.successor = fact.successor or target
        fact.succession = rec
        return
    if named is not None:
        _name_from_code(fact, rec, named, new_of)
    # a twin: the same body as the named successor's, apart from the warning, in the same
    # owner (so its names resolve to the same globals), with no wrapper on either side
    if fact.successor and new_step is not None:
        tgt = new_of(fact.successor)
        mine = (new_step.get("body") or {}).get("key")
        theirs = ((tgt.step.get("body") or {}).get("key") if tgt is not None else None)
        new_canon = str(new_step.get("canonical") or canon)
        same_owner = tgt is not None and \
            new_canon.rsplit(".", 1)[0] == str(tgt.canonical).rsplit(".", 1)[0]
        if mine and mine == theirs and new_step.get("shape") == tgt.step.get("shape") and \
                not same_owner:
            rec.setdefault("rejected", []).append({
                "name": fact.successor, "why": "no proof: its body reads alike, but in another "
                                               "module or class, whose names may differ"})
            fact.succession = rec
            return
        if mine and mine == theirs and new_step.get("shape") == tgt.step.get("shape") and \
                _incomplete(new_step.get("body")) is None and \
                _incomplete(tgt.step.get("body")) is None:
            rec.update(trust="proof", proof={"kind": "twin", "locus": canon, "version": "new",
                                             "side": "new"},
                       transfer="at-target" if _same_old_body(old_step, new_step) else
                       "old-changed:the old version's own implementation differs")
            rec["template"] = {"kind": "rename", "callee": fact.successor,
                               "member": _is_member(canon),
                               "attr": tgt.step.get("kind") not in ("callable", "class"),
                               "old_shape": new_step.get("shape"),
                               "callee_old_shape": tgt.step.get("shape"),
                               "callee_new_shape": tgt.step.get("shape")}
            rec["old"] = {"spelling": fact.symbol, "canonical": canon, "spellings": members}
            rec["new"] = {"spelling": fact.successor, "canonical": tgt.canonical}
            fact.succession = rec


def _name_from_code(fact: DeltaFact, rec: dict, named: tuple, new_of: Callable[[str],
                    Spell | None]) -> None:
    """An incomplete forward still names its callee: trust ``named`` (an agent decides), the
    reason in ``rejected``. A hop the successor deprecates in the target is followed as for a
    proof (``_chains``), so the name is the one a person would migrate to."""
    target, side, fwd_used, step = named
    rec["trust"] = rec.get("trust") or "named"
    rec["sources"] = list(dict.fromkeys([*rec.get("sources", []), "code"]))
    tgt = new_of(target)
    if side == "old" and tgt is not None:
        hop = _deprecated_hop(fwd_used, ((step or {}).get("callee") or {}).get("shape"), tgt)
        if hop is not None:
            rec["chain"] = [hop]
            rec["transfer"] = f"successor-changed:{target} deprecates {hop['param']}"
    fact.successor = fact.successor or target
    fact.succession = rec


def _incomplete(body: Mapping[str, Any] | None,
                allowed: Iterable[str] = succ_mod.FORWARD_WRAPPERS) -> str | None:
    """Why a body summary is no complete account of the callable (None: it is): a wrapper or
    decorator it does not know, a parameter the body aliases besides forwarding, or a statement
    besides the forward (``_noise`` keeps only warnings, docstrings and conflict checks out)."""
    why = succ_mod.unknown_wrapper(body, allowed)
    if why is not None:
        return why
    if (body or {}).get("aliases"):
        a = body["aliases"][0]
        return (f"the body also makes {a.get('old')} an alias of {a.get('new')}: the forward "
                "is not all it does")
    return None


def _wrapper_symbols(step: Mapping[str, Any] | None) -> list[str]:
    """The trace symbols of the wrappers around a callable (``module.code``): a call recorded
    under one of them is a call of this callable."""
    out = []
    for c in (step or {}).get("cells") or ():
        if c.get("module") and c.get("code"):
            out.append(f"{c['module']}.{c['code']}")
    for layer in ((step or {}).get("body") or {}).get("layers") or ():
        if layer.get("module") and layer.get("code"):
            out.append(f"{layer['module']}.{layer['code']}")
    return sorted(set(out))


def _deprecated_hop(fwd: Mapping[str, Any], callee_old: list | None, tgt: Spell
                    ) -> dict | None:
    """A parameter the forward passes that the successor's own body deprecates in the target
    (polars ``replace(default=...)``: "Use `replace_strict` instead"): ``{via, param, text,
    names}``, the names that text gives; None when there is none."""
    passed = {k for k, _d in fwd.get("kwargs") or () if k != "**"}
    params = _own_params(callee_old)
    positional = [p[0] for p in params if p[1] in _POSITIONAL]
    passed |= {positional[i] for i, _ in enumerate(fwd.get("args") or ())
               if i < len(positional)}
    for text in _param_texts(tgt.step):
        hit = next((p for p in sorted(passed) if _names_param(text, p)), None)
        if hit and _DEPRECATES.search(text):
            names = [c.name for c in succ_mod.candidates_from([text]) if c.role != "param"]
            if names:
                return {"via": tgt.spelling, "param": hit, "text": _trim(text), "names": names}
    return None


def _chains(facts: list[DeltaFact], resolve_new: Callable[[list[str]], Mapping[str, dict]] |
            None, new_of: Callable[[str], Spell | None]) -> None:
    """Resolve a chain's last hop (``_deprecated_hop``) beside the successor it came from. One
    name that exists makes it the fact's successor, named (a text gave the hop): never a
    rewrite. The forward's template is kept, re-aimed at it, for the agent's suggestion."""
    todo = [f for f in facts if (f.succession or {}).get("chain")]
    if not todo or resolve_new is None:
        return
    wanted: dict[str, list[str]] = {}
    for f in todo:
        hop = f.succession["chain"][-1]
        parent = str(hop["via"]).rsplit(".", 1)[0]
        wanted[id(f)] = [f"{parent}.{n.rsplit('.', 1)[-1]}" for n in hop["names"]]
    paths = sorted({p for ps in wanted.values() for p in ps if new_of(p) is None})
    try:
        extra = spellings(resolve_new(paths)) if paths else {}
    except Exception:  # noqa: BLE001
        extra = {}
    for f in todo:
        live = [p for p in wanted[id(f)] if new_of(p) is not None
                or (p in extra and extra[p].exists)]
        live = list(dict.fromkeys(live))
        rec = dict(f.succession)
        hop = dict(rec["chain"][-1])
        if len(live) != 1 or _private(live[0]):
            hop["resolved"] = None
            rec["chain"] = [*rec["chain"][:-1], hop]
            f.succession = rec
            continue
        hop["resolved"] = live[0]
        rec["chain"] = [*rec["chain"][:-1], hop]
        rec["trust"] = "named"
        rec["sources"] = list(dict.fromkeys([*rec.get("sources", []), "chain"]))
        rec["new"] = {"spelling": live[0], "canonical": live[0]}
        template = dict(rec.get("template") or {})
        template["callee"] = live[0]
        rec["template"] = template
        f.successor = live[0]
        f.succession = rec


_GUARDS = ("none", "not_none", "sentinel", "in")


def _param_aliases(step: Mapping[str, Any] | None) -> dict[str, tuple[str, str, str]]:
    """``{old: (new, how, guard)}``: parameters a body makes one another's alias, when that is
    all it does with the old one: in its own statements (``height = size``,
    ``kwargs["height"] = kwargs.pop("size")``), or through a helper that hands one back as the
    other (``method = _check(method, interpolation)``, ``_check`` returning ``interpolation``
    unrebound), or a helper whose own body does it with arguments the caller passes by the
    same names. ``guard`` is the condition the alias holds under: ``none`` (always),
    ``not_none`` (``if old is not None:``: only for a value other than None), ``sentinel``
    (``if old is not _Default:``: any value a caller passes), ``in`` (``if "old" in
    kwargs:``). An alias under another condition, or an old parameter the body also reads
    elsewhere, is none: its other uses are not modelled."""
    body = (step or {}).get("body") or {}
    loads = body.get("loads") or {}
    out: dict[str, tuple[str, str, str]] = {}
    for a in body.get("aliases") or ():
        guard = a.get("guard") or "none"
        reads = 1 + (1 if guard in ("not_none", "sentinel") else 0)
        if guard not in _GUARDS or (a.get("how") != "pop" and loads.get(a["old"], 0) > reads):
            continue
        out.setdefault(a["old"], (a["new"], a.get("how") or "assign", guard))
    for h in (step or {}).get("helper_warns") or ():
        params = list(h.get("params") or ())
        fed: dict[str, str] = {}
        for i, desc in enumerate(h.get("args") or ()):
            if i < len(params) and isinstance(desc, dict) and "p" in desc:
                fed[params[i]] = desc["p"]
        for k, desc in (h.get("kwargs") or {}).items():
            if isinstance(desc, dict) and "p" in desc:
                fed[k] = desc["p"]
        ret = h.get("returns")
        guard = h.get("guard") or "none"
        if ret and h.get("assign") and ret in fed and fed[ret] != h["assign"] and \
                guard in _GUARDS:
            caller = fed[ret]
            reads = 1 + (1 if guard in ("not_none", "sentinel") else 0)
            if loads.get(caller, 0) <= reads:
                out.setdefault(caller, (h["assign"], "helper:" + str(h.get("helper")), guard))
        hloads = h.get("loads") or {}
        for a in h.get("aliases") or ():
            g = a.get("guard") or "none"
            reads = 1 + (1 if g in ("not_none", "sentinel") else 0)
            if a["old"] in fed and a["new"] in fed and fed[a["old"]] == a["old"] and \
                    fed[a["new"]] == a["new"] and g in _GUARDS and \
                    hloads.get(a["old"], 0) <= reads and loads.get(a["old"], 0) <= 1:
                out.setdefault(a["old"], (a["new"], "helper:" + str(h.get("helper")), g))
    return out


def _prove_param(fact: DeltaFact, o: Mapping[str, Any], n: Mapping[str, Any],
                 facts_here: list[DeltaFact]) -> None:
    """A parameter rename proven by the library's code: a registered rename helper, or an alias
    the body makes (``_param_aliases``), on the target side or the old side. Transfer: the new
    parameter is in the target's signature and nothing deprecates it there."""
    param, new = fact.param, fact.successor
    if not param or not new:
        return
    rec = dict(fact.succession or {"scope": "param"})
    proof = rec.get("proof") if rec.get("trust") == "proof" else None
    guard = "none"
    if proof is None:
        for side, step in (("new", n), ("old", o)):
            alias = _param_aliases(step).get(param)
            if alias and alias[0] == new:
                proof = {"kind": "alias", "locus": alias[1] if alias[1].startswith("helper:")
                         else f"{fact.symbol} ({alias[1]})", "version": side, "side": side}
                guard = alias[2]
                break
    if proof is None:
        return
    side_step = n if proof["side"] == "new" else o
    why = succ_mod.unknown_wrapper(side_step.get("body"), succ_mod.KEYWORD_WRAPPERS)
    if why is not None:
        # the rename is proven in the body, but a wrapper around it may treat the two
        # keywords apart: named, not rewritten
        rec["trust"] = "named"
        rec.pop("proof", None)
        rec.setdefault("rejected", []).append({"name": new, "why": f"no proof: {why}"})
        fact.succession = rec
        return
    n_params = {p[0]: p for p in n.get("shape") or ()}
    if new not in n_params:
        transfer = f"successor-changed:{new} is not in the new signature"
    elif any(f.param == new and f.kind in ("DEPRECATED", "REMOVED") for f in facts_here):
        transfer = f"successor-changed:{new} is deprecated in the new version"
    elif proof["side"] == "new" and param in {p[0] for p in o.get("shape") or ()} and \
            not _same_old_body(o, n):
        # the old version still takes the old keyword, through another implementation: the
        # target's alias says nothing about it (the old keyword still works there anyway)
        transfer = "old-changed:the old version's own implementation differs"
    else:
        transfer = "at-target" if proof["side"] == "new" else "successor-unchanged"
    rec.update(trust="proof", proof=proof, transfer=transfer, params={param: new})
    rec["sources"] = list(dict.fromkeys([*rec.get("sources", []), "proof"]))
    rec["template"] = {"kind": "rename-keyword", "old_shape": o.get("shape"),
                       "new_shape": n.get("shape"), "guard": guard}
    rec["old"] = {"spelling": fact.symbol, "spellings": fact.spellings(),
                  "wrappers": _wrapper_symbols(o)}
    fact.succession = rec


_ACCESS_TAGS = ("getattr:", "access-warning:")


def _about_access(evidence: Iterable[str]) -> bool:
    """A deprecation that only accessing the name warns about (a module ``__getattr__``, an
    access warning: ``numpy.core`` -> ``numpy._core``), not one of the object itself."""
    tags = [str(e).removeprefix("deprecated-in-new:") for e in evidence
            if not str(e).startswith("warning:")]
    return bool(tags) and all(t.startswith(_ACCESS_TAGS) for t in tags)


def _param_level(o: dict, n: dict, ev: _Evidence, spell: Any) -> list[tuple[ParamChange,
                                                                               list[str]]]:
    """The parameter changes of one spelling, with what the deprecation helpers add:

    - a parameter the old version accepted only through a rename helper
      (``@deprecate_renamed_parameter("periods", "n")``) that the new one accepts no more is
      removed (SIGNATURE), though both signatures read the same;
    - a parameter the new version still accepts through a rename helper
      (``@_api.rename_parameter("3.9", "labels", "tick_labels")``) is deprecated there, not
      removed or renamed (DEPRECATED);
    - a parameter both versions have that the new one's own body, or a warning helper it calls,
      says is deprecated (seaborn ``kdeplot(shade=)``) is deprecated there (DEPRECATED)."""
    changes = param_changes(o.get("shape"), n.get("shape"), _own_prose(n),
                            bool(n.get("rejects_kwargs")))
    o_ren = succ_mod.param_renames(o)
    n_ren = succ_mod.param_renames(n)
    n_params = set(_shape_params(n))
    o_params = set(_shape_params(o))
    n_varkw = any(p[1] == "VAR_KEYWORD" for p in n.get("shape") or ())
    out: list[tuple[ParamChange, list[str]]] = []
    done: set[str] = set()
    for ch in changes:
        hf = n_ren.get(ch.param or "")
        if hf is not None and ch.kind == "SIGNATURE":
            done.add(ch.param)  # type: ignore[arg-type]
            out.append((ParamChange("DEPRECATED", ch.param, None, None,
                                    f"param {ch.param} deprecated, renamed to {hf.new}"),
                        [f"deprecated-in-new:helper: {hf.label()}"]))
            continue
        out.append((ch, []))
    for old, hf in o_ren.items():
        if old in done or old in o_params or any(ch.param == old for ch, _ in out):
            continue
        if old in n_params:
            continue
        if old in n_ren:
            out.append((ParamChange("DEPRECATED", old, None, None,
                                    f"param {old} deprecated, renamed to {hf.new}"),
                        [f"helper: {hf.label()} (the old version)",
                         f"deprecated-in-new:helper: {n_ren[old].label()}"]))
            continue
        if n_varkw and not n.get("rejects_kwargs") and not _stated_removed(old, _own_prose(n)):
            continue                    # the new **kwargs may still take it: nothing to say
        out.append((ParamChange("SIGNATURE", old, f"alias of {hf.new}", None,
                                f"param {old} removed (the old version accepted it as an alias "
                                f"of {hf.new})"),
                    [f"helper: {hf.label()} (the old version)"]))
    named = {ch.param for ch, _ in out}
    old_texts = [*_param_texts(o), *[w.message for w in ev.baseline]]
    for param in sorted(n_params & o_params):
        if param in named:
            continue

        def about(texts: list[str], param: str = param) -> list[str]:
            # the text deprecates the parameter, not names it as what to use instead
            return [t for t in texts if _names_param(t, param) and _DEPRECATES.search(t)
                    and not _mentions(t, "positional")
                    and param not in {c.name for c in succ_mod.candidates_from([t])}]

        new_texts, old_ones = about(_param_texts(n)), about(old_texts)
        if new_texts or old_ones:
            extra = [f"deprecated-in-new:in its body: {_trim(new_texts[0])}"] if new_texts \
                else []
            extra += [f"old-prose: {_trim(t)}" for t in old_ones[:1]]
            out.append((ParamChange("DEPRECATED", param, None, None,
                                    f"param {param} deprecated"), extra))
    return out


def _param_texts(step: Mapping[str, Any]) -> list[str]:
    """A callable's own texts about its parameters: warn and raise messages, and the messages
    of the warning helpers it calls. None for a class: its source holds every method's texts,
    about any of their parameters (its ``__init__`` speaks for the constructor)."""
    if step.get("kind") == "class":
        return []
    texts = [w.get("message", "") for w in step.get("warns") or ()]
    texts += [w.get("message", "") for w in step.get("raises") or ()]
    for h in step.get("helper_warns") or ():
        texts += list(h.get("messages") or ())
    return [t for t in texts if t]


def _param_successor(ch: ParamChange, o: dict, n: dict, ev: _Evidence
                     ) -> tuple[str | None, succ_mod.Succession]:
    """The parameter a changed parameter is replaced by, and how it is known:

    - a rename helper on either side says so (its data: ``proof`` when the helper is
      registered, ``named`` when its cells were read generically);
    - otherwise the texts about it (either side's warn and raise messages, the warning helpers
      they call, the baseline warnings) name one parameter the new signature has and the old
      one lacked, or that the old body hands over (``named``);
    - a parameter paired only by position (``param_changes``' rename) is a ``hint``, never a
      successor."""
    param = ch.param or ""
    rec = succ_mod.Succession(scope="param")
    n_params = set(_shape_params(n))
    for side, step in (("new", n), ("old", o)):
        hf = succ_mod.param_renames(step).get(param)
        if hf is not None and hf.new and (hf.new in n_params or not n_params):
            rec.trust = "proof" if hf.registered and not hf.values else "named"
            if hf.values:
                rec.caveats = [f"{hf.helper} also maps the values of {param} to {hf.new}: "
                               "a value written for one is not one for the other"]
            rec.sources = ["helper"]
            rec.params = {param: hf.new}
            if hf.registered and not hf.values:
                rec.proof = {"kind": "helper", "locus": hf.helper, "version": hf.since,
                             "side": side}
            return hf.new, rec
    texts = [t for t in [*_param_texts(o), *_param_texts(n),
                         *[w.message for w in ev.baseline]] if _names_param(t, param)]
    rec.caveats = succ_mod.caveats(texts)
    rec.equivalent = succ_mod.asserts_equivalence(texts)
    found: list[str] = []
    rejected: dict[str, str] = {}
    for c in succ_mod.candidates_from(texts, source="prose"):
        name = c.name.split(".")[-1] if c.role == "param" else c.name
        if name == param or "." in name:
            continue
        if n_params and name not in n_params:
            rejected.setdefault(name, "is no parameter of the new signature")
            continue
        if name not in found:
            found.append(name)
    rec.rejected = [{"name": k, "why": v} for k, v in rejected.items() if k not in found]
    if len(found) == 1:
        rec.trust, rec.sources, rec.params = "named", ["prose"], {param: found[0]}
        return found[0], rec
    if found:
        rec.candidates = [{"name": f, "source": "prose"} for f in found]
        return None, rec
    if ch.kind == "SIGNATURE" and ch.new and ch.note.startswith(f"param {param} renamed to"):
        rec.candidates = [{"name": ch.new, "source": "position"}]
        rec.trust = None
    return None, rec


def _same_object_successor(step: dict, any_old: dict, leaf: str,
                           new_of: Callable[[str], Spell | None]) -> Spell | None:
    """A spelling the old interpreter proved to be the same object (``is``) that is live and
    not itself deprecated on the new side. The object's own home (the probe's
    ``same_object_home``, possibly in another package) wins; otherwise the shortest public one."""
    live = []
    for cand in step.get("same_object_as") or []:
        target = new_of(cand)
        if target is None or not _compatible(any_old, target.step, leaf):
            continue
        runtime = target.runtime_warnings if target.final_for else []
        if _side_deprecation(target.step, _leaf(cand), runtime, ""):
            continue
        live.append(target)
    home = step.get("same_object_home")
    for target in live:
        if home and target.spelling == home:
            return target
    return min(live, key=lambda t: _public_first(t.spelling)) if live else None


_VARIANT_TOKENS = ("lazy", "noop", "async", "sync", "iter", "bytes", "unicode")


def _variant_ok(old_leaf: str, new_leaf: str) -> bool:
    """A spelling and its successor carry the same variant suffix: ``ugettext_lazy`` is
    ``gettext_lazy``, never ``gettext`` (a lazy proxy impersonates the function it wraps)."""
    def variant(leaf: str) -> str | None:
        tail = leaf.rsplit("_", 1)[-1]
        return tail if "_" in leaf and tail in _VARIANT_TOKENS else None

    return variant(old_leaf) == variant(new_leaf)


def _public_first(name: str) -> tuple:
    private = any(part.startswith("_") and not part.startswith("__")
                  for part in name.split("."))
    return (private, name.count("."), name)


def _compatible(old: dict, new: dict, leaf: str) -> bool:
    ok = old.get("kind") == new.get("kind") or old.get("kind") is None  # None: a static old side
    families = ({"callable", "class"}, {"property", "cached_property", "lazy_property"})
    return ok or any({old.get("kind"), new.get("kind")} <= f for f in families)


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


def _static_facts(facts: list[DeltaFact], old_results: Mapping[str, dict]) -> list[DeltaFact]:
    """Stamp facts computed against a statically read old side.

    ``basis`` says so; an UNCHANGED fact is dropped when the old signature could not be read
    (a decorated or generated callable) or its kind is unknown: "nothing changed" would claim a
    comparison that never happened.
    """
    unreadable: set[str] = set()
    for res in old_results.values():
        for st in res.get("steps") or []:
            if not st.get("dep") or not st.get("exists"):
                continue
            kind = st.get("kind")
            if kind is None or (kind in ("class", "callable") and st.get("shape") is None):
                unreadable.add(st.get("canonical") or "")
    out = []
    for fact in facts:
        if fact.kind == "UNCHANGED" and (fact.symbol in unreadable
                                         or fact.detail.get("canonical") in unreadable
                                         or any(s in unreadable for s in fact.aliases)):
            continue
        fact.basis = {"old": "static", "new": "probe"}
        out.append(fact)
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
            if f.vanished_at:
                prev.vanished_at = {**(prev.vanished_at or {}), **f.vanished_at}
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
            if old_env.is_static:
                futures["old"] = pool.submit(self._static_run, old_env, request["paths"])
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
            elif old_env.is_static:
                report.quality = "partial"  # an old side read from source is never complete
                report.sides["old"] = f"static:{old_env.static[m.name].source}"

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

            old_exe, old_runner = old_env.executable, old_env.runner

            def resolve_old(paths: list[str]) -> Mapping[str, dict]:
                if old_exe is None:
                    return {}
                run3 = run_probe(old_exe, self.root,
                                 {"paths": paths, "dists": request["dists"], "any_root": True,
                                  "contexts": [], "per_path_timeout": PER_PATH_TIMEOUT},
                                 use_cache=self.use_cache,
                                 **({"runner": old_runner} if old_runner is not None else {}))
                return run3.results if run3.ok else {}

            t_diff = time.perf_counter()
            by_pkg = diff(DiffInput(
                old=runs["old"].results, new=runs["new"].results, touched=touched,
                root_to_package=root_to_package, resolve_new=resolve_new,
                knowledge_hint=self._knowledge_hint,
                import_failed=frozenset(
                    path for path, res in runs["new"].results.items()
                    if res.get("import_errors")),
                resolve_old=resolve_old if not old_env.is_static else None,
            ))
            self.stats["diff_s"] = round(time.perf_counter() - t_diff
                                         - self.stats.get("second_pass_s", 0), 3)
            for name, facts in by_pkg.items():
                if usable.get(name):
                    reports[name].facts = facts
                    if old_env.is_static:
                        reports[name].facts = _static_facts(facts, runs["old"].results)
            self._transient_notes(reports, runs, root_to_package, touched)
            self._online_hints(reports, resolve_new)
            if not old_env.is_static:
                self._type_relocations(reports, usable, root_to_package, old_env, new_env, trace)
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

    def _type_relocations(self, reports, usable, root_to_package, old_env, new_env,
                          trace) -> None:
        """Baseline types whose public spelling now names a different class (same-object
        evidence from both interpreters), attached to the package that owns them."""
        try:
            names = [n for n in baseline_type_names(self.root, trace)
                     if n.split(".")[0] in root_to_package]
            if not names:
                return
            names = names[:MAX_RELOCATION_TYPES]
            paths = sorted({c for n in names for c in relocation_candidates(n)})
            request = {"paths": paths, "dists": sorted({m.name for m in self.migrations}),
                       "any_root": True, "contexts": [], "per_path_timeout": PER_PATH_TIMEOUT}
            runs = {}
            for side, env in (("old", old_env), ("new", new_env)):
                runs[side] = run_probe(env.executable, self.root, request,  # type: ignore[arg-type]
                                       use_cache=self.use_cache,
                                       **({"runner": env.runner} if env.runner else {}))
            if not (runs["old"].ok and runs["new"].ok):
                return
            for item in compute_type_relocations(runs["old"].results, runs["new"].results, names):
                pkg = root_to_package.get(item["old"].split(".")[0])
                if pkg in reports and usable.get(pkg):
                    reports[pkg].type_relocations.append(item)
        except Exception as exc:  # noqa: BLE001 — an enrichment, never a crash
            log.debug("type relocations skipped: %s", exc)

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

    def _static_run(self, env: Interpreter, paths: list[str]) -> ProbeRun:
        """The old side read from the old releases' source (never raises)."""
        t0 = time.perf_counter()
        key = hashlib.sha256(json.dumps({
            "v": STATIC_VERSION, "paths": sorted(paths),
            "src": sorted((n, a.version, a.source) for n, a in env.static.items()),
        }, sort_keys=True).encode()).hexdigest()[:20]
        cache = self.root / CACHE_DIR / f"static-{key}.json"
        if self.use_cache:
            try:
                payload = json.loads(cache.read_text(encoding="utf-8"))
                return ProbeRun(meta=payload["meta"], results=payload["results"],
                                seconds=round(time.perf_counter() - t0, 3), from_cache=True)
            except (OSError, ValueError, KeyError, TypeError):
                pass
        run = ProbeRun()
        try:
            run.meta, run.results = static_probe(env.static, paths)
        except Exception as exc:  # noqa: BLE001 — a reader failure is a reason, not a crash
            run.failure = f"static reader failed: {type(exc).__name__}: {exc}"
            return run
        run.seconds = round(time.perf_counter() - t0, 3)
        try:
            atomic_write_json(cache, {"meta": run.meta, "results": run.results}, indent=None)
        except OSError as exc:
            log.debug("delta static cache not written: %s", exc)
        return run

    def _side_ok(self, report: DeltaReport, side: str, env: Interpreter,
                 run: ProbeRun | None, expected: str) -> bool:
        label = "old" if side == "old" else "new"
        if side == "old" and env.is_static:
            acquired = env.static.get(report.package)
            if acquired is None:
                report.notes.append(f"no old interpreter: {env.reason}; the static old side has "
                                    f"no source for {report.package}: "
                                    f"{env.static_errors.get(report.package)}")
                return False
            if run is None or not run.ok:
                report.notes.append(f"static old side failed: {run.failure if run else 'not run'}")
                return False
            report.notes.append(f"old side read statically from {acquired.source}"
                                f" ({acquired.origin}); no old interpreter: {env.reason}")
            unknown = [p for p, res in run.results.items()
                       if res.get("unknown") and p.split(".")[0] in acquired.roots]
            if unknown:
                report.notes.append(
                    f"{len(unknown)} touched path(s) could not be read statically and are not "
                    f"compared (e.g. {unknown[0]})")
            return True
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
        if version is None and side == "old" and any(
                canonicalize_name(a) == canonicalize_name(report.package) for a in env.absent):
            report.notes.append(f"{report.package} is not in the baseline interpreter: absent "
                                f"in the old version, its symbols are new")
            return True
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

    @staticmethod
    def _verify_successor(fact: DeltaFact,
                          check: Callable[[list[str]], Mapping[str, dict]] | None) -> None:
        """A successor read from release prose must exist in the new interpreter; the spelling
        it resolves to and whether its signature matches the old object's are recorded, so
        only a compatible rename is ever auto-applied (``provider._pattern``)."""
        if check is None or not fact.successor:
            return
        cands = _successor_candidates(fact.successor, fact.old or fact.symbol,
                                      [fact.symbol, *fact.aliases])
        try:
            found = spellings(check(cands))
        except Exception:  # noqa: BLE001 — verification is an upgrade, never a crash
            return
        for cand in cands:
            sp = found.get(cand)
            if sp is not None and sp.exists:
                old_shape = fact.detail.get("old_shape")
                fact.successor = cand
                fact.detail.update(
                    successor_importable=sp.index == 0,
                    successor_same_signature=bool(
                        old_shape is not None and sp.step.get("shape") == old_shape),
                    successor_exists=True,
                )
                return
        fact.detail["successor_exists"] = False

    def _online_hints(self, reports: dict[str, DeltaReport],
                      check: Callable[[list[str]], Mapping[str, dict]] | None = None) -> None:
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
                        self._verify_successor(fact, check)
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
                atomic_write_json(cache_path, cache, indent=1, sort_keys=True)
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
