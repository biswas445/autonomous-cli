"""Stop engine: the system knows when to quit (plan.md §16, §26).

There are exactly two ways a run ends:

  * success  — every task that the plan requires is COMPLETED *and* its
                Definition of Done produced passing executable evidence;
  * stop    — a named, recorded condition fired and a human is told why.

Time and money are budgets, not goals: reaching them produces a stop with a
reason, never a silent "done".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ..core.task import TaskGraph


class StopReason(StrEnum):
    PROJECT_COMPLETE = "PROJECT_COMPLETE"
    HUMAN_APPROVAL_REQUIRED = "HUMAN_APPROVAL_REQUIRED"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    RUNTIME_LIMIT = "RUNTIME_LIMIT"
    UNRECOVERABLE_FAILURE = "UNRECOVERABLE_FAILURE"
    ENVIRONMENT_FAILURE = "ENVIRONMENT_FAILURE"
    SAFETY_BLOCK = "SAFETY_BLOCK"
    REPEATED_FAILURE = "REPEATED_FAILURE"
    PAUSED = "PAUSED"
    USER_REQUESTED = "USER_REQUESTED"
    NO_PROGRESS = "NO_PROGRESS"


STOP_DESCRIPTIONS: dict[str, str] = {
    StopReason.PROJECT_COMPLETE: "Every task completed with passing executable evidence.",
    StopReason.HUMAN_APPROVAL_REQUIRED: "A high-risk or irreversible action needs a human decision.",
    StopReason.BUDGET_EXCEEDED: "The token/cost budget was exhausted before the work was done.",
    StopReason.RUNTIME_LIMIT: "The runtime budget was exhausted before the work was done.",
    StopReason.UNRECOVERABLE_FAILURE: "A failure the system cannot diagnose or repair.",
    StopReason.ENVIRONMENT_FAILURE: "The environment itself is broken (git, filesystem, model).",
    StopReason.SAFETY_BLOCK: "A safety or security policy blocked an action.",
    StopReason.REPEATED_FAILURE: "The same task failed the maximum number of times.",
    StopReason.PAUSED: "The run was paused by the operator.",
    StopReason.USER_REQUESTED: "The run was stopped by the operator.",
    StopReason.NO_PROGRESS: "No task made progress and none can be scheduled.",
}


@dataclass
class StopSignal:
    reason: StopReason
    detail: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    remediation: str = ""

    @property
    def is_success(self) -> bool:
        return self.reason == StopReason.PROJECT_COMPLETE

    def describe(self) -> str:
        base = STOP_DESCRIPTIONS.get(self.reason, self.reason.value)
        parts = [base]
        if self.detail:
            parts.append(self.detail)
        if self.remediation:
            parts.append(f"Next: {self.remediation}")
        return " ".join(parts)

    def as_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason.value,
            "detail": self.detail,
            "description": STOP_DESCRIPTIONS.get(self.reason, ""),
            "evidence": self.evidence,
            "remediation": self.remediation,
            "success": self.is_success,
        }


class StopEngine:
    """Evaluates every hard stop condition once per cycle.

    Order matters: success is checked first so a run that genuinely finished
    is not reported as "budget exceeded" after doing all the work.
    """

    def __init__(self, *, max_task_attempts: int = 3, no_progress_limit: int = 3):
        self.max_task_attempts = max_task_attempts
        self.no_progress_limit = no_progress_limit

    def evaluate(
        self,
        graph: TaskGraph,
        *,
        budget_exhausted: str = "",
        pause_requested: bool = False,
        stop_requested: bool = False,
        escalations: int = 0,
        consecutive_no_progress: int = 0,
        cancelled_by_director: bool = False,
    ) -> StopSignal | None:
        if stop_requested:
            return StopSignal(StopReason.USER_REQUESTED, "operator requested a stop")
        if pause_requested:
            return StopSignal(StopReason.PAUSED, "operator requested a pause")

        if self._project_complete(graph):
            return StopSignal(
                StopReason.PROJECT_COMPLETE,
                detail=f"{len(graph.completed_tasks())} task(s) completed with evidence",
                evidence=graph.progress(),
            )

        if cancelled_by_director:
            return StopSignal(StopReason.USER_REQUESTED, "the director proposed stopping the run")

        if budget_exhausted == "BUDGET_EXCEEDED":
            return StopSignal(
                StopReason.BUDGET_EXCEEDED,
                remediation="raise budget.max_token_budget or reduce scope, then resume.",
            )
        if budget_exhausted == "RUNTIME_LIMIT":
            return StopSignal(
                StopReason.RUNTIME_LIMIT,
                remediation="raise budget.max_runtime_seconds, then resume.",
            )

        if escalations > 0:
            return StopSignal(
                StopReason.HUMAN_APPROVAL_REQUIRED,
                detail=f"{escalations} pending escalation(s) awaiting a human",
                remediation="run `auto approve <id>` or `auto reject <id>`, then resume.",
            )

        exhausted = self._exhausted_tasks(graph)
        if exhausted:
            return StopSignal(
                StopReason.REPEATED_FAILURE,
                detail="tasks exhausted their attempts: " + ", ".join(exhausted),
                evidence={"tasks": exhausted},
                remediation="inspect `auto inspect <task>` and replan, then resume.",
            )

        if consecutive_no_progress >= self.no_progress_limit and not graph.ready_tasks():
            if not graph.all():
                return StopSignal(
                    StopReason.NO_PROGRESS,
                    detail="the task graph is empty; nothing was ever planned",
                )
            return StopSignal(
                StopReason.NO_PROGRESS,
                detail=f"{consecutive_no_progress} cycles without progress and no runnable task",
                remediation="run `auto inspect` and replan the remaining work.",
            )

        return None

    # ---- individual conditions ----

    def _project_complete(self, graph: TaskGraph) -> bool:
        """Complete means: non-empty graph, nothing left to do, nothing failed.

        Cancelled tasks do not count as done, and a graph with zero tasks is
        never "complete" — an empty graph means planning failed.
        """
        if not graph.tasks:
            return False
        if graph.failed_tasks():
            return False
        if graph.active_tasks():
            return False
        if graph.ready_tasks() or graph.blocked_tasks():
            return False
        return all(t.status.value in ("COMPLETED", "CANCELLED") for t in graph.all()) and bool(
            graph.completed_tasks()
        )

    def _exhausted_tasks(self, graph: TaskGraph) -> list[str]:
        return [
            t.id
            for t in graph.all()
            if t.status.value == "ARCHITECTURE_REVIEW" and t.attempts >= self.max_task_attempts
        ]
