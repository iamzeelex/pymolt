"""Runs *inside* each swept interpreter (3.6 - 3.14): stdlib only, 3.6-compatible syntax.

    python -I -c "<this file>" collect   < {"tops": [...], "classes": [...], "skip": [...]}
    python -I -c "<this file>" check     < {"names": ["os.getcwd", ...]}
    python -I -c "<this file>" tops      < {}

``tops`` lists the top-level stdlib module names on disk (for interpreters without
``sys.stdlib_module_names``, i.e. before 3.10), so modules that died before 3.10 are found.

``collect`` imports each top-level module (and, for packages, every public submodule),
and reports its public names: ``dir()`` entries that are not modules, plus ``__all__``,
plus one class level for the listed classes.

``check`` answers ``hasattr`` for names seen on *any* interpreter, because on 3.7-3.9 the
``collections`` ABC aliases are served by module ``__getattr__`` and ``dir()`` never
lists them. Per name it answers ``yes``, ``no`` or ``noimport`` (the containing module
failed to import for a reason other than being absent, so nothing can be said).
"""
import contextlib
import importlib
import inspect
import json
import os
import pkgutil
import sys
import warnings

warnings.simplefilter("ignore")


def _quiet_import(name):
    with open(os.devnull, "w") as sink:
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            return importlib.import_module(name)


def _status(name, exc):
    # "absent" only when this very module is what is missing.
    if isinstance(exc, ModuleNotFoundError) and getattr(exc, "name", None) == name:
        return "absent"
    return "noimport"


def _public(obj):
    names = set()
    for n in dir(obj):
        if n.startswith("_"):
            continue
        try:
            value = getattr(obj, n)
        except Exception:
            names.add(n)  # served by a lazy __getattr__ that raised: still a listed name
            continue
        if not inspect.ismodule(value):
            names.add(n)
    return names


def _all_names(mod):
    out = set()
    for n in getattr(mod, "__all__", None) or ():
        if not isinstance(n, str) or n.startswith("_"):
            continue
        try:
            value = getattr(mod, n)
        except Exception:
            out.add(n)
            continue
        if not inspect.ismodule(value):
            out.add(n)
    return out


def collect(request):
    skip = set(request["skip"])
    modules = {}

    def visit(name):
        parts = name.split(".")
        if any(p in skip or (p.startswith("_") and i > 0) for i, p in enumerate(parts)):
            return
        try:
            mod = _quiet_import(name)
        except BaseException as exc:  # noqa: BLE001 - SystemExit etc. from odd modules
            modules[name] = {"status": _status(name, exc)}
            return
        names = _public(mod) | _all_names(mod)
        modules[name] = {"status": "ok", "names": sorted(names)}
        path = getattr(mod, "__path__", None)
        if path:
            for info in pkgutil.iter_modules(path):
                visit(name + "." + info.name)

    for top in request["tops"]:
        visit(top)
    classes = {}
    for dotted in request["classes"]:
        mod_name, _, cls_name = dotted.rpartition(".")
        try:
            mod = _quiet_import(mod_name)
            cls = getattr(mod, cls_name)
        except BaseException:  # noqa: BLE001
            continue
        if inspect.isclass(cls):
            classes[dotted] = sorted(n for n in dir(cls) if not n.startswith("_"))
    version = "{}.{}.{}".format(*sys.version_info[:3])
    return {"version": version, "modules": modules, "classes": classes}


def resolve(dotted):
    parts = dotted.split(".")
    try:
        obj = _quiet_import(parts[0])
    except BaseException as exc:  # noqa: BLE001
        return {"absent": "no"}.get(_status(parts[0], exc), "noimport")
    prefix = parts[0]
    for part in parts[1:]:
        try:
            obj = getattr(obj, part)
            prefix += "." + part
            continue
        except AttributeError:
            pass
        except Exception:
            return "no"
        if inspect.ismodule(obj):
            full = prefix + "." + part
            try:
                obj = _quiet_import(full)
                prefix = full
                continue
            except BaseException as exc:  # noqa: BLE001
                return "no" if _status(full, exc) == "absent" else "noimport"
        return "no"
    return "yes"


def tops(request):
    stdlib_dir = os.path.dirname(os.__file__)
    found = set(sys.builtin_module_names)
    dirs = [stdlib_dir, os.path.join(stdlib_dir, "lib-dynload")]
    for info in pkgutil.iter_modules([d for d in dirs if os.path.isdir(d)]):
        found.add(info.name)
    return {"version": "{}.{}.{}".format(*sys.version_info[:3]), "tops": sorted(found)}


def check(request):
    present = {n: resolve(n) for n in request["names"]}
    return {"version": "{}.{}.{}".format(*sys.version_info[:3]), "present": present}


if __name__ == "__main__":
    request = json.loads(sys.stdin.read())
    result = {"collect": collect, "check": check, "tops": tops}[sys.argv[1]](request)
    sys.stdout.write(json.dumps(result))
