"""The adversarial-claims guard (design 8, 5.6): what an agent can write into ``.pymolt/`` or
record never turns a change into PASS. Each case runs real captures of code that calls
``json``, records with ``apply --step --record``, and reads the behaviour diff's own verdict
(the toy's empty static surface keeps the whole verdict INCONCLUSIVE anyway, so the diff's
verdict is the one that could pass)."""
from __future__ import annotations

import json

import pytest
from _verifying import capture, edit, json_project, person, site_entries, verify

from pymolt.workorder.apply import StepRefusal
from pymolt.workorder.record import record_step

RAISE = ("y = json.dumps(v)", "y = json.dumps(v, cls=1)")        # json.dumps now raises
VALUE = ("y = json.dumps(v)", "y = json.dumps(v, indent=1)")     # another value


def _intended(root):
    return json_project(root, dict(sid="beh-1", step_type="behaviour-change",
                                   executor="person", claim="intended"))


def _preserve(root):
    return json_project(root, dict(sid="sig-1", step_type="api-change", executor="agent",
                                   claim="preserve"))


def _record(root, plan, sid, change, **done):
    record_step(root, plan, sid, agent=done.pop("agent_begin", False))
    edit(root, *change)
    return record_step(root, plan, sid, done=True, **done)


def _diff_verdict(root) -> str:
    capture(root, "post_migration")
    report = verify(root)
    return report.all_fact_diff["verdict"], report


def _ledger(root):
    return json.loads((root / ".pymolt" / "apply_ledger.json").read_text())


def _write_ledger(root, data):
    (root / ".pymolt" / "apply_ledger.json").write_text(json.dumps(data))


# ── a new raise recorded as intended ──────────────────────────────────────────────────────


@pytest.mark.parametrize("expect", [None, "any", "representation"])
def test_a_new_raise_recorded_as_intended_by_an_agent_fails(tmp_path, expect):
    plan = _intended(tmp_path)
    _record(tmp_path, plan, "beh-1", RAISE, agent=True, agent_begin=True, expect=expect)
    verdict, report = _diff_verdict(tmp_path)
    assert verdict == "fail"
    assert any(e["fails"] for e in site_entries(report))


def test_an_agent_cannot_expect_the_raise(tmp_path):
    plan = _intended(tmp_path)
    record_step(tmp_path, plan, "beh-1", agent=True)
    edit(tmp_path, *RAISE)
    with pytest.raises(StepRefusal, match="refused without an attestation"):
        record_step(tmp_path, plan, "beh-1", done=True, agent=True, expect="raise:TypeError",
                    attestation=person(tmp_path))       # a terminal does not help an agent
    with pytest.raises(StepRefusal, match="refused without an attestation"):
        record_step(tmp_path, plan, "beh-1", done=True, expect="raise:TypeError")


def test_a_person_attested_raise_is_accepted(tmp_path):
    """The positive control: the one way a new raise does not fail."""
    plan = _intended(tmp_path)
    _record(tmp_path, plan, "beh-1", RAISE, expect="raise:TypeError",
            attestation=person(tmp_path))
    verdict, report = _diff_verdict(tmp_path)
    accepted = [e for e in site_entries(report) if e.get("accepted")]
    assert verdict == "pass" and accepted
    assert accepted[0]["accepted"]["how"] == "expectation raise:TypeError"
    assert accepted[0]["accepted"]["by"]


# ── forged attestations ───────────────────────────────────────────────────────────────────


def test_a_forged_attested_flag_and_attestation_id_in_the_ledger_change_nothing(tmp_path):
    plan = _intended(tmp_path)
    _record(tmp_path, plan, "beh-1", RAISE, agent=True, agent_begin=True)
    data = _ledger(tmp_path)
    for entry in data["entries"]:
        entry.update({"attested": True, "expect": "raise:TypeError",
                      "attestation": {"id": "att-1", "user": "boss"}})
    data["events"][-1]["attestation"] = "att-0123456789abcdef"
    _write_ledger(tmp_path, data)

    verdict, report = _diff_verdict(tmp_path)

    assert verdict == "fail"
    assert any("att-0123456789abcdef" in n and "judged as unattested" in n
               for n in report.notes)


def test_an_attestation_copied_from_another_record_does_not_verify(tmp_path):
    """A real attestation, of the same step's earlier record, moved onto a new change."""
    plan = _intended(tmp_path)
    first = _record(tmp_path, plan, "beh-1", ("y = json.dumps(v)", "y = json.dumps(v, cls=2)"),
                    expect="raise:TypeError", attestation=person(tmp_path))
    record_step(tmp_path, plan, "beh-1", agent=True)
    edit(tmp_path, "y = json.dumps(v, cls=2)", "y = json.dumps(v, indent=1)")
    record_step(tmp_path, plan, "beh-1", done=True, agent=True)
    data = _ledger(tmp_path)
    for entry in data["entries"]:
        if entry["epoch"] == data["events"][-1]["seq"]:
            entry["expect"] = "return:str"
    data["events"][-1]["attestation"] = first.attestation
    _write_ledger(tmp_path, data)

    verdict, report = _diff_verdict(tmp_path)

    assert verdict != "pass"
    assert not any(e.get("accepted") for e in site_entries(report))
    assert any(first.attestation in n for n in report.notes)


# ── edits beside a record, and a deleted entry ────────────────────────────────────────────


def test_an_unrecorded_edit_beside_a_recorded_one_never_passes(tmp_path):
    plan = _preserve(tmp_path)
    _record(tmp_path, plan, "sig-1", ("y = json.dumps(v)", "y = json.dumps(v, indent=None)"),
            agent=True, agent_begin=True)
    edit(tmp_path, "return json.dumps([v])", "return json.dumps([v, v])")   # not recorded

    verdict, report = _diff_verdict(tmp_path)

    assert verdict != "pass"
    beside = site_entries(report, line=13)
    assert beside and all(e.get("provenance") == "hand" for e in beside)
    assert any(e.get("review") or e.get("fails") for e in beside)
    assert all(not e["fails"] for e in site_entries(report))          # the record held


def test_a_preserve_fail_on_an_agent_edit(tmp_path):
    plan = _preserve(tmp_path)
    _record(tmp_path, plan, "sig-1", VALUE, agent=True, agent_begin=True)
    verdict, report = _diff_verdict(tmp_path)
    assert verdict == "fail"
    failing = [e for e in site_entries(report) if e["fails"]]
    assert failing and failing[0]["provenance"] == "ledger"
    assert failing[0]["step_id"] == "sig-1"


def test_a_deleted_preserve_entry_is_a_review_never_a_pass(tmp_path):
    plan = _preserve(tmp_path)
    _record(tmp_path, plan, "sig-1", VALUE, agent=True, agent_begin=True)
    data = _ledger(tmp_path)
    data["entries"] = [e for e in data["entries"] if e["step_id"] != "sig-1"]
    _write_ledger(tmp_path, data)

    verdict, report = _diff_verdict(tmp_path)

    assert verdict == "inconclusive"
    assert any(e.get("review") for e in site_entries(report))


def test_a_deleted_raise_entry_still_fails(tmp_path):
    plan = _preserve(tmp_path)
    _record(tmp_path, plan, "sig-1", RAISE, agent=True, agent_begin=True)
    (tmp_path / ".pymolt" / "apply_ledger.json").unlink()
    verdict, _report = _diff_verdict(tmp_path)
    assert verdict == "fail"
