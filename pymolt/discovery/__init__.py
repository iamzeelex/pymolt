"""Discovery: map every Python surface in a repo before any resolution.

Single-repo, offline, multi-root (monorepo) from the start. Builds a
``SurfaceMap`` of project roots and the version/tool evidence found in their
manifests, Dockerfiles and tox/nox configs — each fact carrying its provenance
and a confidence level. Feeds Reconciliation (version divergence) and Risk
(e.g. an Alpine/musl base implies compilation).
"""
