"""The setup service is the shared core for interfaces: detection in,
choices in, EnvConfig out — no prompts, no printing."""

from pymolt.ingestion.config import ToolChoice
from pymolt.setup import (
    SetupChoices,
    apply_setup,
    build_target_options,
    gather_setup_options,
)


def test_gather_detects_manifest_and_defaults(tmp_path):
    (tmp_path / "requirements.txt").write_text("requests==2.31.0\n")
    options = gather_setup_options(tmp_path)

    assert [m.name for m in options.manifests] == ["requirements.txt"]
    assert options.default_manifest == "requirements.txt"
    # uv/system always among the tools; a default is always picked.
    assert options.default_tool in {t.name for t in options.tools}
    assert options.base_python_default


def test_gather_empty_dir_has_no_manifests(tmp_path):
    options = gather_setup_options(tmp_path)
    assert options.manifests == []
    assert any("No Python dependency sources" in n for n in options.notes)


def test_build_target_options_respects_floor():
    # Base 3.11 -> never offer anything below it.
    versions = build_target_options("3.11")
    assert versions
    assert all([int(p) for p in v.version.split(".")] >= [3, 11] for v in versions)
    assert sum(v.is_default for v in versions) <= 1


def _eol_on(days_from_now: int) -> str:
    from datetime import datetime, timedelta

    return (datetime.now() + timedelta(days=days_from_now)).strftime("%Y-%m-%d")


def test_build_target_options_default_is_lowest_supported_not_eol(monkeypatch):
    """A migration tool must not default to an already-EOL (or soon-EOL) target."""
    import pymolt.setup.service as svc

    monkeypatch.setattr(
        svc, "get_available_python_versions",
        lambda: ["3.7", "3.8", "3.10", "3.11", "3.12"],
    )
    monkeypatch.setattr(svc, "get_eol_versions", lambda: {
        "3.7": _eol_on(-900),   # eol
        "3.8": _eol_on(-300),   # eol
        "3.10": _eol_on(90),    # soon
        "3.11": _eol_on(400),   # supported
        "3.12": _eol_on(700),   # supported
    })

    versions = svc.build_target_options("3.6")
    assert [v.version for v in versions] == ["3.7", "3.8", "3.10", "3.11", "3.12"]
    default = next(v for v in versions if v.is_default)
    assert default.version == "3.11"          # lowest *supported*, not base+1 (3.7 is EOL)
    assert default.eol_status == "supported"
    assert sum(v.is_default for v in versions) == 1


def test_build_target_options_all_eol_falls_back_to_smallest_jump(monkeypatch):
    import pymolt.setup.service as svc

    monkeypatch.setattr(svc, "get_available_python_versions", lambda: ["3.7", "3.8"])
    monkeypatch.setattr(
        svc, "get_eol_versions",
        lambda: {"3.7": _eol_on(-900), "3.8": _eol_on(-300)},
    )

    versions = svc.build_target_options("3.7")
    default = next(v for v in versions if v.is_default)
    assert default.version == "3.8"           # legacy just-above-base behaviour


def test_apply_setup_writes_config(tmp_path):
    (tmp_path / "requirements.txt").write_text("requests==2.31.0\n")
    (tmp_path / ".gitignore").write_text("__pycache__/\n")
    choices = SetupChoices(
        selected_manifest="requirements.txt",
        selected_tool=ToolChoice.UV,
        base_python="3.8",
        target_python="3.12",
    )
    config = apply_setup(tmp_path, choices)

    assert config.target_python == "3.12"
    assert (tmp_path / ".pymolt" / "env_config.json").is_file()
    # Config is gitignored.
    assert ".pymolt/env_config.json" in (tmp_path / ".gitignore").read_text()


def test_apply_setup_persists_system_interpreter(tmp_path):
    (tmp_path / "requirements.txt").write_text("flask\n")
    choices = SetupChoices(
        selected_manifest="requirements.txt",
        selected_tool=ToolChoice.SYSTEM,
        base_python="3.8",
        target_python="3.12",
        system_python="/usr/bin/python3",
    )
    config = apply_setup(tmp_path, choices)
    assert config.system_python == "/usr/bin/python3"
    # Round-trips through the persisted file.
    import json
    saved = json.loads((tmp_path / ".pymolt" / "env_config.json").read_text())
    assert saved["system_python"] == "/usr/bin/python3"


def test_gather_exposes_interpreters(tmp_path):
    (tmp_path / "requirements.txt").write_text("flask\n")
    options = gather_setup_options(tmp_path)
    # At least the system python3/python should be discoverable in CI.
    assert isinstance(options.interpreters, list)
