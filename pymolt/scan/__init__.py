"""Scan — phase 1: the primary as-is reconnaissance.

Merges surface discovery (monorepo roots, Dockerfiles, tox/nox, version evidence
+ divergence) with dependency-edge inventory into one ``ScanReport``, so an
engineer who knows nothing about the project gets all the scattered information,
structured, in a single offline pass — before any target is chosen or resolved.
"""

from pymolt.scan.report import ScanReport, run_scan

__all__ = ["ScanReport", "run_scan"]
