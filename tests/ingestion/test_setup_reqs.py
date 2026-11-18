from pymolt.ingestion.detect import (
    detect_manifest,
    detect_sources,
    extract_declared_requirements,
    find_source,
)
from pymolt.ingestion.setup_reqs import parse_setup_cfg, parse_setup_py


def _py(tmp_path, body):
    p = tmp_path / "setup.py"
    p.write_text(body)
    return p


def _by_group(deps):
    out = {}
    for r in deps.requirements:
        out.setdefault(r.group, []).append(r.raw)
    return out


def test_literal_install_requires_and_extras(tmp_path):
    deps = parse_setup_py(_py(tmp_path, """
from setuptools import setup
setup(
    name='x',
    install_requires=['h2>=2.4', 'hyperframe'],
    extras_require={'fast': ['ujson==1.35', 'uvloop'], 'dev': ['pytest']},
)
"""))
    assert _by_group(deps) == {"main": ["h2>=2.4", "hyperframe"],
                               "fast": ["ujson==1.35", "uvloop"], "dev": ["pytest"]}
    assert deps.notes == []


def test_module_level_names_and_addition(tmp_path):
    deps = parse_setup_py(_py(tmp_path, """
import setuptools
install_reqs = ['numpy>=1.9']
more = ['six']
extra_reqs = {'test': ['nose']}
if __name__ == '__main__':
    setuptools.setup(install_requires=install_reqs + more, extras_require=extra_reqs)
"""))
    assert _by_group(deps) == {"main": ["numpy>=1.9", "six"], "test": ["nose"]}


def test_extra_key_marker_is_split_off(tmp_path):
    deps = parse_setup_py(_py(tmp_path, """
from setuptools import setup
setup(extras_require={
    ':python_version == "2.7"': ['enum34'],
    'a:sys_platform == "win32"': ['pywin'],
})
"""))
    got = {(r.raw, r.group, r.marker) for r in deps.requirements}
    assert got == {("enum34", "main", 'python_version == "2.7"'),
                   ("pywin", "a", 'sys_platform == "win32"')}


def test_computed_values_are_noted_not_executed(tmp_path):
    marker = tmp_path / "ran"
    deps = parse_setup_py(_py(tmp_path, f"""
from setuptools import setup
def read(p):
    open({str(marker)!r}, 'w').write('x')
    return [l for l in open(p)]
setup(install_requires=read('requirements.in'), extras_require=build_extras())
"""))
    assert deps.requirements == []
    assert deps.notes == ["computed install_requires not read", "computed extras_require not read"]
    assert not marker.exists()


def test_partly_computed_list_keeps_nothing_and_notes(tmp_path):
    deps = parse_setup_py(_py(tmp_path, "from setuptools import setup\n"
                                        "setup(install_requires=['a', dep()])\n"))
    assert deps.requirements == []
    assert deps.notes == ["computed install_requires not read"]


def test_unparseable_setup_py_is_a_note(tmp_path):
    deps = parse_setup_py(_py(tmp_path, "setup(install_requires=[\n"))
    assert deps.requirements == [] and "install_requires not read" in deps.notes[0]


def test_setup_cfg_options(tmp_path):
    cfg = tmp_path / "setup.cfg"
    cfg.write_text("""[options]
install_requires =
    requests>=2
    # comment
    click
[options.extras_require]
docs =
    sphinx
Test = pytest
""")
    deps = parse_setup_cfg(cfg)
    assert _by_group(deps) == {"main": ["requests>=2", "click"], "docs": ["sphinx"],
                               "Test": ["pytest"]}


def test_setup_cfg_file_directive_is_computed(tmp_path):
    cfg = tmp_path / "setup.cfg"
    cfg.write_text("[options]\ninstall_requires = file: requirements.txt\n")
    deps = parse_setup_cfg(cfg)
    assert deps.requirements == [] and deps.notes == ["computed install_requires not read"]


def test_declared_requirements_use_the_same_parser(tmp_path):
    p = _py(tmp_path, """
from setuptools import setup
reqs = ['Foo_Bar>=1']
setup(install_requires=reqs, extras_require={'x': ['extra']})
""")
    assert extract_declared_requirements(p) == {"foo-bar": "Foo_Bar>=1"}


def test_detect_sources_lists_subroot_manifests_only_on_request(tmp_path):
    (tmp_path / "setup.py").write_text("from setuptools import setup\nsetup()\n")
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc" / "requirements_py36_locked.txt").write_text("numpy==1.15.0\n")
    assert [s.path.name for s in detect_sources(tmp_path)] == ["setup.py"]
    sources = detect_sources(tmp_path, include_subroots=True)
    assert [s.ref(tmp_path) for s in sources] == ["setup.py", "etc/requirements_py36_locked.txt"]
    assert [s.subroot for s in sources] == [False, True]


def test_find_source_by_relative_path_and_by_name(tmp_path):
    (tmp_path / "requirements.txt").write_text("a\n")
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc" / "requirements.txt").write_text("b==1\n")
    sources = detect_sources(tmp_path, include_subroots=True)
    top = find_source(sources, "requirements.txt", tmp_path)
    assert top.path == tmp_path / "requirements.txt"
    assert find_source(sources, "etc/requirements.txt", tmp_path).subroot is True
    assert find_source(sources, "etc/missing.txt", tmp_path) is None


def test_detect_manifest_rejects_outside_missing_and_unsupported(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "README.rst").write_text("x")
    (tmp_path / "requirements.txt").write_text("a\n")
    (proj / "requirements.txt").write_text("a\n")
    assert detect_manifest(proj, "README.rst") is None
    assert detect_manifest(proj, "nope.txt") is None
    assert detect_manifest(proj, "../requirements.txt") is None
    assert detect_manifest(proj, "requirements.txt") is not None
