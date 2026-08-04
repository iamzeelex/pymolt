import json

from pymolt.ingestion.config import CONFIG_SCHEMA_VERSION, EnvConfig, ToolChoice


def test_envconfig_defaults_and_roundtrip(tmp_path):
    cfg = EnvConfig(selected_manifest="requirements.txt", selected_tool="uv", base_python="3.11")
    path = tmp_path / ".pymolt" / "env_config.json"
    cfg.save(path)

    assert path.is_file()
    raw = json.loads(path.read_text(encoding="utf-8"))
    # Serialized values are plain JSON (enum dumped to its string).
    assert raw["selected_tool"] == "uv"
    assert raw["schema_version"] == CONFIG_SCHEMA_VERSION
    assert raw["target_overrides"] == {}

    loaded = EnvConfig.load(path)
    assert loaded is not None
    assert loaded.selected_manifest == "requirements.txt"
    assert loaded.selected_tool is ToolChoice.UV
    assert loaded.base_python == "3.11"


def test_envconfig_migrates_legacy_file(tmp_path):
    # A pre-schema config (no schema_version) with an unknown legacy key.
    path = tmp_path / "env_config.json"
    path.write_text(
        json.dumps({
            "selected_manifest": "pyproject.toml",
            "selected_tool": "conda",
            "base_python": "3.9",
            "legacy_unused_key": "ignore me",
        }),
        encoding="utf-8",
    )
    cfg = EnvConfig.load(path)
    assert cfg is not None
    assert cfg.schema_version == CONFIG_SCHEMA_VERSION  # filled default
    assert cfg.selected_tool is ToolChoice.CONDA
    assert cfg.target_overrides == {}  # filled default
    # Unknown key is dropped on dump.
    assert "legacy_unused_key" not in cfg.model_dump()


def test_envconfig_load_missing_and_invalid(tmp_path):
    assert EnvConfig.load(tmp_path / "nope.json") is None

    bad = tmp_path / "bad.json"
    bad.write_text("{ not json", encoding="utf-8")
    assert EnvConfig.load(bad) is None

    invalid_tool = tmp_path / "invalid.json"
    invalid_tool.write_text(json.dumps({"selected_tool": "pixi"}), encoding="utf-8")
    assert EnvConfig.load(invalid_tool) is None
