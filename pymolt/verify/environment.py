"""The runtime environment two captures ran in, compared (host side).

HOST side (>=3.12). Never imported by the injected runtime, which records the raw facts
(``boundary_tracer._environment``: one ``environment`` record per process, read before tracing
starts). Here they become **aspects**, compared between the two captures:

* ``os`` (system), ``release``, ``machine``, ``container`` (a hint, ``host`` when none);
* ``privilege`` (uid, euid, Linux effective capabilities), ``rlimit:<NAME>`` (soft, hard);
* ``cpus`` (count, affinity), ``tmpdir``, ``utc_offset``, ``encoding``.

The Python version is never an aspect (it is the migration itself) and the hash seed is not
either (``hash_info``: only the dict-order rules read it). A side is the multiset of its
processes' records: an aspect differs when the two sides hold other values for it, or when a
side's processes disagree on it and the two multisets are not the same (which process made a
call is not recorded, so then it cannot be told apart). Identical multisets are the same
environment, however many processes disagree within a side. A side without a record for every
process is **unknown**: then nothing here applies and every rule stays strict.

Which calls read an aspect comes from the curated ``pymolt/knowledge/environment_readers.json``
(``reads``), plus evidence the facts carry (a platform-tagged type name, ``platform_neutral``).
"""
from __future__ import annotations

import fnmatch
import json
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from functools import cache
from pathlib import Path
from typing import Any

ASPECTS = ("os", "release", "machine", "container", "privilege", "rlimit", "cpus", "tmpdir",
           "utc_offset", "encoding")
INF = "inf"
# RLIM_INFINITY as the C library spells it (Linux: -1 as unsigned 64 bit; macOS: 2**63 - 1).
_RLIM_INFINITY = frozenset({-1, 2 ** 63 - 1, 2 ** 64 - 1})
# resource.RLIMIT_* numbers per platform (the input of ``resource.getrlimit(R)``)
_RLIMIT_NUMBERS = {
    "linux": {0: "CPU", 1: "FSIZE", 2: "DATA", 3: "STACK", 4: "CORE", 5: "RSS", 6: "NPROC",
              7: "NOFILE", 8: "MEMLOCK", 9: "AS"},
    "darwin": {0: "CPU", 1: "FSIZE", 2: "DATA", 3: "STACK", 4: "CORE", 5: "AS", 6: "MEMLOCK",
               7: "NPROC", 8: "NOFILE"},
}
# setprofile below 3.12 cannot see a C exception's type; these are what an environment causes
_ENV_RAISES = frozenset({
    "<unknown>", "OSError", "IOError", "EnvironmentError", "PermissionError",
    "FileNotFoundError", "FileExistsError", "IsADirectoryError", "NotADirectoryError",
    "BlockingIOError", "ChildProcessError", "ConnectionError", "BrokenPipeError",
    "ConnectionAbortedError", "ConnectionRefusedError", "ConnectionResetError",
    "InterruptedError", "ProcessLookupError", "TimeoutError", "socket.error", "select.error",
    "resource.error", "ValueError", "NotImplementedError", "AttributeError", "ImportError",
    "ModuleNotFoundError",
})


def env_raise(name: str) -> bool:
    """A raise type an environment difference can cause (never TypeError, KeyError, ...)."""
    name = name[len("builtins."):] if name.startswith("builtins.") else name
    return name in _ENV_RAISES


# ── aspects ───────────────────────────────────────────────────────────────────────────────


def _canon(v: Any) -> str:
    return json.dumps(v, sort_keys=True, separators=(",", ":"), default=str)


def process_aspects(rec: Mapping[str, Any]) -> dict[str, Any]:
    """One process's environment record as ``{aspect: value}``."""
    out: dict[str, Any] = {
        "os": rec.get("system") or rec.get("platform"),
        "release": rec.get("release"),
        "machine": rec.get("machine"),
        "container": rec.get("container") or "host",
        "privilege": {"uid": rec.get("uid"), "euid": rec.get("euid"), "caps": rec.get("caps")},
        "cpus": {"count": rec.get("cpus"), "affinity": rec.get("affinity")},
        "tmpdir": rec.get("tmpdir"),
        "utc_offset": rec.get("utc_offset"),
        "encoding": rec.get("encoding"),
    }
    rl = rec.get("rlimits")
    if isinstance(rl, Mapping):
        for name, value in rl.items():
            out[f"rlimit:{name}"] = value
    return out


def side_aspects(envs: Sequence[Mapping[str, Any]] | None) -> dict[str, Counter] | None:
    """Every aspect's values (canonical JSON) over one side's processes, as a multiset; None
    when the side is unknown (no process recorded, or one did not)."""
    if not envs or not all(isinstance(e, Mapping) for e in envs):
        return None
    out: dict[str, Counter] = {}
    for rec in envs:
        for aspect, value in process_aspects(rec).items():
            out.setdefault(aspect, Counter())[_canon(value)] += 1
    n = len(envs)
    for counter in out.values():   # a process without the aspect holds None for it
        missing = n - sum(counter.values())
        if missing:
            counter[_canon(None)] += missing
    return out


def _label(aspect: str, values: Iterable[str]) -> str:
    shown = []
    for raw in sorted(values):
        v = json.loads(raw)
        if aspect == "privilege" and isinstance(v, dict):
            text = f"uid {v.get('uid')}" + (f"/euid {v.get('euid')}"
                                             if v.get("euid") != v.get("uid") else "")
            if v.get("caps"):
                text += f" caps {v['caps']}"
            shown.append(text)
        elif aspect == "cpus" and isinstance(v, dict):
            shown.append(str(v.get("count")) + (f" (affinity {v['affinity']})"
                                                if v.get("affinity") not in (None, v.get("count"))
                                                else ""))
        elif aspect.startswith("rlimit:") and isinstance(v, list) and len(v) == 2:
            shown.append(f"soft {v[0]}, hard {v[1]}")
        elif isinstance(v, dict):
            shown.append(", ".join(f"{k} {x}" for k, x in sorted(v.items()) if x is not None))
        else:
            shown.append("unknown" if v is None else str(v))
    return " | ".join(shown) if len(shown) > 1 else (shown[0] if shown else "unknown")


class EnvComparison:
    """The two captures' environments: ``differs`` = ``{aspect: (old label, new label)}``."""

    def __init__(self, old: dict[str, Counter], new: dict[str, Counter],
                 old_envs: Sequence[Mapping[str, Any]], new_envs: Sequence[Mapping[str, Any]]):
        self.old, self.new = old, new
        self.old_envs, self.new_envs = list(old_envs), list(new_envs)
        self.differs: dict[str, tuple[str, str]] = {}
        none = Counter({_canon(None): 1})
        for aspect in sorted(old.keys() | new.keys(), key=_aspect_rank):
            a, b = old.get(aspect, none), new.get(aspect, none)
            sa, sb = set(a), set(b)
            if sa != sb or ((len(sa) > 1 or len(sb) > 1) and a != b):
                self.differs[aspect] = (_label(aspect, sorted(sa)), _label(aspect, sorted(sb)))

    @property
    def same(self) -> bool:
        return not self.differs

    def differ(self, aspect: str) -> bool:
        """Whether ``aspect`` differs; ``rlimit`` stands for any ``rlimit:<NAME>``."""
        if aspect == "rlimit":
            return any(a.startswith("rlimit:") for a in self.differs)
        return aspect in self.differs

    def matching(self, aspects: Iterable[str]) -> list[str]:
        """The differing aspects among ``aspects`` (``rlimit`` expanded)."""
        out: list[str] = []
        for a in aspects:
            if a == "rlimit":
                out.extend(x for x in self.differs if x.startswith("rlimit:"))
            elif a in self.differs:
                out.append(a)
        return list(dict.fromkeys(out))

    def describe(self, aspects: Iterable[str]) -> str:
        return ", ".join(f"{a} {self.differs[a][0]} -> {self.differs[a][1]}" for a in aspects)


def _aspect_rank(aspect: str) -> tuple[int, str]:
    base = aspect.split(":", 1)[0]
    return (ASPECTS.index(base) if base in ASPECTS else len(ASPECTS), aspect)


def compare(old_envs: Sequence[Mapping[str, Any]] | None,
            new_envs: Sequence[Mapping[str, Any]] | None) -> EnvComparison | None:
    """The two sides' environments compared; None when either is unknown."""
    a, b = side_aspects(old_envs), side_aspects(new_envs)
    if a is None or b is None:
        return None
    return EnvComparison(a, b, old_envs or (), new_envs or ())


def summary_text(envs: Sequence[Mapping[str, Any]] | None) -> str:
    """``linux 6.10.14-linuxkit aarch64, docker, uid 0`` (the first process; ``+N`` more)."""
    if not envs:
        return "unknown"
    rec = envs[0]
    parts = [" ".join(str(x) for x in (rec.get("platform"), rec.get("release"),
                                         rec.get("machine")) if x),
             rec.get("container") or "host", f"uid {rec.get('euid', rec.get('uid'))}"]
    text = ", ".join(p for p in parts if p)
    return text + (f" (+{len(envs) - 1} process(es))" if len(envs) > 1 else "")


def stamp(cmp: EnvComparison | None, old_envs: Any, new_envs: Any, *, reviews: int = 0,
          explained: int = 0, inferred: str | None = None) -> Any:
    """The report's ``quality.environment``: "same", the differences, or "not recorded"."""
    if cmp is None:
        missing = [side for side, envs in (("old", old_envs), ("new", new_envs))
                   if side_aspects(envs) is None]
        text = f"not recorded ({', '.join(missing) or 'old, new'})"
        return f"{text}; {inferred}" if inferred else text
    if cmp.same:
        return "same"
    return {"old": summary_text(cmp.old_envs), "new": summary_text(cmp.new_envs),
            "differs": list(cmp.differs), "reviews": reviews, "explained": explained,
            "comparable": False}


# ── hash seed (the dict-order rules) ──────────────────────────────────────────────────────


def hash_randomized(envs: Sequence[Mapping[str, Any]] | None) -> bool | None:
    """True when every process hashed str with a random seed, False when any had a fixed one,
    None when unknown."""
    if not envs:
        return None
    out = True
    for rec in envs:
        h = rec.get("hash") if isinstance(rec, Mapping) else None
        if not isinstance(h, Mapping) or not isinstance(h.get("randomized"), bool):
            return None
        out = out and h["randomized"]
    return out


def hash_pinned(envs: Sequence[Mapping[str, Any]] | None) -> tuple[int, str] | None:
    """``(seed, algorithm)`` when every process ran with the same fixed PYTHONHASHSEED."""
    seen = set()
    for rec in envs or ():
        h = rec.get("hash") if isinstance(rec, Mapping) else None
        if not isinstance(h, Mapping) or not isinstance(h.get("seed"), int) \
                or h.get("randomized") is not False:
            return None
        seen.add((h["seed"], str(h.get("algorithm"))))
    return seen.pop() if len(seen) == 1 else None


# ── readers ───────────────────────────────────────────────────────────────────────────────


@cache
def _readers() -> tuple[dict[str, Any], ...]:
    try:
        from pymolt.knowledge import environment_readers
        return tuple(e.model_dump() if hasattr(e, "model_dump") else dict(e)
                     for e in environment_readers())
    except ImportError:  # a knowledge package without the loader: read the file itself
        path = Path(__file__).resolve().parents[1] / "knowledge" / "environment_readers.json"
        return tuple(json.loads(path.read_text(encoding="utf-8"))["entries"])


def _matches(symbol: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatchcase(symbol, p) for p in patterns)


def _low_port(inputs: Any) -> bool:
    """An address tuple ``(host, port)`` with a port below 1024 anywhere in the input."""
    if isinstance(inputs, dict):
        t = inputs.get("__tuple__")
        if isinstance(t, list) and len(t) >= 2 and isinstance(t[1], int) \
                and not isinstance(t[1], bool) and 0 <= t[1] < 1024:
            return True
        return any(_low_port(x) for x in inputs.values())
    if isinstance(inputs, list):
        return any(_low_port(x) for x in inputs)
    return False


def reader_aspects(symbol: str, inputs: Any = None) -> set[str]:
    """The aspects a call reads per the curated list (``rlimit`` for any rlimit)."""
    out: set[str] = set()
    for entry in _readers():
        if not _matches(symbol, entry.get("symbols") or ()):
            continue
        if entry.get("when") == "port<1024" and not _low_port(inputs):
            continue
        out.update(entry.get("aspects") or ())
    return out


def reading_kind(symbol: str) -> str | None:
    """Which recorded reading a call returns (``cpus``, ``uid``, ``rlimit``...), or None."""
    for entry in _readers():
        kind = (entry.get("readings") or {}).get(symbol)
        if kind:
            return kind
    return None


def _rlimit_name(rec: Mapping[str, Any], inputs: Any) -> str | None:
    number = None
    if isinstance(inputs, dict):
        bound = inputs.get("bound")
        if isinstance(bound, dict) and isinstance(bound.get("resource"), int):
            number = bound["resource"]
        args = inputs.get("args")
        if number is None and isinstance(args, list) and args and isinstance(args[0], int):
            number = args[0]
    if number is None or isinstance(number, bool):
        return None
    return _RLIMIT_NUMBERS.get(str(rec.get("platform")), {}).get(number)


def _inf(v: Any) -> Any:
    return INF if isinstance(v, int) and not isinstance(v, bool) and v in _RLIM_INFINITY else v


def is_own_reading(symbol: str, inputs: Any, value: Any,
                   envs: Sequence[Mapping[str, Any]]) -> bool:
    """Whether a returned (normalized) value is this side's own recorded reading of the
    aspect the call reads: ``os.cpu_count()`` -> its ``cpus``, ``resource.getrlimit(R)`` -> its
    ``rlimits[R]``, ``platform.system()`` -> its ``system``, ``os.getuid()`` -> its ``uid``."""
    kind = reading_kind(symbol)
    if kind is None:
        return False
    for rec in envs:
        if kind == "cpus":
            want: Any = rec.get("cpus")
            if want is not None and value == want:
                return True
        elif kind in ("uid", "euid", "system", "machine", "release"):
            want = rec.get(kind)
            if want is not None and value == want:
                return True
        elif kind == "rlimit":
            name = _rlimit_name(rec, inputs)
            want = (rec.get("rlimits") or {}).get(name) if name else None
            t = value.get("__tuple__") if isinstance(value, dict) else None
            if isinstance(want, list) and isinstance(t, list) and len(t) == 2 \
                    and [_inf(x) for x in t] == list(want):
                return True
    return False


# ── evidence ──────────────────────────────────────────────────────────────────────────────

_PS_OS = re.compile(r"^_ps(?:osx|linux|bsd|sunos|aix|windows)$")
_OS_SEGMENT = re.compile(r"^(?:posix|nt)(path)?$|^(?:linux|darwin|win32|windows|osx|macos)$")


def platform_neutral(name: str) -> str:
    """A type name with its platform-tagged segments replaced: ``psutil._psosx.pmem`` and
    ``psutil._pslinux.pmem`` -> ``psutil._ps⟨os⟩.pmem``; ``posixpath`` / ``ntpath``."""
    out = []
    for seg in name.split("."):
        if _PS_OS.match(seg):
            out.append("_ps⟨os⟩")
        else:
            m = _OS_SEGMENT.match(seg)
            out.append(("⟨os⟩" + (m.group(1) or "")) if m else seg)
    return ".".join(out)


def less_privileged(envs_a: Sequence[Mapping[str, Any]],
                    envs_b: Sequence[Mapping[str, Any]]) -> bool | None:
    """Whether side A ran with less privilege than side B (non-root vs root, or a strict
    subset of Linux effective capabilities); None when it cannot tell."""
    def level(envs: Sequence[Mapping[str, Any]]) -> tuple[bool | None, int | None]:
        roots = {rec.get("euid") == 0 for rec in envs if rec.get("euid") is not None}
        caps = set()
        for rec in envs:
            try:
                caps.add(int(str(rec.get("caps")), 16))
            except (TypeError, ValueError):
                pass
        return (roots.pop() if len(roots) == 1 else None,
                caps.pop() if len(caps) == 1 else None)

    (ra, ca), (rb, cb) = level(envs_a), level(envs_b)
    if ra is None or rb is None:
        return None
    if ra != rb:
        return rb and not ra
    if ca is not None and cb is not None and ca != cb:
        return (ca & cb) == ca
    return None


def tokens(cmp: EnvComparison | None, side: int) -> dict[str, str]:
    """One side's environment tokens to mask (``⟨env:<aspect>⟩``), for the aspects that differ
    between the captures: OS name (system and ``sys.platform``), kernel release and machine as
    whole tokens of 5+ characters, and the tmpdir path."""
    if cmp is None or cmp.same:
        return {}
    envs = cmp.old_envs if side == 0 else cmp.new_envs
    out: dict[str, str] = {}
    for rec in envs:
        for aspect, fields in (("os", ("system", "platform")), ("release", ("release",)),
                               ("machine", ("machine",)), ("tmpdir", ("tmpdir",))):
            if not cmp.differ(aspect):
                continue
            for f in fields:
                v = rec.get(f)
                if not isinstance(v, str):
                    continue
                v = v.rstrip("/") if aspect == "tmpdir" else v
                if len(v) >= 5:
                    out.setdefault(v, f"⟨env:{aspect}⟩")
    return out
