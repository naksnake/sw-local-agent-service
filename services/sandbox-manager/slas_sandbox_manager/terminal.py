"""The Terminal tab's session: a shell inside the sandbox, recorded to the ticket (§5.7).

Line mode: each line the person types runs inside their sandbox as the workspace user
through the manager's exec API, with the same TTL and isolation as the agent. The transcript
is redacted line by line before it is kept. `git push` is allowed to fail on its own (no
route, no credential) and the session appends the sentence that says where pushing happens.

The WebSocket carrying `TerminalMessage`s and the xterm.js front end arrive with their
dependency decisions; this module is the part both will talk to.
"""

from __future__ import annotations

import json
import shlex
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol

from pydantic import Field

from slas_git.redact import redact
from slas_sandbox_manager.manager import PUSH_EXPLANATION, SandboxManager
from slas_schemas.common import SlasModel

MAX_LINE_CHARS = 4000


class Clock(Protocol):
    def now(self) -> datetime: ...


class TerminalMessage(SlasModel):
    """One frame on the WebSocket, both directions."""

    type: Literal["input", "output", "exit", "notice"]
    text: str = ""
    exit_code: int | None = None


class TerminalLine(SlasModel):
    n: int = Field(ge=1)
    at: datetime
    command: str
    output: str
    exit_code: int

    def messages(self) -> list[TerminalMessage]:
        frames = [TerminalMessage(type="output", text=self.output)] if self.output else []
        frames.append(TerminalMessage(type="exit", exit_code=self.exit_code))
        return frames


class TerminalSession:
    def __init__(
        self,
        manager: SandboxManager,
        session_id: str,
        *,
        clock: Clock,
        record_path: Path | None = None,
    ) -> None:
        self.manager = manager
        self.session_id = session_id
        self.clock = clock
        self.record_path = record_path
        self.lines: list[TerminalLine] = []

    def run(self, line: str) -> TerminalLine | None:
        text = line.strip()
        if not text:
            return None
        if len(text) > MAX_LINE_CHARS:
            text = text[:MAX_LINE_CHARS]
        result = self.manager.exec(self.session_id, ["bash", "-lc", text])
        output = (result.stdout + result.stderr).rstrip("\n")
        if _is_git_push(text):
            output = (output + "\n" if output else "") + PUSH_EXPLANATION
        entry = TerminalLine(
            n=len(self.lines) + 1,
            at=self.clock.now(),
            command=redact(text),
            output=redact(output),
            exit_code=result.exit_code,
        )
        self.lines.append(entry)
        if self.record_path is not None:
            self.record_path.parent.mkdir(parents=True, exist_ok=True)
            with self.record_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry.model_dump(mode="json"), ensure_ascii=False) + "\n")
        return entry

    def handle(self, frame: TerminalMessage) -> list[TerminalMessage]:
        if frame.type != "input":
            return [
                TerminalMessage(
                    type="notice", text="Only input frames are accepted from the browser."
                )
            ]
        entry = self.run(frame.text)
        return entry.messages() if entry else []

    def transcript(self) -> str:
        return "\n".join(f"$ {line.command}\n{line.output}".rstrip() for line in self.lines)


def _is_git_push(text: str) -> bool:
    try:
        argv = shlex.split(text)
    except ValueError:
        return False
    return len(argv) >= 2 and argv[0] == "git" and argv[1] == "push"
