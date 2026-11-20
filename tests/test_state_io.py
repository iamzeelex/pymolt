"""``pymolt.state_io``: a state file is read strictly (missing is None; damaged or newer
raises ``StateFileError`` naming the file and what to do)."""
from __future__ import annotations

import json
import pickle

import pytest
from pydantic import BaseModel

from pymolt.state_io import StateFileError, ignoring, load_state, read_state_json

KIND, REMEDY = "capture state", "Move it into .pymolt/archive/ to keep it, then capture again."


class _Model(BaseModel):
    schema_version: int = 1
    name: str


def _file(tmp_path, text: str):
    path = tmp_path / ".pymolt" / "contract_state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _read(path, newest=2):
    return read_state_json(path, kind=KIND, newest=newest, remedy=REMEDY)


def test_a_missing_file_is_none(tmp_path):
    assert _read(tmp_path / ".pymolt" / "contract_state.json") is None
    assert load_state(tmp_path / "nope.json", _Model, kind=KIND, newest=2, remedy=REMEDY) is None


@pytest.mark.parametrize(("text", "detail"), [
    ("not json{", "it is not JSON"),
    ("", "the file is empty"),
    ("[1, 2]", "it holds a JSON list, not an object"),
    ('"text"', "it holds a JSON string, not an object"),
    ('{"schema_version": "two"}', "its schema_version is 'two', not a number"),
])
def test_a_damaged_file_raises_naming_the_file_and_what_is_wrong(tmp_path, text, detail):
    path = _file(tmp_path, text)

    with pytest.raises(StateFileError) as info:
        _read(path)

    exc = info.value
    assert exc.problem == "damaged" and exc.kind == KIND and exc.path == path
    assert detail in exc.detail
    assert str(exc).startswith(".pymolt/contract_state.json (capture state) is damaged: ")
    assert "pymolt will not overwrite it." in str(exc) and str(exc).endswith(REMEDY)


def test_bytes_that_are_not_utf8_are_damaged(tmp_path):
    path = tmp_path / "state.json"
    path.write_bytes(b'{"a": "\xff\xfe"}')

    with pytest.raises(StateFileError, match="not UTF-8") as info:
        _read(path)
    assert info.value.problem == "damaged"


def test_a_directory_in_its_place_is_damaged(tmp_path):
    path = tmp_path / ".pymolt" / "contract_state.json"
    path.mkdir(parents=True)

    with pytest.raises(StateFileError, match="is a directory"):
        _read(path)


def test_a_newer_schema_is_refused_with_both_versions(tmp_path):
    path = _file(tmp_path, json.dumps({"schema_version": 3}))

    with pytest.raises(StateFileError) as info:
        _read(path, newest=2)

    exc = info.value
    assert exc.problem == "newer"
    assert str(exc) == (".pymolt/contract_state.json (capture state) was written by a newer "
                        "pymolt (schema 3; this pymolt reads up to 2). Upgrade pymolt.")


def test_an_older_or_current_schema_is_returned_for_the_callers_adapter(tmp_path):
    assert _read(_file(tmp_path, json.dumps({"schema_version": 1, "x": 1}))) == {
        "schema_version": 1, "x": 1}
    assert _read(_file(tmp_path, json.dumps({"x": 1}))) == {"x": 1}       # no version: 1


def test_a_file_with_no_version_is_not_checked_for_one(tmp_path):
    path = _file(tmp_path, json.dumps({"schema_version": 99}))
    assert read_state_json(path, kind=KIND, newest=None, remedy=REMEDY) == {"schema_version": 99}


def test_a_custom_version_reader_decides_newer(tmp_path):
    path = _file(tmp_path, json.dumps({"schema": "x/5"}))

    with pytest.raises(StateFileError, match=r"schema 5; this pymolt reads up to 2"):
        read_state_json(path, kind=KIND, newest=2, remedy=REMEDY,
                        version_of=lambda data: int(data["schema"].split("/")[1]))


def test_a_validation_error_is_damaged_with_its_first_problem(tmp_path):
    path = _file(tmp_path, json.dumps({"name": 7, "schema_version": "1x"}))

    with pytest.raises(StateFileError) as info:
        load_state(path, _Model, kind=KIND, newest=None, remedy=REMEDY)

    exc = info.value
    assert exc.problem == "damaged"
    assert exc.detail.startswith("schema_version: ") and "(and 1 more problem)" in exc.detail


def test_a_valid_file_loads_as_the_model(tmp_path):
    path = _file(tmp_path, json.dumps({"name": "a"}))
    assert load_state(path, _Model, kind=KIND, newest=1, remedy=REMEDY) == _Model(name="a")


def test_the_adapter_reads_the_object_and_its_validation_errors_are_damage(tmp_path):
    path = _file(tmp_path, json.dumps({"schema_version": 1, "old": "a"}))

    loaded = load_state(path, _Model, kind=KIND, newest=2, remedy=REMEDY,
                        adapt=lambda data: _Model(name=data["old"]))
    assert loaded == _Model(name="a")
    with pytest.raises(StateFileError):
        load_state(path, _Model, kind=KIND, newest=2, remedy=REMEDY,
                   adapt=lambda data: _Model.model_validate({"name": None}))


def test_the_error_has_a_headline_without_the_remedy_and_structured_fields(tmp_path):
    path = _file(tmp_path, "{")
    with pytest.raises(StateFileError) as info:
        _read(path)

    exc = info.value
    assert REMEDY not in exc.headline and str(exc) == f"{exc.headline} {REMEDY}"
    assert exc.fields() == {"path": str(path), "kind": KIND, "problem": "damaged",
                            "detail": exc.detail, "remedy": REMEDY}
    copy = pickle.loads(pickle.dumps(exc))
    assert str(copy) == str(exc) and copy.fields() == exc.fields()


def test_a_path_outside_pymolt_is_shown_as_given(tmp_path):
    path = tmp_path / "plan.json"
    path.write_text("{", encoding="utf-8")
    with pytest.raises(StateFileError) as info:
        _read(path)
    assert info.value.headline.startswith(f"{path} (capture state) is damaged")


def test_ignoring_reads_the_named_files_as_missing_inside_the_block_only(tmp_path):
    path = _file(tmp_path, "{")

    with ignoring([path]):
        assert _read(path) is None
    with pytest.raises(StateFileError):
        _read(path)
