"""The wheel reader of ``--online`` treats a wheel as untrusted input: a member is inflated only
up to a cap, so a zip bomb stops as a FetchError instead of filling memory."""

from __future__ import annotations

import io
import os
import zipfile

import pytest

from pymolt.delta import fetch


class _Response:
    def __init__(self, content: bytes, status: int, headers: dict | None = None):
        self.content = content
        self.status_code = status
        self.headers = headers or {}


class _Client:
    """Serves one wheel's bytes with HEAD and Range GET, as files.pythonhosted.org does."""

    def __init__(self, blob: bytes):
        self.blob = blob

    def head(self, url):
        return _Response(b"", 200, {"content-length": str(len(self.blob))})

    def get(self, url, headers=None):
        start, end = headers["Range"].removeprefix("bytes=").split("-")
        return _Response(self.blob[int(start): int(end) + 1], 206)


def _wheel(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buf.getvalue()


def _fetcher(tmp_path, blob: bytes) -> fetch.Fetcher:
    f = fetch.Fetcher(tmp_path, use_cache=False)
    f._client = _Client(blob)
    return f


def test_a_module_is_read_from_a_wheel_by_range_requests(tmp_path):
    blob = _wheel({"pkg/__init__.py": b"import warnings\n", "pkg/mod.py": b"x = 1\n"})
    assert _fetcher(tmp_path, blob)._range_member("https://w/pkg.whl", "pkg/mod.py") == "x = 1\n"


def test_a_member_that_inflates_past_the_cap_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch, "_MAX_MEMBER", 64 * 1024)
    bomb = b"#" * (1024 * 1024)                 # 1 MB that deflates to about 1 KB
    blob = _wheel({"pkg/bomb.py": bomb})
    assert len(blob) < 8 * 1024
    with pytest.raises(fetch.FetchError, match="inflates past"):
        _fetcher(tmp_path, blob)._range_member("https://w/pkg.whl", "pkg/bomb.py")


def test_a_member_compressed_past_the_cap_is_not_downloaded(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch, "_MAX_MEMBER", 1024)
    blob = _wheel({"pkg/big.py": os.urandom(16 * 1024)})   # 16 KB that does not compress
    with pytest.raises(fetch.FetchError, match="compressed, past the cap"):
        _fetcher(tmp_path, blob)._range_member("https://w/pkg.whl", "pkg/big.py")
