import subprocess
from unittest.mock import MagicMock, patch

import pymolt.ingestion.conda_ingest as conda_ingest
from pymolt.core.enums import Provenance, ResolutionQuality, SourceFixation
from pymolt.ingestion.conda_ingest import (
    find_conda_executable_in_container,
    find_conda_executable_on_host,
    ingest,
    parse_conda_solve_json,
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


def test_find_conda_executable_on_host_prefers_the_path(monkeypatch):
    monkeypatch.setattr(conda_ingest.shutil, "which",
                        lambda x: "/usr/bin/mamba" if x == "mamba" else None)
    assert find_conda_executable_on_host() == "/usr/bin/mamba"


def test_find_conda_executable_on_host_falls_back_to_a_home_install(monkeypatch, tmp_path):
    conda = tmp_path / "miniconda3" / "condabin" / "conda"
    conda.parent.mkdir(parents=True)
    conda.touch()
    monkeypatch.setattr(conda_ingest.shutil, "which", lambda x: None)
    monkeypatch.setattr(conda_ingest.Path, "home", classmethod(lambda cls: tmp_path))
    assert find_conda_executable_on_host() == str(conda)


def _which_in_container(available):
    """A `docker exec <id> which <exe>` boundary where only ``available`` exist."""
    calls = []

    def run(cmd, **kwargs):
        calls.append(list(cmd))
        exe = cmd[-1]
        if exe in available:
            return subprocess.CompletedProcess(cmd, 0, f"/opt/conda/bin/{exe}\n", "")
        return subprocess.CompletedProcess(cmd, 1, "", "")
    return run, calls


def test_find_conda_executable_in_container_takes_the_first_present(monkeypatch):
    run, calls = _which_in_container({"mamba", "conda"})
    monkeypatch.setattr(conda_ingest.subprocess, "run", run)
    assert find_conda_executable_in_container("dummy_container") == "mamba"
    assert calls == [["docker", "exec", "dummy_container", "which", "mamba"]]


def test_find_conda_executable_in_container_falls_back_to_conda(monkeypatch):
    run, calls = _which_in_container({"conda"})
    monkeypatch.setattr(conda_ingest.subprocess, "run", run)
    assert find_conda_executable_in_container("dummy_container") == "conda"
    assert calls == [["docker", "exec", "dummy_container", "which", "mamba"],
                     ["docker", "exec", "dummy_container", "which", "conda"]]


def test_find_conda_executable_in_container_none_when_absent(monkeypatch):
    run, calls = _which_in_container(set())
    monkeypatch.setattr(conda_ingest.subprocess, "run", run)
    assert find_conda_executable_in_container("dummy_container") is None
    assert [c[-1] for c in calls] == ["mamba", "conda", "micromamba"]


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
