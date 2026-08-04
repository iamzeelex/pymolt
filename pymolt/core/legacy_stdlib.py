"""Modules that were the standard library in Python 2 and are not in Python 3.

``sys.stdlib_module_names`` only knows about the interpreter running pymolt, so on
Python 3 every one of these names looks like a third-party package. In a legacy
codebase — exactly the kind pymolt is pointed at — that turned ``import
ConfigParser`` into a phantom dependency in the contact map, alongside real ones.

They are not dependencies, and they are not noise either: an import from this set
is *evidence the code is still Python 2*, which is a migration fact worth
surfacing on its own. Callers exclude them from dependency sets and report them
as what they are.
"""

from __future__ import annotations

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
