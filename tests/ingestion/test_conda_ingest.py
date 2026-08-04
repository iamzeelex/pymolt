from unittest.mock import patch, MagicMock
from pymolt.core.enums import Provenance, ResolutionQuality, SourceFixation
from pymolt.ingestion.conda_ingest import (
    parse_conda_solve_json,
    find_conda_executable_on_host,
    find_conda_executable_in_container,
    ingest
)


def test_parse_conda_solve_json_dict():
    json_text = """{
        "actions": {
            "LINK": [
                {"name": "numpy", "version": "1.24.3", "channel": "conda-forge"},
                {"name": "python", "version": "3.12.0", "channel": "defaults"}
            ]
        }
    }"""
    solved = parse_conda_solve_json(json_text)
    assert len(solved) == 2
    assert solved[0] == {"name": "numpy", "version": "1.24.3", "channel": "conda-forge"}
    assert solved[1] == {"name": "python", "version": "3.12.0", "channel": "defaults"}


def test_parse_conda_solve_json_strings():
    json_text = """{
        "actions": {
            "LINK": [
                "conda-forge::numpy-1.24.3-py312h123_0",
                "python-3.12.0-h456_1"
            ]
        }
    }"""
    solved = parse_conda_solve_json(json_text)
    assert len(solved) == 2
    assert solved[0] == {"name": "numpy", "version": "1.24.3", "channel": "conda-forge"}
    assert solved[1] == {"name": "python", "version": "3.12.0", "channel": ""}


def test_parse_conda_solve_json_with_logs():
    json_text = """
    Retrieving package metadata (repodata.json): ...working... done
    Solving environment: ...working... done
    {
        "actions": {
            "LINK": [
                {"name": "numpy", "version": "1.24.3", "channel": "conda-forge"}
            ]
        }
    }
    """
    solved = parse_conda_solve_json(json_text)
    assert len(solved) == 1
    assert solved[0] == {"name": "numpy", "version": "1.24.3", "channel": "conda-forge"}


@patch("pymolt.ingestion.conda_ingest.shutil.which")
@patch("pymolt.ingestion.conda_ingest.Path.is_file")
def test_find_conda_executable_on_host(mock_is_file, mock_which):
    # Case 1: shutil.which finds mamba
    mock_which.side_effect = lambda x: "/usr/bin/mamba" if x == "mamba" else None
    path = find_conda_executable_on_host()
    assert path == "/usr/bin/mamba"

    # Case 2: shutil.which finds nothing, but candidate miniconda file exists
    mock_which.side_effect = lambda x: None
    mock_is_file.side_effect = lambda: True
    path = find_conda_executable_on_host()
    assert path is not None
    assert "miniconda3" in path or "anaconda3" in path


@patch("pymolt.ingestion.conda_ingest.subprocess.run")
def test_find_conda_executable_in_container(mock_run):
    mock_res = MagicMock()
    mock_res.returncode = 0
    mock_res.stdout = "/opt/conda/bin/conda\n"
    mock_run.return_value = mock_res

    exe = find_conda_executable_in_container("dummy_container")
    assert exe == "mamba"  # since "mamba" is checked first and mock_run returned success
    mock_run.assert_called_once()


@patch("pymolt.ingestion.conda_ingest.find_conda_executable_on_host")
@patch("pymolt.ingestion.conda_ingest.run_command")
def test_ingest_conda_solve_local_success(mock_run_command, mock_find_conda, tmp_path):
    mock_find_conda.return_value = "/usr/bin/conda"
    
    mock_res = MagicMock()
    mock_res.stdout = """{
        "actions": {
            "LINK": [
                {"name": "numpy", "version": "1.24.3", "channel": "conda-forge"},
                {"name": "python", "version": "3.12.0", "channel": "defaults"}
            ]
        }
    }"""
    mock_run_command.return_value = mock_res

    env_file = tmp_path / "environment.yml"
    env_file.write_text("""
name: test-env
dependencies:
  - python=3.12
  - numpy>=1.22
  - pip:
    - requests==2.31.0
""", encoding="utf-8")

    graph = ingest(env_file)
    assert graph.resolution_quality == ResolutionQuality.RESOLVED
    assert graph.source_fixation == SourceFixation.PINNED
    
    # Assert nodes
    assert "numpy" in graph.nodes
    assert "python" in graph.nodes
    assert "requests" in graph.nodes
    
    # Direct vs transitive checking
    assert graph.nodes["numpy"].direct is True
    assert graph.nodes["numpy"].version == "1.24.3"
    assert graph.nodes["numpy"].declared_requirement == "numpy>=1.22"
    
    assert graph.nodes["requests"].direct is True
    assert graph.nodes["requests"].provenance == Provenance.PIP_IN_CONDA
    assert graph.nodes["requests"].declared_requirement == "requests==2.31.0"


def test_conda_lock_level2_recovers_edges_and_roles(tmp_path):
    """A conda-lock is the full closure: direct roles come from the env file and
    transitive edges are rebuilt from each package's dependencies."""
    env = tmp_path / "environment.yml"
    env.write_text(
        """
name: e
dependencies:
  - numpy
""",
        encoding="utf-8",
    )
    lock = tmp_path / "conda-lock.yml"
    lock.write_text(
        """
version: 1
package:
  - name: numpy
    version: "1.24.3"
    manager: conda
    dependencies:
      libblas: ">=3.9"
  - name: libblas
    version: "3.9.0"
    manager: conda
""",
        encoding="utf-8",
    )

    # No host solver: force the lock (Level 2) path.
    with patch("pymolt.ingestion.conda_ingest.find_conda_executable_on_host", return_value=None):
        graph = ingest(env, lock_file=lock)

    assert graph.resolution_quality == ResolutionQuality.LOCK_PARSED
    # numpy is declared in the env (direct); libblas is transitive.
    assert graph.nodes["numpy"].direct is True
    assert graph.nodes["libblas"].direct is False
    # Edge convention: source requires target. numpy -> libblas.
    assert any(e.source == "numpy" and e.target == "libblas" for e in graph.edges)
    assert graph.roots == ["numpy"]
