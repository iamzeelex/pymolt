from pymolt.ingestion.orchestrator import orchestrate_ingestion
from pymolt.core.enums import Provenance


def test_orchestrate_prefers_lock_over_recompile(tmp_path, monkeypatch):
    """With a populated uv.lock present, the baseline graph is read from the lock
    verbatim (LOCK_PARSED) rather than recompiled."""
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'proj'\ndependencies = ['httpx']", encoding="utf-8"
    )
    (tmp_path / "uv.lock").write_text(
        """
version = 1

[[package]]
name = "proj"
version = "0.1.0"
source = { editable = "." }
dependencies = [{ name = "httpx" }]

[[package]]
name = "httpx"
version = "0.27.0"
source = { registry = "https://pypi.org/simple" }
""",
        encoding="utf-8",
    )

    import pymolt.ingestion.uv_runner as uv_runner

    def fail_if_called(*args, **kwargs):
        raise AssertionError("baseline must come from the lock, not a recompile")

    monkeypatch.setattr(uv_runner, "run_command", fail_if_called)

    graph, report = orchestrate_ingestion(tmp_path, base_python="3.12")
    assert graph.resolution_quality.value == "lock-parsed"
    assert graph.nodes["httpx"].version == "0.27.0"
    assert report.source_fixation == "pinned"


def test_orchestrate_conda_ingestion(tmp_path):
    env_file = tmp_path / "environment.yml"
    env_file.write_text("""
name: test-env
dependencies:
  - python=3.12
  - numpy>=1.22
  - pip:
    - requests==2.31.0
""", encoding="utf-8")
    
    # Run the orchestrator on this temporary directory
    graph, report = orchestrate_ingestion(tmp_path)
    
    assert graph.source_fixation.value == "intent"
    assert graph.resolution_quality.value == "declared-only"
    
    assert "python" in graph.nodes
    assert "numpy" in graph.nodes
    assert "requests" in graph.nodes
    
    assert graph.nodes["python"].provenance == Provenance.CONDA_FORGE
    assert graph.nodes["requests"].provenance == Provenance.PIP_IN_CONDA
    
    assert report.resolution_quality == "declared-only"
    assert report.source_fixation == "intent"
    
    # We should have warnings since it's not fully pinned
    assert len(report.warnings) > 0
    assert any("reconstruction" in w for w in report.warnings)
