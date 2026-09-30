"""Run control: how an operator stops, pauses, or unblocks a running loop.

The orchestrator must be controllable from another process (the CLI) while it
is mid-cycle, so control is a small file on disk that the loop polls once per
cycle. It is deliberately declarative — no signals, no IPC — so a control
request survives a crash and is visible in the workspace.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.task import now_iso

CONTROL_FILENAME = "control.json"


@dataclass
class ControlSignal:
    pause: bool = False
    stop: bool = False
    resume: bool = False
    reason: str = ""
    requested_at: str = ""
    approvals: list[str] = field(default_factory=list)  # escalation ids approved
    rejections: list[str] = field(default_factory=list)  # escalation ids rejected

    def any_request(self) -> bool:
        return bool(self.pause or self.stop or self.resume or self.approvals or self.rejections)

    def as_dict(self) -> dict[str, Any]:
        return {
            "pause": self.pause,
            "stop": self.stop,
            "resume": self.resume,
            "reason": self.reason,
            "requested_at": self.requested_at,
            "approvals": self.approvals,
            "rejections": self.rejections,
        }


class ControlChannel:
    """Reads and writes `.agents/execution/control.json`."""

    def __init__(self, execution_dir: Path):
        self.path = execution_dir / CONTROL_FILENAME
        self.execution_dir = execution_dir

    def read(self) -> ControlSignal:
        if not self.path.is_file():
            return ControlSignal()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return ControlSignal()
        if not isinstance(data, dict):
            return ControlSignal()
        return ControlSignal(
            pause=bool(data.get("pause", False)),
            stop=bool(data.get("stop", False)),
            resume=bool(data.get("resume", False)),
            reason=str(data.get("reason", "")),
            requested_at=str(data.get("requested_at", "")),
            approvals=[str(x) for x in data.get("approvals", []) or []],
            rejections=[str(x) for x in data.get("rejections", []) or []],
        )

    def request(self, **fields: Any) -> ControlSignal:
        signal = self.read()
        payload = signal.as_dict()
        payload.update({k: v for k, v in fields.items() if v is not None})
        payload["requested_at"] = now_iso()
        self.execution_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)
        return self.read()

    def clear(self) -> None:
        """Consume the current control request so it is acted on exactly once."""
        if self.path.is_file():
            self.path.unlink()
