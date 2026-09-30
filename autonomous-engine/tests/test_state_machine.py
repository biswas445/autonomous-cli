"""Task state machine: legal transitions and the failure paths (plan.md §11)."""

from __future__ import annotations

import pytest

from autonomous_engine.core.state_machine import (
    IllegalTransition,
    TaskState,
    can_transition,
    next_after_failure,
    transition,
)
from autonomous_engine.core.task import Task


def test_happy_path_is_legal():
    path = [
        TaskState.QUEUED,
        TaskState.READY,
        TaskState.ASSIGNED,
        TaskState.IMPLEMENTING,
        TaskState.VERIFYING,
        TaskState.REVIEWING,
        TaskState.COMPLETED,
    ]
    for current, target in zip(path, path[1:], strict=False):
        assert can_transition(current, target), f"{current} -> {target}"


def test_failure_repair_loop_is_legal():
    assert can_transition(TaskState.VERIFYING, TaskState.FAILED)
    assert can_transition(TaskState.FAILED, TaskState.DIAGNOSING)
    assert can_transition(TaskState.DIAGNOSING, TaskState.REPAIRING)
    assert can_transition(TaskState.REPAIRING, TaskState.VERIFYING)


def test_architecture_escalation_is_legal():
    assert can_transition(TaskState.FAILED, TaskState.ARCHITECTURE_REVIEW)
    assert can_transition(TaskState.ARCHITECTURE_REVIEW, TaskState.REPLAN)
    assert can_transition(TaskState.REPLAN, TaskState.QUEUED)


def test_ready_can_replan():
    """A schedulable task whose plan was invalidated passes through REPLAN."""
    assert can_transition(TaskState.READY, TaskState.REPLAN)


def test_terminal_states_are_absorbing():
    assert not can_transition(TaskState.COMPLETED, TaskState.READY)
    assert not can_transition(TaskState.CANCELLED, TaskState.READY)
    assert not can_transition(TaskState.COMPLETED, TaskState.FAILED)


def test_illegal_transition_raises():
    with pytest.raises(IllegalTransition):
        transition(TaskState.COMPLETED, TaskState.IMPLEMENTING)


def test_task_set_state_records_history():
    task = Task(title="demo")
    task.set_state(TaskState.READY, agent="orchestrator", note="deps ok")
    task.set_state(TaskState.ASSIGNED, agent="orchestrator")
    assert task.status == TaskState.ASSIGNED
    assert [h.to_state for h in task.history] == ["READY", "ASSIGNED"]
    assert task.updated_at


def test_task_set_state_rejects_illegal():
    task = Task(title="demo")
    task.set_state(TaskState.READY)
    with pytest.raises(IllegalTransition):
        task.set_state(TaskState.COMPLETED)


def test_next_after_failure_escalates_at_max_attempts():
    assert next_after_failure(attempts=1, max_attempts=3) == TaskState.DIAGNOSING
    assert next_after_failure(attempts=3, max_attempts=3) == TaskState.ARCHITECTURE_REVIEW


def test_task_active_and_terminal_helpers():
    task = Task(title="demo")
    assert not task.is_active() and not task.is_terminal()
    task.set_state(TaskState.READY)
    task.set_state(TaskState.ASSIGNED)
    assert task.is_active()
    task.set_state(TaskState.FAILED)
    assert not task.is_active()
    task.set_state(TaskState.DIAGNOSING)
    task.set_state(TaskState.REPAIRING)
    task.set_state(TaskState.VERIFYING)
    task.set_state(TaskState.COMPLETED)
    assert task.is_terminal()
    assert task.completed_at is not None
