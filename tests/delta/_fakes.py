"""Builders for fake probe answers (the shape ``pymolt/delta/probe.py`` emits)."""

from __future__ import annotations


def shape(*params):
    """``shape(("a", "POSITIONAL_OR_KEYWORD", None), ...)`` → the probe's list form."""
    return [list(p) for p in params]


def step(attr, owner, *, exists=True, kind="callable", canonical=None, shape=None,
         error=None, owner_module=None, **extra):
    d = {"attr": attr, "owner": owner, "dep": True, "exists": exists,
         "owner_module": owner_module or owner.rsplit(".", 1)[0],
         "receiver_path": owner}
    if exists:
        d["kind"] = kind
        d["canonical"] = canonical or owner
        if shape is not None:
            d["shape"] = shape
    else:
        d["error"] = error or f"AttributeError: {attr}"
    d.update(extra)
    return d


def res(module, *steps, runtime_warnings=None, module_file=None):
    out = {"module": module, "steps": list(steps),
           "exists": all(s.get("exists") for s in steps)}
    if runtime_warnings:
        out["runtime_warnings"] = runtime_warnings
    if module_file:
        out["module_file"] = module_file
    return out


def missing(module, attr, owner=None):
    return res(module, step(attr, owner or f"{module}.{attr}", exists=False))
