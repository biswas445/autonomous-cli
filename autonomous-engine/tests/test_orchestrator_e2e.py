"""End-to-end orchestration tests, fully offline via the echo provider.

These exercise the real loop: intent compilation, planning, implementation,
executable verification, review, QA gating, release, checkpoints, escalation,
supervised approval, pause/resume, and crash recovery.
"""

from __future__ import annotations

import json

from autonomous_engine.core.state_machine import TaskState
from autonomous_engine.core.task import Task
from autonomous_engine.runtime.context_setup import open_context
from autonomous_engine.runtime.control import ControlChannel
from autonomous_engine.runtime.orchestrator import Orchestrator
from autonomous_engine.runtime.stop import StopReason


def _orchestrator(context, **kwargs) -> Orchestrator:
    config = context.config
    config.budget.max_runtime_seconds = 3600
    return Orchestrator(context, use_model_director=False, **kwargs)


def _seed_task(context, **task_kwargs) -> Task:
    graph = context.workspace.load_graph()
    task = Task(**{"title": "seeded task", **task_kwargs})
    graph.add_task(task)
    context.workspace.save_graph(graph)
    return task


# ---- the full loop ---------------------------------------------------------


async def test_full_offline_run_completes_project(project):
    context = open_context(project)
    orchestrator = _orchestrator(context)
    result = await orchestrator.run_loop("Build a note-taking REST API")

    assert result.status == "completed", result.summary
    assert result.stop.reason == StopReason.PROJECT_COMPLETE
    assert result.completed >= 3
    assert result.failed == 0

    # artifacts were really written
    assert (project / "README.md").is_file()
    assert any((project / "src").glob("*.py"))
    # CHANGELOG produced by the release step
    assert (project / "CHANGELOG.md").is_file()
    # a git commit exists per task plus the release commit
    log = orchestrator.git.log(20)
    assert any("checkpoint" in line for line in log)
    assert any("release" in line for line in log)

    # events are event-sourced and replayable
    events = orchestrator.workspace.events.read_all()
    names = [e["event"] for e in events]
    assert "intent.compiled" in names
    assert "task.created" in names
    assert "qa.gate" in names
    assert "release.prepared" in names
    assert "run.stopped" in names

    # QA gate artefact records the alignment verdict
    qa = json.loads(
        (project / ".agents" / "verification" / "qa_gate.json").read_text(encoding="utf-8")
    )
    assert qa["aligned"] is True

    # every completed task carries verification evidence
    graph = context.workspace.load_graph()
    for task in graph.completed_tasks():
        assert task.verification.get("passed") is True
        assert task.attempts_history, "attempts must be recorded"
    context.db.close()


async def test_task_lifecycle_records_history(project):
    context = open_context(project)
    orchestrator = _orchestrator(context)
    await orchestrator.run_loop("Build a thing")
    graph = context.workspace.load_graph()
    first = sorted(graph.all(), key=lambda t: t.created_at)[0]
    states = [h.to_state for h in first.history]
    assert states[0] == "READY"
    assert "IMPLEMENTING" in states
    assert "VERIFYING" in states
    assert states[-1] == "COMPLETED"


# ---- failure, diagnosis, repair, escalation --------------------------------


async def test_failing_task_goes_through_repair_then_escalates(project):
    context = open_context(project)
    _seed_task(
        context,
        id="TASK-F1",
        title="always fails",
        verification_commands=['python -c "import sys; sys.exit(1)"'],
    )
    orchestrator = _orchestrator(context)
    result = await orchestrator.run_loop()

    assert result.status == "human_approval_required"
    assert result.stop.reason == StopReason.HUMAN_APPROVAL_REQUIRED
    task = context.workspace.load_graph().get("TASK-F1")
    assert task.attempts == 3  # max_task_attempts
    assert task.status == TaskState.ARCHITECTURE_REVIEW
    # failure memory recorded the root cause for future agents
    failures = context.workspace.load_failures()
    assert failures, "failure memory must be written"
    escalations = context.workspace.pending_escalations()
    assert escalations and escalations[0]["kind"] == "repeated_failure"
    context.db.close()


async def test_repair_loop_can_recover_after_diagnosis(project, echo):
    """A task failing once, then passing, completes via the repair path."""
    context = open_context(project)
    marker = context.repo_root / "flaky-marker.txt"
    # forward slashes keep shlex quoting portable across platforms
    check = (
        f"import pathlib,sys; sys.exit(0 if pathlib.Path('{marker.as_posix()}').exists() else 1)"
    )
    _seed_task(
        context,
        id="TASK-R1",
        title="flaky but recoverable",
        verification_commands=[f'python -c "{check}"'],
    )
    # the diagnosis applies a repair (as the repair loop would) and the
    # second attempt's verification then passes for real.
    original_diagnose = Orchestrator._diagnose

    async def repairing_diagnose(self, task):
        marker.write_text("repaired", encoding="utf-8")
        return {"root_cause": "marker missing", "files_affected": [], "tests_required": []}

    Orchestrator._diagnose = repairing_diagnose  # type: ignore[method-assign]
    try:
        orchestrator = _orchestrator(context)
        result = await orchestrator.run_loop()
    finally:
        Orchestrator._diagnose = original_diagnose  # type: ignore[method-assign]

    assert result.status == "completed"
    task = context.workspace.load_graph().get("TASK-R1")
    assert task.status == TaskState.COMPLETED
    assert task.attempts == 2
    context.db.close()


# ---- stop conditions -------------------------------------------------------


async def test_budget_exhaustion_stops_with_reason(project):
    context = open_context(project)
    context.config.budget.max_token_budget = 0.000001
    orchestrator = _orchestrator(context)
    result = await orchestrator.run_loop("Build something")
    # either budget stops the run immediately or the first model call tips it;
    # both must produce a *reasoned* stop, never a claim of completion.
    assert result.stop is not None
    assert result.status != "completed"
    assert result.stop.reason in (StopReason.BUDGET_EXCEEDED, StopReason.NO_PROGRESS)
    context.db.close()


async def test_operator_pause_stops_run(project):
    context = open_context(project)
    _seed_task(context, id="TASK-P1", title="pausable")
    control = ControlChannel(context.workspace.paths.execution)
    control.request(pause=True, reason="operator test")
    orchestrator = _orchestrator(context)
    result = await orchestrator.run_loop()
    assert result.stop.reason == StopReason.PAUSED
    assert not control.path.exists()  # consumed exactly once
    context.db.close()


async def test_operator_stop_request(project):
    context = open_context(project)
    _seed_task(context, id="TASK-S1", title="stoppable")
    ControlChannel(context.workspace.paths.execution).request(stop=True, reason="testing stop")
    orchestrator = _orchestrator(context)
    result = await orchestrator.run_loop()
    assert result.stop.reason == StopReason.USER_REQUESTED
    context.db.close()


# ---- supervised mode (§16) --------------------------------------------------


async def test_supervised_mode_gates_high_risk_tasks(project):
    context = open_context(project)
    context.config.run_mode = "supervised"
    _seed_task(context, id="TASK-H1", title="risky operation", risk="high")
    orchestrator = _orchestrator(context)
    result = await orchestrator.run_loop()

    assert result.stop.reason == StopReason.HUMAN_APPROVAL_REQUIRED
    task = context.workspace.load_graph().get("TASK-H1")
    assert task.status in (TaskState.QUEUED, TaskState.READY)  # never executed
    escalation = context.workspace.pending_escalations()[0]
    assert escalation["kind"] == "approval_gate"
    assert escalation["task_id"] == "TASK-H1"
    context.db.close()


async def test_supervised_approval_allows_execution(project):
    context = open_context(project)
    context.config.run_mode = "supervised"
    _seed_task(
        context,
        id="TASK-H2",
        title="approved risky operation",
        risk="high",
        definition_of_done=["file exists: README.md"],
    )
    orchestrator = _orchestrator(context)
    result = await orchestrator.run_loop()
    assert result.stop.reason == StopReason.HUMAN_APPROVAL_REQUIRED

    # the operator approves via the control channel, then re-runs
    pending = context.workspace.pending_escalations()
    assert len(pending) == 1
    context.workspace.resolve_escalation(pending[0]["id"], "approved", "ok")
    ControlChannel(context.workspace.paths.execution).request(approvals=[pending[0]["id"]])

    second = _orchestrator(context)
    result2 = await second.run_loop()
    assert result2.status == "completed", result2.summary
    task = context.workspace.load_graph().get("TASK-H2")
    assert task.status == TaskState.COMPLETED
    context.db.close()


# ---- recovery --------------------------------------------------------------


async def test_stale_active_task_is_recovered_on_restart(project):
    """A crashed run leaves a task mid-flight; the next run must recover it."""
    context = open_context(project)
    graph = context.workspace.load_graph()
    task = Task(id="TASK-X1", title="interrupted work")
    graph.add_task(task)
    task.set_state(TaskState.READY)
    task.set_state(TaskState.ASSIGNED)
    task.set_state(TaskState.IMPLEMENTING)
    context.workspace.save_graph(graph)

    orchestrator = _orchestrator(context)
    result = await orchestrator.run_loop()
    assert result.status == "completed"
    events = [e["event"] for e in orchestrator.workspace.events.read_all()]
    assert "run.recovered" in events
    graph = context.workspace.load_graph()
    assert graph.get("TASK-X1").status == TaskState.COMPLETED
    context.db.close()


async def test_run_resumes_across_process_restart(project):
    """Two orchestrator instances on one workspace: work continues, not restarts."""
    context = open_context(project)
    first = _orchestrator(context, max_cycles=2)
    partial = await first.run_loop("Build a note-taking REST API")
    # 2 cycles cannot finish 3+ tasks; the run stopped without completing
    assert partial.completed >= 1
    assert partial.status != "completed"

    # a brand-new orchestrator (simulating a restarted process) resumes
    context2 = open_context(project)
    second = _orchestrator(context2)
    final = await second.run_loop()
    assert final.status == "completed", final.summary
    assert final.completed > partial.completed
    context.db.close()
    context2.db.close()


# ---- QA gate ----------------------------------------------------------------


async def test_qa_gate_reopens_graph_for_unmet_requirements(project, echo):
    """When QA finds an unmet feature, a catch-up task is created and executed."""
    context = open_context(project)
    echo.canned["QACheck"] = {
        "aligned": False,
        "gaps": ["billing invoices"],
        "drift": [],
        "summary": "billing was required but never delivered",
        "confidence": 0.8,
    }
    orchestrator = _orchestrator(context)
    result = await orchestrator.run_loop("Build a thing with billing invoices")

    assert result.status == "completed"
    graph = context.workspace.load_graph()
    gap_tasks = [t for t in graph.all() if t.id.startswith("TASK-GAP")]
    assert gap_tasks, "the QA gate must create catch-up tasks"
    assert all(t.status == TaskState.COMPLETED for t in gap_tasks)
    context.db.close()


# ---- dynamic task creation (§54) --------------------------------------------


async def test_director_can_create_tasks_mid_run(project, echo):
    from autonomous_engine.agents.director import DirectorProposal

    context = open_context(project)
    orchestrator = _orchestrator(context)
    proposal = DirectorProposal(
        action="create_tasks",
        rationale="a hidden requirement was discovered",
        new_tasks=[
            {"id": "TASK-DYN-1", "title": "Handle discovered requirement"},
        ],
    )
    handled = await orchestrator._apply_management_actions(proposal)
    assert handled is False  # management cycles run no task
    graph = context.workspace.load_graph()
    assert "TASK-DYN-1" in graph.tasks
    context.db.close()


# ---- worktree parallelism ---------------------------------------------------


async def test_parallel_worktree_execution(project):
    context = open_context(project)
    context.config.worktree_parallelism = True
    graph = context.workspace.load_graph()
    graph.add_task(
        Task(
            id="TASK-PA",
            title="parallel a",
            definition_of_done=["file exists: README.md"],
        )
    )
    graph.add_task(
        Task(
            id="TASK-PB",
            title="parallel b",
            definition_of_done=["file exists: README.md"],
        )
    )
    context.workspace.save_graph(graph)

    orchestrator = _orchestrator(context)
    result = await orchestrator.run_loop("parallel work")
    assert result.status == "completed", result.summary
    graph = context.workspace.load_graph()
    assert graph.get("TASK-PA").status == TaskState.COMPLETED
    assert graph.get("TASK-PB").status == TaskState.COMPLETED
    context.db.close()


# ---- locks ------------------------------------------------------------------


async def test_locks_prevent_conflicting_simultaneous_writes(project):
    from autonomous_engine.runtime.locks import LockManager, resource_keys

    locks = LockManager()
    ok, blocking = locks.acquire("TASK-1", resource_keys(["src/models.py"]))
    assert ok
    ok, blocking = locks.acquire("TASK-2", resource_keys(["src/models.py"]))
    assert not ok and blocking
    ok, _ = locks.acquire("TASK-1", resource_keys(["src/models.py"]))
    assert ok  # re-entrant for the owner
    locks.release("TASK-1")
    ok, _ = locks.acquire("TASK-2", resource_keys(["src/models.py"]))
    assert ok
