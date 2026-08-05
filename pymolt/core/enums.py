from enum import Enum


class Mode(str, Enum):
    PYPI = "pypi"
    CONDA = "conda"


class Provenance(str, Enum):
    PYPI = "pypi"
    CONDA_FORGE = "conda-forge"
    CONDA_ONLY = "conda-only"
    PIP_IN_CONDA = "pip-in-conda"


class ResolutionQuality(str, Enum):
    RESOLVED = "resolved"
    LOCK_PARSED = "lock-parsed"
    DECLARED_ONLY = "declared-only"


class SourceFixation(str, Enum):
    PINNED = "pinned"
    INTENT = "intent"
    MIXED = "mixed"


class RiskTier(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class EdgeKind(str, Enum):
    """How a declared dependency is sourced — the 'edges' that complicate a migration."""
    PYPI = "pypi"        # ordinary name + specifier from an index
    VCS = "vcs"          # git+/hg+/svn+/bzr+ reference
    URL = "url"          # direct http(s) artifact (wheel/sdist/zip)
    LOCAL = "local"      # local path or editable install
    UNKNOWN = "unknown"  # could not be classified


class Confidence(str, Enum):
    """Trust level for a discovered fact (e.g. a Python version from some source)."""
    PINNED = "pinned"        # exact/concrete (python:3.6.15, envlist py311)
    FLOATING = "floating"    # moving (python:3, latest, no tag)
    INFERRED = "inferred"    # derived, e.g. an ARG default — overridable
    UNKNOWN = "unknown"      # could not be determined (digest, private base, dynamic)


# ── verify/ (Component 6) ────────────────────────────────────────────────────
class Verdict(str, Enum):
    """Per-node behavioral verdict. The component decides nothing about migration;
    it states whether behavior is empirically stable, changed, or undecidable."""
    BEHAVIOR_STABLE = "behavior-stable"
    BEHAVIOR_CHANGED = "behavior-changed"
    NEEDS_ACTION = "needs-action"


class EvidenceLevel(str, Enum):
    """Where the cascade reached its verdict — the artifact's own trust label."""
    TESTS = "tests"
    GOLDEN = "golden"
    TRACE = "trace"


class TraceScope(str, Enum):
    """Run-level breadth of boundary tracing. An explicit input, never inferred."""
    BLIND_SPOTS = "blind-spots"  # trace only what tests/golden left unexercised (default)
    FULL = "full"                # trace everything, even covered+settled nodes (audit)


class TestStatus(str, Enum):
    """Outcome of the L1 test level for a node, comparing the suite under both versions."""
    __test__ = False  # not a pytest test class (dunder is excluded from Enum members)
    PASS = "pass"                # suite touching the node passes under both versions
    FAIL = "fail"                # suite differs / fails under the new version
    INCONCLUSIVE = "inconclusive"  # suite does not exercise the node confidently
