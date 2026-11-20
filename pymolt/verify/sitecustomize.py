"""Mode-B watcher delivery via ``sitecustomize`` (one of two forms; see ``_install_hook``).

INJECTED RUNTIME - pure stdlib. This file must stay ASCII-only and parse on every interpreter
it can be dropped into (2.7 included): it is the code that reports when the rest of the
runtime cannot load there.

Python imports a module named ``sitecustomize`` automatically at interpreter startup if one is
found on the path. Drop this file's directory onto ``PYTHONPATH`` inside the target image and
every interpreter (including children) calls ``boundary_tracer.activate()`` - which is itself a
no-op unless ``PYMOLT_TRACE_TARGET`` is set. So the same image can toggle tracing purely by
environment, with zero edits to the application.

Devcontainer (flasgger, python:3.6) example:
    PYTHONPATH=/workspace/tests/.. \\
    PYMOLT_TRACE_TARGET=flask \\
    PYMOLT_TRACE_OUT=/tmp/old-{pid}.jsonl \\
        python -m flask run        # or: pytest, or the demo app / test client

Requires ``pymolt.verify`` to be importable in the target environment (e.g. the repo on
PYTHONPATH). Only the pure-stdlib injected modules are pulled in - no pydantic.

Failure policy: a startup hook must never break the interpreter, so activation errors are
caught. But when tracing WAS requested (``PYMOLT_TRACE_TARGET`` set), an untraced run must not
look like a clean one: the error is reported on stderr and written next to the expected trace
as ``<trace>.activation-error.json`` for the host to surface.
"""


def _report_activation_failure(exc):
    import os
    import sys

    if not os.environ.get("PYMOLT_TRACE_TARGET"):
        return  # tracing not requested: nothing was promised, nothing to report
    message = "%s: %s" % (type(exc).__name__, exc)
    try:
        sys.stderr.write(
            "pymolt: tracing was requested but the watcher failed to activate in this "
            "interpreter (%s); this process runs UNTRACED\n" % message
        )
    except Exception:
        pass
    try:
        import json

        out = os.environ.get("PYMOLT_TRACE_OUT") or "./pymolt-trace-{pid}.jsonl"
        out = out.replace("{pid}", str(os.getpid()))
        payload = {
            "error": message,
            "error_type": type(exc).__name__,
            "pid": os.getpid(),
            "python": "%d.%d.%d" % tuple(sys.version_info[:3]),
            "executable": sys.executable,
        }
        with open(out + ".activation-error.json", "w") as fh:
            json.dump(payload, fh, sort_keys=True)
    except Exception:
        pass


try:
    from pymolt.verify.boundary_tracer import activate

    activate()
except Exception as _exc:  # noqa: BLE001 - see the failure policy above
    try:
        _report_activation_failure(_exc)
    except Exception:
        pass
