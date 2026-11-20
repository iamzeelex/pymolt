"""``pymolt.state_io``: a state file is read strictly (missing is None; damaged or newer
raises ``StateFileError`` naming the file and what to do), written atomically, and changed
under the state lock."""
from __future__ import annotations

import json
import multiprocessing
import os
import pickle
import stat
import threading
import time
from pathlib import Path

import pytest
from pydantic import BaseModel

from pymolt.state_io import (
    StateFileError,
    StateLockTimeout,
    atomic_write_bytes,
    atomic_write_json,
    atomic_write_text,
    holds_state_lock,
    ignoring,
    load_state,
    read_state_json,
    state_lock,
)

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


# ── atomic writes ─────────────────────────────────────────────────────────────────────────


def _no_temp_files(directory) -> bool:
    return not [p for p in directory.iterdir() if p.name.endswith(".tmp")]


def test_a_failed_replace_leaves_the_old_file_and_no_temp_file(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    path.write_text('{"old": true}\n', encoding="utf-8")

    def refuse(src, dst):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", refuse)
    with pytest.raises(OSError, match="No space left"):
        atomic_write_json(path, {"new": True})

    assert path.read_text(encoding="utf-8") == '{"old": true}\n'
    assert _no_temp_files(tmp_path)


def test_a_failed_write_leaves_the_old_file_and_no_temp_file(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    path.write_bytes(b"old")

    def broken_fsync(fd):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(os, "fsync", broken_fsync)
    with pytest.raises(OSError, match="Input/output"):
        atomic_write_bytes(path, b"new")

    assert path.read_bytes() == b"old" and _no_temp_files(tmp_path)


def test_json_round_trips_with_indent_sorted_keys_and_a_final_newline(tmp_path):
    path = tmp_path / "deep" / "dir" / "state.json"            # parents are created

    atomic_write_json(path, {"b": 1, "a": [1, 2]}, indent=2, sort_keys=True)

    text = path.read_text(encoding="utf-8")
    assert text == json.dumps({"a": [1, 2], "b": 1}, indent=2, sort_keys=True) + "\n"
    assert json.loads(text) == {"a": [1, 2], "b": 1}
    atomic_write_json(path, {"x": "y"}, indent=None)
    assert path.read_text(encoding="utf-8") == '{"x": "y"}\n'


def test_a_replaced_file_keeps_its_permissions_and_a_new_one_gets_the_usual_ones(tmp_path):
    kept = tmp_path / "requirements-target.txt"
    kept.write_text("a==1\n", encoding="utf-8")
    os.chmod(kept, 0o640)
    fresh = tmp_path / "PLAN.md"

    atomic_write_text(kept, "a==2\n")
    atomic_write_text(fresh, "# plan\n")

    umask = os.umask(0)
    os.umask(umask)
    assert stat.S_IMODE(kept.stat().st_mode) == 0o640
    assert stat.S_IMODE(fresh.stat().st_mode) == 0o666 & ~umask
    assert kept.read_text(encoding="utf-8") == "a==2\n"


# ── the state lock ────────────────────────────────────────────────────────────────────────


def _append(path: Path, line: str) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(line + "\n")
        stream.flush()


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def _child_waits_for_the_lock(root: str, log: str) -> None:
    """In another process: say it is about to wait, take the lock, say it has it."""
    _append(Path(log), "child waiting")
    with state_lock(root, timeout=30, operation="child"):
        _append(Path(log), "child acquired")


def _wait_until(predicate, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        time.sleep(0.01)


def test_a_second_process_waits_until_the_first_releases_the_state_lock(tmp_path):
    log = tmp_path / "log"
    context = multiprocessing.get_context("spawn")

    with state_lock(tmp_path, operation="parent"):
        child = context.Process(target=_child_waits_for_the_lock, args=(str(tmp_path), str(log)))
        child.start()
        _wait_until(lambda: "child waiting" in _lines(log))
        time.sleep(0.3)                     # the child is inside state_lock, polling
        _append(log, "parent releasing")
    child.join(30)

    assert child.exitcode == 0
    assert _lines(log) == ["child waiting", "parent releasing", "child acquired"]


def _attempt(root, results: list, timeout: float = 0.2) -> None:
    try:
        with state_lock(root, timeout=timeout, operation="other"):
            results.append("acquired")
    except StateLockTimeout as exc:
        results.append(exc)


def _from_another_thread(root, timeout: float = 0.2):
    results: list = []
    thread = threading.Thread(target=_attempt, args=(root, results, timeout))
    thread.start()
    thread.join(10)
    return results[0]


def test_the_state_lock_is_reentrant_in_a_thread_and_held_until_the_outer_exit(tmp_path):
    with state_lock(tmp_path):
        with state_lock(tmp_path):                  # a locked writer calling another
            assert holds_state_lock(tmp_path)
        assert holds_state_lock(tmp_path)
        assert isinstance(_from_another_thread(tmp_path), StateLockTimeout)
    assert not holds_state_lock(tmp_path)
    assert _from_another_thread(tmp_path) == "acquired"


def test_a_state_lock_timeout_names_the_holder(tmp_path):
    with state_lock(tmp_path, operation="capture baseline"):
        error = _from_another_thread(tmp_path, timeout=0.1)

    assert isinstance(error, StateLockTimeout)
    assert f"pid {os.getpid()} (capture baseline) since " in str(error)
    assert "waited 0.1s for .pymolt/state.lock" in str(error)


def test_the_migration_lock_is_never_taken_inside_the_state_lock(tmp_path):
    from pymolt.migration_plan import _migration_lock

    with state_lock(tmp_path), pytest.raises(RuntimeError, match="lock order"):
        with _migration_lock(tmp_path, "apply"):
            pass
    with _migration_lock(tmp_path, "apply"), state_lock(tmp_path):    # the right order
        assert holds_state_lock(tmp_path)
