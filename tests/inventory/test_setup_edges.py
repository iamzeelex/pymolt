from pymolt.inventory.edges import build_inventory


def test_setup_py_edges_carry_setup_py_provenance(tmp_path):
    (tmp_path / "setup.py").write_text("""
from setuptools import setup
install_reqs = ['numpy>=1.9', 'git+https://github.com/o/dep.git#egg=dep']
setup(install_requires=install_reqs, extras_require={'fast': ['ujson==1.35']})
""")
    inv = build_inventory(tmp_path)
    got = {(e.name, e.group, e.source_file) for e in inv.edges}
    assert got == {("numpy", "main", "setup.py"), ("dep", "main", "setup.py"),
                   ("ujson", "fast", "setup.py")}
    assert inv.notes == []
    assert [e.kind.value for e in inv.edges if e.name == "dep"] == ["vcs"]


def test_computed_install_requires_becomes_a_note(tmp_path):
    (tmp_path / "setup.py").write_text(
        "from setuptools import setup\nsetup(install_requires=read('r.in'))\n")
    inv = build_inventory(tmp_path)
    assert inv.edges == []
    assert inv.notes == ["setup.py: computed install_requires not read"]


def test_setup_cfg_edges_and_no_duplicate_with_setup_py(tmp_path):
    (tmp_path / "setup.py").write_text(
        "from setuptools import setup\nsetup(install_requires=['click'])\n")
    (tmp_path / "setup.cfg").write_text(
        "[options]\ninstall_requires =\n    click\n    six\n")
    inv = build_inventory(tmp_path)
    assert sorted((e.name, e.source_file) for e in inv.edges) == [
        ("click", "setup.py"), ("six", "setup.cfg")]
