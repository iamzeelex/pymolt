from pymolt.scan.report import run_scan


def test_scan_reports_setup_py_edges_per_root(tmp_path):
    (tmp_path / "setup.py").write_text("""
from setuptools import setup
setup(install_requires=['a', 'b'], extras_require={'fast': ['c']})
""")
    sub = tmp_path / "svc"
    sub.mkdir()
    (sub / "setup.py").write_text("from setuptools import setup\nsetup(install_requires=load())\n")
    report = run_scan(tmp_path, contacts=False)
    root = report.inventory_by_root["."]
    assert [(e.name, e.group) for e in root.edges] == [("a", "main"), ("b", "main"), ("c", "fast")]
    assert {e.source_file for e in root.edges} == {"setup.py"}
    child = report.inventory_by_root["svc"]
    assert child.edges == []
    assert child.notes == ["setup.py: computed install_requires not read"]
