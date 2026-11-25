"""Version equality and marker-gated dependencies in assess (QA I6, hyper).

``hyperframe 3.2 → 3.2.0`` was an "▲ upgrade", a plan dependency move and a pin step;
pyOpenSSL, service-identity, cryptography and enum34, gated to PyPy / < 2.7.9 / 2.7 by
setup.py's marker-keyed extras, were "⚠ removed" on 3.12.
"""

from __future__ import annotations

from pymolt.assess.service import build_comparison_rows
from pymolt.codemods.service import migrations_from_rows, witness_rows
from pymolt.core.enums import Mode, Provenance, ResolutionQuality, SourceFixation
from pymolt.core.graph import DependencyGraph, Node
from pymolt.core.versions import marker_excludes, same_version
from pymolt.ingestion.detect import extract_declared_requirements
from pymolt.workorder.env import manifest_pins

HYPER_SETUP = """from setuptools import setup
setup(
    name="hyper",
    install_requires=['h2>=2.4,<3.0,!=2.5.0', 'hyperframe>=3.2,<4.0'],
    extras_require={
        'fast': ['pycohttpparser'],
        ':python_full_version < "2.7.9"': ['pyOpenSSL>=0.15', 'service_identity>=14.0.0'],
        ':platform_python_implementation == "PyPy" and python_full_version < "2.7.9"': [
            'cryptography<1.0'],
        ':python_version == "2.7" or python_version == "3.3"': ['enum34>=1.0.4, <2'],
    },
)
"""


def _graph(specs: dict[str, tuple[str | None, str | None]]) -> DependencyGraph:
    nodes = {name: Node(name=name, version=version, mode=Mode.PYPI, provenance=Provenance.PYPI,
                        direct=True, declared_requirement=declared)
             for name, (version, declared) in specs.items()}
    return DependencyGraph(nodes=nodes, resolution_quality=ResolutionQuality.RESOLVED,
                           source_fixation=SourceFixation.INTENT)


def test_same_version_follows_pep_440():
    assert same_version("3.2", "3.2.0") and same_version("1.0.0", "1")
    assert not same_version("3.2", "3.2.1") and not same_version(None, "1")


def test_a_release_spelled_two_ways_is_unchanged():
    rows = build_comparison_rows(_graph({"hyperframe": ("3.2", "hyperframe>=3.2,<4.0")}),
                                 _graph({"hyperframe": ("3.2.0", None)}), "3.12", None)
    assert rows[0]["status"] == "unchanged"
    assert migrations_from_rows(rows) == []


def test_the_markers_of_setup_py_extras_keys_are_kept(tmp_path):
    (tmp_path / "setup.py").write_text(HYPER_SETUP)
    declared = extract_declared_requirements(tmp_path / "setup.py")

    assert declared["pyopenssl"] == 'pyOpenSSL>=0.15; python_full_version < "2.7.9"'
    assert declared["hyperframe"] == "hyperframe>=3.2,<4.0"
    assert "pycohttpparser" not in declared                       # an extra, not the base
    for name in ("pyopenssl", "service-identity", "cryptography", "enum34"):
        assert marker_excludes(declared[name], "3.12"), name
    assert not marker_excludes(declared["h2"], "3.12")


def test_a_dependency_its_marker_excludes_on_the_target_is_not_removed(tmp_path):
    (tmp_path / "setup.py").write_text(HYPER_SETUP)
    declared = extract_declared_requirements(tmp_path / "setup.py")
    base = _graph({"pyopenssl": ("0.15", declared["pyopenssl"]),
                   "enum34": ("1.0.4", declared["enum34"]),
                   "rfc3986": ("1.1.0", "rfc3986>=1.1.0")})

    rows = {r["name"]: r["status"]
            for r in build_comparison_rows(base, _graph({}), "3.12", None)}

    assert rows == {"pyopenssl": "excluded", "enum34": "excluded", "rfc3986": "removed"}


def test_neither_the_witness_nor_the_pin_step_moves_an_equal_release():
    rows = [{"name": "hyperframe", "baseline_version": "3.2.0", "target_version": "3.2.0",
             "status": "unchanged"}]
    assert witness_rows(rows, {"hyperframe": "3.2"}) == (rows, [])

    rewrite, other = manifest_pins("hyperframe==3.2\nh2==2.4\n",
                                   {"hyperframe": "3.2.0", "h2": "2.6.2"})
    assert [p.name for p in rewrite] == ["h2"] and other == []
