"""Task state machine.

Every task exists in a known state; transitions are validated here.
Failure transitions (VERIFYING -> FAILED -> DIAGNOSING -> REPAIRING -> VERIFYING)
and architecture-escalation (FAILED -> ARCHITECTURE_REVIEW -> REPLAN) are
first-class, preventing undefined situations.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class TaskState(StrEnum):
    QUEUED = "QUEUED"
    READY = "READY"
    ASSIGNED = "ASSIGNED"
    IMPLEMENTING = "IMPLEMENTING"
    VERIFYING = "VERIFYING"
    REVIEWING = "REVIEWING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    DIAGNOSING = "DIAGNOSING"
    REPAIRING = "REPAIRING"
    ARCHITECTURE_REVIEW = "ARCHITECTURE_REVIEW"
    REPLAN = "REPLAN"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"


class TaskRole(StrEnum):
    """Which kind of agent performs the task."""

    CODE = "coding"
    TEST = "testing"
    REVIEW = "review"
    RESEARCH = "research"
    ARCHITECTURE = "architecture"
    PLANNING = "planning"
    DEBUG = "debugging"
    SECURITY = "security"
    RELEASE = "release"


# Legal transitions. Anything not listed here raises IllegalTransition.
#
# Two families of edge exist beyond the happy path:
#   * crash recovery — an assignment or an in-flight attempt is abandoned and
#     re-queued, so nothing is lost and nothing is double-counted;
#   * repair requeue — a diagnosed failure goes back to READY so the repair is
#     actually implemented before verification is retried.
_TRANSITIONS: dict[TaskState, set[TaskState]] = {
    TaskState.QUEUED: {TaskState.READY, TaskState.BLOCKED, TaskState.CANCELLED},
    # READY -> REPLAN: a schedulable task whose plan was invalidated must pass
    # through REPLAN before re-queueing, so the replan is visible in history.
    TaskState.READY: {
        TaskState.ASSIGNED,
        TaskState.QUEUED,
        TaskState.REPLAN,
        TaskState.CANCELLED,
    },
    TaskState.ASSIGNED: {
        TaskState.IMPLEMENTING,
        TaskState.FAILED,  # abandoned before work started (crash, cancellation)
        TaskState.CANCELLED,
    },
    TaskState.IMPLEMENTING: {
        TaskState.VERIFYING,
        TaskState.FAILED,
        TaskState.CANCELLED,
    },
    TaskState.VERIFYING: {
        TaskState.REVIEWING,
        TaskState.COMPLETED,  # simple tasks with no review requirement
        TaskState.FAILED,
        TaskState.CANCELLED,
    },
    TaskState.REVIEWING: {
        TaskState.COMPLETED,
        TaskState.FAILED,
        TaskState.REPAIRING,
        TaskState.CANCELLED,
    },
    TaskState.FAILED: {
        TaskState.DIAGNOSING,
        TaskState.ARCHITECTURE_REVIEW,
        TaskState.REPLAN,
        TaskState.READY,  # requeue after transient infrastructure failure
        TaskState.CANCELLED,
    },
    TaskState.DIAGNOSING: {
        TaskState.REPAIRING,
        TaskState.REPLAN,
        TaskState.READY,
        TaskState.CANCELLED,
    },
    TaskState.REPAIRING: {
        TaskState.VERIFYING,
        TaskState.READY,
        TaskState.FAILED,
        TaskState.CANCELLED,
    },
    TaskState.ARCHITECTURE_REVIEW: {TaskState.REPLAN, TaskState.READY, TaskState.CANCELLED},
    TaskState.REPLAN: {TaskState.QUEUED, TaskState.READY, TaskState.CANCELLED},
    TaskState.BLOCKED: {TaskState.READY, TaskState.CANCELLED},
    TaskState.COMPLETED: set(),
    TaskState.CANCELLED: set(),
}

TERMINAL_STATES = {TaskState.COMPLETED, TaskState.CANCELLED}
ACTIVE_STATES = {
    TaskState.ASSIGNED,
    TaskState.IMPLEMENTING,
    TaskState.VERIFYING,
    TaskState.REVIEWING,
    TaskState.DIAGNOSING,
    TaskState.REPAIRING,
    TaskState.ARCHITECTURE_REVIEW,
}


class IllegalTransition(Exception):
    def __init__(self, current: TaskState, target: TaskState, reason: str = ""):
        self.current = current
        self.target = target
        self.reason = reason
        super().__init__(
            f"Illegal task transition {current.value} -> {target.value}"
            + (f": {reason}" if reason else "")
        )


def can_transition(current: TaskState | str, target: TaskState | str) -> bool:
    current = TaskState(current)
    target = TaskState(target)
    return target in _TRANSITIONS.get(current, set())


def transition(current: TaskState | str, target: TaskState | str) -> TaskState:
    current = TaskState(current)
    target = TaskState(target)
    if not can_transition(current, target):
        raise IllegalTransition(current, target)
    return target


def failure_path_repair() -> list[TaskState]:
    """VERIFYING -> FAILED -> DIAGNOSING -> REPAIRING -> VERIFYING"""
    return [
        TaskState.FAILED,
        TaskState.DIAGNOSING,
        TaskState.REPAIRING,
        TaskState.VERIFYING,
    ]


def next_after_failure(attempts: int, max_attempts: int) -> TaskState:
    """Deterministic decision after a verification failure.

    attempts < max_attempts  -> DIAGNOSING (normal repair loop)
    attempts >= max_attempts -> ARCHITECTURE_REVIEW (escalate, stop repeating)
    """
    if attempts >= max_attempts:
        return TaskState.ARCHITECTURE_REVIEW
    return TaskState.DIAGNOSING


def validate_schema(payload: dict[str, Any]) -> list[str]:
    """Cheap structural validation used by tests and by state loading."""
    errors: list[str] = []
    for state in payload.get("states", []):
        try:
            TaskState(state)
        except ValueError:
            errors.append(f"unknown state: {state}")
    return errors
