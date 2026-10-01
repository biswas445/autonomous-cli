"""Event log and SQLite store round-trips."""

from __future__ import annotations

from pathlib import Path

from autonomous_engine.core.database import Database
from autonomous_engine.core.events import EventLog
from autonomous_engine.core.state_machine import TaskState
from autonomous_engine.core.store import (
    CheckpointRecord,
    DecisionRecord,
    FailureRecord,
    ProjectRecord,
    RunRecord,
    Store,
    UnknownRecord,
)
from autonomous_engine.core.task import Task, TaskGraph, new_id

# ---- events ----------------------------------------------------------------


def test_event_log_roundtrip(tmp_path: Path):
    log = EventLog(tmp_path / "events.jsonl")
    log.append("run.started", run_id="r1")
    log.append("task.completed", task_id="TASK-1", ok=True)
    events = log.read_all()
    assert [e["event"] for e in events] == ["run.started", "task.completed"]
    assert events[1]["task_id"] == "TASK-1"


def test_event_log_tolerates_torn_lines(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    path.write_text('{"event": "a", "timestamp": "t"}\n{"event": "bro', encoding="utf-8")
    log = EventLog(path)
    events = log.read_all()
    assert [e["event"] for e in events] == ["a"]


def test_event_log_tail_since(tmp_path: Path):
    log = EventLog(tmp_path / "events.jsonl")
    log.append("a", timestamp="2026-01-01T00:00:00Z")
    log.append("b", timestamp="2026-01-02T00:00:00Z")
    later = log.tail_since("2026-01-01T00:00:01Z")
    assert [e["event"] for e in later] == ["b"]
    # same-second events are included (>= cursor): a strict > skipped events
    # appended within the same second the UI had already seen
    same_second = log.tail_since("2026-01-01T00:00:00Z")
    assert [e["event"] for e in same_second] == ["a", "b"]


# ---- store -----------------------------------------------------------------


def _store(tmp_path: Path) -> Store:
    return Store(Database(tmp_path / "state.sqlite"), "proj-1")


def test_project_roundtrip(tmp_path: Path):
    store = _store(tmp_path)
    store.upsert_project(ProjectRecord(id="proj-1", name="p", objective="do things"))
    project = store.get_project()
    assert project is not None
    assert project.objective == "do things"


def test_task_graph_roundtrip(tmp_path: Path):
    store = _store(tmp_path)
    graph = TaskGraph()
    graph.add_task(Task(id="TASK-001", title="first", priority=2, status=TaskState.COMPLETED))
    store.save_graph(graph)
    loaded = store.load_graph()
    assert loaded.get("TASK-001").status.value == "COMPLETED"


def test_run_lifecycle(tmp_path: Path):
    store = _store(tmp_path)
    run = RunRecord(id="RUN-1", project_id="proj-1")
    store.start_run(run)
    latest = store.latest_run()
    assert latest is not None and latest.status == "running"
    store.finish_run("RUN-1", "completed", "PROJECT_COMPLETE", {"cycles": 4})
    latest = store.latest_run()
    assert latest.status == "completed"
    assert latest.stop_reason == "PROJECT_COMPLETE"


def test_events_mirror(tmp_path: Path):
    store = _store(tmp_path)
    store.append_event("cycle.completed", {"ok": True})
    store.append_event("task.completed", {"task_id": "TASK-1"})
    events = store.recent_events()
    assert [e["event"] for e in events] == ["cycle.completed", "task.completed"]


def test_checkpoints(tmp_path: Path):
    store = _store(tmp_path)
    record = CheckpointRecord(
        id="checkpoint-0001", project_id="proj-1", git_commit="abc", task_graph={"version": 1}
    )
    store.save_checkpoint(record)
    assert [r["id"] for r in store.list_checkpoints()] == ["checkpoint-0001"]
    loaded = store.get_checkpoint("checkpoint-0001")
    assert loaded is not None and loaded.git_commit == "abc"
    assert store.get_checkpoint("nope") is None


def test_decisions_failures_unknowns(tmp_path: Path):
    store = _store(tmp_path)
    store.save_decision(
        DecisionRecord(id=new_id("DEC"), project_id="proj-1", title="use sqlite", confidence=0.9)
    )
    assert store.list_decisions()[0].title == "use sqlite"

    store.save_failure(
        FailureRecord(id=new_id("FAIL"), project_id="proj-1", task_id="TASK-1", summary="boom")
    )
    assert store.list_failures()[0].summary == "boom"

    unknown_id = new_id("UNK")
    store.save_unknown(UnknownRecord(id=unknown_id, project_id="proj-1", question="which db?"))
    assert [u.id for u in store.open_unknowns()] == [unknown_id]
    store.save_unknown(
        UnknownRecord(
            id=unknown_id,
            project_id="proj-1",
            question="which db?",
            status="resolved",
            answer="sqlite",
            resolved_at="now",
        )
    )
    assert store.open_unknowns() == []


def test_budget_usage_aggregation(tmp_path: Path):
    store = _store(tmp_path)
    store.record_usage("RUN-1", "coder", "m", 100, 50, 0.01)
    store.record_usage("RUN-1", "coder", "m", 200, 100, 0.02)
    usage = store.total_usage()
    assert usage.tokens_in == 300
    assert usage.tokens_out == 150
    assert abs(usage.cost_usd - 0.03) < 1e-9
    assert usage.calls == 2


def test_record_timestamps_are_per_instance():
    """Regression: default timestamps were frozen at module import time, so
    every decision/failure/checkpoint record got the process-start time."""
    import time

    from autonomous_engine.core import store as store_module

    before = store_module.now_iso()
    time.sleep(1.1)
    record = store_module.DecisionRecord(id="D1", project_id="P", title="a")
    other = store_module.FailureRecord(id="F1", project_id="P", summary="s")
    assert record.created_at > before
    assert other.created_at > before
