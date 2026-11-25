"""The terminal a person attests on (``pymolt.attest``): the one place that reads a typed nonce.

A person is there only when stdin and stdout are TTYs and the command runs with no ``--agent``
and no ``--json`` (and not over MCP, which never builds one). The text and the nonce go to
stdout; the answer is one line from stdin.
"""

from __future__ import annotations

import sys
from typing import TextIO


class TtyPrompter:
    def __init__(self, *, agent: bool = False, json_output: bool = False,
                 stdin: TextIO | None = None, stdout: TextIO | None = None) -> None:
        self.agent, self.json_output = agent, json_output
        self.stdin = stdin or sys.stdin
        self.stdout = stdout or sys.stdout

    def unavailable(self) -> str | None:
        if self.agent:
            return "--agent: an agent cannot attest"
        if self.json_output:
            return "--json: machine output cannot attest"
        for name, stream in (("stdin", self.stdin), ("stdout", self.stdout)):
            try:
                if not stream.isatty():
                    return f"{name} is not a terminal"
            except (AttributeError, ValueError, OSError):
                return f"{name} is not a terminal"
        return None

    def confirm(self, text: str, nonce: str) -> bool:
        out = self.stdout
        out.write(text.rstrip() + "\n")
        out.write(f"To attest this as a person, type {nonce}: ")
        out.flush()
        try:
            answer = self.stdin.readline()
        except (OSError, ValueError, KeyboardInterrupt):
            return False
        return answer.strip() == nonce


__all__ = ["TtyPrompter"]
