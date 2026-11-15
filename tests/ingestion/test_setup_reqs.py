from pymolt.ingestion.detect import extract_declared_requirements
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
