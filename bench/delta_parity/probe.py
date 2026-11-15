# ruff: noqa: UP031  -- runs inside legacy interpreters; keep 3.6-era formatting (like verify/'s injected runtime)
"""Scoped API-fact probe. Runs INSIDE one environment's interpreter.

Pure stdlib, 3.6-compatible syntax (it must run in legacy capture interpreters, like
pymolt's identity resolver). Never imports pymolt. Prints one JSON document to stdout.

For each touched path (a public import path, possibly continued through attribute
chains such as ``flask.request.args.get``) it reports, per resolved dependency step:

  exists / kind / defining path (the class or module that actually owns the attribute)
  signature   [name, kind, default-repr] for callables, ``__init__`` for classes
  bases       for classes
  warns       ``warnings.warn`` messages mined statically from the object's own source
              (function/class body), or from the module ``__getattr__`` branch that
              serves the name (PEP 562 deprecations, e.g. ``flask.Markup`` in 2.3)
  runtime_warnings  warnings actually emitted while resolving the path
  forwards_to the callee when the body is a single ``return <call>``

Usage: python -I probe.py SYMBOLS.json [--flask-context]
"""

import ast
import importlib
import inspect
import json
import re
import sys
import textwrap
import time
import warnings

DEP_ROOTS = set()  # filled from argv: top-level packages that count as "the dependency"
_ADDR = re.compile(r" at 0x[0-9a-fA-F]+")


def _norm(text):
    return _ADDR.sub("", text)


def _unproxy(obj):
    getter = getattr(type(obj), "_get_current_object", None)
    if getter is not None:
        try:
            return obj._get_current_object()
        except Exception:
            return obj
    return obj


def _owner_path(real, attr):
    """Where ``attr`` is really defined for this object: '<module>.<Qual>.<attr>'."""
    if inspect.ismodule(real):
        return real.__name__ + "." + attr, real.__name__
    cls = real if inspect.isclass(real) else type(real)
    for klass in inspect.getmro(cls):
        if attr in vars(klass):
            return "%s.%s.%s" % (klass.__module__, klass.__qualname__, attr), klass.__module__
    if attr in getattr(real, "__dict__", {}):
        return "%s.%s.%s" % (cls.__module__, cls.__qualname__, attr), cls.__module__
    return "%s.%s.%s" % (cls.__module__, cls.__qualname__, attr), cls.__module__


def _receiver_path(real, attr):
    """The attribute as seen on the receiver's own class (``flask.app.Flask.route``), which is
    how whole-library tools (griffe) list inherited members; ``owner`` is where it is defined."""
    if inspect.ismodule(real):
        return real.__name__ + "." + attr
    cls = real if inspect.isclass(real) else type(real)
    return "%s.%s.%s" % (cls.__module__, cls.__qualname__, attr)


def _shape(obj):
    target = obj
    if inspect.isclass(obj):
        target = obj.__init__
    try:
        sig = inspect.signature(target)
    except (TypeError, ValueError):
        return None
    out = []
    for p in sig.parameters.values():
        default = None if p.default is inspect.Parameter.empty else _norm(repr(p.default))[:80]
        out.append([p.name, str(p.kind).split(".")[-1], default])
    return out


_HAS_CONSTANT = sys.version_info >= (3, 8)  # ast.Str is deprecated (and warns) on 3.12+


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
                out.append({"category": cat, "message": msg[:400]})
    return out


def _mine(obj):
    fn = obj
    fn = getattr(fn, "fget", fn)
    try:
        fn = inspect.unwrap(fn)
        src = textwrap.dedent(inspect.getsource(fn))
        tree = ast.parse(src)
    except Exception:
        return {"warns": [], "forwards_to": None, "decorators": []}
    node = tree.body[0] if tree.body else None
    decorators = [_name(d) for d in getattr(node, "decorator_list", [])]
    forwards = None
    if isinstance(node, (ast.FunctionDef, getattr(ast, "AsyncFunctionDef", ast.FunctionDef))):
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
    return {
        "warns": _warn_calls(tree),
        "forwards_to": forwards,
        "decorators": [d for d in decorators if d],
    }


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
                out.extend(_warn_calls(ast.Module(body=node.body, type_ignores=[])))
    return out


def _is_dep(module_name):
    return (module_name or "").split(".")[0] in DEP_ROOTS


def resolve(path):
    parts = path.split(".")
    module, i = None, 0
    for i in range(len(parts), 0, -1):
        try:
            module = importlib.import_module(".".join(parts[:i]))
            break
        except Exception:
            continue
    if module is None:
        return {"exists": False, "error": "no importable prefix", "steps": [], "module": None}
    cur = module
    steps = []
    caught = []
    if True:
        for attr in parts[i:]:
            real = _unproxy(cur)
            owner, owner_mod = _owner_path(real, attr)
            if not _is_dep(owner_mod):
                steps.append({"attr": attr, "owner": owner, "dep": False})
                break  # a builtin / project-owned step: the dependency contact ends before it
            step = {
                "attr": attr,
                "owner": owner,
                "dep": True,
                "receiver_path": _receiver_path(real, attr),
                "getattr_warns": _module_getattr_warns(real, attr),
            }
            try:
                with warnings.catch_warnings(record=True) as got:
                    warnings.simplefilter("always")
                    try:
                        nxt = getattr(real, attr)
                    finally:
                        caught.extend(got)
            except Exception as exc:  # noqa: BLE001 — the answer IS the exception
                step["exists"] = False
                step["error"] = type(exc).__name__ + ": " + _norm(str(exc))[:200]
                steps.append(step)
                return {
                    "exists": False,
                    "steps": steps,
                    "module": module.__name__,
                    "runtime_warnings": [_w(w) for w in caught],
                }
            step["exists"] = True
            real_next = _unproxy(nxt)
            kind = (
                "class"
                if inspect.isclass(real_next)
                else "module"
                if inspect.ismodule(real_next)
                else "callable"
                if callable(real_next)
                else type(real_next).__name__
            )
            step["kind"] = kind
            mod = getattr(real_next, "__module__", None)
            qual = getattr(real_next, "__qualname__", None)
            if kind in ("class", "callable") and isinstance(mod, str) and isinstance(qual, str):
                step["defined_as"] = mod + "." + qual
            if kind in ("class", "callable"):
                step["shape"] = _shape(real_next)
                step.update(_mine(real_next))
            if kind == "class":
                step["bases"] = [
                    "%s.%s" % (b.__module__, b.__qualname__) for b in real_next.__bases__
                ]
            steps.append(step)
            cur = nxt
    return {
        "exists": True,
        "steps": steps,
        "module": module.__name__,
        "runtime_warnings": [_w(w) for w in caught],
    }


def _w(w):
    return {"category": w.category.__name__, "message": _norm(str(w.message))[:400]}


def flask_context():
    from flask import Flask

    app = Flask("probe")
    app.add_url_rule("/x", "x_ep", lambda: "ok", methods=["GET", "POST"])
    ctx = app.test_request_context(
        "/x?a=1", method="POST", json={"message": "hi"}, headers={"Authorization": "Basic dTpw"}
    )
    ctx.push()
    return app, ctx


def main():
    symbols = json.load(open(sys.argv[1]))
    roots = [a.split("=", 1)[1] for a in sys.argv[2:] if a.startswith("--deps=")]
    DEP_ROOTS.update(roots[0].split(",") if roots else {s.split(".")[0] for s in symbols})
    t0 = time.perf_counter()
    if "--flask-context" in sys.argv:
        flask_context()
    t1 = time.perf_counter()
    facts = {path: resolve(path) for path in symbols}
    t2 = time.perf_counter()
    versions = {}
    try:
        from importlib.metadata import version

        for root in sorted(DEP_ROOTS):
            try:
                versions[root] = version(root)
            except Exception:
                pass
    except ImportError:
        pass
    print(
        json.dumps(
            {
                "python": sys.version.split()[0],
                "versions": versions,
                "context_s": round(t1 - t0, 3),
                "probe_s": round(t2 - t1, 3),
                "facts": facts,
            },
            indent=1,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
