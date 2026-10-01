"""Persistent daemon mode (plan.md §45): run until the project is *done*.

A single `auto run` process stops at the first human gate or budget wall. The
daemon keeps going — it is the difference between "an agent that happened to
run for a long time" and "a system engineered to run for a long time":

    PROJECT_COMPLETE            -> finished (success)
    HUMAN_APPROVAL_REQUIRED     -> wait for the operator; approved repeated-
                                   failure tasks are requeued, rejected ones
                                   are cancelled; then resume automatically
    PAUSED                      -> wait for `auto resume`, then continue
    BUDGET_EXCEEDED/RUNTIME_LIMIT/USER_REQUESTED -> stop and report
    NO_PROGRESS / UNRECOVERABLE / REPEATED_FAILURE(no gate)
                                -> bounded restart with backoff

State is persisted after every cycle, so a killed daemon resumes with
`auto daemon` (or `auto run`) and loses nothing. All decisions taken here are
deterministic; no model is consulted.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from dataclasses import dataclass, field
from typing import Any

from ..core.events import EventTypes
from ..core.state_machine import TaskState
from ..core.task import now_iso
from .context_setup import RuntimeContext
from .control import ControlChannel
from .orchestrator import Orchestrator
from .stop import StopReason

# Stop reasons the daemon resolves by waiting for a human.
_WAIT_REASONS = {StopReason.HUMAN_APPROVAL_REQUIRED, StopReason.PAUSED}
# Stop reasons that are worth one bounded retry (the world may have changed).
_RETRY_REASONS = {
    StopReason.NO_PROGRESS,
    StopReason.UNRECOVERABLE_FAILURE,
    StopReason.ENVIRONMENT_FAILURE,
}
# Stop reasons that end the daemon: PROJECT_COMPLETE, BUDGET_EXCEEDED,
# RUNTIME_LIMIT, USER_REQUESTED, SAFETY_BLOCK, REPEATED_FAILURE.
_MAX_RUNS = 50  # hard bound so wait/approve ping-pong can never loop forever


@dataclass
class DaemonReport:
    status: str  # completed | stopped | gave_up
    reason: str = ""
    runs: int = 0
    restarts: int = 0
    waits: int = 0
    requeued_tasks: list[str] = field(default_factory=list)
    cancelled_tasks: list[str] = field(default_factory=list)
    cycles: int = 0
    cost_usd: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "runs": self.runs,
            "restarts": self.restarts,
            "waits": self.waits,
            "requeued_tasks": self.requeued_tasks,
            "cancelled_tasks": self.cancelled_tasks,
            "cycles": self.cycles,
            "cost_usd": round(self.cost_usd, 4),
        }


class DaemonLoop:
    """Runs the orchestrator repeatedly, resolving human gates as they open."""

    def __init__(
        self,
        context: RuntimeContext,
        *,
        poll_seconds: float = 5.0,
        max_restarts: int = 5,
        max_cycles_per_run: int = 200,
        on_event: Any = None,
    ):
        self.context = context
        self.poll_seconds = poll_seconds
        self.max_restarts = max_restarts
        self.max_cycles_per_run = max_cycles_per_run
        self.on_event = on_event
        self.control = ControlChannel(context.workspace.paths.execution)
        self._daemon_state_path = context.workspace.paths.execution / "daemon.json"

    # ---- public entry ----

    async def run(self) -> DaemonReport:
        report = DaemonReport(status="stopped")
        self._write_state(report)
        try:
            while report.runs < _MAX_RUNS:
                orchestrator = Orchestrator(
                    self.context,
                    max_cycles=self.max_cycles_per_run,
                    use_model_director=True,
                    on_event=self.on_event,
                )
                try:
                    result = await orchestrator.run_loop()
                except Exception as exc:  # noqa: BLE001 - a crashed run is restartable
                    # An unexpected exception must not kill the unattended
                    # daemon: treat it like a retryable failure below.
                    self._emit("daemon.run_crashed", run=report.runs + 1, error=str(exc)[:400])
                    report.restarts += 1
                    if report.restarts > self.max_restarts:
                        report.status = "gave_up"
                        report.reason = f"crashed {report.restarts} times: {exc}"[:200]
                        self._emit("daemon.gave_up", reason=report.reason)
                        return report
                    await asyncio.sleep(self.poll_seconds)
                    continue
                report.runs += 1
                report.cycles = max(report.cycles, result.cycles)
                report.cost_usd += result.cost_usd
                reason = result.stop.reason if result.stop else StopReason.NO_PROGRESS
                self._emit("daemon.run_finished", stop_reason=reason.value, run=report.runs)
                self._write_state(report, last_stop=reason.value)

                if reason == StopReason.PROJECT_COMPLETE:
                    report.status = "completed"
                    report.reason = reason.value
                    return report
                if reason in (
                    StopReason.BUDGET_EXCEEDED,
                    StopReason.RUNTIME_LIMIT,
                    StopReason.USER_REQUESTED,
                    StopReason.SAFETY_BLOCK,
                ):
                    report.reason = reason.value
                    return report

                if reason in _WAIT_REASONS:
                    report.waits += 1
                    self._emit("daemon.waiting", for_reason=reason.value)
                    resolved = await self._wait_for_human(reason)
                    if resolved == "stop":
                        report.status = "stopped"
                        report.reason = StopReason.USER_REQUESTED.value
                        return report
                    actions = self._apply_human_resolutions()
                    report.requeued_tasks.extend(actions["requeued"])
                    report.cancelled_tasks.extend(actions["cancelled"])
                    self._emit(
                        "daemon.resumed",
                        requeued=actions["requeued"],
                        cancelled=actions["cancelled"],
                    )
                    continue

                # retryable failures
                report.restarts += 1
                if report.restarts > self.max_restarts:
                    report.status = "gave_up"
                    report.reason = reason.value
                    self._emit("daemon.gave_up", reason=reason.value, restarts=report.restarts)
                    return report
                self._emit(
                    "daemon.restarting",
                    reason=reason.value,
                    restart=report.restarts,
                    max_restarts=self.max_restarts,
                )
                await asyncio.sleep(self.poll_seconds)

            report.status = "gave_up"
            report.reason = f"reached the {_MAX_RUNS}-run safety bound"
            return report
        finally:
            self._write_state(report)

    # ---- human gate handling ----

    async def _wait_for_human(self, reason: StopReason) -> str:
        """Poll until the gate that stopped the run is resolved.

        HUMAN_APPROVAL_REQUIRED waits for every escalation to be resolved.
        PAUSED waits for an explicit resume request — the pause flag itself
        was already consumed by the run that stopped, so waiting only on the
        escalation queue would silently ignore the operator's pause.
        Both honor `auto stop`, which ends the daemon.
        """
        while True:
            signal = self.control.read()
            if signal.stop:
                # Consume the request: a stale stop on disk would otherwise
                # halt the very next `auto run` at cycle 1 without any work.
                self.control.clear()
                return "stop"
            if reason == StopReason.PAUSED:
                if signal.resume:
                    self.control.clear()
                    return "resolved"
            elif not self.context.workspace.pending_escalations():
                return "resolved"
            await asyncio.sleep(self.poll_seconds)

    def _apply_human_resolutions(self) -> dict[str, list[str]]:
        """Turn resolved escalations into deterministic graph updates.

        Approved repeated-failure tasks are requeued (the human accepted
        another attempt). Rejected ones are cancelled — continuing to retry
        rejected work would override an explicit human decision.
        """
        requeued: list[str] = []
        cancelled: list[str] = []
        graph = self.context.workspace.load_graph()
        changed = False
        for escalation in self.context.workspace.load_escalations():
            task_id = escalation.get("task_id", "")
            status = escalation.get("status")
            if not task_id or task_id not in graph.tasks:
                continue
            task = graph.get(task_id)
            if task.is_terminal():
                continue
            if status == "approved" and task.status == TaskState.ARCHITECTURE_REVIEW:
                task.set_state(
                    TaskState.READY, agent="daemon", note="human approved another attempt"
                )
                requeued.append(task_id)
                changed = True
            elif status == "rejected":
                # Cascade: dependents of rejected work can never run, so
                # leaving them BLOCKED would make PROJECT_COMPLETE forever
                # unreachable.
                for cancelled_id in graph.cascade_cancel(
                    task_id, agent="daemon", note="human rejected the work"
                ):
                    cancelled.append(cancelled_id)
                changed = True
        if changed:
            self.context.workspace.save_graph(graph)
            for task_id in cancelled:
                self._emit(EventTypes.TASK_CANCELLED, task_id=task_id, agent="daemon")
        return {"requeued": requeued, "cancelled": cancelled}

    # ---- plumbing ----

    def _emit(self, event: str, **fields: Any) -> None:
        payload = self.context.workspace.events.append(event, **fields)
        if self.on_event is not None:
            with contextlib.suppress(Exception):
                self.on_event(event, payload)  # a broken listener must never kill the daemon

    def _write_state(self, report: DaemonReport, *, last_stop: str = "") -> None:
        try:
            self._daemon_state_path.parent.mkdir(parents=True, exist_ok=True)
            self._daemon_state_path.write_text(
                json_dumps(
                    {
                        "pid": os.getpid(),
                        "started_at": now_iso(),
                        "updated_at": now_iso(),
                        "runs": report.runs,
                        "restarts": report.restarts,
                        "waits": report.waits,
                        "status": report.status,
                        "last_stop": last_stop,
                        "poll_seconds": self.poll_seconds,
                    }
                ),
                encoding="utf-8",
            )
        except OSError:
            pass


def json_dumps(payload: dict[str, Any]) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False, indent=2)


def wait_for_file_change(path, timeout: float) -> bool:
    """Test helper: block (thread) until `path` exists or timeout elapses."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            return True
        time.sleep(0.01)
    return False
