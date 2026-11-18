"""Person attestations (design 5.6): the out-of-tree HMAC log, the terminal adapter and the
record's attested commands. The log and key live in temp dirs (``tests/conftest.py`` points
``XDG_STATE_HOME`` and ``XDG_CONFIG_HOME`` there): no test writes the real ones."""
from __future__ import annotations

import io
import json
import os
import stat
import sys
from pathlib import Path

import pytest

from pymolt.adapters.attest_tty import TtyPrompter
from pymolt.attest import AttestationStore, Attester, ask, files_digest, project_id

sys.path.insert(0, str(Path(__file__).resolve().parent / "workorder"))


def test_the_store_lives_outside_the_project_under_the_xdg_folders(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    store = AttestationStore()
    assert store.log == tmp_path / "state" / "pymolt" / "attestations.jsonl"
    assert store.key_path == tmp_path / "config" / "pymolt" / "attest.key"
    assert str(Path.home()) not in str(store.log)


def test_an_attestation_verifies_and_binds_its_fields(tmp_path):
    store = AttestationStore(tmp_path / "s", tmp_path / "c")
    record = store.append({"kind": "record", "project": project_id(tmp_path / "proj"),
                           "run_id": "r1", "step": "beh-1", "files": "abc"})
    assert record["id"].startswith("att-") and record["hmac"] and record["user"]
    assert stat.S_IMODE(os.stat(store.key_path).st_mode) == 0o600
    assert len(store.key_path.read_bytes()) == 32

    ok = store.check(record["id"], kind="record", project=tmp_path / "proj", run_id="r1",
                     step="beh-1", files="abc")
    assert ok.ok and ok.info()["id"] == record["id"]
    assert not store.check(record["id"], step="beh-2").ok
    assert "another files" in store.check(record["id"], files="xyz").reason
    assert "another project" in store.check(record["id"], project=tmp_path).reason
    assert not store.check("att-0000000000000000").ok
    assert not store.check(None).ok


def test_a_tampered_record_or_a_missing_key_does_not_verify(tmp_path):
    store = AttestationStore(tmp_path / "s", tmp_path / "c")
    record = store.append({"kind": "record", "step": "beh-1"})
    line = json.loads(store.log.read_text())
    line["step"] = "beh-2"                                  # moved to another step by hand
    store.log.write_text(json.dumps(line) + "\n")
    assert "HMAC differs" in store.check(record["id"], step="beh-2").reason

    other = AttestationStore(tmp_path / "s2", tmp_path / "c2")
    good = other.append({"kind": "record", "step": "beh-1"})
    other.key_path.unlink()
    assert "no attestation key" in other.check(good["id"]).reason


def test_a_record_written_with_another_key_does_not_verify(tmp_path):
    """A forged log line (an agent writing its own record with its own HMAC key)."""
    mine = AttestationStore(tmp_path / "s", tmp_path / "c")
    mine.append({"kind": "record"})                         # makes this machine's key
    forger = AttestationStore(tmp_path / "s", tmp_path / "forged-key")
    forged = forger.append({"kind": "record", "step": "beh-1"})
    assert "HMAC differs" in mine.check(forged["id"]).reason


def test_files_digest_ignores_order():
    a = [{"file": "x.py", "before": "1", "after": "2"}, {"file": "y.py", "before": "3",
                                                         "after": "4"}]
    assert files_digest(a) == files_digest(list(reversed(a)))
    assert files_digest(a) != files_digest(a[:1])


class _Tty(io.StringIO):
    def isatty(self):
        return True


def test_the_terminal_adapter_needs_two_terminals_and_no_machine_output():
    assert "agent" in TtyPrompter(agent=True, stdin=_Tty(), stdout=_Tty()).unavailable()
    assert "--json" in TtyPrompter(json_output=True, stdin=_Tty(), stdout=_Tty()).unavailable()
    assert "stdin is not a terminal" in TtyPrompter(stdin=io.StringIO(),
                                                    stdout=_Tty()).unavailable()
    assert "stdout is not a terminal" in TtyPrompter(stdin=_Tty(),
                                                     stdout=io.StringIO()).unavailable()
    assert TtyPrompter(stdin=_Tty(), stdout=_Tty()).unavailable() is None


def test_the_person_types_the_nonce_back(tmp_path):
    out = _Tty()
    good = TtyPrompter(stdin=_Tty("abc123\n"), stdout=out)
    assert good.confirm("Attest X", "abc123") and "type abc123" in out.getvalue()
    assert not TtyPrompter(stdin=_Tty("nope\n"), stdout=_Tty()).confirm("X", "abc123")

    store = AttestationStore(tmp_path / "s", tmp_path / "c")
    record, why = ask(None, store, "X", {"kind": "record"})
    assert record is None and "no person can attest" in why
    record, why = ask(TtyPrompter(stdin=io.StringIO(), stdout=_Tty()), store, "X", {})
    assert record is None and "not a terminal" in why

    class Wrong:
        def unavailable(self):
            return None

        def confirm(self, text, nonce):
            return False

    record, why = ask(Wrong(), store, "X", {})
    assert record is None and "nonce" in why
    assert not store.log.exists()                           # nothing attested, nothing written


# ── the record's attested commands ────────────────────────────────────────────────────────


def test_a_person_on_a_terminal_attests_a_record(tmp_path):
    from _verifying import M, edit, json_project, person

    from pymolt.workorder.record import record_step

    plan = json_project(tmp_path, dict(sid="beh-1", step_type="behaviour-change",
                                       executor="person", claim="intended"))
    attester = person(tmp_path)
    record_step(tmp_path, plan, "beh-1", by="person", actor="ana")
    edit(tmp_path, "y = json.dumps(v)", "y = json.dumps(v, cls=1)")

    result = record_step(tmp_path, plan, "beh-1", done=True, claim="intended",
                         expect="raise:TypeError", attestation=attester)

    assert result.attestation and result.attestation.startswith("att-")
    assert attester.prompter.asked and "beh-1" in attester.prompter.asked[0]
    ledger = json.loads((tmp_path / ".pymolt" / "apply_ledger.json").read_text())
    event = ledger["events"][-1]
    assert event["attestation"] == result.attestation
    check = AttestationStore().check(
        result.attestation, kind="record", project=tmp_path, run_id=event["run_id"],
        step="beh-1", claim="intended", expect="raise:TypeError",
        files=files_digest(event["files"]))
    assert check.ok
    assert {e["expect"] for e in ledger["entries"] if e["step_id"] == "beh-1"} == {
        "raise:TypeError"}
    assert M in result.files


def test_without_the_nonce_a_person_only_expectation_is_refused(tmp_path):
    from _verifying import edit, json_project, person

    from pymolt.workorder.apply import StepRefusal
    from pymolt.workorder.record import record_step

    plan = json_project(tmp_path, dict(sid="beh-1", step_type="behaviour-change",
                                       executor="person", claim="intended"))
    record_step(tmp_path, plan, "beh-1")
    edit(tmp_path, "y = json.dumps(v)", "y = json.dumps(v, cls=1)")
    with pytest.raises(StepRefusal, match="not attested: the nonce was not typed back"):
        record_step(tmp_path, plan, "beh-1", done=True, expect="raise:TypeError",
                    attestation=person(tmp_path, types=False))
    with pytest.raises(StepRefusal, match="refused without an attestation"):
        record_step(tmp_path, plan, "beh-1", done=True, expect="raise:TypeError",
                    attestation=Attester(prompter=TtyPrompter(agent=True)))
    # --by person with no attestation: recorded, unattested.
    done = record_step(tmp_path, plan, "beh-1", done=True,
                       attestation=person(tmp_path, types=False))
    assert done.attestation is None and any("unattested" in n for n in done.notes)
