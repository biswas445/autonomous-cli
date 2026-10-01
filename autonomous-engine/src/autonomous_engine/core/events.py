"""Event-sourced execution: every important action produces an event (§9)."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from .task import now_iso


class EventLog:
    """Append-only JSONL event log, thread-safe, crash-safe."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event: str, **fields: Any) -> dict[str, Any]:
        record = {"event": event, "timestamp": now_iso(), **fields}
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock, self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
        return record

    def read_all(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        events: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    # tolerate a torn final line after a crash
                    continue
        return events

    def read_last(self, n: int = 20) -> list[dict[str, Any]]:
        return self.read_all()[-n:]

    def tail_since(self, timestamp: str) -> list[dict[str, Any]]:
        # >= rather than >: timestamps resolve to whole seconds, so a strict
        # comparison permanently skipped events appended within the same
        # second as the cursor. Re-read duplicates are dropped by the UI's
        # dedup filter.
        events = self.read_all()
        return [e for e in events if e.get("timestamp", "") >= timestamp]


class EventTypes:
    """Canonical event names (auditability + replay)."""

    RUN_STARTED = "run.started"
    RUN_STOPPED = "run.stopped"
    RUN_HEARTBEAT = "run.heartbeat"
    RUN_PAUSED = "run.paused"
    RUN_RESUMED = "run.resumed"

    INTENT_COMPILED = "intent.compiled"

    TASK_CREATED = "task.created"
    TASK_ASSIGNED = "task.assigned"
    TASK_STATE_CHANGED = "task.state_changed"
    TASK_COMPLETED = "task.completed"
    TASK_FAILED = "task.failed"
    TASK_CANCELLED = "task.cancelled"
    TASK_REPLANNED = "task.replanned"

    AGENT_STARTED = "agent.started"
    AGENT_FINISHED = "agent.finished"
    AGENT_CRASHED = "agent.crashed"

    VERIFICATION_PASSED = "verification.passed"
    VERIFICATION_FAILED = "verification.failed"
    EVIDENCE_RECORDED = "evidence.recorded"
    QUALITY_GATE_EVALUATED = "quality_gate.evaluated"
    FLAKY_TEST_DETECTED = "verification.flaky"

    COMMIT_CREATED = "git.commit"
    CHECKPOINT_CREATED = "checkpoint.created"
    CHECKPOINT_RESTORED = "checkpoint.restored"

    DECISION_RECORDED = "decision.recorded"
    ESCALATION_RAISED = "escalation.raised"
    UNKNOWN_QUEUED = "unknown.queued"
    FAILURE_RECORDED = "failure.recorded"
    BUDGET_EXCEEDED = "budget.exceeded"
    STOP_CONDITION = "stop.condition"
