"""``verify --accept`` (design 5.2): finding ids, accepts bound to the finding, its statement, its
outcome fingerprint and both captures, ad-hoc ``x-<id>`` steps for unedited lines and hand
edits, expiry, ``--strict``, and the copied-fingerprint cases of the adversarial guard."""
from __future__ import annotations

import json

import pytest
from _verifying import capture, edit, json_project, person, site_entries, verify
from typer.testing import CliRunner

from pymolt.attest import Attester
from pymolt.interfaces.cli.commands import app
from pymolt.verify.accepts import AcceptRefusal, accept_finding, apply_accepts
from pymolt.verify.facts import Outcome
from pymolt.workorder.record import record_step

VALUE = ("y = json.dumps(v)", "y = json.dumps(v, indent=1)")


def _finding(report, line=6) -> str:
    ids = {e.get("finding") for e in site_entries(report, line) if e.get("finding")}
    assert len(ids) == 1, site_entries(report, line)
    return ids.pop()


def _accept(root, report, finding, *, attester=None):
    return accept_finding(root, report._fact_diff, finding,
                          attester=attester or person(root), ctx=report._accepts)


def _ledger(root):
    return json.loads((root / ".pymolt" / "apply_ledger.json").read_text())


def _hand_edit(root):
    """A hand edit (no record) that changes a value: a review finding."""
    json_project(root)
    edit(root, *VALUE)
    capture(root, "post_migration")
    return verify(root)


def test_failing_and_review_findings_carry_a_stable_id(tmp_path):
    (tmp_path / "review").mkdir()
    report = _hand_edit(tmp_path / "review")
    finding = _finding(report)
    summary = report.all_fact_diff["summary"]
    assert any(f["id"] == finding for r in summary["review"] for f in r["findings"])
    assert _finding(verify(tmp_path / "review")) == finding        # the same on every verify
    entry = site_entries(report)[0]
    assert entry["fingerprint"] and len(entry["fingerprint"]) == 64
    # A failing finding: a preserve step's recorded edit changed the value.
    failing = tmp_path / "failing"
    failing.mkdir()
    plan = json_project(failing, dict(sid="sig-1", step_type="api-change", executor="agent",
                                      claim="preserve"))
    record_step(failing, plan, "sig-1", agent=True)
    edit(failing, *VALUE)
    record_step(failing, plan, "sig-1", done=True, agent=True)
    capture(failing, "post_migration")
    report = verify(failing)
    assert report.all_fact_diff["verdict"] == "fail"
    [entry] = [e for e in site_entries(report) if e["fails"]]
    finding = entry["finding"]
    assert finding and any(f["id"] == finding
                           for f in report.all_fact_diff["summary"]["failing"])
    assert _finding(verify(failing)) == finding                   # the same on every verify
    assert entry["fingerprint"] and len(entry["fingerprint"]) == 64


def test_accepting_a_hand_edit_makes_an_ad_hoc_step_and_the_item_accepted(tmp_path):
    report = _hand_edit(tmp_path)
    assert report.all_fact_diff["verdict"] == "inconclusive"
    finding = _finding(report)

    done = _accept(tmp_path, report, finding)

    assert done.adhoc and done.step == f"x-{finding}"
    event = _ledger(tmp_path)["events"][-1]
    assert (event["event"], event["step_id"], event["finding"]) == ("accept", done.step, finding)
    assert event["adhoc"] == {"type": "review", "executor": "person", "claim": "intended"}
    assert set(event["captures"]) == {"baseline", "post"}
    assert event["anchor_new"]["stmt"] == "y = json.dumps(v, indent=1)"
    again = verify(tmp_path)
    accepted = [e for e in site_entries(again) if e.get("accepted")]
    assert accepted and accepted[0]["accepted"]["step"] == done.step
    assert again.all_fact_diff["verdict"] == "pass"
    assert again.all_fact_diff["summary"]["accepted_total"] == 1
    assert again.all_fact_diff["edited_sites"]["accepted"] == 1


def test_an_unedited_line_s_change_is_accepted_as_an_ad_hoc_step(tmp_path):
    json_project(tmp_path)
    run = tmp_path / "run.py"
    capture(tmp_path, "post_migration")
    # The code did not change, the input did: the library-behaviour case on an unedited line.
    from _verifying import RUN

    RUN_3 = RUN.replace("other(2)", "other(3)")
    run.write_text(RUN_3.format(root=str(tmp_path)))
    from pymolt.verify import service
    from pymolt.verify.models import CaptureMode

    service.capture_named_trace(tmp_path, "post_migration", CaptureMode.LIVE_COMMAND,
                                command=[__import__("sys").executable, str(run)],
                                target="json", privacy="values")
    report = verify(tmp_path)
    entries = site_entries(report, 13)
    assert {e["category"] for e in entries} == {"disappeared", "appeared"}
    assert all(e.get("provenance") in (None, "unedited") for e in entries)

    for finding in sorted({e["finding"] for e in entries}):     # one finding per side
        assert _accept(tmp_path, report, finding).adhoc

    again = verify(tmp_path)
    assert all(e.get("accepted") for e in site_entries(again, 13))
    assert again.all_fact_diff["verdict"] == "pass"


def test_an_accept_on_an_intended_step_s_line_is_that_step_s(tmp_path):
    plan = json_project(tmp_path, dict(sid="beh-1", step_type="behaviour-change",
                                       executor="person", claim="intended"))
    record_step(tmp_path, plan, "beh-1", agent=True)
    edit(tmp_path, *VALUE)
    record_step(tmp_path, plan, "beh-1", done=True, agent=True)
    capture(tmp_path, "post_migration")
    report = verify(tmp_path)
    assert any(e.get("review") for e in site_entries(report))

    done = _accept(tmp_path, report, _finding(report))

    assert (done.step, done.adhoc) == ("beh-1", False)
    assert verify(tmp_path).all_fact_diff["verdict"] == "pass"


def test_a_preserve_line_cannot_be_accepted(tmp_path):
    plan = json_project(tmp_path, dict(sid="sig-1", step_type="api-change", executor="agent",
                                       claim="preserve"))
    record_step(tmp_path, plan, "sig-1", agent=True)
    edit(tmp_path, *VALUE)
    record_step(tmp_path, plan, "sig-1", done=True, agent=True)
    capture(tmp_path, "post_migration")
    report = verify(tmp_path)
    with pytest.raises(AcceptRefusal, match="claims to preserve"):
        _accept(tmp_path, report, _finding(report))


def test_nobody_but_a_person_accepts(tmp_path):
    report = _hand_edit(tmp_path)
    with pytest.raises(AcceptRefusal, match="person's attested act"):
        accept_finding(tmp_path, report._fact_diff, _finding(report),
                       attester=Attester(prompter=None), ctx=report._accepts)


def test_an_unknown_finding_is_refused(tmp_path):
    report = _hand_edit(tmp_path)
    with pytest.raises(AcceptRefusal, match="no finding nope"):
        _accept(tmp_path, report, "nope")


# ── expiry ────────────────────────────────────────────────────────────────────────────────


def test_an_accept_expires_when_a_capture_is_replaced(tmp_path):
    report = _hand_edit(tmp_path)
    finding = _finding(report)
    _accept(tmp_path, report, finding)
    capture(tmp_path, "post_migration")                       # the same code, captured again

    again = verify(tmp_path)

    assert not any(e.get("accepted") for e in site_entries(again))
    assert again.all_fact_diff["verdict"] == "inconclusive"
    assert any(f"finding {finding}" in n and "post-migration capture it was accepted on was "
               "replaced" in n for n in again.notes)


def test_an_accept_expires_when_the_outcome_changes(tmp_path):
    report = _hand_edit(tmp_path)
    finding = _finding(report)
    _accept(tmp_path, report, finding)
    again = verify(tmp_path)
    fdiff, ctx = again._fact_diff, again._accepts
    for e in fdiff.entries:
        e.accepted = None
        if e.finding == finding:
            e.new = [Outcome(kind="return", value="something else")]

    notes = apply_accepts(fdiff, ctx)

    assert any("its fingerprint differs" in n for n in notes)
    assert not any(e.accepted for e in fdiff.entries)


def test_strict_reviews_every_accepted_change(tmp_path):
    from pymolt.verify import service

    report = _hand_edit(tmp_path)
    _accept(tmp_path, report, _finding(report))
    strict = service.build_contract_report_from_state(tmp_path, strict=True)
    entries = site_entries(strict)
    assert not any(e.get("accepted") for e in entries)
    assert any("--strict reviews every accepted change" in (e.get("review") or "")
               for e in entries)
    assert strict.all_fact_diff["verdict"] == "inconclusive"


# ── a copied fingerprint (the adversarial guard) ──────────────────────────────────────────


def _events(root):
    return _ledger(root)["events"]


def _write(root, data):
    (root / ".pymolt" / "apply_ledger.json").write_text(json.dumps(data))


def test_a_fingerprint_written_into_the_ledger_is_not_an_accept(tmp_path):
    report = _hand_edit(tmp_path)
    entry = site_entries(report)[0]
    from pymolt.apply_ledger import LedgerEvent, append_ledger

    append_ledger(tmp_path, [(LedgerEvent(
        event="accept", run_id="0" * 32, step_id="x-" + entry["finding"], at="now",
        attestation="att-0000000000000000", finding=entry["finding"],
        fingerprint=entry["fingerprint"], captures=report._accepts.captures,
        symbol=entry["symbol"]), [])])
    again = verify(tmp_path)
    assert again.all_fact_diff["verdict"] == "inconclusive"         # still the review
    assert any("not in this machine's attestation log" in n for n in again.notes)


def test_a_fingerprint_copied_onto_another_finding_is_not_an_accept(tmp_path):
    report = _hand_edit(tmp_path)
    finding = _finding(report)
    _accept(tmp_path, report, finding)
    edit(tmp_path, "return json.dumps([v])", "return json.dumps([v, v])")
    capture(tmp_path, "post_migration")
    report = verify(tmp_path)
    other = _finding(report, line=13)
    data = _ledger(tmp_path)
    copied = {**data["events"][-1], "seq": data["events"][-1]["seq"] + 1, "finding": other,
              "captures": report._accepts.captures, "step_id": f"x-{other}"}
    data["events"].append(copied)
    _write(tmp_path, data)

    again = verify(tmp_path)

    assert not any(e.get("accepted") for e in site_entries(again, 13))
    assert again.all_fact_diff["verdict"] == "inconclusive"         # line 13 is still a review


def test_an_accept_copied_to_a_later_capture_is_expired(tmp_path):
    """The same finding, captured again: the accept's captures no longer match, and rewriting
    them in the ledger breaks the attestation."""
    report = _hand_edit(tmp_path)
    finding = _finding(report)
    _accept(tmp_path, report, finding)
    capture(tmp_path, "post_migration")
    again = verify(tmp_path)
    data = _ledger(tmp_path)
    data["events"][-1]["captures"] = again._accepts.captures
    _write(tmp_path, data)

    third = verify(tmp_path)

    assert not any(e.get("accepted") for e in site_entries(third))
    assert any("is for another captures" in n for n in third.notes)


# ── the CLI ───────────────────────────────────────────────────────────────────────────────


def test_cli_accept_is_refused_under_agent_and_off_a_terminal(tmp_path):
    report = _hand_edit(tmp_path)
    finding = _finding(report)
    runner = CliRunner()
    agent = runner.invoke(app, ["verify", str(tmp_path), "--accept", finding, "--agent"])
    assert agent.exit_code == 2 and "refused under --agent" in agent.stdout
    plain = runner.invoke(app, ["verify", str(tmp_path), "--accept", finding])
    assert plain.exit_code == 2
    assert "not a terminal" in plain.output
    assert not [e for e in _events(tmp_path) if e["event"] == "accept"] if (
        tmp_path / ".pymolt" / "apply_ledger.json").exists() else True
