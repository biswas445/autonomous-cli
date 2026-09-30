"""UI state store + event adapter (spec §37, §38, §39, §61).

Flow: Runtime events -> EventAdapter -> UIState -> Views.

The store holds *temporary* UI state only (bounded buffers, selection,
filters). All business state is re-read from the runtime through the facade,
so the UI cannot drift from the runtime. The adapter converts the runtime's
event vocabulary into view-oriented records, deduplicates by (event,
timestamp), and tolerates out-of-order arrival.
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .facade import RuntimeFacade

MAX_ACTIVITY = 500  # bounded buffer (spec §59/§60)
_MAX_DEDUP = 2000


def _hhmmss(ts: str) -> str:
    return ts.split("T")[-1][:8] if "T" in ts else ts


@dataclass
class ActivityItem:
    """One UI-facing activity record derived from a real runtime event."""

    timestamp: str
    event: str
    actor: str
    task_id: str
    message: str
    kind: str = "info"  # info | success | warning | error | decision | tool | verify

    def time(self) -> str:
        return _hhmmss(self.timestamp)

    def matches(self, needle: str) -> bool:
        needle = needle.lower()
        return (
            needle in self.event.lower()
            or needle in self.actor.lower()
            or needle in self.task_id.lower()
            or needle in self.message.lower()
        )


_EVENT_KINDS: dict[str, str] = {
    "task.completed": "success",
    "verification.passed": "success",
    "run.completed": "success",
    "milestone.completed": "success",
    "git.commit": "success",
    "task.failed": "error",
    "verification.failed": "error",
    "agent.crashed": "error",
    "failure.recorded": "error",
    "escalation.raised": "warning",
    "budget.exceeded": "warning",
    "goal.drift_detected": "warning",
    "worktree.merge_failed": "warning",
    "director.proposed": "decision",
    "qa.gate": "decision",
    "architecture.recorded": "decision",
    "task.replanned": "decision",
}


def _classify(event: str) -> str:
    if event.startswith("message."):
        # Communication observability (comms spec §60): every protocol
        # transition shows in the live feed with its own colour.
        if event in ("message.failed", "message.dead_lettered", "message.rejected"):
            return "error"
        if event in ("message.expired", "message.cancelled", "message.publish_error", "message.interpret_error"):
            return "warning"
        return "decision"
    return _EVENT_KINDS.get(event, "info")


def _actor(event: dict[str, Any]) -> str:
    if event.get("sender"):
        return f"{event.get('sender')}→{event.get('recipient', '?')}"
    return str(event.get("agent") or event.get("actor") or "runtime")


def _message(event: dict[str, Any]) -> str:
    """Human summary from real event fields; never fabricated."""
    name = event.get("event", "")
    for key in ("summary", "detail", "reason", "title", "question", "criterion"):
        value = event.get(key)
        if value:
            text = str(value).replace("\n", " ")
            return text[:180]
    if name == "task.state_changed":
        return f"{event.get('from_state', '')} -> {event.get('to_state', '')}"
    if name in ("agent.started", "agent.finished"):
        task_id = event.get("task_id", "")
        ok = "ok" if name == "agent.finished" and event.get("ok", True) else "failed"
        return f"{task_id or 'session'} {ok}".strip()
    if name == "run.stopped":
        stop = event.get("stop") or {}
        return str(stop.get("reason", "stopped"))
    if name == "git.commit":
        return f"commit {str(event.get('commit', ''))[:12]} {str(event.get('tag', ''))[:30]}".strip()
    if name == "git.memory_commit":
        return f"memory snapshot {str(event.get('commit', ''))[:12]}"
    if name == "task.created":
        return f"{event.get('count', '?')} task(s) created (total {event.get('total', '?')})"
    if name.startswith("message."):
        msg_type = str(event.get("msg_type", ""))
        state = str(event.get("state", ""))
        prefix = msg_type or "message"
        if name == "message.sent":
            return f"{prefix} sent ({state})"
        if name == "message.delivered":
            return f"{prefix} delivered to mailbox"
        if name == "message.acknowledged":
            return f"{prefix} acknowledged"
        if name == "message.completed":
            return f"{prefix} handled"
        if name == "message.failed":
            return f"{prefix} processing failed: {str(event.get('reason', ''))[:80]}"
        if name == "message.dead_lettered":
            return f"{prefix} dead-lettered"
        if name in ("message.publish_error", "message.interpret_error"):
            return f"{prefix}: {str(event.get('error', ''))[:100]}"
        return f"{prefix} ({state or name})"
    extra = {k: v for k, v in event.items() if k not in ("event", "timestamp", "task_id", "agent")}
    return json.dumps(extra, ensure_ascii=False, default=str)[:180] if extra else ""


class EventAdapter:
    """Converts raw runtime events into ActivityItems (spec §37)."""

    def __init__(self) -> None:
        self._seen: deque[tuple[str, str, str]] = deque(maxlen=_MAX_DEDUP)

    def convert(self, event: dict[str, Any]) -> ActivityItem | None:
        name = str(event.get("event", ""))
        if not name:
            return None
        key = (name, str(event.get("timestamp", "")), str(event.get("task_id", "")))
        if key in self._seen:  # duplicate protection (spec §61)
            return None
        self._seen.append(key)
        return ActivityItem(
            timestamp=str(event.get("timestamp", "")),
            event=name,
            actor=_actor(event),
            task_id=str(event.get("task_id", "")),
            message=_message(event),
            kind=_classify(name),
        )


@dataclass
class UIState:
    """Everything the views render; temporary, bounded, runtime-fed."""

    facade: RuntimeFacade
    activity: deque[ActivityItem] = field(default_factory=lambda: deque(maxlen=MAX_ACTIVITY))
    adapter: EventAdapter = field(default_factory=EventAdapter)
    filter_text: str = ""
    tick: int = 0
    last_refresh: float = 0.0

    # ---- event intake ----

    def ingest(self, events: list[dict[str, Any]]) -> int:
        added = 0
        for event in events:
            item = self.adapter.convert(event)
            if item is not None:
                self.activity.append(item)
                added += 1
        return added

    def poll_events(self) -> int:
        return self.ingest(self.facade.events_since())

    def load_recent(self, limit: int = MAX_ACTIVITY) -> None:
        self.ingest(self.facade.recent_events(limit))

    # ---- filtering (spec §35) ----

    def visible_activity(self) -> list[ActivityItem]:
        if not self.filter_text:
            return list(self.activity)
        return [item for item in self.activity if item.matches(self.filter_text)]

    # ---- runtime state snapshots (thin views over the facade) ----

    def progress(self) -> dict[str, int]:
        return self.facade.progress()

    def run_status(self) -> str:
        return str(self.facade.current_run().get("status") or "idle")

    def stop_reason(self) -> str:
        return str(self.facade.current_run().get("stop_reason") or "")

    def pending_escalations(self) -> list[dict[str, Any]]:
        return self.facade.escalations()

    def budget_remaining_pct(self) -> float | None:
        snap = self.facade.budget_snapshot()
        total = float(snap.get("max_token_budget") or 0)
        remaining = snap.get("remaining_usd")
        if total <= 0 or remaining is None:
            return None
        return round(100.0 * float(remaining) / total, 1)

    def elapsed_seconds(self) -> float:
        snap = self.facade.budget_snapshot()
        try:
            return float(snap.get("elapsed_seconds") or 0)
        except (TypeError, ValueError):
            return 0.0
