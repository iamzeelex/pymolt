"""resolve_capture_env / container_is_running / capture_named_trace's auto env
resolution — the fix for the root-cause bug where a baseline capture of a
legacy (container-based) project silently ran against the local interpreter
instead of the configured container, corrupting the baseline<->post-migration
diff. All docker interaction is mocked; no real container is ever touched."""
from __future__ import annotations

from pymolt.ingestion.config import EnvConfig, ToolChoice
from pymolt.verify import service
from pymolt.verify.models import CaptureMode, ContractSlot


def _write_config(project_dir, **kwargs) -> None:
    cfg = EnvConfig(**kwargs)
    cfg.save(project_dir / ".pymolt" / "env_config.json")


class TestResolveCaptureEnv:
    def test_no_config_runs_locally(self, tmp_path):
        resolved = service.resolve_capture_env(tmp_path, "baseline")
        assert resolved == {
            "container": None, "workdir": None, "command_prefix": None,
            "note": "no saved config — running locally",
        }

    def test_baseline_container_running_selects_container(self, tmp_path, monkeypatch):
        _write_config(tmp_path, selected_tool=ToolChoice.CONTAINER, container_id="abc123def456")
        monkeypatch.setattr(service, "container_is_running", lambda cid: True)

        resolved = service.resolve_capture_env(tmp_path, "baseline")
        assert resolved["container"] == "abc123def456"
        assert resolved["command_prefix"] is None
        assert "abc123def456" in resolved["note"]

    def test_baseline_container_not_running_degrades_to_local(self, tmp_path, monkeypatch):
        _write_config(tmp_path, selected_tool=ToolChoice.CONTAINER, container_id="abc123def456")
        monkeypatch.setattr(service, "container_is_running", lambda cid: False)

        resolved = service.resolve_capture_env(tmp_path, "baseline")
        assert resolved["container"] is None
        assert "not running" in resolved["note"]
        assert "re-run setup" in resolved["note"]

    def test_post_migration_venv_yields_command_prefix(self, tmp_path):
        venv = tmp_path / ".venv_target"
        bin_dir = venv / "bin"
        bin_dir.mkdir(parents=True)
        python_path = bin_dir / "python"
        python_path.write_text("")  # existence is all resolve_capture_env checks

        _write_config(tmp_path, target_env_path=str(venv))
        resolved = service.resolve_capture_env(tmp_path, "post_migration")
        assert resolved["container"] is None
        assert resolved["command_prefix"] is not None
        assert resolved["command_prefix"][0] == str(python_path.resolve())
        assert resolved["command_prefix"][1] == "-m"
        assert "target env" in resolved["note"]

    def test_post_migration_no_venv_runs_locally(self, tmp_path):
        _write_config(tmp_path, target_env_path=str(tmp_path / "nonexistent"))
        resolved = service.resolve_capture_env(tmp_path, "post_migration")
        assert resolved["container"] is None
        assert resolved["command_prefix"] is None
        assert resolved["note"] == "running locally"

    def test_no_config_for_post_migration_runs_locally(self, tmp_path):
        resolved = service.resolve_capture_env(tmp_path, "post_migration")
        assert resolved["note"] == "no saved config — running locally"


class TestCaptureNamedTraceAutoSelectsEnv:
    def _stub_capture_trace(self, monkeypatch, calls: list):
        def _fake_capture_trace(target, command, out_path, backend, container=None,
                                 workdir=None, exclude="", source="", include_internal=False,
                                 cancel_event=None, cwd=None, privacy="values",
                                 sample_rate=1.0):
            calls.append({"container": container, "workdir": workdir, "command": command})
            from pymolt.verify.service import TraceCaptureResult

            return TraceCaptureResult(
                out_path=str(out_path), events=0, processes=0,
                where=f"container:{container}" if container else "local",
            )

        monkeypatch.setattr(service, "capture_trace", _fake_capture_trace)

    def test_baseline_auto_selects_configured_running_container(self, tmp_path, monkeypatch):
        _write_config(tmp_path, selected_tool=ToolChoice.CONTAINER, container_id="mycontainer")
        monkeypatch.setattr(service, "container_is_running", lambda cid: True)
        calls: list = []
        self._stub_capture_trace(monkeypatch, calls)

        slot = service.capture_named_trace(
            tmp_path, "baseline", CaptureMode.TEST_SUITE, command=["pytest", "tests/"],
        )
        assert calls == [{"container": "mycontainer", "workdir": None,
                          "command": ["pytest", "tests/"]}]
        assert slot.env_note is not None
        assert "mycontainer" in slot.env_note

    def test_explicit_container_arg_overrides_config(self, tmp_path, monkeypatch):
        _write_config(tmp_path, selected_tool=ToolChoice.CONTAINER, container_id="mycontainer")
        monkeypatch.setattr(service, "container_is_running", lambda cid: True)
        calls: list = []
        self._stub_capture_trace(monkeypatch, calls)

        slot = service.capture_named_trace(
            tmp_path, "baseline", CaptureMode.TEST_SUITE, command=["pytest", "tests/"],
            container="x",
        )
        assert calls == [{"container": "x", "workdir": None,
                          "command": ["pytest", "tests/"]}]
        # explicit container passed -> no auto-resolution note attached
        assert slot.env_note is None

    def test_no_config_project_never_shells_out_to_docker(self, tmp_path, monkeypatch):
        """A project with no .pymolt/env_config.json must behave exactly as
        before: locals, no docker shell-out — guards existing capture tests
        that don't set up a container config."""
        def _boom(*a, **kw):
            raise AssertionError("container_is_running should not be called with no config")

        monkeypatch.setattr(service, "container_is_running", _boom)
        calls: list = []
        self._stub_capture_trace(monkeypatch, calls)

        slot = service.capture_named_trace(
            tmp_path, "baseline", CaptureMode.TEST_SUITE, command=["pytest", "tests/"],
        )
        assert calls == [{"container": None, "workdir": None,
                          "command": ["pytest", "tests/"]}]
        assert slot.env_note == "no saved config — running locally"

    def test_post_capture_uses_configured_target_venv_executable(self, tmp_path, monkeypatch):
        bin_dir = tmp_path / ".target" / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "python").write_text("")
        pytest_exe = bin_dir / "pytest"
        pytest_exe.write_text("")
        _write_config(tmp_path, target_env_path=str(tmp_path / ".target"))
        calls: list = []
        self._stub_capture_trace(monkeypatch, calls)

        slot = service.capture_named_trace(
            tmp_path, "post_migration", CaptureMode.TEST_SUITE,
            command=["pytest", "tests/"],
        )

        assert calls == [{"container": None, "workdir": None,
                          "command": [str(pytest_exe), "tests/"]}]
        assert slot.command == [str(pytest_exe), "tests/"]


class TestContainerIsRunning:
    def test_no_docker_binary_returns_false(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: None)
        assert service.container_is_running("whatever") is False


class TestContractSlotBackwardCompatible:
    def test_old_shape_dict_without_env_note_loads(self):
        old_shape = {
            "trace_path": "/tmp/x.jsonl",
            "captured_at": "2026-07-03T00:00:00+00:00",
            "mode": "test_suite",
            "command": ["pytest"],
            "target": "all",
            "events": 3,
            "processes": 1,
            # no command_log, returncode, or env_note — pre-existing on-disk shape
        }
        slot = ContractSlot.model_validate(old_shape)
        assert slot.env_note is None
        assert slot.command_log is None
        assert slot.returncode is None
