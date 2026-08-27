"""Local pymolt configuration and offline cache manager.

Manages:
1. API token (``PYMOLT_API_TOKEN`` / stored ``config.json`` with 0600 permissions).
2. Axiom Cloud Hub / On-Prem endpoint (``PYMOLT_AXIOM_ENDPOINT`` / stored ``config.json`` / default ``https://hub.pymolt.dev``).
3. Local offline delta cache (``$XDG_CACHE_HOME/pymolt/deltas/``) for instant zero-latency repeat runs.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

ENV_TOKEN = "PYMOLT_API_TOKEN"
ENV_ENDPOINT = "PYMOLT_AXIOM_ENDPOINT"
DEFAULT_ENDPOINT = "https://hub.pymolt.dev"

_TOKEN_KEY = "api_token"
_ENDPOINT_KEY = "endpoint"


# ---------------------------------------------------------------------------
# Configuration paths & persistence
# ---------------------------------------------------------------------------

def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "pymolt"


def config_path() -> Path:
    return config_dir() / "config.json"


def _read() -> dict[str, Any]:
    try:
        return json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write(data: dict[str, Any]) -> Path:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


# ---------------------------------------------------------------------------
# Token management
# ---------------------------------------------------------------------------

def save_token(token: str) -> Path:
    """Persist ``token`` to user config (0600). Returns the file path."""
    data = _read()
    data[_TOKEN_KEY] = token.strip()
    return _write(data)


def clear_token() -> bool:
    """Remove the stored token. Returns True if one was present."""
    data = _read()
    if _TOKEN_KEY not in data:
        return False
    del data[_TOKEN_KEY]
    path = config_path()
    try:
        if data:
            _write(data)
        else:
            path.unlink(missing_ok=True)
    except OSError:
        return False
    return True


def load_token() -> str | None:
    """Resolve API token: ``PYMOLT_API_TOKEN`` env wins, else stored file."""
    return os.environ.get(ENV_TOKEN) or _read().get(_TOKEN_KEY) or None


# ---------------------------------------------------------------------------
# Endpoint management
# ---------------------------------------------------------------------------

def save_endpoint(endpoint: str) -> Path:
    """Persist custom Axiom Cloud Hub / On-Prem endpoint URL."""
    clean_url = endpoint.strip().rstrip("/")
    data = _read()
    data[_ENDPOINT_KEY] = clean_url
    return _write(data)


def clear_endpoint() -> bool:
    """Reset endpoint to default. Returns True if custom endpoint was present."""
    data = _read()
    if _ENDPOINT_KEY not in data:
        return False
    del data[_ENDPOINT_KEY]
    _write(data)
    return True


def load_endpoint(override: str | None = None) -> str:
    """
    Resolve Axiom Hub endpoint URL:
    1. Explicit override argument (e.g. from CLI flag --endpoint)
    2. ``PYMOLT_AXIOM_ENDPOINT`` environment variable
    3. Stored ``endpoint`` in user config
    4. Default ``DEFAULT_ENDPOINT`` (https://hub.pymolt.dev)
    """
    if override and override.strip():
        return override.strip().rstrip("/")
    env_val = os.environ.get(ENV_ENDPOINT)
    if env_val and env_val.strip():
        return env_val.strip().rstrip("/")
    stored_val = _read().get(_ENDPOINT_KEY)
    if stored_val and str(stored_val).strip():
        return str(stored_val).strip().rstrip("/")
    return DEFAULT_ENDPOINT


# ---------------------------------------------------------------------------
# Local Delta Cache Manager (Offline caching)
# ---------------------------------------------------------------------------

def cache_dir() -> Path:
    """Root cache directory for pymolt."""
    base = os.environ.get("XDG_CACHE_HOME")
    root = Path(base) if base else Path.home() / ".cache"
    return root / "pymolt"


def deltas_cache_dir() -> Path:
    """Cache directory for Axiom Graph delta bundles."""
    return cache_dir() / "deltas"


def _delta_cache_path(package: str, from_version: str, to_version: str) -> Path:
    pkg_norm = package.replace("-", "_").lower()
    return deltas_cache_dir() / pkg_norm / f"{from_version}__{to_version}.json"


def get_cached_delta(package: str, from_version: str, to_version: str) -> dict[str, Any] | None:
    """Retrieve cached delta bundle dictionary if present."""
    path = _delta_cache_path(package, from_version, to_version)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.debug("Failed reading cached delta at %s: %s", path, exc)
        return None


def save_cached_delta(
    package: str, from_version: str, to_version: str, data: dict[str, Any]
) -> Path:
    """Persist delta bundle to local cache for instant offline repeat migrations."""
    path = _delta_cache_path(package, from_version, to_version)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(f".tmp_{os.getpid()}")
    try:
        tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp_path.replace(path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
    return path


def clear_delta_cache() -> int:
    """Purge all cached deltas. Returns the number of purged package cache folders."""
    c_dir = deltas_cache_dir()
    if not c_dir.exists():
        return 0
    count = sum(1 for p in c_dir.iterdir() if p.is_dir())
    shutil.rmtree(c_dir, ignore_errors=True)
    return count


def cache_stats() -> dict[str, Any]:
    """Calculate cache metrics (size, entry count, package count)."""
    c_dir = deltas_cache_dir()
    if not c_dir.exists():
        return {"total_files": 0, "total_size_bytes": 0, "packages": 0, "path": str(c_dir)}

    files = list(c_dir.rglob("*.json"))
    total_size = sum(f.stat().st_size for f in files if f.is_file())
    packages = len({f.parent.name for f in files})
    return {
        "total_files": len(files),
        "total_size_bytes": total_size,
        "packages": packages,
        "path": str(c_dir),
    }


def config_show() -> dict[str, Any]:
    """Summary of current config and cache state."""
    tok = load_token()
    masked_tok = f"{tok[:6]}...{tok[-4:]}" if tok and len(tok) > 10 else ("Set" if tok else "None")
    return {
        "endpoint": load_endpoint(),
        "is_default_endpoint": load_endpoint() == DEFAULT_ENDPOINT,
        "token_configured": bool(tok),
        "token_preview": masked_tok,
        "config_file": str(config_path()),
        "cache": cache_stats(),
    }
