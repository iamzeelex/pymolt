"""Local pymolt configuration — currently just the API token used to authenticate
to the Axiom Graph service.

Stored at ``$XDG_CONFIG_HOME/pymolt/config.json`` (falling back to
``~/.config/pymolt/config.json``), written ``0600``. Token resolution order is
**env var wins over the stored file**, so ``PYMOLT_API_TOKEN`` can override a saved
token in CI or one-off runs without editing the file.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

ENV_TOKEN = "PYMOLT_API_TOKEN"
_TOKEN_KEY = "api_token"


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "pymolt"


def config_path() -> Path:
    return config_dir() / "config.json"


def _read() -> dict:
    try:
        return json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_token(token: str) -> Path:
    """Persist ``token`` to the user config (0600). Returns the file path."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _read()
    data[_TOKEN_KEY] = token
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def clear_token() -> bool:
    """Remove the stored token. Returns True if one was present."""
    data = _read()
    if _TOKEN_KEY not in data:
        return False
    del data[_TOKEN_KEY]
    path = config_path()
    try:
        if data:
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        else:
            path.unlink(missing_ok=True)
    except OSError:
        return False
    return True


def load_token() -> str | None:
    """Resolve the API token: ``PYMOLT_API_TOKEN`` env wins, else the stored file."""
    return os.environ.get(ENV_TOKEN) or _read().get(_TOKEN_KEY) or None
