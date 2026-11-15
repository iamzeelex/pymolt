import pytest

from pymolt.setup import service
from pymolt.setup.service import SetupError, check_setup_values, gather_setup_options


@pytest.fixture(autouse=True)
def no_docker(monkeypatch):
    monkeypatch.setattr(service, "list_running_containers", lambda: [])


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
