"""Static identity — resolve public import paths to the runtime's canonical names.

The static contact map speaks in *public import paths* (``requests.Request``,
``pandas.concat``); the boundary tracer speaks in *defining* names (``sym`` =
``requests.models.Request.__init__``; v1 traces only carry ``q`` =
``<frame module>.<co_name>``). The two never meet as strings. This module joins
them by asking the interpreter that ran the capture what each path actually is.

Host-side only (pydantic, 3.12+), but the resolution itself runs in a
**subprocess of the capture's interpreter**: resolving imports the project's
dependencies, which must never happen inside pymolt's own process, and the
answer is only meaningful under the dependency versions the trace observed.
The probe sent to that interpreter is pure stdlib and parses on Python 3.6+.

Per path the probe imports the longest module prefix, getattr-walks the rest,
unwraps decorators (``inspect.unwrap``), and reports::

    {"sym": "<frame module>.<qualname>", "kind": "function|class|module|other",
     "aliases": [...], "legacy": [...]}      # or {"error": "..."}

``sym`` is spelled the way a profile-backend trace spells it (the module whose
globals the code runs in, plus the code's qualname), so libraries that rewrite
``__module__`` still meet their frames; the ``__module__`` spelling (what the
wrap backend records) is kept in ``aliases``. ``legacy`` holds v1 runtime names
(``<module>.<co_name>``) that are unambiguous: the code name occurs once among
the defining module's functions (``typing.overload`` stubs discounted). For a
class they are its own methods' unambiguous legacy names. An ambiguous name
(every ``__init__`` in a module) is never listed: a v1 event carrying it cannot
honestly confirm one particular class.

Never raises into the caller: every failure degrades to per-path ``error``
entries plus a note.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

from pydantic import BaseModel, Field

from pymolt.adapters.python_runner import RunnerSpec, run_in, runner_available, to_container
from pymolt.state_io import atomic_write_json

logger = logging.getLogger(__name__)

# Bump when the probe's output semantics change: cached answers from an older
# probe are then ignored instead of trusted.
PROBE_VERSION = 1
_MARK = "@@pymolt-identity@@ "
_MANIFESTS = (
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "uv.lock",
    "poetry.lock",
    "Pipfile",
    "Pipfile.lock",
    "environment.yml",
    "environment.yaml",
)
_MAX_ERROR = 300

# The probe runs inside the capture's interpreter (possibly 3.6): stdlib only,
# no f-strings, no walrus, no pymolt imports. Results stream one line per path,
# so a hang or crash part-way still returns everything resolved before it.
_PROBE_SOURCE = r'''
import functools
import importlib
import inspect
import json
import os
import signal
import sys
import threading
import types

_MARK = "@@pymolt-identity@@ "
_MAX_ALIASES = 2000


class _Timeout(BaseException):
    pass


def _on_alarm(signum, frame):
    raise _Timeout()


def _qual(obj):
    try:
        module = getattr(obj, "__module__", None)
        if not isinstance(module, str):
            owner = getattr(obj, "__objclass__", None)
            module = getattr(owner, "__module__", None)
        qualname = getattr(obj, "__qualname__", None)
        if not isinstance(qualname, str):
            qualname = getattr(obj, "__name__", None)
    except Exception:
        return None
    if isinstance(module, str) and module and isinstance(qualname, str) and qualname:
        return module + "." + qualname
    return None


def _walk(path):
    parts = path.split(".")
    obj = importlib.import_module(parts[0])
    for index in range(1, len(parts)):
        part = parts[index]
        try:
            obj = getattr(obj, part)
            continue
        except AttributeError:
            if not isinstance(obj, types.ModuleType):
                raise
        name = obj.__name__ + "." + part
        try:
            importlib.import_module(name)
        except ImportError as exc:
            if getattr(exc, "name", None) == name:
                raise AttributeError(
                    "module {0!r} has no attribute {1!r}".format(obj.__name__, part))
            raise
        try:
            obj = getattr(obj, part)
        except AttributeError:
            obj = sys.modules[name]
    return obj


def _target(obj):
    if isinstance(obj, property) and obj.fget is not None:
        obj = obj.fget
    if isinstance(obj, (staticmethod, classmethod)):
        obj = obj.__func__
    if isinstance(obj, functools.partial):
        obj = obj.func
    if inspect.ismethod(obj):
        obj = obj.__func__
    if not inspect.isclass(obj) and not isinstance(obj, types.ModuleType):
        try:
            obj = inspect.unwrap(obj)
        except Exception:
            pass
    return obj


_CODE_NAMES = {}
_CO_NEWLOCALS = 0x0002  # set on function code; unset on module and class bodies


def _code_names(module_name):
    """(file, {co_name: count}) over every function code object in the module's source."""
    if module_name in _CODE_NAMES:
        return _CODE_NAMES[module_name]
    found = None
    try:
        module = sys.modules.get(module_name)
        loader = getattr(module, "__loader__", None)
        get_code = getattr(loader, "get_code", None)
        code = get_code(module_name) if get_code is not None else None
    except Exception:
        code = None
    if code is not None:
        counts = {}
        stack = [code]
        while stack:
            current = stack.pop()
            if current.co_flags & _CO_NEWLOCALS:
                counts[current.co_name] = counts.get(current.co_name, 0) + 1
            for const in current.co_consts:
                if isinstance(const, types.CodeType):
                    stack.append(const)
        found = (code.co_filename, counts)
    _CODE_NAMES[module_name] = found
    return found


def _overload_stubs(func, code):
    """``typing.overload`` stubs of ``func`` (3.11+ registry): compiled, never run."""
    get_overloads = getattr(sys.modules.get("typing"), "get_overloads", None)
    if get_overloads is None:
        return 0
    try:
        stubs = get_overloads(func)
    except Exception:
        return 0
    return sum(1 for stub in stubs
               if getattr(getattr(stub, "__code__", None), "co_name", None) == code.co_name)


def _legacy(func):
    """The v1 runtime name of ``func`` when it is unambiguous in its module."""
    code = getattr(func, "__code__", None)
    globs = getattr(func, "__globals__", None)
    if not isinstance(code, types.CodeType) or not isinstance(globs, dict):
        return None
    module_name = globs.get("__name__")
    if not isinstance(module_name, str):
        return None
    found = _code_names(module_name)
    if found is None or found[0] != code.co_filename:
        return None
    count = found[1].get(code.co_name, 0)
    if count > 1:
        count -= _overload_stubs(func, code)
    if count != 1:
        return None
    return module_name + "." + code.co_name


def _class_functions(cls):
    try:
        members = list(vars(cls).values())
    except Exception:
        return
    for member in members:
        if isinstance(member, property):
            for accessor in (member.fget, member.fset, member.fdel):
                if accessor is not None:
                    yield accessor
            continue
        if isinstance(member, (staticmethod, classmethod)):
            member = member.__func__
        if isinstance(member, types.FunctionType):
            yield member
            try:
                inner = inspect.unwrap(member)
            except Exception:
                inner = member
            if inner is not member:
                yield inner


def _code_qualname(func):
    code = getattr(func, "__code__", None)
    qualname = getattr(code, "co_qualname", None)  # 3.11+; immune to __qualname__ rewrites
    if not isinstance(qualname, str):
        qualname = getattr(func, "__qualname__", None)
    return qualname if isinstance(qualname, str) else None


def _frame_module(func):
    globs = getattr(func, "__globals__", None)
    name = globs.get("__name__") if isinstance(globs, dict) else None
    return name if isinstance(name, str) and name else None


def _function_sym(func):
    """``<frame module>.<code qualname>`` — the name a profile-backend trace records.

    Libraries that rewrite ``__module__`` for documentation (``httpx.Client``) would
    otherwise never meet the frames their code actually runs in.
    """
    module = _frame_module(func)
    qualname = _code_qualname(func)
    if module and qualname:
        return module + "." + qualname
    return _qual(func)


def _class_sym(cls):
    qualname = getattr(cls, "__qualname__", None)
    if isinstance(qualname, str):
        for func in _class_functions(cls):
            code_qualname = _code_qualname(func)
            module = _frame_module(func)
            if module and code_qualname and code_qualname.startswith(qualname + "."):
                return module + "." + qualname
    return _qual(cls)


def _aliases(sym, attribute_sym, legacy):
    """Every other name the runtime may record for the object (sym excluded)."""
    aliases = []
    if attribute_sym and attribute_sym != sym:
        aliases.append(attribute_sym)  # what a wrap-backend trace records (__module__)
    for name in legacy:
        if name != sym and name not in aliases:
            aliases.append(name)
    return aliases


def _resolve(path):
    obj = _walk(path)
    target = _target(obj)
    unwrapped = target is not obj
    if isinstance(target, types.ModuleType):
        return {"sym": target.__name__, "kind": "module", "aliases": []}
    if inspect.isclass(target):
        sym = _class_sym(target)
        legacy = []
        for func in _class_functions(target):
            name = _legacy(func)
            if name and name not in legacy and len(legacy) < _MAX_ALIASES:
                legacy.append(name)
        return {"sym": sym, "kind": "class",
                "aliases": _aliases(sym, _qual(target), legacy), "legacy": legacy}
    attribute_sym = _qual(target)
    if inspect.isroutine(target) or (callable(target) and attribute_sym is not None):
        sym = _function_sym(target)
        legacy = [name for name in [_legacy(target)] if name]
        result = {"sym": sym, "kind": "function",
                  "aliases": _aliases(sym, attribute_sym, legacy), "legacy": legacy}
        if unwrapped:
            result["unwrapped"] = True
        return result
    return {"sym": None, "kind": "other", "aliases": [], "type": _qual(type(target))}


def _emit(out, payload):
    out.write(_MARK + json.dumps(payload) + "\n")
    out.flush()


def main():
    out = os.fdopen(os.dup(1), "w")
    try:
        os.dup2(2, 1)  # anything an import prints goes to stderr, not the protocol
    except Exception:
        pass
    sys.stdout = sys.stderr
    request = json.loads(sys.stdin.read() or "{}")
    try:
        sys.stdin = open(os.devnull)
    except Exception:
        pass
    root = request.get("root")
    if root and root not in sys.path:
        sys.path.insert(0, root)
    per_path = float(request.get("per_path_timeout") or 0)
    use_alarm = (per_path > 0 and hasattr(signal, "setitimer")
                 and threading.current_thread() is threading.main_thread())
    if use_alarm:
        signal.signal(signal.SIGALRM, _on_alarm)
    site_dirs = []
    for entry in sys.path:
        if entry and os.path.basename(entry.rstrip(os.sep)) in ("site-packages", "dist-packages"):
            if os.path.isdir(entry) and entry not in site_dirs:
                site_dirs.append(entry)
    _emit(out, {"meta": {"python": "{0}.{1}.{2}".format(*sys.version_info[:3]),
                         "executable": sys.executable, "site_dirs": site_dirs}})
    for path in request.get("paths") or []:
        try:
            if use_alarm:
                signal.setitimer(signal.ITIMER_REAL, per_path)
            try:
                result = _resolve(path)
            finally:
                if use_alarm:
                    signal.setitimer(signal.ITIMER_REAL, 0)
        except _Timeout:
            result = {"error": "timeout: resolving took longer than {0}s".format(per_path),
                      "transient": True}
        except BaseException as exc:  # a module may raise anything, SystemExit included
            try:
                detail = str(exc)
            except Exception:
                detail = "<unprintable>"
            result = {"error": "{0}: {1}".format(type(exc).__name__, detail)[:300]}
        _emit(out, {"path": path, "result": result})
    _emit(out, {"done": True})
    os._exit(0)  # never wait on threads or atexit hooks a dependency started


main()
'''


class ResolvedIdentity(BaseModel):
    """What one static import path is at runtime (or why that is unknown)."""

    sym: str | None = None  # canonical "<defining module>.<qualname>"
    kind: str | None = None  # function | class | module | other
    # Other names the runtime may record for the same object: the ``__module__``
    # spelling (wrap backend) and ``legacy`` below.
    aliases: list[str] = Field(default_factory=list)
    # v1 names (``<frame module>.<co_name>``) that occur once in the defining
    # module, so a v1 ``q`` carrying one denotes this object and nothing else.
    legacy: list[str] = Field(default_factory=list)
    unwrapped: bool = False  # a decorator wrapper was followed to the inner function
    error: str | None = None
    transient: bool = False  # timeout/crash: not cached, may succeed next time

    @property
    def resolved(self) -> bool:
        return self.error is None and self.sym is not None


class IdentityResolution(BaseModel):
    interpreter: str
    interpreter_source: str  # probe-python | trace-metadata | host
    python: str | None = None  # x.y.z reported by the resolving interpreter
    results: dict[str, ResolvedIdentity] = Field(default_factory=dict)
    from_cache: int = 0
    notes: list[str] = Field(default_factory=list)

    @property
    def resolved(self) -> int:
        return sum(1 for identity in self.results.values() if identity.resolved)


class InterpreterChoice(BaseModel):
    executable: str
    source: str  # probe-python | trace-metadata | host | runner | runner-unavailable
    note: str | None = None
    # Set when the capture's interpreter lives in a container: the probe runs there
    # (``executable`` is then the path *inside* it).
    runner: RunnerSpec | None = None


def _trace_processes(trace: str | Path) -> list[dict]:
    try:
        with open(str(trace) + ".meta.json", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        return []
    processes = payload.get("processes") if isinstance(payload, dict) else None
    return [p for p in processes if isinstance(p, dict)] if isinstance(processes, list) else []


def _runnable(executable: str) -> bool:
    return os.path.isfile(executable) and os.access(executable, os.X_OK)


def select_interpreter(
    probe_python: str | None = None, trace: str | Path | None = None
) -> InterpreterChoice:
    """Explicit ``probe_python`` > the capture's container runner > the trace's recorded
    interpreter > ``sys.executable``.

    A trace captured in a container resolves *in that container* (``source="runner"``). When
    the runner is recorded but not running, the choice is ``runner-unavailable``: identities
    are then left unresolved with a note, never resolved against the host's different
    packages."""
    if probe_python:
        found = probe_python if os.sep in probe_python else shutil.which(probe_python)
        return InterpreterChoice(executable=found or probe_python, source="probe-python")
    # Per interpreter, the events its processes recorded: when a workload spawns
    # other Pythons (tests probing interpreters), the one that produced the trace's
    # bulk is the one whose dependency versions the events describe.
    recorded: dict[str, int] = {}
    if trace is not None:
        for process in _trace_processes(trace):
            executable = process.get("executable")
            if not isinstance(executable, str) or not executable:
                continue
            counters = process.get("counters")
            events = counters.get("events_written") if isinstance(counters, dict) else None
            recorded[executable] = recorded.get(executable, 0) + (
                events if isinstance(events, int) and events > 0 else 0
            )
    ranked = sorted(recorded, key=lambda exe: -recorded[exe])  # stable: ties keep order
    runner_choice = _runner_choice(trace, ranked[0] if ranked else None)
    if runner_choice is not None:
        return runner_choice
    for executable in ranked:
        if _runnable(executable):
            distinct = {os.path.realpath(exe) for exe in recorded}
            note = None
            if len(distinct) > 1:
                among = "" if executable == ranked[0] else " of those runnable here"
                note = (
                    f"the capture ran {len(distinct)} interpreters; static identities were "
                    f"resolved under {executable}, which recorded the most events{among}"
                )
            return InterpreterChoice(executable=executable, source="trace-metadata", note=note)
    if recorded:
        note = (
            f"the capture's interpreter ({ranked[0]}) is not runnable here (container "
            f"capture?); static identities resolved under pymolt's own interpreter "
            f"({sys.executable}) — dependency versions may differ from the capture's"
        )
    else:
        note = (
            f"resolved under pymolt's own interpreter ({sys.executable}): the trace records "
            "no capture interpreter, so dependency versions may differ from the capture's"
        )
    return InterpreterChoice(executable=sys.executable, source="host", note=note)


def _runner_choice(trace: str | Path | None, executable: str | None) -> InterpreterChoice | None:
    """The container choice for a trace captured in one; ``None`` for a host capture."""
    if trace is None:
        return None
    from pymolt.verify.facts import runner_for_trace

    spec, origin = runner_for_trace(trace)
    if spec is None:
        return None
    # A config-level runner says where the project's container is, not that this trace ran
    # there: it counts only when the recorded interpreter does not exist on this host.
    if origin != "slot" and executable and _runnable(executable):
        return None
    exe = executable or spec.executable
    usable, why = runner_available(spec)
    if not usable:
        return InterpreterChoice(
            executable=exe, source="runner-unavailable", runner=spec,
            note=(f"the capture ran in container {spec.name} and it is not available "
                  f"({why}): static identities are left unresolved rather than resolved "
                  "against the host's packages"))
    return InterpreterChoice(
        executable=exe, source="runner", runner=spec,
        note=f"static identities resolved inside container {spec.name} ({exe})")


# ─── cache ────────────────────────────────────────────────────────────────────


def _mtime(path: str) -> int | None:
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


def _cache_file(root: Path, interpreter: str) -> Path:
    digest = hashlib.sha256(os.path.abspath(interpreter).encode("utf-8")).hexdigest()[:16]
    return root / ".pymolt" / "cache" / f"identity-{digest}.json"


def _manifest_stamps(root: Path) -> dict[str, int | None]:
    names = list(_MANIFESTS)
    try:
        names += sorted(p.name for p in root.glob("requirements*.txt"))
    except OSError:
        pass
    return {
        f"manifest:{name}": _mtime(str(root / name))
        for name in names
        if (root / name).is_file()
    }


def _stamps(root: Path, interpreter: str, site_dirs: Iterable[str]) -> dict[str, int | None]:
    stamps: dict[str, int | None] = {
        f"interpreter:{os.path.realpath(interpreter)}": _mtime(os.path.realpath(interpreter))
    }
    for site_dir in site_dirs:
        stamps[f"site:{site_dir}"] = _mtime(site_dir)
    stamps.update(_manifest_stamps(root))
    return stamps


def _load_cache(root: Path, interpreter: str) -> dict | None:
    """The cached entry when every stamp still holds; otherwise ``None``."""
    path = _cache_file(root, interpreter)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("probe_version") != PROBE_VERSION:
        return None
    if payload.get("interpreter") != os.path.abspath(interpreter):
        return None
    stamps = payload.get("stamps")
    site_dirs = payload.get("site_dirs")
    results = payload.get("results")
    if not isinstance(stamps, dict) or not isinstance(site_dirs, list):
        return None
    if not isinstance(results, dict):
        return None
    if _stamps(root, interpreter, site_dirs) != stamps:
        return None
    return payload


def _write_cache(
    root: Path,
    interpreter: str,
    python: str | None,
    site_dirs: list[str],
    results: dict[str, ResolvedIdentity],
) -> None:
    path = _cache_file(root, interpreter)
    payload = {
        "probe_version": PROBE_VERSION,
        "interpreter": os.path.abspath(interpreter),
        "python": python,
        "site_dirs": site_dirs,
        "stamps": _stamps(root, interpreter, site_dirs),
        "results": {
            name: identity.model_dump(exclude_defaults=True)
            for name, identity in sorted(results.items())
            if not identity.transient
        },
    }
    try:
        atomic_write_json(path, payload, indent=None, sort_keys=True)
    except OSError as exc:  # a read-only project still gets its answer, just uncached
        logger.debug("identity cache not written (%s): %s", path, exc)


# ─── subprocess ───────────────────────────────────────────────────────────────


def _child_env() -> dict[str, str]:
    # Never let an injected watcher (sitecustomize on PYTHONPATH + PYMOLT_TRACE_*)
    # activate inside the resolver: it would trace the imports we trigger.
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYMOLT_TRACE")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def _run_probe(
    interpreter: str,
    root: Path,
    paths: list[str],
    timeout: float,
    per_path_timeout: float,
    runner: RunnerSpec | None = None,
) -> tuple[dict, dict[str, ResolvedIdentity], list[str]]:
    """(meta, results, notes); paths the child never answered are transient errors."""
    notes: list[str] = []
    root_arg: str | None = str(root)
    if runner is not None:
        root_arg = to_container(runner, str(root))
        if root_arg is None:
            failure = f"{root} is not mounted into container {runner.name}"
            notes.append(failure)
            return {}, {p: ResolvedIdentity(error=failure, transient=True) for p in paths}, notes
    request = json.dumps(
        {"root": root_arg, "paths": paths, "per_path_timeout": per_path_timeout}
    )
    stdout = ""
    stderr = ""
    failure: str | None = None
    if runner is not None:
        try:
            proc = run_in(runner, [interpreter, "-B", "-c", _PROBE_SOURCE], stdin=request,
                          cwd=root_arg, timeout=timeout)
            stdout, stderr = proc.stdout or "", proc.stderr or ""
            if proc.returncode not in (0, None):
                failure = f"identity resolver exited with code {proc.returncode}"
        except subprocess.TimeoutExpired:
            failure = f"identity resolver timed out after {timeout:g}s"
        except OSError as exc:
            failure = f"could not run {interpreter} in {runner.name}: {exc}"
    else:
        try:
            proc = subprocess.Popen(
                [interpreter, "-B", "-c", _PROBE_SOURCE],
                cwd=str(root),
                env=_child_env(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as exc:
            failure = f"could not start {interpreter}: {exc}"
        else:
            try:
                stdout, stderr = proc.communicate(request, timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout, stderr = proc.communicate()
                failure = f"identity resolver timed out after {timeout:g}s"
            if failure is None and proc.returncode not in (0, None):
                failure = f"identity resolver exited with code {proc.returncode}"

    meta: dict = {}
    results: dict[str, ResolvedIdentity] = {}
    done = False
    for line in (stdout or "").splitlines():
        if not line.startswith(_MARK):
            continue
        try:
            payload = json.loads(line[len(_MARK):])
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        if isinstance(payload.get("meta"), dict):
            meta = payload["meta"]
        elif payload.get("done"):
            done = True
        elif isinstance(payload.get("path"), str) and isinstance(payload.get("result"), dict):
            raw = payload["result"]
            try:
                results[payload["path"]] = ResolvedIdentity(
                    sym=raw.get("sym") if isinstance(raw.get("sym"), str) else None,
                    kind=raw.get("kind") if isinstance(raw.get("kind"), str) else None,
                    aliases=[a for a in raw.get("aliases") or [] if isinstance(a, str)],
                    legacy=[a for a in raw.get("legacy") or [] if isinstance(a, str)],
                    unwrapped=bool(raw.get("unwrapped")),
                    error=str(raw["error"])[:_MAX_ERROR] if raw.get("error") else None,
                    transient=bool(raw.get("transient")),
                )
            except (TypeError, ValueError):
                continue

    if failure is None and not done:
        failure = "identity resolver stopped before finishing"
    if failure is not None:
        tail = (stderr or "").strip().splitlines()[-1:] or [""]
        detail = f" ({tail[0][:200]})" if tail[0] else ""
        notes.append(f"{failure}{detail}")
        for path in paths:
            if path not in results:
                results[path] = ResolvedIdentity(error=failure, transient=True)
    return meta, results, notes


def resolve_identities(
    paths: Iterable[str],
    project_dir: str | Path,
    interpreter: InterpreterChoice | str | None = None,
    *,
    timeout: float = 120.0,
    per_path_timeout: float = 15.0,
    use_cache: bool = True,
) -> IdentityResolution:
    """Resolve static import paths in the capture's interpreter. Never raises."""
    root = Path(project_dir).resolve()
    if interpreter is None:
        choice = select_interpreter()
    elif isinstance(interpreter, str):
        choice = InterpreterChoice(executable=interpreter, source="probe-python")
    else:
        choice = interpreter
    resolution = IdentityResolution(
        interpreter=choice.executable, interpreter_source=choice.source
    )
    if choice.note:
        resolution.notes.append(choice.note)
    wanted = sorted({p.strip() for p in paths if isinstance(p, str) and p.strip()})
    if not wanted:
        return resolution
    if choice.source == "runner-unavailable":
        for name in wanted:
            resolution.results[name] = ResolvedIdentity(
                error="the capture's container is not available", transient=True)
        return resolution
    use_cache = use_cache and choice.runner is None  # a container's packages change unseen

    try:
        cached: dict[str, ResolvedIdentity] = {}
        payload = _load_cache(root, choice.executable) if use_cache else None
        if payload is not None:
            resolution.python = payload.get("python")
            for name, raw in payload["results"].items():
                if not isinstance(raw, dict):
                    continue
                try:
                    cached[name] = ResolvedIdentity.model_validate(raw)
                except ValueError:
                    continue
        todo = [p for p in wanted if p not in cached]
        resolution.from_cache = len(wanted) - len(todo)
        fresh: dict[str, ResolvedIdentity] = {}
        meta: dict = {}
        if todo:
            extra = {"runner": choice.runner} if choice.runner is not None else {}
            meta, fresh, notes = _run_probe(
                choice.executable, root, todo, timeout, per_path_timeout, **extra
            )
            resolution.notes.extend(notes)
            if isinstance(meta.get("python"), str):
                resolution.python = meta["python"]
        for name in wanted:
            identity = cached.get(name) or fresh.get(name)
            if identity is None:  # defensive: the child skipped a path
                identity = ResolvedIdentity(error="not resolved", transient=True)
            resolution.results[name] = identity
        if use_cache and fresh and meta:
            site_dirs = [d for d in meta.get("site_dirs") or [] if isinstance(d, str)]
            merged = {**cached, **fresh} if payload is not None else dict(fresh)
            _write_cache(root, choice.executable, resolution.python, site_dirs, merged)
    except Exception as exc:  # noqa: BLE001 — identity is an enhancement, never a crash
        logger.warning("identity resolution failed: %s", exc)
        resolution.notes.append(f"identity resolution failed: {exc}")
        for name in wanted:
            resolution.results.setdefault(
                name, ResolvedIdentity(error=f"identity resolution failed: {exc}", transient=True)
            )
    return resolution
