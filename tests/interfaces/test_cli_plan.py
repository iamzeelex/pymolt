from __future__ import annotations

import json
import sys

from typer.testing import CliRunner

from pymolt.codemods import service as codemod_service
from pymolt.codemods.client import DependencyMigration
from pymolt.codemods.models import CodemodPattern, FilePreview, PreviewProgress
from pymolt.ingestion.config import EnvConfig, ToolChoice
from pymolt.interfaces.cli.commands import app
from pymolt.migration_plan import MigrationPlan
from pymolt.migration_state import MigrationReceipt
from pymolt.status import build_status
from pymolt.verify import service as verify_service
from pymolt.verify.models import CaptureMode

runner = CliRunner()


def _configured_project(tmp_path):
    (tmp_path / "requirements.txt").write_text("flask==2.0.3\n", encoding="utf-8")
    (tmp_path / "requirements-target.txt").write_text("flask==3.0.0\n", encoding="utf-8")
    EnvConfig(
        selected_manifest="requirements.txt",
        selected_tool=ToolChoice.UV,
        base_python="3.11",
        target_python="3.12",
    ).save(tmp_path / ".pymolt" / "env_config.json")
    script = tmp_path / "baseline.py"
    script.write_text("import json\njson.loads('{}')\n", encoding="utf-8")
    verify_service.capture_named_trace(
        tmp_path,
        "baseline",
        CaptureMode.LIVE_COMMAND,
        command=[sys.executable, str(script)],
        target="json",
    )


def _mock_plan_services(tmp_path, monkeypatch):
    source = "from flask.helpers import safe_join\n"
    path = tmp_path / "app.py"
    path.write_text(source, encoding="utf-8")
    pattern = CodemodPattern(
        old_qualname="flask.helpers.safe_join",
        new_qualname="werkzeug.utils.safe_join",
        kind="rewrite-import",
    )
    preview = FilePreview(
        path=str(path),
        old_source=source,
        new_source="from werkzeug.utils import safe_join\n",
        sites=1,
        patterns=[pattern],
    )
    migration = DependencyMigration("flask", "2.0.3", "3.0.0")
    monkeypatch.setattr(
        codemod_service,
        "resolve_codemod_migrations",
        lambda *a, **k: ([migration], "requirements-target.txt"),
    )

    def fake_preview(*args, **kwargs):
        if callback := kwargs.get("on_file"):
            callback(PreviewProgress(path=str(path), sites=1))
        assert kwargs["auto_apply_only"] is True
        return {"flask": [pattern]}, [preview]

    monkeypatch.setattr(codemod_service, "preview_codemods", fake_preview)
    return path, source


def test_plan_apply_rollback_cli_lifecycle(tmp_path, monkeypatch):
    _configured_project(tmp_path)
    source_path, original = _mock_plan_services(tmp_path, monkeypatch)
    patch_path = tmp_path / "migration.patch"

    planned = runner.invoke(app, [
        "plan", str(tmp_path), "--patch", str(patch_path),
    ])

    assert planned.exit_code == 0, planned.output
    assert "Migration plan" in planned.output
    assert "--- a/app.py" in planned.output
    assert source_path.read_text(encoding="utf-8") == original
    assert patch_path.is_file()
    plan = MigrationPlan.load(tmp_path)
    assert plan is not None

    applied = runner.invoke(app, ["apply", str(tmp_path)])

    assert applied.exit_code == 0, applied.output
    assert "Migration applied" in applied.output
    assert "werkzeug.utils" in source_path.read_text(encoding="utf-8")
    receipt = MigrationReceipt.load(tmp_path)
    assert receipt is not None and receipt.status == "applied"

    rolled_back = runner.invoke(app, ["rollback", str(tmp_path)])

    assert rolled_back.exit_code == 0, rolled_back.output
    assert "Migration rolled back" in rolled_back.output
    assert source_path.read_text(encoding="utf-8") == original
    receipt = MigrationReceipt.load(tmp_path)
    assert receipt is not None and receipt.status == "rolled_back"
    status = build_status(tmp_path)
    assert status.phase("plan").state == "stale"
    assert status.next_command == "pymolt plan ."


def test_plan_json_is_machine_readable(tmp_path, monkeypatch):
    _configured_project(tmp_path)
    _mock_plan_services(tmp_path, monkeypatch)

    result = runner.invoke(app, ["plan", str(tmp_path), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["files_to_change"] == 1
    assert payload["rewrite_sites"] == 1
    assert payload["plan_path"].endswith(".pymolt/migration_plan.json")


def test_apply_rejects_source_changed_after_plan(tmp_path, monkeypatch):
    _configured_project(tmp_path)
    source_path, original = _mock_plan_services(tmp_path, monkeypatch)
    assert runner.invoke(app, ["plan", str(tmp_path)]).exit_code == 0
    source_path.write_text(original + "# user edit\n", encoding="utf-8")

    result = runner.invoke(app, ["apply", str(tmp_path)])

    assert result.exit_code == 2
    assert "stale migration plan" in result.output
    assert source_path.read_text(encoding="utf-8").endswith("# user edit\n")


def test_rollback_refuses_to_discard_later_edits(tmp_path, monkeypatch):
    _configured_project(tmp_path)
    source_path, _original = _mock_plan_services(tmp_path, monkeypatch)
    assert runner.invoke(app, ["plan", str(tmp_path)]).exit_code == 0
    assert runner.invoke(app, ["apply", str(tmp_path)]).exit_code == 0
    source_path.write_text(
        source_path.read_text(encoding="utf-8") + "# post migration\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["rollback", str(tmp_path)])

    assert result.exit_code == 2
    assert "explicitly pass --force" in result.output
    assert source_path.read_text(encoding="utf-8").endswith("# post migration\n")
