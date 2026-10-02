"""Model-output streaming bridge (§12 of the streaming directive).

The chain is: model output → agent/runtime events → event log → IPC → TUI.

`ModelStreamObserver` is the single hook providers call while generating; the
runtime wires an instance that forwards into the workspace event log, so
streaming becomes ordinary events with the same durability/replay guarantees
as every other event. Only token deltas and metadata flow — never secret
values, never full prompts.
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

Listener = Callable[[dict[str, Any]], None]


@dataclass
class ModelStreamObserver:
    """Fan-out point for streaming model output (thread-safe, bounded)."""

    listeners: list[Listener] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def subscribe(self, listener: Listener) -> None:
        with self._lock:
            self.listeners.append(listener)

    def unsubscribe(self, listener: Listener) -> None:
        with self._lock, contextlib.suppress(ValueError):
            self.listeners.remove(listener)

    def emit(self, payload: dict[str, Any]) -> None:
        with self._lock:
            listeners = list(self.listeners)
        for listener in listeners:
            with contextlib.suppress(Exception):  # a slow UI never breaks a model call
                listener(payload)

    def emit_delta(
        self,
        *,
        agent: str,
        model: str,
        provider: str,
        delta: str,
        role: str = "",
        task_id: str = "",
        call_id: str = "",
        final: bool = False,
    ) -> None:
        self.emit(
            {
                "event": "model.stream",
                "agent": agent or "model",
                "provider": provider,
                "model": model,
                "role": role,
                "task_id": task_id,
                "call_id": call_id,
                "delta": str(delta)[:400],
                "final": final,
            }
        )


_global_observer = ModelStreamObserver()


def global_observer() -> ModelStreamObserver:
    """The process-wide observer providers emit into."""
    return _global_observer
