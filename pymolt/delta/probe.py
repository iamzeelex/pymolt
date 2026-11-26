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

PROBE_VERSION = 11
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


_WARN_LEAVES = ("warn", "warning", "warn_deprecated", "_warn", "warn_explicit")
_NOT_WARN = ("simplefilter", "filterwarnings", "catch_warnings", "resetwarnings",
             "showwarning", "formatwarning")
_TEXT_FUNCS = ("dedent", "cleandoc", "strip", "rstrip", "lstrip")


def _is_warn_name(name):
    """A call that warns or logs about a deprecation: ``warnings.warn``, ``logger.warning``,
    ``_api.warn_deprecated``, or a package's own helper (``issue_deprecation_warning``,
    ``show_deprecation_warning``, ``make_deprecated_name_warning``)."""
    leaf = (name or "").split(".")[-1]
    low = leaf.lower()
    if not leaf or low in _NOT_WARN:
        return False
    return leaf in _WARN_LEAVES or low.endswith("warn") or low.endswith("_warning") \
        or "deprecat" in low


def _nodes(tree):
    """``ast.walk`` without decorator lists: a decorator call is no statement of the body
    (``@deprecate_renamed_function("group_by")`` is not a warning whose text is "group_by")."""
    todo = [tree]
    while todo:
        node = todo.pop()
        yield node
        for name, value in ast.iter_fields(node):
            if name == "decorator_list":
                continue
            if isinstance(value, list):
                todo.extend(v for v in reversed(value) if isinstance(v, ast.AST))
            elif isinstance(value, ast.AST):
                todo.append(value)


def _text_of(node, assigned):
    """A string a message argument holds: a literal (``_const_str``), a text helper applied to
    one (``textwrap.dedent(...)``), or a local name bound to one (one step, ``msg = "..."``)."""
    text = _const_str(node)
    if text is not None:
        return text
    if isinstance(node, ast.Call) and node.args and \
            (_name(node.func) or "").split(".")[-1] in _TEXT_FUNCS:
        return _text_of(node.args[0], assigned)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and \
            node.func.attr in _TEXT_FUNCS:
        return _text_of(node.func.value, assigned)
    if isinstance(node, ast.Name):
        found = [t for line, t in assigned.get(node.id, ()) if line <= getattr(node, "lineno", 0)]
        if found:
            return found[-1]
    return None


def _assigned_texts(tree):
    """``{name: [(line, text)]}``: the strings local names are bound to (``msg = "..."``,
    ``msg += "..."``), in line order, for one step of message propagation."""
    out = {}
    for node in _nodes(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and \
                isinstance(node.targets[0], ast.Name):
            text = _text_of(node.value, out)
            if text is not None:
                out.setdefault(node.targets[0].id, []).append((node.lineno, text))
        elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name) and \
                isinstance(node.op, ast.Add):
            prev = out.get(node.target.id)
            more = _text_of(node.value, out)
            if prev and more is not None:
                prev.append((node.lineno, prev[-1][1] + more))
    for name in out:
        out[name].sort(key=lambda item: item[0])
    return out


def _lit(node):
    """A literal argument as JSON (str, number, bool, None, lists of them); else
    ``{"expr": <name or node type>}``."""
    text = _const_str(node)
    if text is not None:
        return text[:_MAX_MSG]
    if _HAS_CONSTANT:
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float, bool,
                                                                      type(None))):
            return node.value
    else:
        if isinstance(node, ast.Num):
            return node.n
        if isinstance(node, ast.NameConstant):
            return node.value
    if isinstance(node, (ast.Tuple, ast.List)) and len(node.elts) <= 8:
        items = [_lit(e) for e in node.elts]
        if not any(isinstance(i, dict) for i in items):
            return items
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        inner = _lit(node.operand)
        if isinstance(inner, (int, float)) and not isinstance(inner, bool):
            return -inner
    return {"expr": _name(node) or type(node).__name__}


def _top_calls(tree):
    """The ids of the calls a ``def`` makes as statements of its own body, outside any branch,
    loop or ``try``: they run on every call."""
    body = getattr(tree, "body", None)
    if not isinstance(tree, (ast.FunctionDef, getattr(ast, "AsyncFunctionDef", ast.FunctionDef))):
        return set()
    out = set()
    for stmt in body or []:
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
            out.add(id(stmt.value))
    return out


def _warn_calls(tree):
    """Warnings and deprecation logs in the body (decorators excluded): their message, with a
    local message variable followed one step, and the call's literal arguments when it is a
    package helper (``make_deprecated_name_warning("experimental_rerun", "rerun", ...)``).
    ``always``: the call is a statement of the body itself, so every call warns."""
    out = []
    assigned = _assigned_texts(tree)
    top = _top_calls(tree)
    for node in _nodes(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _name(node.func) or ""
        if not _is_warn_name(name):
            continue
        msg = _text_of(node.args[0], assigned) if node.args else None
        if msg is None:
            for kw in node.keywords:
                if kw.arg in ("message", "msg", "text"):
                    msg = _text_of(kw.value, assigned)
        cat = _name(node.args[1]) if len(node.args) > 1 else None
        for kw in node.keywords:
            if kw.arg == "category":
                cat = _name(kw.value)
        leaf = name.split(".")[-1]
        entry = {"category": cat, "message": (msg or "")[:_MAX_MSG]}
        if id(node) in top:
            entry["always"] = True
        if leaf not in _WARN_LEAVES and leaf.lower() not in ("warning", "warn"):
            entry["callee"] = name
            entry["args"] = [_lit(a) for a in node.args[:6]]
            kws = dict((kw.arg, _lit(kw.value)) for kw in node.keywords[:8] if kw.arg)
            if kws:
                entry["kwargs"] = kws
        if entry.get("callee") and " " not in (msg or ""):
            entry["message"] = ""             # a name argument, not prose
        if msg or entry.get("callee"):
            out.append(entry)
        if len(out) >= 16:
            break
    return out


def _raise_messages(tree):
    """Constant messages of ``raise X("...")`` statements: where a callable says what it no
    longer accepts (pydantic ``Field``: "`regex` is removed. use `pattern` instead")."""
    out = []
    assigned = _assigned_texts(tree)
    for node in _nodes(tree):
        if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call) and node.exc.args:
            msg = _text_of(node.exc.args[0], assigned)
            if msg:
                out.append({"message": msg[:_MAX_MSG]})
        if len(out) >= 12:
            break
    return out


def _decorator_calls(node, fn):
    """``[{name, qualname, args, kwargs}]`` of the decorator calls on a ``def``
    (``@deprecate_renamed_parameter("periods", "n", version="0.19.11")``): the decorator as
    written, the function it resolves to in the defining module (None if it does not), and its
    literal arguments. Raw data: the host reads them against ``knowledge/deprecation_helpers``."""
    out = []
    globs = getattr(fn, "__globals__", None) if fn is not None else None
    for dec in getattr(node, "decorator_list", []) or []:
        if not isinstance(dec, ast.Call):
            continue
        name = _name(dec.func)
        if not name:
            continue
        entry = {"name": name, "args": [_lit(a) for a in dec.args[:8]],
                 "kwargs": dict((kw.arg, _lit(kw.value)) for kw in dec.keywords[:12] if kw.arg)}
        qual = None
        if isinstance(globs, dict):
            try:
                parts = name.split(".")
                obj = globs.get(parts[0])
                if obj is None:
                    import builtins as _b
                    obj = getattr(_b, parts[0], None)
                for attr in parts[1:]:
                    if obj is None:
                        break
                    obj = getattr(obj, attr, None)
                if obj is not None:
                    qual = _qual(obj)
            except Exception:
                qual = None
        entry["qualname"] = qual
        out.append(entry)
    return out


def _cell_lit(value, depth=0):
    """A closure cell's value as JSON when it is data a helper keeps (a name, a version, a
    message, a mapping of names), a function or class by its name; else None."""
    if isinstance(value, str):
        return value[:_MAX_MSG]
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return value
    if depth == 0 and isinstance(value, (tuple, list)) and len(value) <= 8 and \
            all(isinstance(v, str) for v in value):
        return [v[:80] for v in value]
    if depth == 0 and isinstance(value, dict) and len(value) <= 24 and \
            all(isinstance(k, str) and isinstance(v, (str, type(None))) for k, v in value.items()):
        return dict((k[:80], v if v is None else v[:120]) for k, v in value.items())
    if inspect.isroutine(value) or inspect.isclass(value):
        try:
            sym = _function_sym(value) if inspect.isroutine(value) else _qual(value)
        except Exception:
            sym = None
        return {"ref": sym} if sym else None
    return None


def _cells(obj):
    """The closure cells of each wrapper along ``obj``'s ``__wrapped__`` chain, read before
    anything unwraps it: ``[{code, module, cells}]`` where ``code`` is the wrapper's code
    qualname (``deprecate_renamed_parameter.<locals>.decorate.<locals>.wrapper``) and ``cells``
    its data cells (``{"old_name": "periods", "new_name": "n", "version": "0.19.11"}``)."""
    out = []
    cur = getattr(obj, "fget", None) or obj
    for _ in range(5):
        if cur is None:
            break
        fn = getattr(cur, "__func__", cur)
        code = getattr(fn, "__code__", None)
        closure = getattr(fn, "__closure__", None)
        if code is not None and closure:
            cells = {}
            for name, cell in zip(code.co_freevars, closure):
                try:
                    value = cell.cell_contents
                except ValueError:
                    continue
                lit = _cell_lit(value)
                if lit is not None:
                    cells[name] = lit
                if len(cells) >= 12:
                    break
            if cells:
                out.append({"code": _code_qualname(fn) or code.co_name,
                            "module": _frame_module(fn), "cells": cells})
        try:
            cur = getattr(cur, "__wrapped__", None)
        except Exception:
            cur = None
    return out


_HELPER_CACHE = {}
_DEPRECATION_WORDS = re.compile(r"deprecat|renamed|will be removed|no longer|instead|in favou?r",
                                re.IGNORECASE)


def _arg_desc(node, params):
    if isinstance(node, ast.Name):
        return {"p": node.id} if node.id in params else {"n": node.id}
    lit = _lit(node)
    if not isinstance(lit, dict):
        return {"c": lit}
    return {"x": lit.get("expr")}


def _params_of(node):
    args = getattr(node, "args", None)
    if args is None:
        return []
    names = [a.arg for a in getattr(args, "posonlyargs", [])] + [a.arg for a in args.args]
    if args.vararg is not None:
        names.append(args.vararg.arg)
    names += [a.arg for a in args.kwonlyargs]
    if args.kwarg is not None:
        names.append(args.kwarg.arg)
    return names


def _returns_param(node, params):
    """The parameter a helper hands back unchanged (``return interpolation``) when every
    ``return`` returns that one name and the body never rebinds it; else None."""
    found = set()
    assigned = set()
    for n in _nodes(node):
        if isinstance(n, ast.Return):
            if isinstance(n.value, ast.Name) and n.value.id in params:
                found.add(n.value.id)
            else:
                return None
        targets = n.targets if isinstance(n, ast.Assign) else (
            [n.target] if isinstance(n, (ast.AugAssign, ast.AnnAssign)) else [])
        for t in targets:
            if isinstance(t, ast.Name):
                assigned.add(t.id)
    name = found.pop() if len(found) == 1 else None
    return name if name is not None and name not in assigned else None


def _helper_summary(helper):
    """One level down: what a package helper says when it warns (its messages), its
    parameters, and the parameter it returns unchanged. Cached per function."""
    key = id(helper)
    if key in _HELPER_CACHE:
        return _HELPER_CACHE[key][1]
    summary = None
    try:
        src = textwrap.dedent(inspect.getsource(helper))
        tree = ast.parse(src)
        node = tree.body[0] if tree.body else None
        if isinstance(node, (ast.FunctionDef, getattr(ast, "AsyncFunctionDef", ast.FunctionDef))):
            msgs = [w["message"] for w in _warn_calls(node) if w.get("message")]
            msgs = [m for m in msgs if _DEPRECATION_WORDS.search(m)]
            if msgs:
                params = _params_of(node)
                summary = {"helper": _function_sym(helper), "params": params,
                           "messages": msgs[:6],
                           "returns": _returns_param(node, params)}
                aliases = []
                for stmt in node.body:
                    aliases += _alias_stmt(stmt, params) or []
                if aliases:
                    summary["aliases"] = aliases[:8]
                    summary["loads"] = _loads(node.body, params)
    except Exception:
        summary = None
    _HELPER_CACHE[key] = (helper, summary)
    return summary


def _helper_warns(node, fn):
    """The package helpers a body calls that warn about a deprecation (one level down):
    ``[{helper, params, messages, returns, args, kwargs, assign}]``. ``args``/``kwargs`` say
    what the call passes (``{"p": param}`` for one of the caller's parameters), ``assign`` the
    local name the result is bound to (``method = _check_interpolation_as_method(...)``)."""
    globs = getattr(fn, "__globals__", None)
    if not isinstance(globs, dict) or node is None:
        return []
    params = set(_params_of(node))
    targets = {}
    guards = {}
    for n in _nodes(node):
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and \
                isinstance(n.targets[0], ast.Name) and isinstance(n.value, ast.Call):
            targets[id(n.value)] = n.targets[0].id
        if isinstance(n, ast.If):
            for stmt in n.body:
                if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Call):
                    guards[id(stmt.value)] = n.test
    out = []
    seen = set()
    for n in _nodes(node):
        if not isinstance(n, ast.Call) or isinstance(n.func, ast.Call):
            continue
        name = _name(n.func)
        if not name or name.split(".")[0] in ("self", "cls", "super") or _is_warn_name(name):
            continue
        if not name.split(".")[-1].startswith("_"):
            continue                      # a public function is an API call, not a helper
        try:
            parts = name.split(".")
            obj = globs.get(parts[0])
            for attr in parts[1:]:
                if obj is None:
                    break
                obj = getattr(obj, attr, None)
        except Exception:
            obj = None
        if not isinstance(obj, types.FunctionType) or obj is fn or id(obj) in seen:
            continue
        if not _is_dep(getattr(obj, "__module__", None)):
            continue
        seen.add(id(obj))
        summary = _helper_summary(obj)
        if summary is None:
            continue
        entry = dict(summary)
        entry["args"] = [_arg_desc(a, params) for a in n.args[:8]]
        entry["kwargs"] = dict((kw.arg, _arg_desc(kw.value, params)) for kw in n.keywords[:16]
                               if kw.arg)
        if id(n) in targets:
            entry["assign"] = targets[id(n)]
            fed = [a.id for a in n.args if isinstance(a, ast.Name)]
            test = guards.get(id(n))
            entry["guard"] = "none" if test is None else (
                next((g for g in (_guard(test, x) for x in fed) if g != "other"), "other"))
        out.append(entry)
        if len(out) >= 6:
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


def _desc(node, params):
    """One argument of a forwarding call: ``{"p": param}`` (one of the caller's parameters),
    ``{"star": param}`` / ``{"dstar": param}`` (passed through ``*`` / ``**``), ``{"c": literal}``,
    else ``{"x": <what it is>}`` (an expression the host cannot rebuild)."""
    star = getattr(ast, "Starred", None)
    if star is not None and isinstance(node, star):
        inner = node.value
        if isinstance(inner, ast.Name) and inner.id in params:
            return {"star": inner.id}
        return {"x": "starred"}
    if isinstance(node, ast.Name):
        return {"p": node.id} if node.id in params else {"x": "name:" + node.id}
    lit = _lit(node)
    if not isinstance(lit, dict):
        return {"c": lit}
    return {"x": lit.get("expr")}


def _call_desc(call, params):
    """A forwarding call: its callee as written, its arguments (``_desc``), and the method calls
    chained on its result (``F.len().alias("count")``)."""
    chain = []
    cur = call
    while isinstance(cur, ast.Call) and isinstance(cur.func, ast.Attribute) and \
            isinstance(cur.func.value, ast.Call):
        chain.insert(0, [cur.func.attr, [_desc(a, params) for a in cur.args],
                         [[k.arg, _desc(k.value, params)] for k in cur.keywords if k.arg]])
        cur = cur.func.value
    if not isinstance(cur, ast.Call):
        return None
    callee = _name(cur.func)
    if not callee or isinstance(cur.func, ast.Call):
        return None
    kwargs = []
    for k in cur.keywords:
        if k.arg is None:
            inner = k.value
            kwargs.append(["**", {"dstar": inner.id} if isinstance(inner, ast.Name)
                           and inner.id in params else {"x": "dstar"}])
        else:
            kwargs.append([k.arg, _desc(k.value, params)])
    out = {"callee": callee, "args": [_desc(a, params) for a in cur.args], "kwargs": kwargs}
    if chain:
        out["chain"] = chain
    return out


def _noise(stmt, params=()):
    """A statement a forwarding body may hold besides its forward: a docstring, a warning (or a
    deprecation log), a message built for it (``msg = "..."``, a local that is no parameter),
    or an ``if`` whose body is only those or a raise (a conflict check:
    ``if new is not None: raise TypeError(...)``)."""
    if isinstance(stmt, ast.Expr):
        if _const_str(stmt.value) is not None:
            return True
        if isinstance(stmt.value, ast.Call) and _is_warn_name(_name(stmt.value.func) or "") \
                and not _receives(stmt.value, params):
            return True
    if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and \
            isinstance(stmt.targets[0], ast.Name) and stmt.targets[0].id not in params and \
            _text_of(stmt.value, {}) is not None:
        return True
    if isinstance(stmt, ast.If) and not stmt.orelse and stmt.body and \
            all(_noise(s, params) or isinstance(s, ast.Raise) for s in stmt.body):
        return True
    if isinstance(stmt, ast.Pass):
        return True
    return False


def _receives(call, params):
    """The call is handed one of the caller's parameters (or ``self``) as is, so it may change
    it (``_handle_deprecated_kwargs(kwargs)``): never a mere warning."""
    names = set(params) | set(["self", "cls"])
    star = getattr(ast, "Starred", None)
    for arg in list(call.args) + [k.value for k in call.keywords]:
        node = arg.value if star is not None and isinstance(arg, star) else arg
        if isinstance(node, ast.Name) and node.id in names:
            return True
    return False


def _slice_text(sub):
    """The constant string a subscript indexes with (``kwargs["height"]``), on every Python:
    3.8 and before wrap it in ``ast.Index``."""
    sl = sub.slice
    index = getattr(ast, "Index", None)
    if index is not None and isinstance(sl, index):
        sl = sl.value
    return _const_str(sl)


def _guard(test, old):
    """How an ``if`` guards an alias of ``old``: ``not_none`` (``old is not None``),
    ``sentinel`` (``old is not <a module constant>``: any value a caller passes),
    ``in`` (``"old" in kwargs``), else ``other``."""
    if isinstance(test, ast.Compare) and len(test.ops) == 1:
        left, op, right = test.left, test.ops[0], test.comparators[0]
        if isinstance(left, ast.Name) and left.id == old and isinstance(op, ast.IsNot):
            none = (_HAS_CONSTANT and isinstance(right, ast.Constant) and right.value is None) \
                or (not _HAS_CONSTANT and isinstance(right, getattr(ast, "NameConstant", ()))
                    and right.value is None)
            if none:
                return "not_none"
            if isinstance(right, (ast.Name, ast.Attribute)):
                return "sentinel"
        if isinstance(op, ast.In) and _const_str(left) == old:
            return "in"
    return "other"


def _alias_stmt(stmt, params):
    """``[{old, new, how}]`` when the statement makes one parameter the other's alias: ``new =
    old``; ``if old is not None: [warn]; new = old``; ``kwargs["new"] = kwargs.pop("old")``
    (inside ``if "old" in kwargs:``). None for any other statement."""
    if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
        t, v = stmt.targets[0], stmt.value
        if isinstance(t, ast.Name) and isinstance(v, ast.Name) and t.id in params and \
                v.id in params and t.id != v.id:
            return [{"old": v.id, "new": t.id, "how": "assign"}]
        if isinstance(t, ast.Subscript) and isinstance(v, ast.Call) and \
                isinstance(v.func, ast.Attribute) and v.func.attr == "pop" and v.args:
            new = _slice_text(t)
            old = _const_str(v.args[0])
            base_t, base_v = _name(t.value), _name(v.func.value)
            if new and old and base_t and base_t == base_v and base_t in params and \
                    len(v.args) == 1:
                return [{"old": old, "new": new, "how": "pop"}]
    if isinstance(stmt, ast.If) and not stmt.orelse:
        names = set()
        for n in ast.walk(stmt.test):
            if isinstance(n, ast.Name):
                names.add(n.id)
            text = _const_str(n)
            if text:
                names.add(text)
        rest = [s for s in stmt.body if not (_noise(s, params) or isinstance(s, ast.Raise))]
        found = []
        for s in rest:
            got = _alias_stmt(s, params)
            if got is None:
                return None
            found += got
        if found and all(a["old"] in names for a in found):
            return [dict(a, guard=_guard(stmt.test, a["old"])) for a in found]
    return None


def _setdefault_stmt(stmt, params):
    """``{key: literal}`` for ``kwargs.setdefault("kind", "point")``: a default the body
    materializes before forwarding. None otherwise."""
    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
        call = stmt.value
        if isinstance(call.func, ast.Attribute) and call.func.attr == "setdefault" and \
                _name(call.func.value) in params and len(call.args) == 2:
            key = _const_str(call.args[0])
            value = _lit(call.args[1])
            if key and not isinstance(value, dict):
                return {key: value}
    return None


def _cond(test, params):
    """A branch condition the call's own arguments decide: ``not p`` / ``p`` / ``p is None`` /
    ``p is not None`` (``{"falsy": p}``...). None for any other test."""
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not) and \
            isinstance(test.operand, ast.Name) and test.operand.id in params:
        return {"falsy": test.operand.id}
    if isinstance(test, ast.Name) and test.id in params:
        return {"truthy": test.id}
    if isinstance(test, ast.Compare) and len(test.ops) == 1 and \
            isinstance(test.left, ast.Name) and test.left.id in params:
        right = test.comparators[0]
        none = (_HAS_CONSTANT and isinstance(right, ast.Constant) and right.value is None) or \
            (not _HAS_CONSTANT and isinstance(right, getattr(ast, "NameConstant", ()))
             and right.value is None)
        if none and isinstance(test.ops[0], ast.Is):
            return {"none": test.left.id}
        if none and isinstance(test.ops[0], ast.IsNot):
            return {"not_none": test.left.id}
    return None


def _body_key(stmts):
    import hashlib
    try:
        dumped = "\n".join(ast.dump(s) for s in stmts)
    except Exception:
        return None
    return hashlib.sha1(dumped.encode("utf-8")).hexdigest()[:16]


def _body_summary(node):
    """What a body proves, as raw data: ``forward`` (after its docstring, warnings and conflict
    checks, it is one ``return f(...)``, or ``return self.x``), ``cond_forward`` (``if <test on
    a parameter>: [warn]; return f(...)`` then another ``return``), ``aliases`` (parameters made
    one another's alias), ``materialize`` (``kwargs.setdefault``) and ``key`` (a hash of the
    body without its warnings: equal keys are twins)."""
    params = _params_of(node)
    body = [s for s in node.body if not _noise(s, params)]
    out = {}
    aliases, materialize, rest = [], {}, []
    for stmt in body:
        got = _alias_stmt(stmt, params)
        if got is not None:
            aliases += got
            continue
        got = _setdefault_stmt(stmt, params)
        if got is not None:
            materialize.update(got)
            continue
        rest.append(stmt)
    if aliases:
        out["aliases"] = aliases[:8]
    if materialize:
        out["materialize"] = materialize
    if len(rest) == 1 and isinstance(rest[0], ast.Return) and rest[0].value is not None:
        value = rest[0].value
        if isinstance(value, ast.Call):
            fwd = _call_desc(value, params)
            if fwd:
                out["forward"] = fwd
        elif isinstance(value, ast.Attribute) and _name(value.value) == "self":
            out["forward"] = {"attr": value.attr}
    elif len(rest) == 2 and isinstance(rest[0], ast.If) and isinstance(rest[1], ast.Return) \
            and not rest[0].orelse:
        branch = [s for s in rest[0].body if not _noise(s, params)]
        cond = _cond(rest[0].test, params)
        if cond and len(branch) == 1 and isinstance(branch[0], ast.Return) and \
                isinstance(branch[0].value, ast.Call):
            fwd = _call_desc(branch[0].value, params)
            if fwd:
                out["cond_forward"] = {"cond": cond, "forward": fwd,
                                       "warns": any(_noise(s, params)
                                                    for s in rest[0].body)}
    key = _body_key(body)
    if key:
        out["key"] = key
    out["loads"] = _loads(body, params)
    return out


def _loads(stmts, params):
    """How often each parameter is read in the statements a summary keeps (warnings left
    out): an alias is a proof only when the old parameter has no other use."""
    counts = {}
    todo = list(stmts)
    while todo:
        node = todo.pop()
        if isinstance(node, ast.stmt) and _noise(node, params):
            continue
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in params:
            counts[node.id] = counts.get(node.id, 0) + 1
        for child in ast.iter_child_nodes(node):
            todo.append(child)
    return counts


def _decorator_names(node, fn):
    """Every decorator on a ``def``, by the function it resolves to in the defining module
    (else as written)."""
    out = []
    globs = getattr(fn, "__globals__", None)
    for dec in getattr(node, "decorator_list", []) or []:
        expr = dec.func if isinstance(dec, ast.Call) else dec
        name = _name(expr) or type(expr).__name__
        qual = None
        if isinstance(globs, dict):
            obj = _resolve_in(fn, name)
            if obj is None:
                import builtins as _b
                obj = getattr(_b, name, None) if "." not in name else None
            import functools
            if isinstance(obj, functools.partial):      # numpy: partial(array_function_dispatch)
                obj = obj.func
            if obj is not None:
                try:
                    qual = _qual(obj)
                except Exception:
                    qual = None
        out.append(qual or name)
    return out


def _layers(obj):
    """The wrappers on ``obj``'s ``__wrapped__`` chain, outermost first: each by its code
    (``{module, code}``) or, for an object that is no Python function, by its type."""
    out = []
    cur = getattr(obj, "fget", None) or obj
    for _ in range(6):
        nxt = getattr(cur, "__wrapped__", None)
        if nxt is None:
            break
        fn = getattr(cur, "__func__", cur)
        code = getattr(fn, "__code__", None)
        if code is not None:
            out.append({"module": _frame_module(fn), "code": _code_qualname(fn) or code.co_name})
        else:
            t = type(cur)
            out.append({"type": "%s.%s" % (t.__module__, t.__qualname__)})
        cur = nxt
    return out


def _mine(obj):
    """Static facts from the object's own source: warn() prose (message variables followed one
    step, a package's own deprecation helpers included), raise texts, one-line forwarding, the
    decorator calls with their literal arguments, and the warning helpers it calls one level
    down."""
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
                        and _is_warn_name(_name(s.value.func) or "")
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
    warns = _warn_calls(node if node is not None else tree)
    if warns:
        out["warns"] = warns
    raises = _raise_messages(node if node is not None else tree)
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
    calls = _decorator_calls(node, fn)
    if calls:
        out["decorator_calls"] = calls
    if isinstance(node, func_types):
        try:
            helpers = _helper_warns(node, fn)
        except Exception:
            helpers = []
        if helpers:
            out["helper_warns"] = helpers
        try:
            body = _body_summary(node)
            body["decorators"] = _decorator_names(node, fn)
            body["layers"] = _layers(obj)
        except Exception:
            body = {}
        if body:
            out["body"] = body
            fwd = body.get("forward") or (body.get("cond_forward") or {}).get("forward")
            callee = (fwd or {}).get("callee") or ""
            head, _sep, rest = callee.partition(".")
            if callee and head not in ("self", "cls"):
                found = _resolve_in(fn, callee)
                if found is not None:
                    out["_callee"] = (found, callee.split(".")[-1])
            elif rest and "." not in rest:
                out["_member"] = rest
    return out


def _safe_mine(obj):
    """``_mine``, which must never lose a path's other facts to one callable it cannot read."""
    try:
        return _mine(obj)
    except Exception:
        return {}


def _resolve_in(fn, dotted):
    """What ``dotted`` names in the module that defines ``fn`` (its globals, then builtins)."""
    globs = getattr(fn, "__globals__", None)
    if not isinstance(globs, dict):
        return None
    parts = dotted.split(".")
    obj = globs.get(parts[0])
    try:
        for attr in parts[1:]:
            if obj is None:
                return None
            obj = getattr(obj, attr)
    except Exception:
        return None
    return obj


def _callee_spellings(obj, owner, leaf):
    """Public spellings, in this interpreter, of the very object a forward calls: beside the
    spelling being resolved (``numpy.prod`` for ``numpy.product``), its home and its package's
    exports (a ``functools.partial`` by its function's module)."""
    cands = []
    if inspect.ismodule(owner):
        cands.append(owner.__name__ + "." + leaf)
    import functools

    module = getattr(obj, "__module__", None)
    if isinstance(obj, functools.partial) or not isinstance(module, str):
        # a partial's ``__module__`` is functools': its function's module is its home
        module = getattr(getattr(obj, "func", None), "__module__", None)
    qual = getattr(obj, "__qualname__", None)
    if isinstance(module, str):
        if isinstance(qual, str) and "<" not in qual:
            cands.append(module + "." + qual)
        parts = module.split(".")
        for i in range(len(parts), 0, -1):
            cands.append(".".join(parts[:i]) + "." + leaf)
    out = []
    for cand in cands:
        if cand in out:
            continue
        try:
            if _resolve_spelling(cand) is obj:
                out.append(cand)
        except _Timeout:
            raise
        except Exception:
            continue
    return sorted(out, key=lambda c: (not _public_name(c), c.count("."), c))[:6]


def _module_getattr_warns(module, attr):
    """Warnings in a PEP 562 ``__getattr__`` branch that serves ``attr``, and the messages it
    raises there (``raise AttributeError("... Use st.cache_data instead")``)."""
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
                    out.extend(dict(r, category="raise") for r in _raise_messages(stmt))
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
    if kind in ("class", "callable") or isinstance(real_next, property):
        cells = _cells(real_next)
        if cells:
            step["cells"] = cells
    if isinstance(real_next, property):
        mined = _safe_mine(real_next)
        if mined:
            step.update(mined)
    if kind in ("class", "callable"):
        step["shape"] = _shape(real_next)
        step.update(_safe_mine(real_next))
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
        attrs = _instance_attrs(real_next)
        if attrs:
            step["instance_attrs"] = attrs
    return real_next


def _instance_attrs(cls):
    """Public names the class's own ``__init__`` (and its package bases') assigns on ``self``
    (``self.status = status``): attributes an instance has that the class does not."""
    out = []
    try:
        mro = inspect.getmro(cls)
    except Exception:
        mro = (cls,)
    for klass in mro[:6]:
        if not _is_dep(getattr(klass, "__module__", None)):
            continue
        init = vars(klass).get("__init__")
        if not isinstance(init, types.FunctionType):
            continue
        try:
            tree = ast.parse(textwrap.dedent(inspect.getsource(init)))
        except Exception:
            continue
        for node in ast.walk(tree):
            targets = node.targets if isinstance(node, ast.Assign) else (
                [node.target] if isinstance(node, (ast.AnnAssign, ast.AugAssign)) else [])
            for t in targets:
                if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) and \
                        t.value.id == "self" and not t.attr.startswith("_") and \
                        t.attr not in out:
                    out.append(t.attr)
        if len(out) >= 60:
            break
    return out[:60]


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
        step["oid"] = id(real_next)
        callee = step.pop("_callee", None)
        member = step.pop("_member", None)
        try:
            if callee is not None:
                obj, leaf = callee
                step["callee"] = {"spellings": _callee_spellings(obj, real, leaf),
                                  "shape": _shape(obj)}
            elif member is not None and inspect.isclass(real) and "." not in member:
                target = getattr(real, member, None)
                if target is not None:
                    step["callee"] = {"spellings": [], "shape": _shape(target),
                                      "member": member}
        except _Timeout:
            raise
        except Exception:
            pass
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
