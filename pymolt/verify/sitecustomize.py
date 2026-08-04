"""Mode-B watcher delivery via ``sitecustomize`` (one of two forms; see ``_install_hook``).

INJECTED RUNTIME — pure stdlib, Python 3.6+.

Python imports a module named ``sitecustomize`` automatically at interpreter startup if one is
found on the path. Drop this file's directory onto ``PYTHONPATH`` inside the target image and
every interpreter (including children) calls ``boundary_tracer.activate()`` — which is itself a
no-op unless ``PYMOLT_TRACE_TARGET`` is set. So the same image can toggle tracing purely by
environment, with zero edits to the application.

Devcontainer (flasgger, python:3.6) example:
    PYTHONPATH=/workspace/tests/.. \\
    PYMOLT_TRACE_TARGET=flask \\
    PYMOLT_TRACE_OUT=/tmp/old-{pid}.jsonl \\
        python -m flask run        # or: pytest, or the demo app / test client

Requires ``pymolt.verify`` to be importable in the target environment (e.g. the repo on
PYTHONPATH). Only the pure-stdlib injected modules are pulled in — no pydantic.
"""
try:
    from pymolt.verify.boundary_tracer import activate

    activate()
except Exception:
    # Startup hooks must never break the interpreter. If pymolt isn't importable or
    # activation fails, the application proceeds untraced.
    pass
