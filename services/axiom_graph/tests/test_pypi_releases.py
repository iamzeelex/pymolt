"""
Tests for pypi_releases.py — version filtering and sorting (mocked HTTP).
"""

from unittest.mock import MagicMock, patch
import pytest
from packaging.version import Version
from axiom_graph.sources.pypi_releases import fetch_ordered_releases, fetch_project_urls


MOCK_PYPI_RESPONSE = {
    "info": {
        "home_page": "https://pandas.pydata.org",
        "project_urls": {
            "Source": "https://github.com/pandas-dev/pandas",
        },
    },
    "releases": {
        "1.3.0": [{}],
        "1.3.5": [{}],
        "1.4.0": [{}],
        "1.4.1": [{}],
        "1.4.2": [{}],
        "1.5.0": [{}],
        "1.5.3": [{}],
        "2.0.0rc1": [{}],  # pre-release
        "2.0.0": [{}],
        "2.1.0": [{}],     # beyond our range
    },
}


class TestFetchOrderedReleases:
    def _mock_metadata(self, data):
        with patch("axiom_graph.sources.pypi_releases.fetch_pypi_metadata", return_value=data):
            yield

    def test_basic_range(self):
        with patch("axiom_graph.sources.pypi_releases.fetch_pypi_metadata", return_value=MOCK_PYPI_RESPONSE):
            releases = fetch_ordered_releases("pandas", "1.3.5", "2.0.0")
        # Should include 1.4.0, 1.4.1, 1.4.2, 1.5.0, 1.5.3, 2.0.0
        assert "1.4.0" in releases
        assert "2.0.0" in releases
        # Should NOT include from_v itself
        assert "1.3.5" not in releases
        # Should NOT include versions beyond to_v
        assert "2.1.0" not in releases

    def test_excludes_prereleases_by_default(self):
        with patch("axiom_graph.sources.pypi_releases.fetch_pypi_metadata", return_value=MOCK_PYPI_RESPONSE):
            releases = fetch_ordered_releases("pandas", "1.3.5", "2.0.0")
        assert "2.0.0rc1" not in releases

    def test_includes_prereleases_when_requested(self):
        with patch("axiom_graph.sources.pypi_releases.fetch_pypi_metadata", return_value=MOCK_PYPI_RESPONSE):
            releases = fetch_ordered_releases("pandas", "1.3.5", "2.0.0", include_prereleases=True)
        assert "2.0.0rc1" in releases

    def test_sorted_ascending(self):
        with patch("axiom_graph.sources.pypi_releases.fetch_pypi_metadata", return_value=MOCK_PYPI_RESPONSE):
            releases = fetch_ordered_releases("pandas", "1.3.5", "2.0.0")
        parsed = [Version(v) for v in releases]
        assert parsed == sorted(parsed)

    def test_invalid_from_to_raises(self):
        with pytest.raises(ValueError, match="strictly less than"):
            with patch("axiom_graph.sources.pypi_releases.fetch_pypi_metadata", return_value=MOCK_PYPI_RESPONSE):
                fetch_ordered_releases("pandas", "2.0.0", "1.3.5")

    def test_empty_if_no_versions_in_range(self):
        data = {"info": {}, "releases": {"1.0.0": [{}], "3.0.0": [{}]}}
        with patch("axiom_graph.sources.pypi_releases.fetch_pypi_metadata", return_value=data):
            releases = fetch_ordered_releases("pkg", "1.5.0", "2.0.0")
        assert releases == []


class TestFetchProjectUrls:
    def test_extracts_source_url(self):
        data = {
            "info": {
                "home_page": "https://example.com",
                "project_urls": {"Source": "https://github.com/org/repo"},
            },
            "releases": {},
        }
        with patch("axiom_graph.sources.pypi_releases.fetch_pypi_metadata", return_value=data):
            urls = fetch_project_urls("pkg")
        assert urls.get("Source") == "https://github.com/org/repo"

    def test_falls_back_to_home_page(self):
        data = {
            "info": {"home_page": "https://myproject.org", "project_urls": None},
            "releases": {},
        }
        with patch("axiom_graph.sources.pypi_releases.fetch_pypi_metadata", return_value=data):
            urls = fetch_project_urls("pkg")
        assert "Homepage" in urls
