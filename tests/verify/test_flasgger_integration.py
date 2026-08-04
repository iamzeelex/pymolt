"""Integration scaffold: real flasgger boundary trace across a Flask version pair.

SCAFFOLD — skipped on the host (only Python 3.11/3.12 here, and Flask 1.0.4 won't install on
them). Designed to run inside the devcontainer (`tests/.devcontainer`, python:3.6, flasgger +
pinned deps), where the Mode-B watcher injects into that interpreter. Enable with
``PYMOLT_VERIFY_INTEGRATION=1``.

Why a watcher (Mode B) and not record() (Mode A): flasgger is a Flask extension; its contract
with user code is exercised during HTTP request handling and Flask lifecycle hooks, not at
import or in a linear call. So we trace flasgger's own pytest suite (which drives the test
client through routes) with the watcher active.

How to produce the two artifacts inside the container (Flask 1.0.4 -> 2.0.3, both install on
3.6 and are in the fixture's tox.ini):

    cd /workspace/tests/artifacts/flasgger
    export PYTHONPATH=/workspace/..              # make pymolt.verify importable
    export PYMOLT_TRACE_TARGET=flask

    pip install 'flask==1.0.4'
    PYMOLT_TRACE_OUT=/tmp/old-{pid}.jsonl  python -m pytest -q tests/    # -> /tmp/old-*.jsonl
    # merge child-process files if any: cat /tmp/old-*.jsonl > /tmp/old.jsonl

    pip install 'flask==2.0.3'
    PYMOLT_TRACE_OUT=/tmp/new-{pid}.jsonl  python -m pytest -q tests/    # -> /tmp/new-*.jsonl
    # cat /tmp/new-*.jsonl > /tmp/new.jsonl

    PYMOLT_VERIFY_INTEGRATION=1 \
    PYMOLT_OLD_JSONL=/tmp/old.jsonl PYMOLT_NEW_JSONL=/tmp/new.jsonl \
        python -m pytest -q tests/verify/test_flasgger_integration.py

The watcher is pure stdlib, so C-level Werkzeug/Flask calls are classified by the backend and
must not crash the hook — proven here by the artifacts parsing cleanly.

DoD this covers: real-flasgger watcher capture during request handling under both versions;
BoundaryDiff classifies >=1 real behavioral change of the pinned pair; C-level calls survive.
"""
import os

import pytest

from pymolt.verify.diff import build_boundary_diff

pytestmark = pytest.mark.integration

_ENABLED = os.environ.get("PYMOLT_VERIFY_INTEGRATION") == "1"
_SKIP_REASON = (
    "flasgger integration runs in the python:3.6 devcontainer (Flask 1.0.4 -> 2.0.3); "
    "set PYMOLT_VERIFY_INTEGRATION=1 with PYMOLT_OLD_JSONL/PYMOLT_NEW_JSONL. See module docstring."
)


@pytest.mark.skipif(not _ENABLED, reason=_SKIP_REASON)
def test_flasgger_boundary_diff_across_flask_versions():
    old = os.environ["PYMOLT_OLD_JSONL"]
    new = os.environ["PYMOLT_NEW_JSONL"]

    diff = build_boundary_diff(old, new)

    # The hook survived C-level traffic: artifacts folded without error and captured contacts.
    assert (diff.counts()["appeared"] + diff.counts()["disappeared"]
            + diff.counts()["result_changed"] + diff.counts()["raise_changed"]
            + diff.counts()["skipped_opaque"]) > 0, "no flask boundary contacts captured"

    # The pinned pair has at least one real behavioral change at the flask boundary.
    assert not diff.is_clean(), "expected >=1 behavioral change across Flask 1.0.4 -> 2.0.3"
