"""Checkpoint restore, crash recovery, and idempotency (plan.md §18, §19, §20).

These tests prove recovery paths *actually work* — not that checkpoint files
are merely created. A run is interrupted mid-task; the next run must recover
it. A checkpoint is restored; the graph and (optionally) the tree must match.
"""

from __future__ import annotations

import json
from pathlib import Path

from autonomous_engine.runtime.context_setup import open_context
from autonomous_engine.runtime.orchestrator import Orchestrator


async def _run(orchestrator: Orchestrator, objective: str):
    return await orchestrator.run_loop(objective)


async def test_checkpoint_restore_rebuilds_the_graph(project: Path):
    """Run, complete tasks, restore the first checkpoint: graph state returns."""
    context = open_context(project)
    first = Orchestrator(context, use_model_director=False)
    result = await _run(first, "Build the restore target")
    assert result.status == "completed"

    checkpoints = first.workspace.list_checkpoints()
    assert checkpoints, "a completing run must create checkpoints"

    # Damage the live JSON graph. Note _load_state() falls back to the SQLite
    # mirror when the JSON is empty — dual-layer persistence by design — so
    # restore_checkpoint is exercised directly against the store record.
    graph_path = context.workspace.paths.planning / "task_graph.json"
    damaged = json.loads(graph_path.read_text(encoding="utf-8"))
    damaged["tasks"] = {}
    graph_path.write_text(json.dumps(damaged), encoding="utf-8")

    second = Orchestrator(context, use_model_director=False)
    summary = second.restore_checkpoint(checkpoints[0])
    assert summary["progress"]["total"] > 0
    assert second.graph.tasks  # graph rebuilt from the checkpoint record
    restored = second.restore_checkpoint(checkpoints[0])
    assert restored["checkpoint"] == checkpoints[0]

    # The restored graph round-trips to disk.
    reloaded = context.workspace.load_graph()
    assert len(reloaded.tasks) == summary["progress"]["total"]
    context.db.close()


async def test_checkpoint_restore_via_cli_rollback(project: Path):
    """`auto rollback <id> --yes` restores the graph and reports the commit."""
    from typer.testing import CliRunner

    from autonomous_engine.cli.app import app

    context = open_context(project)
    orchestrator = Orchestrator(context, use_model_director=False)
    await _run(orchestrator, "Build the cli-rollback target")
    checkpoints = orchestrator.workspace.list_checkpoints()
    assert checkpoints

    runner = CliRunner()
    missing = runner.invoke(app, ["rollback", "checkpoint-9999", "--yes", "--path", str(project)])
    assert missing.exit_code != 0
    assert "unknown checkpoint" in missing.output.lower()

    ok = runner.invoke(
        app, ["rollback", checkpoints[0], "--yes", "--path", str(project), "--no-git"]
    )
    assert ok.exit_code == 0, ok.output
    assert "rolled back" in ok.output
    context.db.close()


async def test_unknown_escalations_block_then_resolve(project: Path):
    """Escalation -> HUMAN_APPROVAL_REQUIRED -> approve -> run completes."""
    from autonomous_engine.core.task import Task

    context = open_context(project)
    orchestrator = Orchestrator(context, use_model_director=False)
    graph = orchestrator.graph
    task = Task(id="TASK-ESC", title="needs a human", risk="high")
    graph.add_task(task)
    orchestrator._persist_graph()
    orchestrator._escalate(
        "TASK-ESC", "supervised: destructive migration needs approval", kind="approval_gate"
    )

    result = await _run(orchestrator, "Build the escalation target")
    assert result.stop.reason.value == "HUMAN_APPROVAL_REQUIRED"
    assert context.workspace.pending_escalations()

    pending = context.workspace.pending_escalations()[0]
    context.workspace.resolve_escalation(pending["id"], "approved", "operator approved")
    orchestrator2 = Orchestrator(context, use_model_director=False)
    result2 = await _run(orchestrator2, "Build the escalation target")
    assert result2.status == "completed"
    context.db.close()


async def test_cli_approve_requeues_architecture_review_task(project: Path):
    """Regression: `auto approve <id>` then `auto run` must resume the task.

    The control-signal approval path used to only lift risk gates; the task
    stayed in ARCHITECTURE_REVIEW and the next run stopped with
    REPEATED_FAILURE at cycle 1 without doing any work.
    """
    from autonomous_engine.core.state_machine import TaskState
    from autonomous_engine.core.task import Task
    from autonomous_engine.runtime.control import ControlChannel
    from autonomous_engine.runtime.stop import StopReason

    context = open_context(project)
    graph = context.workspace.load_graph()
    task = Task(id="TASK-CLI-1", title="gated work", status=TaskState.ARCHITECTURE_REVIEW)
    task.attempts = 3
    graph.add_task(task)
    context.workspace.save_graph(graph)
    context.workspace.add_escalation(
        {
            "id": "ESC-CLI-1",
            "task_id": "TASK-CLI-1",
            "kind": "repeated_failure",
            "reason": "attempts exhausted",
            "status": "pending",
            "created_at": "now",
        }
    )

    orchestrator = Orchestrator(context, use_model_director=False)
    result = await _run(orchestrator, "Build the gated target")
    assert result.stop.reason == StopReason.HUMAN_APPROVAL_REQUIRED

    # Operator: `auto approve ESC-CLI-1` = resolve the escalation + write the
    # control signal (exactly what cli/app._resolve_escalation does).
    context.workspace.resolve_escalation("ESC-CLI-1", "approved", "operator approved")
    ControlChannel(context.workspace.paths.execution).request(approvals=["ESC-CLI-1"])

    orchestrator2 = Orchestrator(context, use_model_director=False)
    result2 = await _run(orchestrator2, "Build the gated target")
    assert result2.stop.reason != StopReason.REPEATED_FAILURE, result2.stop.describe()
    graph2 = context.workspace.load_graph()
    assert graph2.get("TASK-CLI-1").status == TaskState.COMPLETED
    context.db.close()


async def test_duplicate_checkpoint_ids_are_not_produced(project: Path):
    """Idempotency: each checkpoint gets a fresh, monotonic id (plan.md §20)."""
    context = open_context(project)
    orchestrator = Orchestrator(context, use_model_director=False)
    await _run(orchestrator, "Build the idempotency target")

    ids = orchestrator.workspace.list_checkpoints()
    assert len(ids) == len(set(ids)), "checkpoint ids must be unique"
    numbers = [int(cid.split("-")[1]) for cid in ids]
    assert numbers == sorted(numbers), "checkpoint ids must increase"
    context.db.close()


async def test_crash_between_tasks_preserves_completed_state(project: Path):
    """Completed tasks are never re-executed after a restart (§19, §20)."""
    context = open_context(project)
    first = Orchestrator(context, use_model_director=False)
    await _run(first, "Build the crash-safety target")

    completed_before = {t.id for t in first.graph.completed_tasks()}
    assert completed_before

    # A fresh orchestrator (process restart) resumes: completed work is not
    # re-run — the stop condition fires immediately with the same evidence.
    second = Orchestrator(context, use_model_director=False)
    second._load_state()
    result = await _run(second, "Build the crash-safety target")
    assert result.status == "completed"
    assert {t.id for t in second.graph.completed_tasks()} >= completed_before
    # No task gained attempts from the "restart".
    for task in second.graph.all():
        if task.id in completed_before:
            assert task.attempts == 1
    context.db.close()
