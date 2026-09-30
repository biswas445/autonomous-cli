"""Task graph: dependencies, readiness, cycles, waves, dynamic mutation."""

from __future__ import annotations

import pytest

from autonomous_engine.core.state_machine import TaskState
from autonomous_engine.core.task import Task, TaskGraph


def _done(graph: TaskGraph, task_id: str) -> None:
    graph.get(task_id).set_state(TaskState.READY)
    graph.get(task_id).set_state(TaskState.ASSIGNED)
    graph.get(task_id).set_state(TaskState.IMPLEMENTING)
    graph.get(task_id).set_state(TaskState.VERIFYING)
    graph.get(task_id).set_state(TaskState.COMPLETED)


def test_ready_respects_dependencies():
    graph = TaskGraph()
    a = graph.add_task(Task(id="A", title="a"))
    b = graph.add_task(Task(id="B", title="b", dependencies=["A"]))
    ready = graph.ready_tasks()
    assert [t.id for t in ready] == ["A"]
    _done(graph, "A")
    ready = graph.ready_tasks()
    assert [t.id for t in ready] == ["B"]
    assert a.id and b.id


def test_blocked_tasks_report_unsatisfied_dependencies():
    graph = TaskGraph()
    graph.add_task(Task(id="A", title="a"))
    graph.add_task(Task(id="B", title="b", dependencies=["A"]))
    assert [t.id for t in graph.blocked_tasks()] == ["B"]
    _done(graph, "A")
    assert graph.blocked_tasks() == []


def test_missing_dependency_does_not_crash_and_blocks():
    graph = TaskGraph()
    graph.add_task(Task(id="B", title="b", dependencies=["GHOST"]))
    assert [t.id for t in graph.blocked_tasks()] == ["B"]
    assert graph.ready_tasks() == []


def test_detect_cycles_and_topological_order():
    graph = TaskGraph()
    graph.add_task(Task(id="A", title="a", dependencies=["C"]))
    graph.add_task(Task(id="B", title="b", dependencies=["A"]))
    graph.add_task(Task(id="C", title="c", dependencies=["B"]))
    assert graph.detect_cycles() != []
    with pytest.raises(ValueError):
        graph.topological_order()

    graph.get("A").dependencies = []
    assert graph.detect_cycles() == []
    order = graph.topological_order()
    assert order.index("A") < order.index("B") < order.index("C")


def test_independent_wave_never_shares_dependencies():
    graph = TaskGraph()
    graph.add_task(Task(id="shared", title="shared"))
    graph.add_task(Task(id="x", title="x", dependencies=["shared"]))
    graph.add_task(Task(id="y", title="y", dependencies=["shared"]))
    # only the dependency root is ready at first
    assert graph.independent_wave(max_tasks=4) == ["shared"]
    _done(graph, "shared")
    # a completed dependency is not a conflict: x and y run together
    wave = graph.independent_wave(max_tasks=4)
    assert sorted(wave) == ["x", "y"]


def test_progress_counts():
    graph = TaskGraph()
    graph.add_task(Task(id="A", title="a"))
    graph.add_task(Task(id="B", title="b"))
    _done(graph, "A")
    progress = graph.progress()
    assert progress["total"] == 2
    assert progress["completed"] == 1
    assert progress["pending"] == 1


def test_add_remove_task_and_duplicate_rejection():
    graph = TaskGraph()
    graph.add_task(Task(id="A", title="a"))
    with pytest.raises(ValueError):
        graph.add_task(Task(id="A", title="duplicate"))
    graph.add_task(Task(id="B", title="b", dependencies=["A"]))
    graph.remove_task("A")
    assert graph.get("B").dependencies == []


def test_priority_then_age_ordering():
    graph = TaskGraph()
    graph.add_task(Task(id="LOW", title="low", priority=8))
    graph.add_task(Task(id="HIGH", title="high", priority=1))
    assert [t.id for t in graph.ready_tasks()] == ["HIGH", "LOW"]
