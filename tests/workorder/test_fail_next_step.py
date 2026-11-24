"""What a FAIL leaves to a person (QA V8): the FAIL names the plan steps whose sites failed,
in effect or not (here a step that was never applied, and one that was rolled back), gives a
person's two options (fix and capture again, or accept an intended change on a terminal), and
`status` no longer points at a whole-run rollback that would undo the verified steps too."""
from __future__ import annotations

import json
import re

from _verifying import capture, edit, json_project
from typer.testing import CliRunner

from pymolt.interfaces.cli.commands import app
from pymolt.status import build_status
from pymolt.verify.models import VerificationVerdict
from pymolt.verify.unified import UnifiedVerificationReport
from pymolt.workorder.apply import skip_step
from pymolt.workorder.record import record_step
from pymolt.workorder.rollback import rollback_steps

RAISE = ("y = json.dumps(v)", "y = json.dumps(v, cls=1)")        # json.dumps now raises
SAME = ("y = json.dumps(v)", "y = json.dumps(v, indent=None)")   # equal outcome
runner = CliRunner()


def _plan(root):
    return json_project(root, dict(sid="beh-1", step_type="behaviour-change",
                                   executor="person", claim="preserve"))


def _verify(root, *extra):
    out = runner.invoke(app, ["verify", str(root), *extra], env={"COLUMNS": "200"})
    return out.exit_code, " ".join(re.sub(r"[│╭╮╰╯─]", " ", out.output).split())


def test_a_fail_at_a_planned_steps_site_names_it_and_a_persons_options(tmp_path):
    _plan(tmp_path)
    edit(tmp_path, *RAISE)                     # by hand, outside any step
    capture(tmp_path, "post_migration")

    code, text = _verify(tmp_path)

    assert code == 1
    saved = UnifiedVerificationReport.load(tmp_path)
    assert saved.verdict is VerificationVerdict.FAIL
    (step,) = saved.failing_steps
    assert (step["id"], step["status"], step["executor"]) == ("beh-1", "planned", "person")
    assert step["findings"]
    assert "Failing at the sites of plan step(s) beh-1 (planned)." in text
    assert (f"or accept an intended change with `pymolt verify {tmp_path} --accept ID` on a "
            "terminal") in text

    view = json.loads(runner.invoke(app, ["verify", str(tmp_path), "--agent"]).output)
    assert [s["id"] for s in view["data"]["failing_steps"]] == ["beh-1"]
    assert "at the sites of plan step(s) beh-1 (planned)" in view["hint"]
    assert "Only a person accepts an intended change" in view["hint"]

    status = build_status(tmp_path)                    # no apply run: made by hand
    assert status.next_command is None
    assert "FAIL at the sites of plan step(s) beh-1 (planned)" in status.next_reason
    assert "nothing to roll back" in status.next_reason
    assert "`pymolt verify . --accept ID`" in status.next_reason


def test_a_fail_at_a_rolled_back_steps_site_says_how_to_bring_it_back(tmp_path):
    plan = _plan(tmp_path)
    record_step(tmp_path, plan, "beh-1")
    edit(tmp_path, *SAME)
    record_step(tmp_path, plan, "beh-1", done=True)
    skip_step(tmp_path, plan, "env-1", "pinned by hand")      # else rule 6 names it first
    rollback_steps(tmp_path, step="beh-1")
    edit(tmp_path, *RAISE)
    capture(tmp_path, "post_migration")

    code, text = _verify(tmp_path)

    assert code == 1
    assert "Failing at the sites of plan step(s) beh-1 (rolled_back)." in text
    assert "Rolled back: `pymolt apply --step beh-1` brings it back." in text
    status = build_status(tmp_path)
    assert status.next_command is None                 # never `pymolt rollback .`
    assert "(`pymolt apply --step beh-1` brings the rolled-back step back)" in status.next_reason
    assert "`pymolt verify . --accept ID`" in status.next_reason
