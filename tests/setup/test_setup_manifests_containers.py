import json

import pytest

from pymolt.ingestion import fallback_compiler as fc
from pymolt.ingestion.config import ToolChoice
from pymolt.setup.service import (
    SetupChoices,
    SetupError,
    apply_setup,
    check_setup_values,
    gather_setup_options,
)


@pytest.fixture(autouse=True)
def no_docker(monkeypatch):
    monkeypatch.setattr(fc, "_docker_inspect_running", lambda: [])


def _zipline_like(tmp_path):
    (tmp_path / "setup.py").write_text("from setuptools import setup\nsetup()\n")
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc" / "requirements_py36_locked.txt").write_text("numpy==1.15.0\n")
    return tmp_path


def test_setup_offers_and_accepts_a_subroot_manifest(tmp_path):
    proj = _zipline_like(tmp_path)
    options = gather_setup_options(proj)
    names = [m.name for m in options.manifests]
    assert "etc/requirements_py36_locked.txt" in names
    assert options.default_manifest == "setup.py"      # a sub-root is never the assumed default
    check_setup_values(options, manifest="etc/requirements_py36_locked.txt")


def test_explicit_manifest_path_not_listed_but_supported(tmp_path):
    proj = _zipline_like(tmp_path)
    (proj / "tests").mkdir()
    (proj / "tests" / "requirements.txt").write_text("pytest\n")   # fixtures dir: not a root
    options = gather_setup_options(proj)
    assert "tests/requirements.txt" not in [m.name for m in options.manifests]
    check_setup_values(options, manifest="tests/requirements.txt")


@pytest.mark.parametrize("ref, message", [
    ("etc/missing.txt", "no such file"),
    ("../outside.txt", "inside the project"),
    ("setup_notes.rst", "not a supported format"),
])
def test_bad_explicit_manifest_is_refused(tmp_path, ref, message):
    proj = _zipline_like(tmp_path)
    (proj / "setup_notes.rst").write_text("x")
    with pytest.raises(SetupError, match=message):
        check_setup_values(gather_setup_options(proj), manifest=ref)


def _inspect(name, source, dest="/work", cid="a" * 64):
    return {"Id": cid, "Name": f"/{name}", "Config": {"Labels": {}},
            "Mounts": [{"Type": "bind", "Source": str(source), "Destination": dest}]}


def test_only_containers_mounting_the_project_are_listed(tmp_path, monkeypatch):
    (tmp_path / "proj").mkdir()
    proj = _zipline_like(tmp_path / "proj")
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setattr(fc, "_docker_inspect_running", lambda: [
        _inspect("mounts-it", proj, cid="1" * 64),
        _inspect("mounts-parent", proj.parent, cid="2" * 64),
        _inspect("elsewhere", other, cid="3" * 64),
        {"Id": "4" * 64, "Name": "/no-mounts", "Config": {}, "Mounts": []},
    ])
    monkeypatch.setattr(fc, "_docker_python_version", lambda cid: "3.6")
    options = gather_setup_options(proj)
    assert [c.name for c in options.containers] == ["mounts-it", "mounts-parent"]
    assert [c.python for c in options.containers] == ["3.6", "3.6"]
    assert not any("No running container" in n for n in options.notes)


def test_no_mounting_container_is_said(tmp_path):
    options = gather_setup_options(_zipline_like(tmp_path))
    assert options.containers == []
    assert "No running container mounts this project." in options.notes
    container = next(t for t in options.tools if t.name == "container")
    assert container.available is False and "mounts" in container.detail


def _choices():
    return SetupChoices(selected_manifest="setup.py", selected_tool=ToolChoice.UV,
                        base_python="3.6", target_python="3.12")


def test_apply_setup_reports_the_gitignore_edit(tmp_path):
    (tmp_path / ".gitignore").write_text("build/")
    notes: list[str] = []
    apply_setup(tmp_path, _choices(), notes=notes)
    assert (tmp_path / ".gitignore").read_text() == "build/\n.pymolt/env_config.json\n"
    assert len(notes) == 1 and ".pymolt/env_config.json" in notes[0] and ".gitignore" in notes[0]

    notes.clear()
    apply_setup(tmp_path, _choices(), notes=notes)       # already there: nothing to say
    assert notes == []
    assert (tmp_path / ".gitignore").read_text().count(".pymolt/env_config.json") == 1


def test_apply_setup_without_gitignore_touches_nothing(tmp_path):
    notes: list[str] = []
    apply_setup(tmp_path, _choices(), notes=notes)
    assert not (tmp_path / ".gitignore").exists() and notes == []
    saved = json.loads((tmp_path / ".pymolt" / "env_config.json").read_text())
    assert saved["selected_manifest"] == "setup.py"
