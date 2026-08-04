"""Shared fixtures for verify/ tests. No target app or coverage tool required.

NodeRef (pydantic, host-only) is imported *inside* the fixtures that need it, so this conftest
stays collectable under the pure-stdlib tox runtime envs (3.6-3.11) where the injected-runtime
tests run without pydantic installed."""
import json

import pytest


@pytest.fixture
def flask_node():
    from pymolt.verify.models import NodeRef
    return NodeRef(name="flask", old_version="2.0.3", new_version="2.2.2", trace_prefix="flask")


@pytest.fixture
def marshmallow_node():
    from pymolt.verify.models import NodeRef
    return NodeRef(name="marshmallow", old_version="3.0.0", new_version="3.20.0",
                   trace_prefix="marshmallow")


@pytest.fixture
def write_aggregated(tmp_path):
    """Write an aggregated-JSON recording (MemorySink.dump shape) and return its path."""
    def _write(name, records, target="flask"):
        p = tmp_path / name
        p.write_text(json.dumps({"target": target, "records": records}))
        return str(p)
    return _write


@pytest.fixture
def write_jsonl(tmp_path):
    """Write a streaming-JSONL recording (JsonlSink shape) and return its path."""
    def _write(name, events):
        p = tmp_path / name
        with p.open("w") as f:
            for ev in events:
                f.write(json.dumps(ev) + "\n")
        return str(p)
    return _write
