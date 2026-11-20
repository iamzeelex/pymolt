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
