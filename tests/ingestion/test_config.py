import json
import sys
from pathlib import Path
from typer.testing import CliRunner
from pymolt.interfaces.cli.commands import app
from pymolt.core.enums import ResolutionQuality, SourceFixation

runner = CliRunner()

def test_start_non_interactive(tmp_path, monkeypatch):
    # Prepare dummy source files in tmp_path
    req_file = tmp_path / "requirements.txt"
    req_file.write_text("requests==2.31.0\n", encoding="utf-8")
    
    # Mock isatty to return False to test non-interactive path
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    
    result = runner.invoke(app, ["setup", str(tmp_path)])
    assert result.exit_code == 0
    assert "Configuration saved" in result.stdout
    
    # Check that .pymolt/env_config.json was written
    config_file = tmp_path / ".pymolt" / "env_config.json"
    assert config_file.is_file()
    
    with open(config_file, "r") as f:
        config = json.load(f)
        
    assert config["selected_manifest"] == "requirements.txt"
    assert config["selected_tool"] in ("uv", "conda", "poetry", "system")
    assert config["base_python"] is not None


def test_start_interactive(tmp_path, monkeypatch):
    # Prepare multiple manifests
    (tmp_path / "requirements.txt").write_text("requests==2.31.0\n", encoding="utf-8")
    (tmp_path / "environment.yml").write_text("name: test_env\n", encoding="utf-8")
    
    # Mock Typer's and System's isatty to return True
    import typer.testing
    monkeypatch.setattr(typer.testing._NamedTextIOWrapper, "isatty", lambda self: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    
    # We will simulate user input:
    # 1. "2" for requirements.txt (as environment.yml is 1)
    # 2. "5" for system tool
    # 3. "3.8" for base Python version
    # 4. "3.12" for target Python version
    # 5. "y" for target env created
    # 6. ".venv_target" for target env path
    inputs = "2\n5\n3.8\n3.12\ny\n.venv_target\n"
    
    result = runner.invoke(app, ["setup", str(tmp_path)], input=inputs)
    print("INTERACTIVE OUT:")
    print(result.stdout)
    assert result.exit_code == 0
    assert "Configuration saved" in result.stdout
    
    config_file = tmp_path / ".pymolt" / "env_config.json"
    assert config_file.is_file()
    
    with open(config_file, "r") as f:
        config = json.load(f)
        
    assert config["selected_manifest"] == "requirements.txt"
    assert config["selected_tool"] == "system"
    assert config["base_python"] == "3.8"
    assert config["target_python"] == "3.12"
    assert config["target_env_created"] is True
    assert config["target_env_path"] == ".venv_target"


def test_audit_uses_saved_config(tmp_path, monkeypatch):
    # Write config
    pymolt_dir = tmp_path / ".pymolt"
    pymolt_dir.mkdir()
    config_file = pymolt_dir / "env_config.json"
    
    config_data = {
        "selected_manifest": "requirements.txt",
        "selected_tool": "system",
        "base_python": "3.8",
        "container_id": None
    }
    with open(config_file, "w") as f:
        json.dump(config_data, f)
        
    # Write requirements.txt
    (tmp_path / "requirements.txt").write_text("requests==2.31.0\n", encoding="utf-8")
    
    # Mock orchestrate_ingestion to avoid running the real heavy resolver
    import pymolt.interfaces.cli.commands as commands
    from unittest.mock import MagicMock
    
    mock_orchestrate = MagicMock()
    
    # Dummy graph and report
    from pymolt.core.graph import DependencyGraph
    from pymolt.core.layers import IngestionReport
    
    dummy_graph = DependencyGraph(
        nodes={},
        edges=[],
        roots=[],
        resolution_quality=ResolutionQuality.RESOLVED,
        source_fixation=SourceFixation.PINNED
    )
    dummy_report = IngestionReport(
        resolution_quality="resolved",
        source_fixation="pinned",
        manual_zone=[],
        warnings=[]
    )
    mock_orchestrate.return_value = (dummy_graph, dummy_report)
    
    monkeypatch.setattr("pymolt.assess.service.orchestrate_ingestion", mock_orchestrate)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    
    result = runner.invoke(app, ["assess", str(tmp_path), "--target-python", "3.10"])
    assert result.exit_code == 0
    
    # Verify mock was called twice: once for baseline, once for target Python
    assert mock_orchestrate.call_count == 2
    
    # Check baseline call
    call_args_baseline = mock_orchestrate.call_args_list[0]
    baseline_args, baseline_kwargs = call_args_baseline
    assert baseline_args[0] == str(tmp_path)
    assert baseline_kwargs["chosen_source"].path.name == "requirements.txt"
    assert baseline_kwargs["target_python"] == "3.10"
    assert baseline_kwargs["container_id"] is None
    assert baseline_kwargs["base_python"] == "3.8"
    
    # Check target call
    call_args_target = mock_orchestrate.call_args_list[1]
    target_args, target_kwargs = call_args_target
    assert target_args[0] == str(tmp_path)
    assert target_kwargs["chosen_source"].path.name == "requirements.txt"
    assert target_kwargs["target_python"] == "3.10"
    assert target_kwargs["container_id"] is None
    assert target_kwargs["base_python"] == "3.10"
    assert target_kwargs["constraint_file"] is None


def test_audit_fallback_warning(tmp_path, monkeypatch):
    # No config file is present in tmp_path
    (tmp_path / "requirements.txt").write_text("requests==2.31.0\n", encoding="utf-8")
    
    import pymolt.interfaces.cli.commands as commands
    from unittest.mock import MagicMock
    from pymolt.core.graph import DependencyGraph
    from pymolt.core.layers import IngestionReport
    
    mock_orchestrate = MagicMock()
    dummy_graph = DependencyGraph(
        nodes={},
        edges=[],
        roots=[],
        resolution_quality=ResolutionQuality.RESOLVED,
        source_fixation=SourceFixation.PINNED
    )
    dummy_report = IngestionReport(
        resolution_quality="resolved",
        source_fixation="pinned",
        manual_zone=[],
        warnings=[]
    )
    mock_orchestrate.return_value = (dummy_graph, dummy_report)
    
    monkeypatch.setattr("pymolt.assess.service.orchestrate_ingestion", mock_orchestrate)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    
    # Use f"{sys.version_info.major}.{sys.version_info.minor + 1}" to avoid downgrade error
    target_py = f"{sys.version_info.major}.{sys.version_info.minor + 1}"
    result = runner.invoke(app, ["assess", str(tmp_path), "--target-python", target_py])
    assert result.exit_code == 0
    # The warning is a diagnostic, so it goes to stderr — stdout carries the answer.
    assert "No environment configuration found" in result.stderr
    assert "No environment configuration found" not in result.stdout


def test_audit_with_constraints(tmp_path, monkeypatch):
    # Prepare dummy files
    (tmp_path / "requirements.txt").write_text("requests==2.31.0\n", encoding="utf-8")
    constraint_file = tmp_path / "constraints.txt"
    constraint_file.write_text("pandas<2.0\n", encoding="utf-8")
    
    import pymolt.interfaces.cli.commands as commands
    from unittest.mock import MagicMock
    from pymolt.core.graph import DependencyGraph
    from pymolt.core.layers import IngestionReport
    
    dummy_graph = DependencyGraph(
        nodes={},
        edges=[],
        roots=[],
        resolution_quality=ResolutionQuality.RESOLVED,
        source_fixation=SourceFixation.PINNED
    )
    dummy_report = IngestionReport(
        resolution_quality="resolved",
        source_fixation="pinned",
        manual_zone=[],
        warnings=[]
    )
    
    captured_constraints_content = None

    def mock_orchestrate_impl(*args, **kwargs):
        nonlocal captured_constraints_content
        constraint_file = kwargs.get("constraint_file")
        if constraint_file and Path(constraint_file).exists():
            captured_constraints_content = Path(constraint_file).read_text(encoding="utf-8")
        return dummy_graph, dummy_report

    mock_orchestrate = MagicMock(side_effect=mock_orchestrate_impl)
    
    monkeypatch.setattr("pymolt.assess.service.orchestrate_ingestion", mock_orchestrate)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    
    target_py = f"{sys.version_info.major}.{sys.version_info.minor + 1}"
    result = runner.invoke(app, [
        "assess", str(tmp_path), 
        "--target-python", target_py, 
        "--upgrade-constraint", str(constraint_file)
    ])
    assert result.exit_code == 0
    
    # Verify the target resolution call passed constraint_file Path object
    assert mock_orchestrate.call_count == 2
    assert captured_constraints_content is not None
    assert "pandas<2.0" in captured_constraints_content


def test_audit_json_output_is_pure_json(tmp_path, monkeypatch):
    # No config present: --json must still emit pure JSON (warning suppressed).
    (tmp_path / "requirements.txt").write_text("anyio==4.3.0\n", encoding="utf-8")

    import pymolt.interfaces.cli.commands as commands
    from unittest.mock import MagicMock
    from pymolt.core.graph import DependencyGraph, Node
    from pymolt.core.layers import IngestionReport
    from pymolt.core.enums import Mode, Provenance

    graph = DependencyGraph(
        nodes={"anyio": Node(
            name="anyio", version="4.3.0", mode=Mode.PYPI, provenance=Provenance.PYPI,
            direct=True, declared_requirement="anyio==4.3.0",
        )},
        edges=[], roots=["anyio"],
        resolution_quality=ResolutionQuality.LOCK_PARSED, source_fixation=SourceFixation.PINNED,
    )
    report = IngestionReport(
        resolution_quality="lock-parsed", source_fixation="pinned",
        manual_zone=[], warnings=[], detected_python="3.11",
    )
    _mock_orch = MagicMock(return_value=(graph, report))
    monkeypatch.setattr("pymolt.assess.service.orchestrate_ingestion", _mock_orch)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)

    target_py = f"{sys.version_info.major}.{sys.version_info.minor + 1}"
    result = runner.invoke(app, ["assess", str(tmp_path), "--target-python", target_py, "--json"])
    assert result.exit_code == 0

    # Entire stdout parses as JSON — no human warnings leaked in.
    data = json.loads(result.stdout)
    assert data["resolution_quality"] == "lock-parsed"
    assert data["target_resolved"] is True
    assert data["packages"][0]["name"] == "anyio"
    assert data["packages"][0]["status"] == "unchanged"
    assert "⚠️" not in result.stdout


def _pypi_target_graph():
    from pymolt.core.graph import DependencyGraph, Node
    from pymolt.core.enums import Mode, Provenance, ResolutionQuality, SourceFixation
    return DependencyGraph(
        nodes={
            "anyio": Node(name="anyio", version="4.3.0", mode=Mode.PYPI, provenance=Provenance.PYPI, direct=True),
            "idna": Node(name="idna", version="3.6", mode=Mode.PYPI, provenance=Provenance.PYPI, direct=False),
        },
        edges=[], roots=["anyio"],
        resolution_quality=ResolutionQuality.RESOLVED, source_fixation=SourceFixation.PINNED,
    )


def test_write_target_manifest_with_hashes(tmp_path, monkeypatch):
    import pymolt.assess.service as service
    monkeypatch.setattr(service, "fetch_pypi_hashes", lambda name, version: [f"hash_{name}"])

    path = service.write_target_manifest(tmp_path, _pypi_target_graph(), "uv", "3.12", with_hashes=True)
    assert path.name == "requirements-target.txt"
    text = path.read_text(encoding="utf-8")
    assert "anyio==4.3.0 \\\n    --hash=sha256:hash_anyio" in text
    assert "--hash=sha256:hash_idna" in text


def test_write_target_manifest_hashes_all_or_nothing(tmp_path, monkeypatch):
    import pymolt.assess.service as service
    # idna returns no hashes -> pip requires all-or-nothing, so fall back to plain.
    monkeypatch.setattr(
        service, "fetch_pypi_hashes",
        lambda name, version: ["h"] if name == "anyio" else [],
    )
    path = service.write_target_manifest(tmp_path, _pypi_target_graph(), "uv", "3.12", with_hashes=True)
    text = path.read_text(encoding="utf-8")
    assert "--hash=" not in text
    assert "hashes omitted" in text
    assert "anyio==4.3.0" in text


def test_write_target_manifest_conda(tmp_path):
    import pymolt.assess.service as service
    from pymolt.core.graph import DependencyGraph, Node
    from pymolt.core.enums import Mode, Provenance, ResolutionQuality, SourceFixation
    graph = DependencyGraph(
        nodes={
            "numpy": Node(name="numpy", version="1.24.3", mode=Mode.CONDA, provenance=Provenance.CONDA_FORGE, direct=True),
            "requests": Node(name="requests", version="2.31.0", mode=Mode.CONDA, provenance=Provenance.PIP_IN_CONDA, direct=True),
        },
        edges=[], roots=["numpy", "requests"],
        resolution_quality=ResolutionQuality.RESOLVED, source_fixation=SourceFixation.PINNED,
    )
    path = service.write_target_manifest(tmp_path, graph, "conda", "3.12", with_hashes=True)
    assert path.name == "environment-target.yml"
    text = path.read_text(encoding="utf-8")
    assert "- python=3.12" in text
    assert "- numpy=1.24.3" in text
    assert "  - pip:" in text
    assert "    - requests==2.31.0" in text


def test_audit_risk_in_json(tmp_path, monkeypatch):
    (tmp_path / "requirements.txt").write_text("vulnlib==1.0\n", encoding="utf-8")

    import pymolt.adapters.pypi_metadata as pypi_mod
    import pymolt.interfaces.cli.commands as commands
    import pymolt.risk.osv as osv_mod
    from unittest.mock import MagicMock
    from pymolt.core.graph import DependencyGraph, Node
    from pymolt.core.layers import IngestionReport
    from pymolt.core.enums import Mode, Provenance

    graph = DependencyGraph(
        nodes={"vulnlib": Node(name="vulnlib", version="1.0", mode=Mode.PYPI,
                               provenance=Provenance.PYPI, direct=True)},
        edges=[], roots=["vulnlib"],
        resolution_quality=ResolutionQuality.RESOLVED, source_fixation=SourceFixation.PINNED,
    )
    report = IngestionReport(resolution_quality="resolved", source_fixation="pinned",
                             manual_zone=[], warnings=[], detected_python="3.11")
    _mock_orch = MagicMock(return_value=(graph, report))
    monkeypatch.setattr("pymolt.assess.service.orchestrate_ingestion", _mock_orch)

    monkeypatch.setattr(osv_mod, "query_vulns", lambda name, version, client: (
        [{"id": "CVE-2024-1", "affected": [{"ranges": [{"events": [{"fixed": "2.0"}]}]}]}]
        if name == "vulnlib" else []
    ))
    monkeypatch.setattr(pypi_mod, "fetch_release_json", lambda *a, **k: {"urls": []})
    monkeypatch.setattr(pypi_mod, "fetch_package_json", lambda *a, **k: {"releases": {}})
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)

    target_py = f"{sys.version_info.major}.{sys.version_info.minor + 1}"
    result = runner.invoke(app, ["assess", str(tmp_path), "--target-python", target_py, "--json", "--risk"])
    assert result.exit_code == 0

    data = json.loads(result.stdout)
    assert "risk" in data
    pkg = data["risk"]["packages"][0]
    assert pkg["name"] == "vulnlib"
    assert pkg["tier"] == "high"
    assert pkg["open_cves"][0]["id"] == "CVE-2024-1"


def test_get_eol_versions():
    from pymolt.interfaces.cli.commands import get_eol_versions, format_eol_status
    eol_map = get_eol_versions()
    assert isinstance(eol_map, dict)
    assert "3.6" in eol_map
    assert eol_map["3.6"] == "2021-12-23"
    
    # Test format_eol_status for EOL
    eol_status_3_6 = format_eol_status("3.6", eol_map)
    assert "EOL since" in eol_status_3_6
    
    # Test format_eol_status for supported
    eol_status_3_13 = format_eol_status("3.13", eol_map)
    assert "Supported until" in eol_status_3_13 or "EOL soon" in eol_status_3_13 or "EOL since" in eol_status_3_13


def test_audit_interactive_overrides(tmp_path, monkeypatch):
    # Write config
    pymolt_dir = tmp_path / ".pymolt"
    pymolt_dir.mkdir()
    config_file = pymolt_dir / "env_config.json"
    
    config_data = {
        "selected_manifest": "requirements.txt",
        "selected_tool": "system",
        "base_python": "3.8",
        "container_id": None,
        "target_python": "3.10",
        "target_overrides": {}
    }
    with open(config_file, "w") as f:
        json.dump(config_data, f)
        
    (tmp_path / "requirements.txt").write_text("requests==2.31.0\n", encoding="utf-8")
    
    import pymolt.interfaces.cli.commands as commands
    from unittest.mock import MagicMock
    from pymolt.core.graph import DependencyGraph, Node
    from pymolt.core.layers import IngestionReport
    from pymolt.core.enums import Mode, Provenance
    
    # We will build a dummy graph with requests package so we can override it
    nodes = {
        "requests": Node(
            name="requests",
            version="2.31.0",
            mode=Mode.PYPI,
            provenance=Provenance.PYPI,
            direct=True,
            declared_requirement="requests==2.31.0",
            manual_bridge=False
        )
    }
    dummy_graph = DependencyGraph(
        nodes=nodes,
        edges=[],
        roots=["requests"],
        resolution_quality=ResolutionQuality.RESOLVED,
        source_fixation=SourceFixation.PINNED
    )
    dummy_report = IngestionReport(
        resolution_quality="resolved",
        source_fixation="pinned",
        manual_zone=[],
        warnings=[]
    )
    
    mock_orchestrate = MagicMock(return_value=(dummy_graph, dummy_report))
    monkeypatch.setattr("pymolt.assess.service.orchestrate_ingestion", mock_orchestrate)
    
    # Mock isatty to return True
    import typer.testing
    monkeypatch.setattr(typer.testing._NamedTextIOWrapper, "isatty", lambda self: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    
    # Simulate user input:
    # 1. "y" to "Do you want to customize target versions for specific packages?"
    # 2. "1" to select requests
    # 3. ">=2.32.0" as the override version/constraint
    # 4. "0" to finish customizing overrides (inner loop)
    # 5. "n" to the next "Do you want to customize target versions for specific packages?" (outer loop)
    inputs = "y\n1\n>=2.32.0\n0\nn\n"
    
    result = runner.invoke(app, ["assess", str(tmp_path)], input=inputs)
    assert result.exit_code == 0
    
    # Verify the config was written with the override
    with open(config_file, "r") as f:
        saved_config = json.load(f)
    assert saved_config["target_overrides"]["requests"] == ">=2.32.0"


def test_ingest_redirects_lockfile(tmp_path, monkeypatch):
    # Prepare both uv.lock and pyproject.toml
    lock_file = tmp_path / "uv.lock"
    lock_file.write_text("", encoding="utf-8")
    
    pyproject_file = tmp_path / "pyproject.toml"
    pyproject_file.write_text("[project]\nname = 'test'\ndependencies = ['requests']", encoding="utf-8")
    
    from pymolt.ingestion.detect import DiscoveredSource
    from pymolt.core.enums import Mode, SourceFixation
    
    chosen_source = DiscoveredSource(
        path=lock_file,
        mode=Mode.PYPI,
        is_lock=True,
        fixation=SourceFixation.PINNED
    )
    
    captured_cmd = None
    import pymolt.ingestion.uv_runner as uv_runner
    import subprocess
    
    def mock_run_command(cmd, **kwargs):
        nonlocal captured_cmd
        captured_cmd = cmd
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="requests==2.31.0\n  # via -r pyproject.toml\n", stderr="")
        
    monkeypatch.setattr(uv_runner, "run_command", mock_run_command)
    
    from pymolt.ingestion.orchestrator import orchestrate_ingestion
    graph, report = orchestrate_ingestion(tmp_path, chosen_source=chosen_source, target_python="3.12")
    
    assert captured_cmd is not None
    assert "pyproject.toml" in captured_cmd
    assert "uv.lock" not in captured_cmd



def test_fetch_pypi_versions(monkeypatch):
    from pymolt.interfaces.cli.commands import fetch_pypi_versions

    class DummyResponse:
        def __init__(self):
            self.data = b"""{
                "releases": {
                    "1.0.0": [],
                    "2.0.0a1": [],
                    "1.1.0": [],
                    "2.0.0": []
                }
            }"""
        def read(self):
            return self.data
        def decode(self, encoding):
            return self.data.decode(encoding)
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc_val, exc_tb):
            pass

    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=None: DummyResponse())

    versions = fetch_pypi_versions("pydantic")
    assert versions == ["2.0.0", "1.1.0", "1.0.0"]
