"""Install a ``.pth`` watcher hook into site-packages (the second Mode-B delivery form).

INJECTED RUNTIME helper — pure stdlib, Python 3.6+.

A ``.pth`` file whose line starts with ``import`` is executed at interpreter startup — the same
trick coverage.py uses to catch subprocesses. Installing one makes
``boundary_tracer.activate()`` run in EVERY interpreter of the environment (still a no-op
unless ``PYMOLT_TRACE_TARGET`` is set), which is what you want when the app spawns workers /
child processes.

Usage (inside the target environment/image):
    python -m pymolt.verify._install_hook            # install
    python -m pymolt.verify._install_hook --remove   # remove

Why an explicit step rather than packaging ``data-files``: the latter places the ``.pth``
relative to the install prefix and misses site-packages on some layouts; an explicit write is
portable. The ``sitecustomize`` form (see ``sitecustomize.py``) is the alternative that needs
no write access.

SCAFFOLD: wired and runnable, but exercised end-to-end only inside the devcontainer (the host
never traces an old Flask). Covered by the skipped integration test, not the host unit suite.
"""
import os
import site
import sys

PTH_NAME = "pymolt_trace.pth"
PTH_LINE = "import pymolt.verify.boundary_tracer; pymolt.verify.boundary_tracer.activate()\n"


def _site_dir():
    """First writeable site-packages of the current environment."""
    cands = []
    try:
        cands.extend(site.getsitepackages())
    except Exception:
        pass
    try:
        cands.append(site.getusersitepackages())
    except Exception:
        pass
    for d in cands:
        if d and os.path.isdir(d) and os.access(d, os.W_OK):
            return d
    raise RuntimeError("no writeable site-packages found; use the sitecustomize.py form instead")


def install():
    path = os.path.join(_site_dir(), PTH_NAME)
    with open(path, "w") as f:
        f.write(PTH_LINE)
    print("installed:", path)


def remove():
    path = os.path.join(_site_dir(), PTH_NAME)
    if os.path.exists(path):
        os.remove(path)
        print("removed:", path)
    else:
        print("not present:", path)


if __name__ == "__main__":
    if "--remove" in sys.argv:
        remove()
    else:
        install()
