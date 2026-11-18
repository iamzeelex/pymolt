# ruff: noqa: UP030, UP031, UP032  -- runs inside legacy interpreters (down to 3.6): stdlib only
"""Scoped API-fact probe. Runs INSIDE one environment's interpreter, as a subprocess.

Pure stdlib and 3.6-compatible syntax (no f-strings, no walrus, no annotations): it runs
in the baseline capture's interpreter, which may be a legacy Python. It never imports
pymolt. The host (``pymolt.delta.engine``) sends one JSON request on stdin::

    {"paths": [...], "dists": ["Flask", ...], "roots": [...], "any_root": false,
     "contexts": ["<setup code>", ...], "per_path_timeout": 15}

and reads one line per answer, each prefixed with ``@@pymolt-delta@@ ``::

    {"meta": {"python": "3.9.23", "executable": ..., "site_dirs": [...],
              "dists": {"Flask": {"version": "2.0.3", "roots": ["flask"]}},
              "roots": [...], "contexts": [...]}}
    {"path": "flask.request.headers.get", "result": {...}}     # one per path
    {"done": true}

A path is a public import path, possibly continued through attributes
(``flask.request.headers.get``). It is resolved by importing the longest importable
prefix and walking the rest with getattr. Each dependency-owned step reports existence,
kind, owner (the class or module that really defines the attribute), canonical name
(spelled as a profile-backend trace spells it), signature, bases, deprecation evidence
(``warnings.warn`` prose in its own source, the PEP 562 ``__getattr__`` branch serving
it, PEP 702 ``__deprecated__``, ``.. deprecated::`` docstring lines), warnings raised on
access, and its forwarding target (``__wrapped__`` or a one-line ``return f(...)``).
A parameter default is rendered as a function of its value alone (``default_repr``): the
two sides of a delta run in two environments installed in two places, so a module is named,
not located, and the site dirs, the virtualenv, the stdlib, the project and the home dir
become placeholders in any other repr.
The walk stops at the first step owned by something that is not one of ``roots``
(``request.method.lower`` ends at ``method``).
"""

import ast
import importlib
import importlib.util
import inspect
import json
import os
import re
import signal
import sys
import textwrap
import threading
import time
import types
import warnings

PROBE_VERSION = 8
MARK = "@@pymolt-delta@@ "
_ADDR = re.compile(r" at 0x[0-9a-fA-F]+")
_MAX_MSG = 400
_HAS_CONSTANT = sys.version_info >= (3, 8)  # ast.Str is deprecated (and warns) on 3.12+

_DEP_ROOTS = set()
_ANY_ROOT = [False]

# same-object search: bounds and state (see _identity_index)
_ID_MAX_SUBMODULES = 120
_ID_BUDGET_S = 6.0
_ID_MAX_RESULTS = 4
_ID_SKIP_SUBMODULES = ("tests", "test", "conftest", "setup", "_version")
_ID_HINTS = []
_ID_INDEX = {}


class _Timeout(BaseException):
    pass


def _on_alarm(signum, frame):
    raise _Timeout()


def _norm(text):
    return _ADDR.sub("", text)


# ── defaults, without the places code lives in ───────────────────────────────────────

# ``(pattern, {path: placeholder})`` for this environment and project; set by ``main``.
_PLACES = None
_DEFAULT_CAP = 80


def place_table(site_dirs, cwd):
    """The places a default's repr may carry, each with its placeholder: the site dirs, the
    virtualenv's prefix, the stdlib, the project (``cwd``) and the home dir. A path counts as
    given and resolved (a symlinked temp dir), never a filesystem root. On a tie the earlier
    kind wins: a project that is the home dir is ``<project>``."""
    table = {}

    def add(path, name):
        if not isinstance(path, str) or not path or not os.path.isabs(path):
            return
        for form in (path, os.path.realpath(path)):
            form = form.rstrip("/\\") or form
            if os.path.dirname(form) == form:      # "/" or "C:\": everything is below it
                continue
            table.setdefault(form, name)
            if "\\" in form:                       # a str's repr doubles its backslashes
                table.setdefault(form.replace("\\", "\\\\"), name)

    for entry in site_dirs or ():
        add(entry, "<site-packages>")
    if sys.prefix != getattr(sys, "base_prefix", sys.prefix):
        add(sys.prefix, "<env>")
        add(sys.exec_prefix, "<env>")
    add(os.path.dirname(os.__file__), "<stdlib>")
    add(cwd, "<project>")
    add(os.path.expanduser("~"), "~")
    return table


def places_pattern(table):
    """``(pattern, table)``: the pattern finds the table's paths as whole path components
    (never inside a longer name), the longest first, so a site dir wins over the prefix that
    holds it. None for an empty table."""
    if not table:
        return None
    alternatives = "|".join(re.escape(p) for p in sorted(table, key=len, reverse=True))
    pattern = re.compile(r"(?<![\w.\-/\\])(?:" + alternatives + r")(?=$|[\\/'\"\s)>,;:\]])")
    return pattern, table


def _unplace(text):
    if _PLACES is None:
        return text
    pattern, table = _PLACES
    return pattern.sub(lambda m: table[m.group(0)], text)


def default_repr(value):
    """A parameter default as the delta compares it, a function of the value alone: a module
    by its name; a set or frozenset with its members sorted (its own order follows the
    process's hash seed); anything else its repr. Addresses and places (``place_table``)
    are dropped, then the text is capped."""
    if isinstance(value, types.ModuleType):
        name = getattr(value, "__name__", None)
        if isinstance(name, str):
            return ("<module %r>" % name)[:_DEFAULT_CAP]
    if type(value) in (set, frozenset) and value:
        members = "{" + ", ".join(sorted(repr(m) for m in value)) + "}"
        text = members if type(value) is set else "frozenset(" + members + ")"
    else:
        text = repr(value)
    return _unplace(_norm(text))[:_DEFAULT_CAP]


# ── objects ───────────────────────────────────────────────────────────────────────────


def _unproxy(obj):
    getter = getattr(type(obj), "_get_current_object", None)
    if getter is not None:
        try:
            return obj._get_current_object()
        except Exception:
            return obj
    return obj


def _owner_path(real, attr):
    """Where ``attr`` is really defined for this object: ('<module>.<Qual>.<attr>', module)."""
    if inspect.ismodule(real):
        return real.__name__ + "." + attr, real.__name__
    cls = real if inspect.isclass(real) else type(real)
    try:
        mro = inspect.getmro(cls)
    except Exception:
        mro = (cls,)
    for klass in mro:
        try:
            if attr in vars(klass):
                return "%s.%s.%s" % (klass.__module__, klass.__qualname__, attr), klass.__module__
        except TypeError:
            continue
    return "%s.%s.%s" % (cls.__module__, cls.__qualname__, attr), cls.__module__


def _receiver_path(real, attr):
    if inspect.ismodule(real):
        return real.__name__ + "." + attr
    cls = real if inspect.isclass(real) else type(real)
    return "%s.%s.%s" % (cls.__module__, cls.__qualname__, attr)


def _is_dep(module_name):
    if _ANY_ROOT[0]:
        return True
    return (module_name or "").split(".")[0] in _DEP_ROOTS


def _kind(obj):
    if inspect.isclass(obj):
        return "class"
    if inspect.ismodule(obj):
        return "module"
    if callable(obj):
        return "callable"
    return type(obj).__name__


def _shape(obj):
    target = obj
    if inspect.isclass(obj):
        target = obj.__init__
    try:
        sig = inspect.signature(target)
    except (TypeError, ValueError):
        return None
    except Exception:
        return None
    out = []
    for p in sig.parameters.values():
        if p.default is inspect.Parameter.empty:
            default = None
        else:
            try:
                default = default_repr(p.default)
            except Exception:
                default = "<unrepresentable>"
        out.append([p.name, str(p.kind).split(".")[-1], default])
    return out


# canonical names, spelled like pymolt.verify.identity_resolver (a profile-backend trace)


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


def _target(obj):
    if isinstance(obj, property) and obj.fget is not None:
        obj = obj.fget
    if isinstance(obj, (staticmethod, classmethod)):
        obj = obj.__func__
    try:
        import functools

        if isinstance(obj, functools.partial):
            obj = obj.func
    except Exception:
        pass
    if inspect.ismethod(obj):
        obj = obj.__func__
    if not inspect.isclass(obj) and not isinstance(obj, types.ModuleType):
        try:
            obj = inspect.unwrap(obj)
        except Exception:
            pass
    return obj


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
    module = _frame_module(func)
    qualname = _code_qualname(func)
    if module and qualname:
        return module + "." + qualname
    return _qual(func)


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


def _class_sym(cls):
    qualname = getattr(cls, "__qualname__", None)
    if isinstance(qualname, str):
        for func in _class_functions(cls):
            code_qualname = _code_qualname(func)
            module = _frame_module(func)
            if module and code_qualname and code_qualname.startswith(qualname + "."):
                return module + "." + qualname
    return _qual(cls)


def _canonical(obj, owner, in_class=False):
    """The object's canonical name; the defining owner path for plain values.

    A factory-made method (``cls.mean = mean`` with ``mean`` defined in a function body) is
    named after the class that holds it, as the boundary tracer names it.
    """
    try:
        target = _target(obj)
    except Exception:
        target = obj
    try:
        if inspect.isclass(target):
            return _class_sym(target) or owner
        if inspect.ismodule(target):
            return target.__name__
        if inspect.isroutine(target):
            sym = _function_sym(target) or owner
            code = getattr(target, "__code__", None)
            if in_class and owner and sym and "<locals>" in sym and code is not None:
                return owner.rsplit(".", 1)[0] + "." + code.co_name
            return sym
    except Exception:
        return owner
    return owner


# ── static mining ─────────────────────────────────────────────────────────────────────


def _const_str(node):
    if _HAS_CONSTANT:
        if isinstance(node, ast.Constant):
            return node.value if isinstance(node.value, str) else None
    elif isinstance(node, ast.Str):
        return node.s
    if isinstance(node, ast.JoinedStr):
        return "".join(_const_str(v) or "{...}" for v in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
        left, right = _const_str(node.left), _const_str(node.right)
        if left or right:
            return (left or "{...}") + (right or "")
    if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "format":
        return _const_str(node.func.value)
    return None


def _name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _name(node.value)
        return (base + "." if base else "") + node.attr
    if isinstance(node, ast.Call):
        return _name(node.func)
    return None


def _warn_calls(tree):
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and (_name(node.func) or "").split(".")[-1] in (
            "warn",
            "warn_deprecated",
            "_warn",
        ):
            msg = _const_str(node.args[0]) if node.args else None
            cat = _name(node.args[1]) if len(node.args) > 1 else None
            for kw in node.keywords:
                if kw.arg == "category":
                    cat = _name(kw.value)
            if msg:
                out.append({"category": cat, "message": msg[:_MAX_MSG]})
    return out


def _raise_messages(tree):
    """Constant messages of ``raise X("...")`` statements: where a callable says what it no
    longer accepts (pydantic ``Field``: "`regex` is removed. use `pattern` instead")."""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call) and node.exc.args:
            msg = _const_str(node.exc.args[0])
            if msg:
                out.append({"message": msg[:_MAX_MSG]})
        if len(out) >= 12:
            break
    return out


def _rejects_kwargs(fn):
    """``def f(*a, **kw): if kw: raise ...``: the ``**kw`` is there to refuse what it collects."""
    name = getattr(fn.args.kwarg, "arg", None)
    if not name:
        return False
    for node in ast.walk(fn):
        if (isinstance(node, ast.If) and isinstance(node.test, ast.Name) and node.test.id == name
                and any(isinstance(n, ast.Raise) for s in node.body for n in ast.walk(s))):
            return True
    return False


def _mine(obj):
    """Static facts from the object's own source: warn() prose, one-line forwarding."""
    fn = getattr(obj, "fget", obj)
    try:
        fn = inspect.unwrap(fn)
        src = textwrap.dedent(inspect.getsource(fn))
        tree = ast.parse(src)
    except Exception:
        return {}
    node = tree.body[0] if tree.body else None
    decorators = [_name(d) for d in getattr(node, "decorator_list", [])]
    forwards = None
    func_types = (ast.FunctionDef, getattr(ast, "AsyncFunctionDef", ast.FunctionDef))
    if isinstance(node, func_types):
        body = [
            s
            for s in node.body
            if not (
                isinstance(s, ast.Expr)
                and (
                    _const_str(s.value) is not None
                    or (
                        isinstance(s.value, ast.Call)
                        and (_name(s.value.func) or "").endswith("warn")
                    )
                )
            )
        ]
        if (
            len(body) == 1
            and isinstance(body[0], ast.Return)
            and isinstance(body[0].value, ast.Call)
        ):
            forwards = _name(body[0].value.func)
    out = {}
    warns = _warn_calls(tree)
    if warns:
        out["warns"] = warns
    raises = _raise_messages(tree)
    if raises:
        out["raises"] = raises
    if isinstance(tree.body[0] if tree.body else None, (ast.FunctionDef,)) \
            and _rejects_kwargs(tree.body[0]):
        out["rejects_kwargs"] = True
    if forwards:
        out["forwards_to"] = forwards
    decorators = [d for d in decorators if d]
    if decorators:
        out["decorators"] = decorators
    return out


def _module_getattr_warns(module, attr):
    """Warnings in a PEP 562 ``__getattr__`` branch that serves ``attr``."""
    hook = vars(module).get("__getattr__") if inspect.ismodule(module) else None
    if hook is None:
        return []
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(hook)))
    except Exception:
        return []
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            consts = [_const_str(c) for c in ast.walk(node.test)]
            if attr in consts:
                for stmt in node.body:
                    out.extend(_warn_calls(stmt))
    return out


_SECTION = re.compile(
    r"^\s*(Parameters|Returns|Yields|Raises|Warns|Examples?|Notes|See Also|Attributes|"
    r"Methods|Other Parameters|Args|Arguments|Keyword Arguments|Kwargs)\s*:?\s*$"
)
_UNDERLINE = re.compile(r"^\s*[-=~^]{3,}\s*$")
_DEPRECATED_WORD = re.compile(r"\bdeprecated\b", re.IGNORECASE)


def _doc_deprecations(obj):
    """Deprecation lines of the object's own docstring.

    ``directive``: a ``.. deprecated::`` block (or a summary line starting "Deprecated")
    before the first section header, i.e. about the object itself. ``mentions``: every
    other line saying "deprecated" (parameter-level, or prose), hint material only.
    """
    doc = getattr(obj, "__doc__", None)
    if not isinstance(doc, str) or "eprecat" not in doc:
        return {}
    lines = inspect.cleandoc(doc).splitlines()
    directive, mentions = [], []
    in_sections = False
    i = 0
    while i < len(lines):
        line = lines[i]
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if _SECTION.match(line) and (_UNDERLINE.match(nxt) or line.rstrip().endswith(":")):
            in_sections = True
        if ".. deprecated::" in line:
            block = [line.strip()]
            j = i + 1
            indent = len(line) - len(line.lstrip())
            while j < len(lines) and len(block) < 4:
                cont = lines[j]
                if cont.strip() and (len(cont) - len(cont.lstrip())) > indent:
                    block.append(cont.strip())
                    j += 1
                elif not cont.strip() and len(block) == 1:
                    j += 1
                else:
                    break
            text = " ".join(block)[:300]
            (mentions if in_sections else directive).append(text)
            i = j
            continue
        if _DEPRECATED_WORD.search(line):
            text = line.strip()[:300]
            if not in_sections and i == 0 and text.lower().startswith("deprecated"):
                directive.append(text)
            else:
                mentions.append(text)
        i += 1
    out = {}
    if directive:
        out["directive"] = directive[:5]
    if mentions:
        out["mentions"] = mentions[:5]
    return out


def _rel_file(module_name):
    """``pkg/sub/mod.py``: the module's file relative to its top-level package's parent."""
    try:
        module = sys.modules.get(module_name)
        path = getattr(module, "__file__", None)
        root = sys.modules.get(module_name.split(".")[0])
        root_file = getattr(root, "__file__", None)
        if not path or not root_file:
            return None
        base = os.path.dirname(root_file)
        if hasattr(root, "__path__"):
            base = os.path.dirname(base)
        rel = os.path.relpath(path, base).replace(os.sep, "/")
        if rel.endswith(".pyc"):
            rel = rel[:-1]
        return rel if not rel.startswith("..") else None
    except Exception:
        return None


def _w(w):
    return {"category": w.category.__name__, "message": _norm(str(w.message))[:_MAX_MSG]}


# ── resolution ────────────────────────────────────────────────────────────────────────


def _describe(step, nxt, in_class):
    real_next = _unproxy(nxt)
    kind = _kind(real_next)
    step["kind"] = kind
    canonical = _canonical(real_next, step["owner"], in_class)
    if canonical:
        step["canonical"] = canonical
    mod = getattr(real_next, "__module__", None)
    qual = getattr(real_next, "__qualname__", None)
    if kind in ("class", "callable") and isinstance(mod, str) and isinstance(qual, str):
        step["defined_as"] = mod + "." + qual
    if kind in ("class", "callable"):
        step["shape"] = _shape(real_next)
        step.update(_mine(real_next))
        doc = _doc_deprecations(real_next)
        if doc:
            step["doc"] = doc
        try:
            inner = inspect.unwrap(real_next)
        except Exception:
            inner = real_next
        if inner is not real_next:
            wrapped = _canonical(inner, None)
            if wrapped and wrapped != canonical:
                step["wrapped"] = wrapped
    try:
        dep_attr = getattr(real_next, "__deprecated__", None)
    except Exception:
        dep_attr = None
    if isinstance(dep_attr, str) and dep_attr:
        step["deprecated_attr"] = dep_attr[:_MAX_MSG]
    if kind == "class":
        try:
            step["bases"] = ["%s.%s" % (b.__module__, b.__qualname__) for b in real_next.__bases__]
        except Exception:
            pass
    return real_next


def _import_origin(name):
    """Where the module that failed to import lives: stdlib, a site-packages root, or other."""
    top = name.split(".")[0]
    try:
        spec = importlib.util.find_spec(top)
        where = (getattr(spec, "origin", None) or "") if spec is not None else ""
    except BaseException:
        where = ""
    if "site-packages" in where or "dist-packages" in where:
        return "site-packages:" + top
    if top in _DEP_ROOTS:
        return "site-packages:" + top
    return "stdlib" if where else "other"


def _import_failure(prefix, exc):
    """``{module, error, origin, cause}`` when importing ``prefix`` raised something other than
    "no module named <prefix or one of its parents>"; None when the name is simply absent."""
    if isinstance(exc, ImportError) and getattr(exc, "name", None) is not None \
            and type(exc).__name__ == "ModuleNotFoundError" \
            and (prefix == exc.name or prefix.startswith(exc.name + ".")):
        return None
    try:
        detail = _norm(str(exc))[:200]
    except Exception:
        detail = "<unprintable>"
    return {"module": prefix, "error": type(exc).__name__ + ": " + detail,
            "origin": _import_origin(prefix),
            "cause": getattr(exc, "name", None) if isinstance(exc, ImportError) else None}


def resolve(path):
    parts = path.split(".")
    module, i = None, 0
    import_errors = []
    for i in range(len(parts), 0, -1):
        prefix = ".".join(parts[:i])
        try:
            module = importlib.import_module(prefix)
            break
        except _Timeout:
            raise
        except BaseException as exc:
            failure = _import_failure(prefix, exc)
            if failure is not None:
                import_errors.append(failure)
            continue
    # Importing "a.b.c" runs a.b first: one raising module fails every longer prefix with the
    # same error. Keep the shortest prefix per error (the module that actually raised).
    by_error = {}
    for failure in import_errors:
        by_error[failure["error"]] = failure
    import_errors = list(by_error.values())
    if module is None:
        out = {"exists": False, "error": "no importable prefix", "steps": [], "module": None}
        if import_errors:
            # The module exists but raised: that is not a missing name.
            out["error"] = "import raised: " + import_errors[0]["error"]
            out["import_errors"] = import_errors
        return out
    out = {"module": module.__name__, "steps": []}
    if import_errors:
        out["import_errors"] = import_errors
    rel = _rel_file(module.__name__)
    if rel:
        out["module_file"] = rel
    cur = module
    caught = []
    spelling = module.__name__
    for attr in parts[i:]:
        spelling += "." + attr
        real = _unproxy(cur)
        owner, owner_mod = _owner_path(real, attr)
        if not _is_dep(owner_mod):
            out["steps"].append({"attr": attr, "owner": owner, "dep": False})
            break
        step = {
            "attr": attr,
            "owner": owner,
            "owner_module": owner_mod,
            "dep": True,
            "receiver_path": _receiver_path(real, attr),
        }
        owner_file = _rel_file(owner_mod)
        if owner_file:
            step["owner_file"] = owner_file
        getattr_warns = _module_getattr_warns(real, attr)
        if getattr_warns:
            step["getattr_warns"] = getattr_warns
        try:
            with warnings.catch_warnings(record=True) as got:
                warnings.simplefilter("always")
                try:
                    nxt = getattr(real, attr)
                finally:
                    caught.extend(got)
        except _Timeout:
            raise
        except BaseException as exc:  # the answer IS the exception
            step["exists"] = False
            try:
                detail = _norm(str(exc))[:200]
            except Exception:
                detail = "<unprintable>"
            step["error"] = type(exc).__name__ + ": " + detail
            out["steps"].append(step)
            out["exists"] = False
            out["runtime_warnings"] = [_w(w) for w in caught]
            return out
        step["exists"] = True
        real_next = _describe(step, nxt, not inspect.ismodule(real))
        same, home = _same_object_as(real_next, spelling)
        aliases = _class_alias_spellings(real, attr, spelling)
        if aliases:
            same = list(same) + [a for a in aliases if a not in same]
        if same:
            step["same_object_as"] = same
            if home:
                step["same_object_home"] = home
        out["steps"].append(step)
        cur = nxt
    out["exists"] = True
    if caught:
        out["runtime_warnings"] = [_w(w) for w in caught]
    return out


# ── same-object search ────────────────────────────────────────────────────────────────


def _shareable(obj):
    """Only classes and functions are identity evidence: a module, ``None``, a bool, a
    builtin or a small shared value is the same object everywhere without being a move."""
    if inspect.ismodule(obj) or obj is None or obj is object or obj is type:
        return False
    if not (inspect.isclass(obj) or inspect.isroutine(obj)):
        return False
    return getattr(obj, "__module__", None) != "builtins"


def _public_name(spelling):
    return not any(part.startswith("_") for part in spelling.split("."))


def _scan_namespace(index, ns, prefix, deadline):
    try:
        items = list(vars(ns).items())
    except Exception:
        return
    for name, val in items:
        if name.startswith("_") or time.time() > deadline:
            continue
        if _shareable(val):
            index.setdefault(id(val), (val, []))[1].append(prefix + "." + name)


def _identity_index(root):
    """``id -> (object, [spellings])`` for ``root``'s top-level exports, its public
    submodules one level down (imported lazily, bounded by count and time) and the request's
    hint spellings. Built once per root; whatever the budget allows."""
    if root in _ID_INDEX:
        return _ID_INDEX[root]
    index = {}
    _ID_INDEX[root] = index
    deadline = time.time() + _ID_BUDGET_S
    try:
        pkg = sys.modules.get(root) or importlib.import_module(root)
        _scan_namespace(index, pkg, root, deadline)
        paths = getattr(pkg, "__path__", None)
        if paths is not None:
            import pkgutil

            names = sorted(n for _, n, _ in pkgutil.iter_modules(list(paths))
                           if not n.startswith("_") and n not in _ID_SKIP_SUBMODULES)
            for n in names[:_ID_MAX_SUBMODULES]:
                if time.time() > deadline:
                    break
                try:
                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore")
                        sub = importlib.import_module(root + "." + n)
                except _Timeout:
                    break
                except BaseException:
                    continue
                _scan_namespace(index, sub, root + "." + n, deadline)
    except _Timeout:
        pass
    except BaseException:
        pass
    for hint in _ID_HINTS:
        if hint.split(".")[0] != root:
            continue
        try:
            parts = hint.split(".")
            obj, k = None, len(parts)
            while k > 0 and obj is None:
                try:
                    obj = importlib.import_module(".".join(parts[:k]))
                except Exception:
                    k -= 1
            for attr in parts[k:]:
                obj = getattr(obj, attr)
            if obj is not None and _shareable(obj):
                index.setdefault(id(obj), (obj, []))[1].append(hint)
        except _Timeout:
            break
        except BaseException:
            continue
    return index


def _resolve_spelling(spelling):
    """The object a dotted ``spelling`` names (longest importable prefix, then getattr), or None."""
    parts = spelling.split(".")
    obj, k = None, len(parts)
    while k > 0 and obj is None:
        head = ".".join(parts[:k])
        obj = sys.modules.get(head)
        if obj is None:
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    obj = importlib.import_module(head)
            except _Timeout:
                raise
            except BaseException:
                obj = None
                k -= 1
    if obj is None:
        return None
    for attr in parts[k:]:
        try:
            obj = getattr(obj, attr)
        except _Timeout:
            raise
        except BaseException:
            return None
    return obj


def _home_spellings(obj):
    """Where the object itself lives, even in another package: ``__module__`` + ``__qualname__``,
    then the top-level package's export of the same name. Only spellings that resolve back to
    the very object (``is``) count; a ``_private`` path is returned last (it is internal)."""
    mod = getattr(obj, "__module__", None)
    qual = getattr(obj, "__qualname__", None)
    if not isinstance(mod, str) or not isinstance(qual, str) or "<" in qual:
        return []
    cands = [mod + "." + qual]
    top = mod.split(".")[0]
    leaf = qual.split(".")[-1]
    if "." in mod or "." in qual:
        cands.append(top + "." + leaf)
    out = []
    for cand in cands:
        if cand in out:
            continue
        try:
            found = _resolve_spelling(cand)
        except _Timeout:
            break
        if found is obj:
            out.append(cand)
    return out


def _alias_worthy(raw):
    """Class attributes whose identity is evidence of an alias: descriptors, functions, classes
    and non-empty immutable constant sets (``node = nodes``; ``DEFAULT_METHOD_WHITELIST``)."""
    if isinstance(raw, (property, classmethod, staticmethod)) or inspect.isroutine(raw):
        return True
    if inspect.isclass(raw):
        return True
    if type(raw).__name__ in ("cached_property", "lazy_property"):
        return True
    return isinstance(raw, frozenset) and len(raw) > 0


def _class_alias_spellings(owner, attr, spelling):
    """Other public attributes of the class ``owner`` that are the very object ``attr`` is
    (``Graph.node`` is ``Graph.nodes``): the same object under another spelling."""
    if not inspect.isclass(owner):
        return []
    try:
        raw = inspect.getattr_static(owner, attr)
    except Exception:
        return []
    try:
        value = getattr(owner, attr)
    except Exception:
        value = raw
    value_ok = isinstance(value, frozenset) and len(value) > 0
    if not (_alias_worthy(raw) or value_ok):
        return []
    prefix = spelling.rsplit(".", 1)[0]
    out = []
    try:
        names = dir(owner)
    except Exception:
        return []
    for name in names:
        if name == attr or name.startswith("_"):
            continue
        try:
            other = inspect.getattr_static(owner, name)
        except Exception:
            continue
        if other is raw:
            out.append(prefix + "." + name)
            continue
        if value_ok:  # a (metaclass) property handing out a shared constant
            try:
                if getattr(owner, name) is value:
                    out.append(prefix + "." + name)
            except Exception:
                continue
    return sorted(out, key=lambda c: (c.count("."), c))[:_ID_MAX_RESULTS]


def _module_siblings(obj, spelling):
    """Public names in the spelling's own module that are the very object
    (``django.utils.encoding.force_str`` for ``force_text``): the identity index only reaches
    a package's top level and the submodules one level down."""
    if "." not in spelling:
        return []
    mod = sys.modules.get(spelling.rsplit(".", 1)[0])
    if mod is None:
        return []
    prefix = spelling.rsplit(".", 1)[0]
    try:
        items = list(vars(mod).items())
    except Exception:
        return []
    return [prefix + "." + n for n, v in items if v is obj and not n.startswith("_")][:4]


def _rank(spelling, homes):
    """0: the public home, 1: a public re-export, 2: internal (a ``_private`` path)."""
    if not _public_name(spelling):
        return 2
    return 0 if spelling in homes else 1


def _same_object_as(obj, spelling):
    """``(spellings, home)``: public spellings, in this interpreter, naming the very object
    ``spelling`` resolved to (``pandas.util.testing.assert_frame_equal is
    pandas.testing.assert_frame_equal``). The object's own home comes first, wherever it lives
    (``jinja2.escape`` is ``markupsafe.escape``, not an incidental ``jinja2.compiler.escape``),
    then public re-exports by depth, internal ones last. ``home`` is the public home or None."""
    root = spelling.split(".")[0]
    if not _shareable(obj) or not (_ANY_ROOT[0] or root in _DEP_ROOTS):
        return [], None
    try:
        entry = _identity_index(root).get(id(obj))
        homes = [h for h in _home_spellings(obj) if h != spelling]
    except _Timeout:
        return [], None
    except BaseException:
        return [], None
    found = set(c for c in (entry[1] if entry is not None and entry[0] is obj else [])
                if c != spelling)
    found.update(homes)
    found.update(c for c in _module_siblings(obj, spelling) if c != spelling)
    ranked = sorted(found, key=lambda c: (_rank(c, homes), c.count("."), c))
    ranked = ranked[:_ID_MAX_RESULTS]
    home = next((c for c in ranked if _rank(c, homes) == 0), None)
    return ranked, home


# ── distributions ─────────────────────────────────────────────────────────────────────

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _norm_dist(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def _dist_info(name):
    version, roots = None, []
    metadata = None
    try:
        from importlib import metadata  # noqa: F811 (3.8+)
    except ImportError:
        try:
            import importlib_metadata as metadata
        except ImportError:
            metadata = None
    if metadata is not None:
        try:
            dist = metadata.distribution(name)
        except Exception:
            dist = None
        if dist is not None:
            version = dist.version
            top = None
            try:
                top = dist.read_text("top_level.txt")
            except Exception:
                top = None
            if top:
                roots = [line.strip() for line in top.splitlines() if _IDENT.match(line.strip())]
            if not roots:
                found = set()
                for f in dist.files or []:
                    parts = str(f).replace("\\", "/").split("/")
                    head = parts[0]
                    if head.endswith((".dist-info", ".egg-info", ".data")) or head in (
                        "..", "__pycache__", "bin"):
                        continue
                    if len(parts) == 1:
                        if head.endswith(".py"):
                            head = head[:-3]
                        else:
                            continue
                    if _IDENT.match(head):
                        found.add(head)
                roots = sorted(found)
    if version is None:
        try:
            import pkg_resources

            dist = pkg_resources.get_distribution(name)
            version = dist.version
            try:
                top = dist.get_metadata("top_level.txt")
                roots = [line.strip() for line in top.splitlines() if _IDENT.match(line.strip())]
            except Exception:
                pass
        except Exception:
            pass
    if not roots:
        roots = [_norm_dist(name).replace("-", "_")]
    return {"version": version, "roots": roots}


# ── main ──────────────────────────────────────────────────────────────────────────────


def _emit(out, payload):
    out.write(MARK + json.dumps(payload, sort_keys=True) + "\n")
    out.flush()


def main():
    global _PLACES
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
    # The project is the cwd, but its own modules must never shadow the dependency.
    cwd = os.getcwd()
    sys.path[:] = [p for p in sys.path if p not in ("", ".", cwd)]
    t0 = time.time()
    dists = {}
    for name in request.get("dists") or []:
        try:
            dists[name] = _dist_info(name)
        except Exception as exc:
            dists[name] = {"version": None, "roots": [], "error": str(exc)[:200]}
    roots = set(request.get("roots") or [])
    for info in dists.values():
        roots.update(info.get("roots") or [])
    _DEP_ROOTS.update(roots)
    _ANY_ROOT[0] = bool(request.get("any_root"))
    _ID_HINTS[:] = [h for h in request.get("identity_hints") or [] if isinstance(h, str)]
    site_dirs = []
    for entry in sys.path:
        if entry and os.path.basename(entry.rstrip(os.sep)) in ("site-packages", "dist-packages"):
            if os.path.isdir(entry) and entry not in site_dirs:
                site_dirs.append(entry)
    _PLACES = places_pattern(place_table(site_dirs, cwd))
    contexts = []
    for code in request.get("contexts") or []:
        try:
            exec(compile(code, "<pymolt-probe-context>", "exec"), {"__name__": "pymolt_probe_ctx"})
            contexts.append({"ok": True})
        except BaseException as exc:
            contexts.append({"ok": False, "error": (type(exc).__name__ + ": " + str(exc))[:200]})
    _emit(out, {"meta": {
        "python": "{0}.{1}.{2}".format(*sys.version_info[:3]),
        "executable": sys.executable,
        "site_dirs": site_dirs,
        "dists": dists,
        "roots": sorted(roots),
        "contexts": contexts,
        "setup_s": round(time.time() - t0, 3),
    }})
    per_path = float(request.get("per_path_timeout") or 0)
    use_alarm = (per_path > 0 and hasattr(signal, "setitimer")
                 and threading.current_thread() is threading.main_thread())
    if use_alarm:
        signal.signal(signal.SIGALRM, _on_alarm)
    for path in request.get("paths") or []:
        if not _ANY_ROOT[0] and path.split(".")[0] not in _DEP_ROOTS:
            continue
        try:
            if use_alarm:
                signal.setitimer(signal.ITIMER_REAL, per_path)
            try:
                result = resolve(path)
            finally:
                if use_alarm:
                    signal.setitimer(signal.ITIMER_REAL, 0)
        except _Timeout:
            result = {"error": "timeout: resolving took longer than {0}s".format(per_path),
                      "transient": True, "steps": [], "module": None}
        except BaseException as exc:  # a module may raise anything, SystemExit included
            try:
                detail = str(exc)
            except Exception:
                detail = "<unprintable>"
            result = {"error": "{0}: {1}".format(type(exc).__name__, detail)[:300],
                      "steps": [], "module": None}
        _emit(out, {"path": path, "result": result})
    _emit(out, {"done": True, "probe_s": round(time.time() - t0, 3)})
    out.flush()
    os._exit(0)  # never wait on threads or atexit hooks a dependency started


if __name__ == "__main__":
    main()
