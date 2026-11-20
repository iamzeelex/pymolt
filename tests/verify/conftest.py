"""Shared fixtures for verify/ tests. No target app or coverage tool required.

Keep this conftest free of host-only imports (pydantic): it must stay collectable under the
pure-stdlib tox runtime envs (3.6-3.11) where the injected-runtime tests run without pydantic."""
import json

import pytest


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
