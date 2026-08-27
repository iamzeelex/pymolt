"""
axiom_graph/analyzers/test_miner.py

Test-suite diff miner.

Extracts migration examples from a library's own test suite by:
  1. Finding test files in old_tests_dir that exercise each target API.
  2. Diffing against the corresponding file in new_tests_dir.
  3. Extracting only the diff hunks that REMOVE the target API call.

This provides ground-truth migration examples written by the library authors —
the highest-quality signal available about how to migrate away from a removed API.

Design notes:
- Pure stdlib: difflib, pathlib. No external dependencies.
- Per-API result is capped at MAX_EXAMPLES to keep output manageable.
- Searches for both ".api_name(" and " api_name(" patterns.
- Gracefully skips files that can't be read (encoding, permissions).
"""

from __future__ import annotations

import ast
import difflib
import logging
import textwrap
from pathlib import Path

log = logging.getLogger(__name__)

MAX_EXAMPLES = 5
"""Maximum number of diff-hunk examples to collect per API name."""


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------

def _find_files_using_api(tests_dir: Path, api_name: str) -> list[Path]:
    """
    Find all .py files in tests_dir that contain a call to api_name.
    Searches for ".api_name(" and "api_name(" to cover both method and
    standalone-function forms.
    """
    patterns = (f".{api_name}(", f"{api_name}(", f".{api_name}")
    matched: list[Path] = []

    for py_file in tests_dir.rglob("*.py"):
        if not py_file.is_file():
            continue
        try:
            content = py_file.read_text(encoding="utf-8", errors="replace")
            if any(p in content for p in patterns):
                matched.append(py_file)
        except OSError:
            pass

    return matched


# ---------------------------------------------------------------------------
# Hunk extraction
# ---------------------------------------------------------------------------

def _extract_removal_hunks(
    old_file: Path,
    new_file: Path,
    api_name: str,
) -> list[str]:
    """
    Diff old_file against new_file and return the hunks where api_name
    is being REMOVED (i.e. appears on a '-' line).

    Returns a list of strings, each being a complete unified diff hunk.
    """
    try:
        old_lines = old_file.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        new_lines = new_file.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    except OSError as exc:
        log.debug("Cannot read diff pair %s / %s: %s", old_file, new_file, exc)
        return []

    diff = list(difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile=f"old/{old_file.name}",
        tofile=f"new/{new_file.name}",
        lineterm="",
    ))

    hunks: list[str] = []
    current_hunk: list[str] = []
    has_removal = False

    for line in diff:
        if line.startswith("@@"):
            if has_removal and current_hunk:
                hunks.append("\n".join(current_hunk))
            current_hunk = [line]
            has_removal = False
        else:
            current_hunk.append(line)
            if line.startswith("-") and api_name in line:
                has_removal = True

    if has_removal and current_hunk:
        hunks.append("\n".join(current_hunk))

    return hunks


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def mine_test_examples(
    old_tests_dir: Path,
    new_tests_dir: Path,
    api_names: list[str],
) -> dict[str, list[str]]:
    """
    Mine migration examples from a library's test suite diff.

    For each name in api_names, finds test files that used the API in
    old_tests_dir and extracts the relevant diff hunks from new_tests_dir,
    showing how the library authors migrated away from it.

    Args:
        old_tests_dir: tests/ directory from the old version's sdist.
        new_tests_dir: tests/ directory from the new version's sdist.
        api_names: List of API names to mine (e.g. ["append", "iteritems"]).

    Returns:
        ``{api_name: [hunk_string, ...]}``
        Capped at MAX_EXAMPLES hunks per API.

    Never raises — logs warnings and returns empty results on failure.
    """
    results: dict[str, list[str]] = {}

    if not old_tests_dir or not old_tests_dir.exists():
        log.debug("old_tests_dir not found: %s", old_tests_dir)
        return {name: [] for name in api_names}

    if not new_tests_dir or not new_tests_dir.exists():
        log.debug("new_tests_dir not found: %s", new_tests_dir)
        return {name: [] for name in api_names}

    for api_name in api_names:
        examples: list[str] = []

        matched_files = _find_files_using_api(old_tests_dir, api_name)
        log.debug("Found %d test files for API %r", len(matched_files), api_name)

        for old_file in matched_files:
            if len(examples) >= MAX_EXAMPLES:
                break
            try:
                rel = old_file.relative_to(old_tests_dir)
            except ValueError:
                continue
            new_file = new_tests_dir / rel
            if not new_file.exists():
                continue

            hunks = _extract_removal_hunks(old_file, new_file, api_name)
            for hunk in hunks:
                if len(examples) >= MAX_EXAMPLES:
                    break
                examples.append(hunk)

        results[api_name] = examples
        log.info("test_miner: %d examples for %r", len(examples), api_name)

    return results


# ---------------------------------------------------------------------------
# Golden-pair reconstruction
# ---------------------------------------------------------------------------

def golden_pair_from_hunk(hunk: str) -> tuple[str, str] | None:
    """
    Reconstruct a (before, after) source pair from a single unified-diff hunk
    string (as mined by mine_test_examples above).

    Context (' ') lines go into both sides, '-' lines go into `before` only,
    '+' lines go into `after` only; '@@' range headers, blank lines, and any
    other non-source line are dropped. Both sides are textwrap.dedent-ed and
    must independently ast.parse — a cheap syntactic sanity check, not a
    semantic verification that the hunk actually demonstrates the migration
    (callers that need that guarantee verify locally). Returns None if either
    side is empty or fails to parse.
    """
    before_lines: list[str] = []
    after_lines: list[str] = []

    for line in hunk.splitlines():
        if not line or line.startswith("@@"):
            continue
        marker, text = line[0], line[1:]
        if marker == " ":
            before_lines.append(text)
            after_lines.append(text)
        elif marker == "-":
            before_lines.append(text)
        elif marker == "+":
            after_lines.append(text)
        # any other marker is not a source line — ignore it.

    before = textwrap.dedent("\n".join(before_lines))
    after = textwrap.dedent("\n".join(after_lines))

    if not before.strip() or not after.strip():
        return None

    try:
        ast.parse(before)
        ast.parse(after)
    except SyntaxError as exc:
        log.debug("golden_pair_from_hunk: unparseable hunk (%s)", exc)
        return None

    return before, after
