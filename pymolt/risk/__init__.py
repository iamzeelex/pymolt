"""Migration risk assessment (opt-in, network-backed).

Turns a resolved dependency graph into per-package risk signals from public,
free data sources: CVEs (OSV.dev), wheel/compilation status and abandonment
(PyPI JSON). Kept separate from ingestion so the default audit stays offline.
"""
