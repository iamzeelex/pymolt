"""FactSink snapshots as the host reads them.

The sink's own behaviour is covered in ``test_fact_sink.py``; these pin the contract
between the injected runtime's snapshot and the host's reader (``facts``): the snapshot
streams line by line, and the runtime's type labels agree with the host's.
"""
import json
import os

import pytest

from pymolt.verify._sinks import SNAPSHOT_SCHEMA, FactSink, type_label
from pymolt.verify.facts import _type_label, iter_snapshot, value_type

SITE = {"file": "/app/main.py", "line": 7, "func": "main"}


def _sink(tmp_path, **kw):
    kw.setdefault("flush_seconds", 0)
    return FactSink(str(tmp_path / "trace-{pid}.jsonl"), **kw)


def _snapshot(sink):
    with open(sink.path) as fh:
        return json.load(fh)


def test_snapshot_is_one_json_document_streamable_line_by_line(tmp_path):
    sink = _sink(tmp_path, metadata={"python": "3.12.1", "mode": "auto"})
    sink.on_return("m.f", {"bound": {"x": "a\nb"}}, "multi\nline", site=SITE, sym="m.f")
    sink.on_return("m.g", {"bound": {}}, None, site=SITE, sym="m.g")
    sink.close()
    doc = _snapshot(sink)
    assert doc["schema"] == SNAPSHOT_SCHEMA
    assert (doc["python"], doc["mode"], doc["pid"]) == ("3.12.1", "auto", os.getpid())
    streamed = list(iter_snapshot(sink.path))
    assert streamed[0][0] == "header" and streamed[0][1]["counters"] == doc["counters"]
    assert [o for k, o in streamed if k == "fact"] == doc["facts"]
    assert [o for k, o in streamed if k == "tally"] == doc["tallies"]


@pytest.mark.parametrize("value", [
    None, True, 3, 2.5, "s", [1], {"__opaque__": "pandas.Timestamp"},
    {"__opaque__": "float-nonfinite"}, {"__shape__": "int"}, {"__cycle__": "m.Node"},
    {"__bytes__": "00"}, {"__decimal__": "1.0"}, {"__uuid__": "x"}, {"__timedelta__": 1.0},
    {"__datetime__": "2026-01-01T00:00:00"}, {"__datetime__": "2026-01-01"},
    {"__datetime__": "10:00:00"}, {"__enum__": "m.Color.RED"}, {"__type__": "m.A"},
    {"__callable__": "m.f"}, {"__list__": [1]}, {"__set__": []}, {"__tuple__": [1, "a"]},
    {"__dict__": [["a", 1]]}, {"__dataclass__": "m.P", "fields": [["x", 1]]},
    {"__structure__": "pandas.DataFrame", "shape": [1, 1], "values": [[1]]},
])
def test_runtime_type_label_agrees_with_the_host(value):
    assert type_label(value) == _type_label(value_type(value))

