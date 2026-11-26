"""Capture ids: ``<captured_at as YYYYMMDDTHHMMSSZ>-<trace sha256[:8]>``.

A capture is stamped with its recording's sha256 and its id when it is made (every mode, and
diagnostic slots too); an older slot gets its id computed through a lenient cache, never
written back. The id names content, so it survives archiving and set-aside, and verify names
the captures it compared by it."""
from __future__ import annotations

import hashlib
import json
import os
import sys

import pytest
from pydantic import create_model
from typer.testing import CliRunner

from pymolt.ingestion.config import EnvConfig, ToolChoice
from pymolt.interfaces import views
from pymolt.interfaces.cli.commands import app
from pymolt.verify import service
from pymolt.verify.models import (
    CaptureMode,
    CaptureValidity,
    ContractSlot,
    ContractState,
    capture_id,
)
from pymolt.workorder.render import capture_name
from tests.fixtures.v02_loader import expected as v02_expected
from tests.fixtures.v02_loader import materialize

SHA = "b3e1a2c0" + "f" * 56


def _sha(path) -> str:
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def _script(root, name="app", body="import json\njson.loads('{}')\n"):
    path = root / f"{name}.py"
    path.write_text(body, encoding="utf-8")
    return path


def _capture(root, when="baseline", mode=CaptureMode.LIVE_COMMAND, **kw):
    script = _script(root, f"{when}_app", kw.pop("body", "import json\njson.loads('{}')\n"))
    return service.capture_named_trace(root, when, mode, command=[sys.executable, str(script)],
                                       target="json", **kw)


def _configure(root):
    (root / "requirements.txt").write_text("flask==2.0.3\n", encoding="utf-8")
    EnvConfig(selected_manifest="requirements.txt", selected_tool=ToolChoice.UV,
              base_python="3.11", target_python="3.12").save(
        root / ".pymolt" / "env_config.json")


def _assert_stamped(slot):
    assert slot.trace_sha256 == _sha(slot.trace_path)
    assert slot.capture_id == capture_id(slot.captured_at, slot.trace_sha256)
    assert slot.capture_id.endswith("-" + slot.trace_sha256[:8])


# ── the format ────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("captured_at", ["2026-11-20T06:06:20+00:00", "2026-11-20T06:06:20Z",
                                         "2026-11-20T08:06:20+02:00", "2026-11-20T06:06:20"])
def test_the_id_is_the_utc_second_and_eight_hex_of_the_trace(captured_at):
    assert capture_id(captured_at, SHA) == "20261120T060620Z-b3e1a2c0"


@pytest.mark.parametrize("captured_at, sha", [(None, SHA), ("", SHA), ("yesterday", SHA),
                                              ("2026-11-20T06:06:20+00:00", None),
                                              ("2026-11-20T06:06:20+00:00", "")])
def test_without_both_parts_there_is_no_id(captured_at, sha):
    assert capture_id(captured_at, sha) is None


def test_plan_md_keeps_its_stamp_only_name_without_a_sha():
    assert capture_name("2026-11-20T06:06:20+00:00", SHA) == "20261120T060620Z-b3e1a2c0"
    assert capture_name("2026-11-20T06:06:20+00:00", None) == "20261120T060620Z"
    assert capture_name(None, SHA) is None


# ── stamped at capture ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("mode", [CaptureMode.TEST_SUITE, CaptureMode.LIVE_COMMAND])
def test_a_capture_pymolt_runs_is_stamped_and_saved(tmp_path, mode):
    slot = _capture(tmp_path, mode=mode)
    assert slot.validity is CaptureValidity.VALID and slot.events > 0
    _assert_stamped(slot)
    saved = service.load_contract_state(tmp_path).baseline
    assert (saved.trace_sha256, saved.capture_id) == (slot.trace_sha256, slot.capture_id)
    raw = json.loads((tmp_path / ".pymolt" / "contract_state.json").read_text())
    assert raw["baseline"]["capture_id"] == slot.capture_id


def test_a_diagnostic_capture_is_stamped_too(tmp_path):
    slot = _capture(tmp_path, body="import json\njson.loads('{}')\nraise SystemExit(3)\n")
    assert slot.validity is CaptureValidity.COMMAND_FAILED
    assert not service.same_slot_path(tmp_path, slot.trace_path,
                                      ".pymolt/contract_traces/baseline.jsonl")
    _assert_stamped(slot)
    assert service.load_contract_state(tmp_path).diagnostic_captures[-1].capture_id == \
        slot.capture_id


def test_an_attached_capture_is_stamped(tmp_path):
    started = service.start_attached_capture(tmp_path, "baseline", target="requests")
    with open(started.out_path, "w") as stream:
        stream.write('{"q": "requests.get"}\n')
    slot = service.finalize_attached_capture(tmp_path, "baseline", started.out_path,
                                             target="requests")
    assert slot.mode is CaptureMode.LIVE_ATTACH and slot.validity is CaptureValidity.VALID
    _assert_stamped(slot)
    assert service.load_contract_state(tmp_path).baseline.capture_id == slot.capture_id


def test_a_replicate_is_stamped_with_its_own_id(tmp_path):
    _configure(tmp_path)
    base = _capture(tmp_path, mode=CaptureMode.TEST_SUITE)
    rep = service.capture_baseline_replicate(tmp_path)
    assert rep.validity is CaptureValidity.VALID and rep.replicate_of == base.captured_at
    _assert_stamped(rep)
    assert service.load_contract_state(tmp_path).baseline_replicate.capture_id == rep.capture_id


# ── an older slot: computed through a cache ───────────────────────────────────────────────


def _old_slot(root, body=b'{"q": "json.loads"}\n'):
    trace = root / ".pymolt" / "contract_traces" / "baseline.jsonl"
    trace.parent.mkdir(parents=True, exist_ok=True)
    trace.write_bytes(body)
    ContractState(baseline=ContractSlot(
        trace_path=str(trace), captured_at="2026-11-20T06:06:20+00:00",
        mode=CaptureMode.TEST_SUITE, validity=CaptureValidity.VALID,
    )).save(root / ".pymolt" / "contract_state.json")
    return service.load_contract_state(root).baseline, trace


@pytest.fixture
def hashes(monkeypatch):
    """Counts the recordings ``trace_sha256`` reads."""
    seen = []
    real = service.trace_sha256

    def counting(path):
        seen.append(str(path))
        return real(path)

    monkeypatch.setattr(service, "trace_sha256", counting)
    return seen


def test_an_old_slot_gets_its_id_computed_and_cached(tmp_path, hashes):
    slot, trace = _old_slot(tmp_path)
    state_bytes = (tmp_path / ".pymolt" / "contract_state.json").read_bytes()
    assert slot.capture_id is None and slot.trace_sha256 is None
    want = capture_id(slot.captured_at, _sha(trace))
    assert service.slot_capture_id(tmp_path, slot) == want
    cache = json.loads((tmp_path / service.CAPTURE_ID_CACHE).read_text())
    st = trace.stat()
    assert cache == {".pymolt/contract_traces/baseline.jsonl": {
        "size": st.st_size, "mtime_ns": st.st_mtime_ns, "sha256": _sha(trace)}}
    assert len(hashes) == 1
    assert service.slot_capture_id(tmp_path, slot) == want      # a hit: not read again
    assert len(hashes) == 1
    # Never written back into the state file.
    assert (tmp_path / ".pymolt" / "contract_state.json").read_bytes() == state_bytes
    assert "cache/" in (tmp_path / ".pymolt" / ".gitignore").read_text()


def test_a_touched_recording_is_hashed_again(tmp_path, hashes):
    slot, trace = _old_slot(tmp_path)
    first = service.slot_capture_id(tmp_path, slot)
    st = trace.stat()
    os.utime(trace, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    assert service.slot_capture_id(tmp_path, slot) == first     # same bytes, same id
    assert len(hashes) == 2
    trace.write_bytes(b'{"q": "json.dumps"}\n')
    changed = service.slot_capture_id(tmp_path, slot)
    assert changed == capture_id(slot.captured_at, _sha(trace)) != first


def test_a_damaged_cache_is_a_miss_and_is_rewritten(tmp_path, hashes):
    slot, trace = _old_slot(tmp_path)
    cache = tmp_path / service.CAPTURE_ID_CACHE
    cache.parent.mkdir(parents=True, exist_ok=True)
    for damage in ("{not json", "[1, 2]", json.dumps(
            {".pymolt/contract_traces/baseline.jsonl": {"size": "x"}})):
        cache.write_text(damage)
        assert service.slot_capture_id(tmp_path, slot) == capture_id(slot.captured_at,
                                                                      _sha(trace))
    assert len(hashes) == 3
    assert json.loads(cache.read_text())[".pymolt/contract_traces/baseline.jsonl"][
        "sha256"] == _sha(trace)


def test_a_stamped_slot_is_never_hashed_and_a_missing_recording_has_no_id(tmp_path, hashes):
    slot = _capture(tmp_path)
    hashes.clear()
    os.unlink(slot.trace_path)
    assert service.slot_capture_id(tmp_path, slot) == slot.capture_id and not hashes
    old, trace = _old_slot(tmp_path)
    trace.unlink()
    assert service.slot_capture_id(tmp_path, old) is None
    assert service.slot_capture_id(tmp_path, None) is None


# ── the id names content: archive, set-aside ──────────────────────────────────────────────


def test_an_archived_recording_keeps_its_id(tmp_path):
    first = _capture(tmp_path)
    second = _capture(tmp_path, body="import json\njson.dumps([1])\n")
    assert second.archived_previous and second.capture_id != first.capture_id
    # The archived file is the first capture's content: the same id names it.
    assert capture_id(first.captured_at, _sha(second.archived_previous)) == first.capture_id


def test_a_set_aside_capture_keeps_its_id(tmp_path):
    post = _capture(tmp_path, "post_migration")
    state = service.load_contract_state(tmp_path)
    moved = service.set_aside_post_migration(tmp_path, state, "f" * 32)
    aside = state.diagnostic_captures[-1]
    assert moved and aside.trace_path == moved and aside.set_aside_by == "f" * 32
    assert (aside.trace_sha256, aside.capture_id) == (post.trace_sha256, post.capture_id)
    assert _sha(moved) == post.trace_sha256


# ── pymolt 0.2 ────────────────────────────────────────────────────────────────────────────


def test_a_02_contract_state_loads_and_its_baseline_gets_an_id(tmp_path):
    exp = v02_expected()
    as_stored = materialize(tmp_path / "stored", relocate=False)
    base = service.load_contract_state(as_stored).baseline
    assert base.capture_id is None and base.trace_sha256 is None
    assert service.slot_capture_id(as_stored, base) == capture_id(
        exp["baseline"]["captured_at"], exp["baseline"]["trace_sha256"])
    relocated = materialize(tmp_path / "moved")
    base = service.load_contract_state(relocated).baseline
    trace = relocated / exp["baseline"]["trace"]
    assert service.slot_capture_id(relocated, base) == capture_id(base.captured_at, _sha(trace))


def test_02s_slot_model_reads_a_stamped_slot():
    """0.2's ``ContractSlot`` is this one without the two fields, with pydantic's default
    ``extra="ignore"``: it loads a file 0.3 wrote, dropping the keys."""
    assert ContractSlot.model_config.get("extra") in (None, "ignore")
    fields = {name: (info.annotation, info) for name, info in ContractSlot.model_fields.items()
              if name not in ("trace_sha256", "capture_id")}
    old_slot = create_model("ContractSlot02", **fields)
    stamped = ContractSlot(trace_path="t.jsonl", captured_at="2026-11-20T06:06:20+00:00",
                           mode=CaptureMode.TEST_SUITE, trace_sha256=SHA,
                           capture_id="20261120T060620Z-b3e1a2c0")
    read = old_slot.model_validate(stamped.model_dump(mode="json"))
    assert read.trace_path == "t.jsonl" and not hasattr(read, "capture_id")


# ── where it is shown ─────────────────────────────────────────────────────────────────────


def test_verify_names_the_captures_it_compared(tmp_path):
    base = _capture(tmp_path)
    post = _capture(tmp_path, "post_migration")
    report = service.build_contract_report_from_state(tmp_path)
    compared = report.compared
    assert (compared.before_capture, compared.before_capture_id) == ("baseline", base.capture_id)
    assert (compared.after_capture, compared.after_capture_id) == ("post-migration",
                                                                    post.capture_id)
    assert views.compared_captures_line(compared) == \
        f"baseline {base.capture_id} → post-migration {post.capture_id}"
    other = service.build_contract_report_from_state(
        tmp_path, trace_override=str(_script(tmp_path, "other", "")))
    assert other.compared.after_capture_id is None
    assert views.compared_captures_line(other.compared) == f"baseline {base.capture_id}"


def test_the_capture_and_verify_commands_print_the_ids(tmp_path):
    _configure(tmp_path)
    runner = CliRunner()
    ids = []
    for when in ("baseline", "post-migration"):
        script = _script(tmp_path, when.replace("-", "_"))
        result = runner.invoke(app, ["capture", when, "--mode", "tests", "--project-dir",
                                     str(tmp_path), "--target", "json", "--", sys.executable,
                                     str(script)])
        assert result.exit_code == 0, result.output
        slot = getattr(service.load_contract_state(tmp_path), when.replace("-", "_"))
        assert f"captured {slot.capture_id}: {slot.events} events -> " in result.output
        ids.append(slot.capture_id)
    verdict = runner.invoke(app, ["verify", str(tmp_path)])
    assert f"baseline {ids[0]}" in verdict.output and ids[1] in verdict.output
    saved = json.loads((tmp_path / ".pymolt" / "verification.json").read_text())
    compared = saved["contract"]["compared"]
    assert (compared["before_capture_id"], compared["after_capture_id"]) == tuple(ids)


def test_the_capture_view_carries_the_id(tmp_path):
    slot = _capture(tmp_path)
    view = views.capture_view(slot, when="baseline", state=service.load_contract_state(tmp_path))
    assert view["data"]["capture_id"] == slot.capture_id
