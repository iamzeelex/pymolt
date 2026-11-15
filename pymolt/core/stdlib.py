"""The standard library across versions: which modules and names exist when.

Two knowledge files back this, both loaded once:

- ``stdlib_lifetimes.json`` is *generated* by ``bench/stdlib_sweep/`` (typeshed
  ``stdlib/VERSIONS`` for module lifetimes, a sweep of real 3.6-3.14 interpreters for
  name lifetimes). Never hand-edited.
- ``stdlib_changes.json`` is curated: successors, fixes and sources for the removals
  that matter.

Versions are ``"X.Y"`` strings. A lifetime is ``(first, last)`` where ``last`` is
``None`` while the name still exists in the newest interpreter swept. A name present
on the oldest swept interpreter (3.6) starts at its module's first version, because
nothing earlier was swept: the start is the module's, not the name's own.

``present_in`` answers from data only (``None`` = the data cannot say), so the base
side of a Python migration can be reasoned about without an interpreter on hand.

Also home of the Python 2 table (formerly ``core/legacy_stdlib.py``, which re-exports
it): modules that were Python 2's standard library and are not in Python 3.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache

from pymolt import knowledge

#: legacy name -> its Python 3 home (or "" when the module was simply removed).
PY2_STDLIB: dict[str, str] = {
    "ConfigParser": "configparser",
    "Cookie": "http.cookies",
    "cookielib": "http.cookiejar",
    "copy_reg": "copyreg",
    "cPickle": "pickle",
    "cStringIO": "io",
    "StringIO": "io",
    "Queue": "queue",
    "SocketServer": "socketserver",
    "BaseHTTPServer": "http.server",
    "SimpleHTTPServer": "http.server",
    "CGIHTTPServer": "http.server",
    "httplib": "http.client",
    "HTMLParser": "html.parser",
    "xmlrpclib": "xmlrpc.client",
    "SimpleXMLRPCServer": "xmlrpc.server",
    "DocXMLRPCServer": "xmlrpc.server",
    "urllib2": "urllib.request",
    "urlparse": "urllib.parse",
    "robotparser": "urllib.robotparser",
    "__builtin__": "builtins",
    "thread": "_thread",
    "dummy_thread": "_dummy_thread",
    "repr": "reprlib",
    "test.test_support": "test.support",
    "Tkinter": "tkinter",
    "tkFileDialog": "tkinter.filedialog",
    "tkMessageBox": "tkinter.messagebox",
    "tkSimpleDialog": "tkinter.simpledialog",
    "ttk": "tkinter.ttk",
    "anydbm": "dbm",
    "whichdb": "dbm",
    "dbhash": "dbm.bsd",
    "dumbdbm": "dbm.dumb",
    "gdbm": "dbm.gnu",
    "commands": "subprocess",
    "exceptions": "",
    "new": "",
    "sets": "",
    "statvfs": "os.statvfs",
    "user": "",
    "md5": "hashlib",
    "sha": "hashlib",
    "mutex": "",
    "UserDict": "collections",
    "UserList": "collections",
    "UserString": "collections",
    "htmlentitydefs": "html.entities",
    "htmllib": "html.parser",
    "markupbase": "_markupbase",
    "mimetools": "email",
    "rfc822": "email",
    "popen2": "subprocess",
    "imputil": "importlib",
    "compiler": "ast",
    "sgmllib": "",
    "future_builtins": "",
    "ScrolledText": "tkinter.scrolledtext",
    "Tkconstants": "tkinter.constants",
    "tkColorChooser": "tkinter.colorchooser",
    "tkCommonDialog": "tkinter.commondialog",
    "tkFont": "tkinter.font",
}


def is_py2_stdlib(top_level: str) -> bool:
    """True for a top-level name that was Python 2's standard library."""
    return top_level in PY2_STDLIB


def py3_replacement(top_level: str) -> str:
    """Where the module went in Python 3 — ``""`` when it was removed outright."""
    return PY2_STDLIB.get(top_level, "")


def describe(names) -> str:
    """A one-line note naming the legacy imports found and where they moved."""
    parts = []
    for name in sorted(set(names)):
        target = py3_replacement(name)
        parts.append(f"{name} → {target}" if target else f"{name} (removed)")
    return ", ".join(parts)


@dataclass(frozen=True)
class StdlibChange:
    """One curated stdlib change (``stdlib_changes.json``).

    ``symbols`` are dotted spellings; an entry for a module (``imp``) also covers every
    attribute under it. ``successor`` may contain ``{name}``, standing for the last
    component of the matched symbol (see :meth:`successor_for`). ``same_object`` is true
    only when the successor is the identical object, so a plain rename is semantically safe.
    """

    id: str
    symbols: tuple[str, ...]
    kind: str
    deprecated_in: str | None = None
    warns_since: str | None = None
    removed_in: str | None = None
    successor: str | None = None
    same_object: bool = False
    successor_since: str | None = None
    fix: str = "advisory"
    hint: str = ""
    shimmed_by: str | None = None
    source: str = ""
    date: str = ""

    def successor_for(self, dotted: str) -> str | None:
        """The successor spelling for one matched symbol (``{name}`` filled in)."""
        if self.successor is None:
            return None
        return self.successor.replace("{name}", dotted.rsplit(".", 1)[-1])


def _ver(version: str) -> tuple[int, int] | None:
    parts = str(version).split(".")
    try:
        return int(parts[0]), int(parts[1])
    except (ValueError, IndexError):
        return None


def _in_range(version: tuple[int, int], span: list) -> bool:
    lo, hi = _ver(span[0]), (_ver(span[1]) if span[1] else None)
    return lo is not None and version >= lo and (hi is None or version <= hi)


@cache
def _data() -> dict:
    return knowledge.stdlib_lifetimes()


@cache
def _modules() -> dict[str, list]:
    mods = {k: v["present"] for k, v in _data().get("modules", {}).items()}
    for name in PY2_STDLIB:
        mods.setdefault(name, ["2.7", "2.7"])
    return mods


@cache
def _submodules() -> frozenset[str]:
    """Modules the sweep imported that typeshed lists only through their parent."""
    return frozenset(_data().get("submodules", ())) | frozenset(_data().get("stable", {}))


@cache
def _since_unknown() -> frozenset[str]:
    """Modules the sweep saw on its oldest interpreter and typeshed does not list."""
    return frozenset(k for k, v in _data().get("modules", {}).items() if v.get("since_unknown"))


@cache
def _tops() -> frozenset[str]:
    return frozenset(m.split(".", 1)[0] for m in _modules())


@cache
def _floor() -> tuple[int, int]:
    """The oldest interpreter swept: below it, name presence is unknown."""
    pythons = [_ver(p) for p in _data().get("pythons", [])]
    return min((p for p in pythons if p), default=(3, 6))


@cache
def _newest() -> tuple[int, int] | None:
    pythons = [_ver(p) for p in _data().get("pythons", [])]
    return max((p for p in pythons if p), default=None)


def is_stdlib(top: str) -> bool:
    """True when ``top`` was a standard-library module in any of 2.7 and 3.0-3.14."""
    return top in _tops()


def module_lifetime(module: str) -> tuple[str, str | None] | None:
    """``(first, last)`` of a module; ``last`` None while it still exists.

    A submodule not listed separately has its parent's lifetime (typeshed's rule).
    """
    mods = _modules()
    parts = module.split(".")
    while parts:
        span = mods.get(".".join(parts))
        if span is not None:
            return span[0], span[1]
        parts.pop()
    return None


def _split(dotted: str) -> tuple[str, str] | None:
    """Split into the longest known module prefix and the remaining attribute path."""
    parts = dotted.split(".")
    for i in range(len(parts) - 1, 0, -1):
        mod = ".".join(parts[:i])
        if mod in _modules() or mod in _data().get("stable", {}):
            return mod, ".".join(parts[i:])
    return None


def name_lifetime(dotted: str) -> tuple[str, str | None] | None:
    """``(first, last)`` of a dotted name (module attribute or class attribute).

    ``("3.0", "3.9")`` for ``collections.Iterable``. ``first`` is the module's first
    version when the name already existed on the oldest swept interpreter.
    """
    entry = _data().get("names", {}).get(dotted)
    if entry is not None:
        return entry["present"][0], entry["present"][1]
    if dotted in _modules():
        return module_lifetime(dotted)
    split = _split(dotted)
    if split and split[1] in _data().get("stable", {}).get(split[0], ()):
        life = module_lifetime(split[0])
        return (life[0], None) if life else None
    return None


def present_in(dotted: str, version: str) -> bool | None:
    """Whether ``dotted`` exists in ``version``, from data only; ``None`` = unknown."""
    ver = _ver(version)
    if ver is None:
        return None
    mods = _modules()
    if dotted in mods:
        if ver < _floor() and dotted in _since_unknown():
            return None  # observed from the oldest swept interpreter on: the real start is unknown
        return _in_range(ver, list(mods[dotted]))
    if dotted in _submodules():  # an unlisted submodule: its parent's lifetime
        life = module_lifetime(dotted)
        return _in_range(ver, list(life)) if life else None
    split = _split(dotted)
    if split is not None:
        life = module_lifetime(split[0])
        if life is not None and not _in_range(ver, list(life)):
            return False  # the module itself is absent in that version
    entry = _data().get("names", {}).get(dotted)
    newest = _newest()
    if entry is not None:
        span = entry["present"]
        hi = _ver(span[1]) if span[1] else None
        if hi is not None and ver > hi:
            return False
        if ver < _floor() or (hi is None and newest is not None and ver > newest):
            return None
        return _in_range(ver, span)
    if split and split[1] in _data().get("stable", {}).get(split[0], ()):
        if ver < _floor() or (newest is not None and ver > newest):
            return None
        return True
    return None


def data_stamp() -> str:
    """Provenance stamp for a fact that rests on the shipped lifetimes data."""
    return f"knowledge:stdlib_lifetimes@{_data().get('date', 'unknown')}"


@cache
def _changes() -> tuple[StdlibChange, ...]:
    return tuple(
        StdlibChange(**{**e.model_dump(), "symbols": tuple(e.symbols)})
        for e in knowledge.stdlib_changes()
    )


def changes_for(dotted: str) -> list[StdlibChange]:
    """Curated entries covering ``dotted`` (the symbol itself or a module prefix of it).

    Most specific first, so ``imp.reload`` yields its own entry before the ``imp`` one.
    """
    scored = []
    for change in _changes():
        best = max(
            (len(s) for s in change.symbols if dotted == s or dotted.startswith(s + ".")),
            default=-1,
        )
        if best >= 0:
            scored.append((-best, change.id, change))
    return [c for _, _, c in sorted(scored, key=lambda t: t[:2])]


def all_changes() -> list[StdlibChange]:
    """Every curated entry (for tests and tooling)."""
    return list(_changes())


__all__ = [
    "PY2_STDLIB",
    "StdlibChange",
    "all_changes",
    "changes_for",
    "data_stamp",
    "describe",
    "is_py2_stdlib",
    "is_stdlib",
    "module_lifetime",
    "name_lifetime",
    "present_in",
    "py3_replacement",
]
