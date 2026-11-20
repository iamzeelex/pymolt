"""Opt-in (``online=True``) successor hints from intermediate releases on PyPI.

For a symbol present in ``from`` and gone in ``to`` with no local hint, the release that
last shipped it usually carries its final deprecation prose ("'flask.Markup' is deprecated
... Import 'markupsafe.Markup' instead."). This module finds that release and reads ONE
module file out of its wheel with HTTP range requests (the zip central directory, then
the member), so only package names and versions leave the machine.

Order is newest patch of each minor series first, then every other release newest first:
bisection is wrong because histories are not monotonic (flask 2.3.0 dropped
``flask.Markup`` by accident and 2.3.1 restored it as a deprecated alias).

Nothing here runs unless the engine is called with ``online=True``. Release JSON, fetched
module files and the resulting hints are cached under ``.pymolt/cache/delta/``.
"""

from __future__ import annotations

import ast
import json
import re
import struct
import time
import zlib
from pathlib import Path
from typing import Any

from packaging.version import InvalidVersion, Version

from pymolt.state_io import atomic_write_bytes, atomic_write_text

MAX_RELEASES = 8
_TAIL = 65536

_ID = r"([A-Za-z_][\w\.]*)"
_Q = r"['\"`]?"
_ROLE = r"(?::\w+:)?`?~?"

# Successor prose, most specific first. Each pattern captures the successor name once.
SUCCESSOR_RES = [
    re.compile(r"[Ii]mport\s+['\"`]?([A-Za-z_][\w\.]*)['\"`]?\s+instead"),
    re.compile(r"[Uu]se\s+:\w+:`~?([A-Za-z_][\w\.]*)`"),
    re.compile(r"[Uu]se\s+['\"`]?([A-Za-z_][\w\.\(\)]*)['\"`]?\s+(?:instead|to )"),
    re.compile(r"[Ii]mport (?:them|it) directly from\s+['\"`]?([\w\.]+)"),
    re.compile(r"[Ss]ubclass(?:ing)?\s+['\"`]?([A-Za-z_][\w\.]*)['\"`]?"),
    # "use `X`" / "Please use 'X'" with the name quoted: any continuation
    re.compile(r"\b[Uu]se\s+(?:the\s+)?[`'\"]~?@?" + _ID + r"(?:\(\))?[`'\"]"),
    # "is renamed to 'X'", "has been renamed to X", "was renamed to `X`"
    re.compile(r"\brenamed\s+to\s+" + _ROLE + _Q + _ID),
    re.compile(r"\bmoved\s+to\s+" + _ROLE + _Q + _ID),
    # "deprecated in favor of X", "in favour of `X`"
    re.compile(r"\bin\s+favou?r\s+of\s+" + _ROLE + _Q + _ID),
    re.compile(r"\b(?:replaced|superseded)\s+(?:by|with)\s+" + _ROLE + _Q + _ID),
    re.compile(r"\b[Mm]igrate\s+to\s+(?:[\w ]+?style\s+)?`@?" + _ID + r"`"),
    # bare "use X instead" / "Please use X." with a name that looks like code
    re.compile(r"\b[Uu]se\s+" + _Q + r"([A-Za-z_][\w]*(?:[_\.][\w\.]*|\(\)))" + _Q),
]

# Words the patterns can capture that are English, not a successor.
_STOPWORDS = frozenset("""
a an the this that these those it its them they their your you we our one ones any some each
either neither both other another same new old more less most least only also just now then
instead rather please note see here there which what when where while who whom whose why how
and or but nor not no yes if else of to in on at by for from with without as is are was were
be been being will would can could should may might must do does did has have had
version versions release releases python python2 python3 module function method class object
type value values name names argument arguments parameter parameters option options feature
api code docs documentation following above below example later earlier future
""".split())

# Generic verbs a "use X to <do something>" sentence puts in X's slot. Never a successor
# unless the prose quotes them as code (`apply`).
_DENYLIST = frozenset("""
operate apply dispatch call calls invoke execute run runs process handle perform evaluate
resolve fire emit trigger supply provide produce create build make get set pass wrap
convert transform generate compute construct implement override extend inherit define
declare register configure specify describe indicate represent return returns yield
""".split())


def _acceptable(found: str, quoted: bool) -> bool:
    name = found.strip("`'\"~").rstrip(".")
    while name.endswith("()"):
        name = name[:-2]
    if not name or not re.match(r"^[A-Za-z_][\w\.]*$", name):
        return False
    low = name.lower()
    if low in _STOPWORDS:
        return False
    codey = "_" in name or "." in name or any(c.isupper() for c in name[1:])
    if not quoted and not codey and low in _DENYLIST:
        return False
    if not quoted and not codey and name.islower() and len(name) < 3:
        return False
    return True


def successor_from(messages: list[str]) -> str | None:
    """The successor named by a deprecation message ("Use X instead", "renamed to X", "in favor
    of X", "replaced by X", "moved to X", "superseded by X", ...): the earliest acceptable
    mention in the first message that names one; stopwords and generic verbs never count."""
    for msg in messages:
        text = msg or ""
        hits: list[tuple[int, int, str]] = []
        for order, rx in enumerate(SUCCESSOR_RES):
            for m in rx.finditer(text):
                raw = m.group(1)
                quoted = text[max(0, m.start(1) - 1): m.start(1)] in "`'\""
                if _acceptable(raw, quoted):
                    hits.append((m.start(1), order, raw))
                    break
        if hits:
            _, _, raw = min(hits)
            found = raw.strip("`'\"~").rstrip(".")
            while found.endswith("()"):
                found = found[:-2]
            if found:
                return found
    return None


class FetchError(RuntimeError):
    """PyPI or a wheel host could not be read."""


class Fetcher:
    """HTTP access to PyPI release metadata and wheel members (lazy client)."""

    def __init__(self, cache_dir: Path, *, use_cache: bool = True, timeout: float = 30.0):
        self.dir = Path(cache_dir) / "pypi"
        self.use_cache = use_cache
        self.timeout = timeout
        self.bytes = 0
        self.requests = 0
        self._client = None

    def _http(self):
        if self._client is None:
            import httpx

            from pymolt import __version__

            self._client = httpx.Client(
                timeout=self.timeout, follow_redirects=True,
                headers={"User-Agent": f"pymolt/{__version__} (delta hints)"},
            )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def releases(self, dist: str) -> dict[str, list[dict]]:
        self.dir.mkdir(parents=True, exist_ok=True)
        f = self.dir / f"{_safe(dist)}.json"
        if not (self.use_cache and f.is_file()):
            r = self._http().get(f"https://pypi.org/pypi/{dist}/json")
            if r.status_code != 200:
                raise FetchError(f"PyPI answered {r.status_code} for {dist}")
            self.bytes += len(r.content)
            self.requests += 1
            atomic_write_bytes(f, r.content)
        return json.loads(f.read_text(encoding="utf-8")).get("releases") or {}

    def member(self, dist: str, version: str, files: list[dict], member: str) -> str | None:
        cached = self.dir / f"{_safe(dist)}-{_safe(version)}-{member.replace('/', '__')}"
        if self.use_cache and cached.is_file():
            return cached.read_text(encoding="utf-8")
        wheels = [x for x in files
                  if x.get("filename", "").endswith(".whl") and not x.get("yanked")]
        wheels.sort(key=lambda x: 0 if ("py3-none-any" in x["filename"]
                                        or "py2.py3" in x["filename"]) else 1)
        if not wheels:
            return None
        text = self._range_member(wheels[0]["url"], member)
        if text is not None:
            atomic_write_text(cached, text)
        return text

    def _get(self, url: str, start: int, end: int) -> bytes:
        r = self._http().get(url, headers={"Range": f"bytes={start}-{end}"})
        if r.status_code != 206:
            raise FetchError(f"range request not honoured ({r.status_code})")
        self.bytes += len(r.content)
        self.requests += 1
        return r.content

    def _range_member(self, url: str, member: str) -> str | None:
        head = self._http().head(url)
        self.requests += 1
        size = int(head.headers["content-length"])
        tail_start = max(0, size - _TAIL)
        tail = self._get(url, tail_start, size - 1)
        eocd = tail.rfind(b"PK\x05\x06")
        if eocd < 0:
            raise FetchError("no zip end-of-central-directory record in the tail")
        _, _, _, _, _, cd_size, cd_off, _ = struct.unpack("<IHHHHIIH", tail[eocd: eocd + 22])
        if cd_off >= tail_start:
            cd = tail[cd_off - tail_start: cd_off - tail_start + cd_size]
        else:
            cd = self._get(url, cd_off, cd_off + cd_size - 1)
        pos = 0
        while pos + 46 <= len(cd) and cd[pos: pos + 4] == b"PK\x01\x02":
            (_, _, _, _, method, _, _, _, csize, _, nlen, elen, clen, _, _, _, lho) = struct.unpack(
                "<IHHHHHHIIIHHHHHII", cd[pos: pos + 46]
            )
            name = cd[pos + 46: pos + 46 + nlen].decode("utf-8", "replace")
            if name == member:
                hdr = self._get(url, lho, lho + 30 + nlen + 512 + csize)
                lnlen, lelen = struct.unpack("<HH", hdr[26:30])
                start = 30 + lnlen + lelen
                data = hdr[start: start + csize]
                if len(data) < csize:
                    data = self._get(url, lho + start, lho + start + csize - 1)
                raw = zlib.decompress(data, -15) if method == 8 else data
                return raw.decode("utf-8", "replace")
            pos += 46 + nlen + elen + clen
        return None


def _safe(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text)


def _text(node) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(_text(v) or "{...}" for v in node.values)
    if isinstance(node, ast.BinOp):
        return (_text(node.left) or "") + (_text(node.right) or "")
    return None


def _scope(tree: ast.Module, chain: list[str]):
    """The AST node defining ``chain`` (``[Class, method]`` or ``[name]``), or None."""
    body = tree.body
    node = None
    for depth, leaf in enumerate(chain):
        node = None
        for item in body:
            if isinstance(item, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and (
                item.name == leaf
            ):
                node = item
            elif isinstance(item, (ast.Import, ast.ImportFrom)) and depth == len(chain) - 1:
                if any((a.asname or a.name.split(".")[-1]) == leaf for a in item.names):
                    node = item
            elif isinstance(item, (ast.Assign, ast.AnnAssign)):
                targets = item.targets if isinstance(item, ast.Assign) else [item.target]
                if any(getattr(t, "id", None) == leaf for t in targets):
                    node = item
            elif (
                isinstance(item, ast.FunctionDef) and item.name == "__getattr__" and depth == 0
                and any(isinstance(c, ast.Constant) and c.value == leaf for c in ast.walk(item))
            ):
                node = item
        if node is None:
            return None
        body = getattr(node, "body", [])
    return node


def present(src: str, chain: list[str]) -> bool:
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return False
    return _scope(tree, chain) is not None


def _warns(scope) -> list[str]:
    out = []
    for n in ast.walk(scope):
        if isinstance(n, ast.Call) and getattr(n.func, "attr", getattr(n.func, "id", "")) == "warn":
            t = _text(n.args[0]) if n.args else None
            if t:
                out.append(t)
    return out


def module_warnings(src: str, chain: list[str]) -> list[str]:
    """``warnings.warn`` prose that belongs to ``chain``: its own body, the ``__getattr__``
    branch serving it, else any module warning naming its leaf."""
    tree = ast.parse(src)
    leaf = chain[-1]
    msgs: list[str] = []
    node = _scope(tree, chain)
    if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
        if node.name == "__getattr__":
            for n in ast.walk(node):
                if isinstance(n, ast.If) and any(
                    isinstance(c, ast.Constant) and c.value == leaf for c in ast.walk(n.test)
                ):
                    for stmt in n.body:
                        msgs.extend(_warns(stmt))
        else:
            msgs.extend(_warns(node))
            doc = ast.get_docstring(node) or ""
            msgs.extend(line.strip() for line in doc.splitlines() if "eprecat" in line
                        or line.strip().lower().startswith("use "))
    if not msgs:
        msgs = [m for m in _warns(tree) if leaf in m]
    return msgs


def _import_member_candidates(member: str, node: ast.ImportFrom) -> list[str]:
    """Wheel members a ``from X import name`` in ``member`` may refer to."""
    parts = member.split("/")[:-1]
    if node.level:
        base = parts[: len(parts) - (node.level - 1)] if node.level > 1 else parts
    else:
        base = []
    mod = (node.module or "").split(".") if node.module else []
    path = "/".join([*base, *mod])
    if not path:
        return []
    return [path + ".py", path + "/__init__.py"]


def _followed_warnings(fetcher: Fetcher, dist: str, version: str, files: list[dict],
                       member: str, src: str, chain: list[str], depth: int = 3) -> list[str]:
    """The name is only re-exported here (``from .utils import contextfunction``): read the
    module that defines it and mine that definition's prose instead."""
    if depth <= 0 or len(chain) != 1:
        return []
    try:
        node = _scope(ast.parse(src), chain)
    except SyntaxError:
        return []
    if not isinstance(node, ast.ImportFrom):
        return []
    original = next((a.name for a in node.names if (a.asname or a.name) == chain[0]), None)
    if original is None or original == "*":
        return []
    for cand in _import_member_candidates(member, node):
        text = fetcher.member(dist, version, files, cand)
        if not text:
            continue
        msgs = module_warnings(text, [original])
        if msgs:
            return msgs
        return _followed_warnings(fetcher, dist, version, files, cand, text, [original],
                                  depth - 1)
    return []


def intermediate_hint(
    fetcher: Fetcher, dist: str, member: str, chain: list[str], old_v: str, new_v: str
) -> dict[str, Any]:
    """Search releases strictly between ``old_v`` and ``new_v`` for the last one with
    ``chain`` in ``member``, and mine its deprecation prose for a successor."""
    t0 = time.perf_counter()
    b0, r0 = fetcher.bytes, fetcher.requests
    releases = fetcher.releases(dist)
    cands = []
    for v, files in releases.items():
        try:
            pv = Version(v)
        except InvalidVersion:
            continue
        try:
            if pv.is_prerelease or not (Version(old_v) < pv < Version(new_v)):
                continue
        except InvalidVersion:
            continue
        if files and not all(f.get("yanked") for f in files):
            cands.append((pv, v, files))
    cands.sort()
    newest_per_minor, seen = [], set()
    for pv, v, files in reversed(cands):
        if (pv.major, pv.minor) not in seen:
            seen.add((pv.major, pv.minor))
            newest_per_minor.append((pv, v, files))
    order = newest_per_minor + [c for c in reversed(cands) if c not in newest_per_minor]
    probes, found = [], None
    for _, v, files in order[:MAX_RELEASES]:
        src = fetcher.member(dist, v, files, member)
        ok = bool(src) and present(src, chain)
        probes.append([v, ok])
        if ok:
            found = (v, src)
            break
    result: dict[str, Any] = {"member": member, "chain": chain, "candidates": len(cands),
                              "probes": probes}
    if found:
        msgs = module_warnings(found[1], chain)
        if not msgs:
            msgs = _followed_warnings(fetcher, dist, v, files, member, found[1], chain)
        result.update(last_version_with_symbol=found[0], messages=msgs[:5],
                      hint=successor_from(msgs))
    else:
        result.update(last_version_with_symbol=None, messages=[], hint=None)
    result.update(seconds=round(time.perf_counter() - t0, 2), bytes=fetcher.bytes - b0,
                  requests=fetcher.requests - r0)
    return result
