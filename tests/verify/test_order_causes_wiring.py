"""The report hands the fact diff the dict-order causes of the Pythons the captures ran."""
import shutil
from pathlib import Path

from pymolt.verify.facts import FactSet
from pymolt.verify.report import _order_causes

FIXTURE = Path(__file__).resolve().parents[1] / "pyversion" / "data" / "locust_stats"


def _fs(*versions):
    fs = FactSet()
    fs.python_versions = list(versions)
    return fs


def _project(tmp_path):
    shutil.copytree(FIXTURE, tmp_path / "locust")
    return tmp_path


def test_causes_follow_the_pythons_the_captures_ran(tmp_path):
    root = _project(tmp_path)
    notes = []
    causes = _order_causes(root, _fs("3.8.20"), _fs("3.12.11"), notes)
    assert causes and not notes
    entry = [c for c in causes if "last_request_timestamp" in c.keys]
    assert entry, [sorted(c.keys) for c in causes]
    c = entry[0]
    assert c.old[0] == "last_request_timestamp" and c.new[:2] == ("name", "method")
    assert c.cause == "typing-typeddict-annotations-order"


def test_no_causes_when_the_move_does_not_cross_3_9(tmp_path):
    root = _project(tmp_path)
    assert _order_causes(root, _fs("3.10.18"), _fs("3.12.11"), []) == []


def test_no_causes_when_a_capture_python_is_unknown_or_mixed(tmp_path):
    root = _project(tmp_path)
    assert _order_causes(root, _fs(), _fs("3.12.11"), []) == []
    assert _order_causes(root, _fs("3.8.20", "3.12.11"), _fs("3.12.11"), []) == []
