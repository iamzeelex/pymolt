"""Person attestations: the one gate in the work order meant to hold an agent back (design 5.6).

HOST side (3.12+, stdlib). Every other gate (``--agent``, the MCP server's opt-ins, an
approval file) stops accidents: an agent following briefs, a person running the wrong command,
a stale plan. None of them stops an adversarial agent, which can edit anything under
``.pymolt/`` and run any command without ``--agent``. An attestation is the one event that can
judge an edit more leniently than a hand edit (an ``intended`` expectation met, an accepted
finding, an approval for agents), so it is the one thing kept out of the project's reach:

- **Who attests.** A person on a terminal: stdin and stdout are TTYs, and the command runs with
  no ``--agent``, no ``--json`` and not over MCP. pymolt prints what is attested and a random
  nonce, and the person types the nonce back. The terminal I/O is an adapter
  (``pymolt.adapters.attest_tty``); this module only asks it, so it stays non-interactive and
  testable.
- **The record** goes to ``$XDG_STATE_HOME/pymolt/attestations.jsonl`` (default
  ``~/.local/state/pymolt/``), outside the project tree: ``{id, kind, project:
  sha256(realpath), run_id, plan_id, step, finding, fingerprint, captures, files, claim,
  expect, scope, at, user, hmac}``, where ``hmac = HMAC-SHA256(key, canonical record)`` and the
  key is 32 random bytes at ``$XDG_CONFIG_HOME/pymolt/attest.key`` (default
  ``~/.config/pymolt/``), created ``0600`` on first use. The ledger or approval file stores
  only the id.
- **Checking one** (``AttestationStore.check``): the record exists, its HMAC verifies with this
  machine's key, and every field the caller binds it to matches (the project, run, step, kind,
  and what it attested: the recorded files, the finding and fingerprint, both captures). Anything
  else is unattested, with the reason.
- **Scope**: per machine and per user. On another machine (CI) an attested item is unattested.

It defeats: a non-interactive agent (subprocess, MCP) producing an attestation; forged in-tree
JSON (``attested: true``, a made-up or copied attestation id); an attestation reused for another
step, finding, change or capture. It does not defeat: an agent with a pseudo-terminal that reads
the nonce and types it; an agent that can read the key in the user's home; a person attesting
carelessly.
"""

from __future__ import annotations

import getpass
import hashlib
import hmac
import json
import os
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

KEY_BYTES = 32
LOG_NAME = "attestations.jsonl"
KEY_NAME = "attest.key"


class Prompter(Protocol):
    """The terminal a person attests on (``pymolt.adapters.attest_tty.TtyPrompter``)."""

    def unavailable(self) -> str | None:
        """None when a person can be asked here, else why not."""

    def confirm(self, text: str, nonce: str) -> bool:
        """Show ``text`` and ``nonce``; True when the person typed the nonce back."""


def _state_home() -> Path:
    raw = os.environ.get("XDG_STATE_HOME")
    return Path(raw).expanduser() if raw else Path.home() / ".local" / "state"


def _config_home() -> Path:
    raw = os.environ.get("XDG_CONFIG_HOME")
    return Path(raw).expanduser() if raw else Path.home() / ".config"


def project_id(project_dir: str | Path) -> str:
    """The project an attestation is for: sha256 of its real path."""
    return hashlib.sha256(os.path.realpath(str(project_dir)).encode("utf-8")).hexdigest()


def files_digest(files: Any) -> str:
    """One hash over a change's files: ``(file, before, after)`` sha256s, sorted."""
    rows = []
    for f in files or ():
        get = f.get if isinstance(f, Mapping) else (lambda k, f=f: getattr(f, k, None))
        rows.append([get("file"), get("before"), get("after")])
    rows.sort(key=lambda r: [str(x) for x in r])
    return hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()


def _canonical(record: Mapping[str, Any]) -> bytes:
    body = {k: v for k, v in record.items() if k != "hmac"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


@dataclass(frozen=True)
class Check:
    """The answer of ``AttestationStore.check``: ``record`` when it holds, else ``reason``."""

    record: dict[str, Any] | None
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.record is not None

    def info(self) -> dict[str, Any] | None:
        """What a report prints of a valid attestation: its id, who, when."""
        if self.record is None:
            return None
        return {"id": self.record.get("id"), "user": self.record.get("user"),
                "at": self.record.get("at"), "kind": self.record.get("kind")}


class AttestationStore:
    """The out-of-tree attestation log and its key (module docstring). ``state_dir`` and
    ``config_dir`` default to ``$XDG_STATE_HOME`` and ``$XDG_CONFIG_HOME``; the log and key are in
    their ``pymolt/`` folders."""

    def __init__(self, state_dir: str | Path | None = None,
                 config_dir: str | Path | None = None) -> None:
        self.log = Path(state_dir or _state_home()) / "pymolt" / LOG_NAME
        self.key_path = Path(config_dir or _config_home()) / "pymolt" / KEY_NAME

    # ── the key ───────────────────────────────────────────────────────────────────────────

    def _key(self, *, create: bool) -> bytes | None:
        try:
            data = self.key_path.read_bytes()
            return data if len(data) == KEY_BYTES else None
        except FileNotFoundError:
            if not create:
                return None
        except OSError:
            return None
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        key = secrets.token_bytes(KEY_BYTES)
        try:
            fd = os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return self._key(create=False)
        with os.fdopen(fd, "wb") as stream:
            stream.write(key)
        return key

    def _mac(self, key: bytes, record: Mapping[str, Any]) -> str:
        return hmac.new(key, _canonical(record), hashlib.sha256).hexdigest()

    # ── writing ───────────────────────────────────────────────────────────────────────────

    def append(self, fields: Mapping[str, Any]) -> dict[str, Any]:
        """Write one attestation (``fields`` plus ``id``, ``at``, ``user`` and its ``hmac``);
        returns the record."""
        key = self._key(create=True)
        if key is None:
            raise OSError(f"the attestation key {self.key_path} cannot be read or made")
        record = {k: v for k, v in fields.items() if v is not None}
        record.setdefault("at", datetime.now(UTC).isoformat(timespec="seconds"))
        record.setdefault("user", _user())
        record["id"] = "att-" + secrets.token_hex(8)
        record["hmac"] = self._mac(key, record)
        self.log.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, sort_keys=True, default=str) + "\n"
        fd = os.open(self.log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as stream:
            stream.write(line)
        return record

    # ── reading ───────────────────────────────────────────────────────────────────────────

    def find(self, att_id: str | None) -> dict[str, Any] | None:
        if not att_id:
            return None
        try:
            lines = self.log.read_text(encoding="utf-8").splitlines()
        except OSError:
            return None
        for line in reversed(lines):
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict) and record.get("id") == att_id:
                return record
        return None

    def check(self, att_id: str | None, **bound: Any) -> Check:
        """Whether ``att_id`` is a valid attestation of this machine whose fields equal
        ``bound`` (every key given; ``project`` may be a directory, it is hashed)."""
        if not att_id:
            return Check(None, "no attestation")
        record = self.find(att_id)
        if record is None:
            return Check(None, f"attestation {att_id} is not in this machine's attestation log")
        key = self._key(create=False)
        if key is None:
            return Check(None, "this machine has no attestation key")
        if not hmac.compare_digest(str(record.get("hmac") or ""), self._mac(key, record)):
            return Check(None, f"attestation {att_id} does not verify (its HMAC differs)")
        for name, want in bound.items():
            if name == "project":
                want = project_id(want)
            if record.get(name) != want:
                return Check(None, f"attestation {att_id} is for another {name}")
        return Check(record)


def _user() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 - no login name is not a failure
        return "unknown"


@dataclass
class Attester:
    """What the interface layer hands the core: a terminal to ask (None: nobody can attest
    here) and the store the attestation goes to."""

    prompter: Prompter | None = None
    store: AttestationStore | None = None

    def ask(self, text: str, fields: Mapping[str, Any]
            ) -> tuple[dict[str, Any] | None, str | None]:
        return ask(self.prompter, self.store, text, fields)


def ask(prompter: Prompter | None, store: AttestationStore | None, text: str,
        fields: Mapping[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """Ask a person to attest ``text``; on the typed nonce, write the attestation of ``fields``.
    Returns ``(record, None)``, or ``(None, why not)``. Never prompts without a prompter."""
    if prompter is None:
        return None, "no person can attest here (--agent, --json or MCP)"
    why = prompter.unavailable()
    if why:
        return None, why
    nonce = secrets.token_hex(3)
    if not prompter.confirm(text, nonce):
        return None, "the nonce was not typed back"
    store = store or AttestationStore()
    return store.append(fields), None


__all__ = ["AttestationStore", "Attester", "Check", "Prompter", "ask", "files_digest",
           "project_id"]
