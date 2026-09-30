"""Tests for the plan.md capabilities added after the first hardening pass:

§40 metrics, §31 milestones + §30 milestone self-evaluation, §38
research-before-coding, §39 review board + §20 model disagreement, §45
daemon, §60 cross-project lessons, §61 docker sandbox, §41 benchmarks.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from autonomous_engine.benchmarks import BENCHMARK_CASES, run_benchmark
from autonomous_engine.core.config import PermissionClass
from autonomous_engine.core.state_machine import TaskState
from autonomous_engine.core.task import Task, TaskGraph
from autonomous_engine.core.workspace import Workspace
from autonomous_engine.models.base import (
    ModelError,
    ModelResponse,
    register_provider,
)
from autonomous_engine.models.router import ModelRouter
from autonomous_engine.runtime.context_setup import open_context
from autonomous_engine.runtime.daemon import DaemonLoop
from autonomous_engine.runtime.lessons import (
    lessons_context_section,
    load_lessons,
    record_lesson,
    record_project_lessons,
)
from autonomous_engine.runtime.metrics import collect_metrics
from autonomous_engine.runtime.permissions import ToolBox
from autonomous_engine.runtime.review_board import ModelDisagreement
from autonomous_engine.runtime.roadmap import build_roadmap, refresh_roadmap

# ---- §40 metrics -----------------------------------------------------------


async def test_metrics_reflect_a_real_run(project):
    context = open_context(project)
    await _make_run(context)
    metrics = collect_metrics(context.workspace, context.store)
    assert metrics["tasks_total"] >= 3
    assert metrics["tasks_completed"] >= 3
    assert metrics["tasks_failed"] == 0
    assert metrics["model_calls"] > 0
    assert metrics["commits"] >= 3
    assert metrics["checkpoints"] >= 3
    assert metrics["events_total"] > 10
    assert metrics["verification_final_pass_rate"] == 1.0
    assert metrics["human_interventions"] == 0
    context.db.close()


async def _make_run(context, objective="Build a metrics target"):
    orchestrator = Orchestrator(context, use_model_director=False)
    await orchestrator.run_loop(objective)
    return orchestrator


from autonomous_engine.runtime.orchestrator import Orchestrator  # noqa: E402

# ---- §31 milestones + §30 self-evaluation ----------------------------------


def test_roadmap_groups_epics_and_tracks_status():
    graph = TaskGraph()
    graph.add_task(Task(id="A1", title="auth schema", epic="Auth"))
    graph.add_task(Task(id="A2", title="auth login", epic="Auth"))
    graph.add_task(Task(id="B1", title="billing", epic="Billing"))
    roadmap = build_roadmap(graph)
    names = {m["name"]: m for m in roadmap["milestones"]}
    assert set(names) == {"Auth", "Billing"}
    assert names["Auth"]["tasks_total"] == 2

    graph.get("A1").set_state(TaskState.READY)
    graph.get("A1").set_state(TaskState.ASSIGNED)
    graph.get("A1").set_state(TaskState.IMPLEMENTING)
    graph.get("A1").set_state(TaskState.VERIFYING)
    graph.get("A1").set_state(TaskState.COMPLETED)
    refreshed = refresh_roadmap(_FakeWorkspace(), graph)
    assert refreshed["newly_completed"] == []  # Auth not fully done yet
    graph.get("A2").set_state(TaskState.READY)
    graph.get("A2").set_state(TaskState.ASSIGNED)
    graph.get("A2").set_state(TaskState.IMPLEMENTING)
    graph.get("A2").set_state(TaskState.VERIFYING)
    graph.get("A2").set_state(TaskState.COMPLETED)
    refreshed = refresh_roadmap(_FakeWorkspace(), graph)
    auth = [m for m in refreshed["roadmap"]["milestones"] if m["name"] == "Auth"][0]
    assert auth["status"] == "done"
    assert [m["name"] for m in refreshed["newly_completed"]] == ["Auth"]


class _FakeWorkspace:
    """Minimal workspace double for roadmap refresh tests."""

    def __init__(self):
        self.saved = []
        self._roadmap = {"milestones": []}

    def load_roadmap(self):
        return self._roadmap

    def save_roadmap(self, roadmap):
        self.saved.append(roadmap)
        self._roadmap = roadmap


async def test_milestone_completion_emits_event_and_self_evaluation(project):
    context = open_context(project)
    graph = context.workspace.load_graph()
    graph.add_task(Task(id="MS-A", title="milestone one", epic="EpicOne", priority=1))
    graph.add_task(Task(id="MS-B", title="milestone two", epic="EpicTwo", priority=2))
    context.workspace.save_graph(graph)
    orchestrator = Orchestrator(context, use_model_director=False)
    await orchestrator.run_loop()

    events = orchestrator.workspace.events.read_all()
    milestone_events = [e for e in events if e["event"] == "milestone.completed"]
    assert milestone_events, "milestone completions must be emitted"
    names = {e["name"] for e in milestone_events}
    assert "EpicOne" in names and "EpicTwo" in names
    # self-evaluation artifacts exist per milestone
    evaluations = list((project / ".agents" / "verification").glob("milestone_*.json"))
    assert evaluations
    # roadmap reflects completion
    roadmap = context.workspace.load_roadmap()
    assert all(m["status"] == "done" for m in roadmap["milestones"])
    context.db.close()


# ---- §38 research-before-coding --------------------------------------------


async def test_complex_task_gets_research_before_coding(project, echo):
    context = open_context(project)
    echo.canned["ResearchAnswer"] = {
        "question": "",
        "answer": "use an existing http framework; do not hand-roll routing",
        "resolved": True,
        "sources": ["repo README"],
        "confidence": 0.7,
    }
    graph = context.workspace.load_graph()
    complex_task = Task(
        id="TASK-RX",
        title="Design the storage layer",
        estimated_complexity=8,
        definition_of_done=["file exists: README.md"],
        verification_commands=["python -m compileall -q ."],
    )
    simple_task = Task(
        id="TASK-RY",
        title="Trivial change",
        estimated_complexity=2,
        definition_of_done=["file exists: README.md"],
    )
    graph.add_task(complex_task)
    graph.add_task(simple_task)
    context.workspace.save_graph(graph)

    orchestrator = Orchestrator(context, use_model_director=False)
    result = await orchestrator.run_loop()
    assert result.status == "completed"
    research = project / ".agents" / "research" / "TASK-RX.md"
    assert research.is_file(), "complex task must get a research artifact"
    assert "storage layer" in research.read_text(encoding="utf-8")
    # simple task: no research artifact
    assert not (project / ".agents" / "research" / "TASK-RY.md").exists()
    events = [
        e for e in orchestrator.workspace.events.read_all() if e["event"] == "research.completed"
    ]
    assert events and events[0]["task_id"] == "TASK-RX"
    context.db.close()


# ---- §39 review board + §20 disagreement ------------------------------------


def test_model_disagreement_agreement_short_circuits():
    class SameProvider:
        name = "same"

        async def complete(self, request, model):
            return ModelResponse(
                text='{"decision": "keep_current"}', provider=self.name, model=model
            )

    register_provider(SameProvider(), replace=True)
    router = ModelRouter.__new__(ModelRouter)
    router.config = _routes("same")
    router.max_retries = 0
    router.retry_backoff_seconds = 0.0
    router.stats = {}
    verdict, judge_used = asyncio.run(
        ModelDisagreement(router).decide(
            system="s",
            prompt="p",
            schema_hint="decision",
            key="decision",
            choices=["keep_current", "revise_plan"],
        )
    )
    assert verdict["decision"] == "keep_current"
    assert judge_used is False


def test_model_disagreement_judge_resolves_conflict():
    calls = {"n": 0}

    class SplitProvider:
        name = "split"

        async def complete(self, request, model):
            calls["n"] += 1
            if "judge" in request.system.lower():
                text = '{"decision": "revise_plan"}'
            elif calls["n"] % 2 == 1:
                text = '{"decision": "keep_current"}'
            else:
                text = '{"decision": "revise_plan"}'
            return ModelResponse(text=text, provider=self.name, model=model)

    register_provider(SplitProvider(), replace=True)
    router = ModelRouter.__new__(ModelRouter)
    router.config = _routes("split")
    router.max_retries = 0
    router.retry_backoff_seconds = 0.0
    router.stats = {}
    verdict, judge_used = asyncio.run(
        ModelDisagreement(router).decide(
            system="You are the board.",
            prompt="p",
            schema_hint="decision",
            key="decision",
            choices=["keep_current", "revise_plan"],
        )
    )
    assert judge_used is True
    assert verdict["decision"] == "revise_plan"


def _routes(provider: str):
    from autonomous_engine.core.config import ModelRoute, ProjectConfig

    config = ProjectConfig()
    config.model_routes = [ModelRoute(role="director", provider=provider, model="m")]
    return config


def test_disagreement_without_valid_proposals_raises():
    class JunkProvider:
        name = "junk"

        async def complete(self, request, model):
            return ModelResponse(text='{"echo": true}', provider=self.name, model=model)

    register_provider(JunkProvider(), replace=True)
    router = ModelRouter.__new__(ModelRouter)
    router.config = _routes("junk")
    router.max_retries = 0
    router.retry_backoff_seconds = 0.0
    router.stats = {}
    with pytest.raises(ModelError):
        asyncio.run(
            ModelDisagreement(router).decide(
                system="s",
                prompt="p",
                schema_hint="decision",
                key="decision",
                choices=["keep_current"],
            )
        )


async def test_review_board_records_decision_and_appends_to_decisions_md(project):
    context = open_context(project)
    orchestrator = Orchestrator(context, use_model_director=False)
    graph = context.workspace.load_graph()
    graph.add_task(Task(id="TASK-B1", title="doomed task"))
    context.workspace.save_graph(graph)

    decision = await orchestrator._convene_board("test review", ["TASK-B1"])
    assert decision is not None
    assert decision.verdict in ("keep_current", "revise_plan", "redesign_module")
    decisions = context.store.list_decisions()
    assert any(d.decided_by == "review-board" for d in decisions)
    decisions_md = (project / ".agents" / "architecture" / "decisions.md").read_text("utf-8")
    assert "Architecture Review Board" in decisions_md
    # the board convenes at most once per question
    again = await orchestrator._convene_board("test review", ["TASK-B1"])
    assert again is None
    context.db.close()


async def test_repeated_failure_convenes_board_before_escalation(project):
    context = open_context(project)
    graph = context.workspace.load_graph()
    graph.add_task(
        Task(
            id="TASK-BF",
            title="always fails",
            verification_commands=['python -c "import sys; sys.exit(1)"'],
        )
    )
    context.workspace.save_graph(graph)
    orchestrator = Orchestrator(context, use_model_director=False)
    result = await orchestrator.run_loop()

    assert result.status == "human_approval_required"
    events = [e["event"] for e in orchestrator.workspace.events.read_all()]
    assert "review_board.decided" in events
    assert any(d.decided_by == "review-board" for d in context.store.list_decisions())
    context.db.close()


# ---- §45 daemon -------------------------------------------------------------


def _daemon(context, *, poll=0.01, restarts=2):
    return DaemonLoop(context, poll_seconds=poll, max_restarts=restarts, max_cycles_per_run=50)


async def _approve_after(context, escalation_id, delay=0.05):
    await asyncio.sleep(delay)
    context.workspace.resolve_escalation(escalation_id, "approved", "test")


async def test_daemon_waits_for_approval_then_completes(project):
    context = open_context(project)
    # seed a pre-existing repeated-failure escalation so the first run stops
    graph = context.workspace.load_graph()
    task = Task(id="TASK-D1", title="gated work", status=TaskState.ARCHITECTURE_REVIEW)
    task.attempts = 3
    graph.add_task(task)
    context.workspace.save_graph(graph)
    context.workspace.add_escalation(
        {
            "id": "ESC-D1",
            "task_id": "TASK-D1",
            "kind": "repeated_failure",
            "reason": "attempts exhausted",
            "status": "pending",
            "created_at": "now",
        }
    )

    async def approver():
        await asyncio.sleep(0.05)
        context.workspace.resolve_escalation("ESC-D1", "approved", "test")

    daemon = _daemon(context)
    approval_task = asyncio.create_task(approver())
    report = await daemon.run()
    await approval_task
    assert report.status == "completed", report.as_dict()
    assert report.waits >= 1
    assert "TASK-D1" in report.requeued_tasks
    graph = context.workspace.load_graph()
    assert graph.get("TASK-D1").status == TaskState.COMPLETED
    context.db.close()


async def test_daemon_rejection_cancels_the_task(project):
    context = open_context(project)
    graph = context.workspace.load_graph()
    rejected = Task(id="TASK-D2", title="rejected work", status=TaskState.ARCHITECTURE_REVIEW)
    rejected.attempts = 3
    graph.add_task(rejected)
    graph.add_task(
        Task(
            id="TASK-D3",
            title="healthy work",
            definition_of_done=["file exists: README.md"],
        )
    )
    context.workspace.save_graph(graph)
    context.workspace.add_escalation(
        {
            "id": "ESC-D2",
            "task_id": "TASK-D2",
            "kind": "repeated_failure",
            "reason": "attempts exhausted",
            "status": "pending",
            "created_at": "now",
        }
    )

    async def rejector():
        await asyncio.sleep(0.05)
        context.workspace.resolve_escalation("ESC-D2", "rejected", "test")

    daemon = _daemon(context)
    reject_task = asyncio.create_task(rejector())
    report = await daemon.run()
    await reject_task
    assert report.status == "completed", report.as_dict()
    assert "TASK-D2" in report.cancelled_tasks
    graph = context.workspace.load_graph()
    assert graph.get("TASK-D2").status == TaskState.CANCELLED
    assert graph.get("TASK-D3").status == TaskState.COMPLETED
    context.db.close()


async def test_daemon_honors_pause_until_explicit_resume(project):
    context = open_context(project)
    graph = context.workspace.load_graph()
    graph.add_task(
        Task(id="TASK-D5", title="paused work", definition_of_done=["file exists: README.md"])
    )
    context.workspace.save_graph(graph)

    from autonomous_engine.runtime.control import ControlChannel

    control = ControlChannel(context.workspace.paths.execution)
    control.request(pause=True, reason="hold")

    async def resumer():
        await asyncio.sleep(0.1)
        control.request(resume=True)

    daemon = _daemon(context)
    resume_task = asyncio.create_task(resumer())
    report = await daemon.run()
    await resume_task
    assert report.status == "completed", report.as_dict()
    assert report.waits >= 1
    graph = context.workspace.load_graph()
    assert graph.get("TASK-D5").status == TaskState.COMPLETED
    context.db.close()


async def test_daemon_gives_up_after_bounded_restarts(project):
    context = open_context(project)
    graph = context.workspace.load_graph()
    ghost = Task(id="TASK-D4", title="blocked forever", dependencies=["GHOST"])
    graph.add_task(ghost)
    context.workspace.save_graph(graph)

    daemon = _daemon(context, poll=0.01, restarts=2)
    report = await daemon.run()
    assert report.status == "gave_up"
    assert report.restarts == 3  # initial + 2 restarts
    assert report.reason == "NO_PROGRESS"
    context.db.close()


async def test_daemon_writes_state_file(project):
    context = open_context(project)
    daemon = _daemon(context)
    await daemon.run()
    state = context.workspace.paths.execution / "daemon.json"
    assert state.is_file()
    payload = json.loads(state.read_text(encoding="utf-8"))
    assert payload["status"] in ("completed", "stopped", "gave_up")
    context.db.close()


# ---- §60 lessons ------------------------------------------------------------


def test_lessons_roundtrip_and_context_section(tmp_path, monkeypatch):
    lessons_path = tmp_path / "lessons.json"
    monkeypatch.setenv("AUTO_LESSONS_FILE", str(lessons_path))
    assert load_lessons() == []
    assert lessons_context_section() == ""

    assert record_lesson(
        "library X breaks under Node 24", source_project="p1", evidence="crash log"
    )
    assert record_lesson("prefer server-side sessions", source_project="p1", kind="decision")
    assert not record_lesson("Library X breaks under Node 24", source_project="p2")  # dedup

    lessons = load_lessons()
    assert len(lessons) == 2
    section = lessons_context_section()
    assert "LESSONS FROM OTHER PROJECTS" in section
    assert "Node 24" in section
    assert "[decision]" in section


def test_project_lessons_harvest_requires_cause_and_lesson(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTO_LESSONS_FILE", str(tmp_path / "lessons.json"))
    ws = Workspace.__new__(Workspace)  # avoid full init; we only need the loader
    failures = [
        {"lesson": "never store tokens in the repo", "root_cause": "secret scanner hit"},
        {"lesson": "", "root_cause": "no lesson recorded"},  # skipped
        {"lesson": "lesson without cause", "root_cause": ""},  # skipped
    ]
    ws.load_failures = lambda: failures
    recorded = record_project_lessons(ws, "proj")
    assert recorded == 1
    assert load_lessons()[0]["statement"] == "never store tokens in the repo"


# ---- §61 docker sandbox -----------------------------------------------------


def test_docker_sandbox_argv_construction(tmp_path, monkeypatch):
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["cwd"] = kwargs.get("cwd")

        class Proc:
            returncode = 0
            stdout = "ok"
            stderr = ""

        return Proc()

    monkeypatch.setattr("subprocess.run", fake_run)
    tools = ToolBox(
        work_root=tmp_path,
        permissions=PermissionClass(
            name="coder", read_repo=True, run_commands=True, allowed_command_globs=["python*"]
        ),
        sandbox_backend="docker",
        sandbox_image="python:3.12-slim",
    )
    result = tools.run_command("python -m compileall -q .")
    assert result.ok
    argv = captured["argv"]
    assert argv[0] == "docker"
    assert "run" in argv and "--rm" in argv
    assert f"{tmp_path}:/workspace" in argv
    assert "/workspace" in argv
    assert "--network" in argv and "none" in argv  # no network permission
    assert "python:3.12-slim" in argv
    assert argv[-5:] == ["python", "-m", "compileall", "-q", "."]
    assert captured["cwd"] is None  # container handles the workdir


def test_docker_sandbox_keeps_network_when_permitted(tmp_path, monkeypatch):
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv

        class Proc:
            returncode = 0
            stdout = ""
            stderr = ""

        return Proc()

    monkeypatch.setattr("subprocess.run", fake_run)
    tools = ToolBox(
        work_root=tmp_path,
        permissions=PermissionClass(
            name="researcher",
            read_repo=True,
            run_commands=True,
            allowed_command_globs=["curl*"],
            network=True,
        ),
        sandbox_backend="docker",
    )
    tools.run_command("curl https://example.com")
    assert "--network" not in captured["argv"]


def test_docker_missing_binary_reports_127(tmp_path):
    tools = ToolBox(
        work_root=tmp_path,
        permissions=PermissionClass(
            name="coder",
            read_repo=True,
            run_commands=True,
            allowed_command_globs=["definitely-not-*"],
        ),
        sandbox_backend="docker",
    )
    result = tools.run_command("definitely-not-real --flag")
    assert result.returncode == 127
    assert "docker" in result.stderr


# ---- §41 benchmarks ---------------------------------------------------------


def test_benchmark_harness_runs_and_reports(tmp_path):
    payload = run_benchmark(tmp_path / "results", cases=BENCHMARK_CASES[:2])
    assert payload["cases"] == 2
    assert payload["finish_rate"] == 1.0
    report = (tmp_path / "results" / "report.md").read_text(encoding="utf-8")
    assert "Benchmark Report" in report
    assert "rest-api" in report
    data = json.loads((tmp_path / "results" / "results.json").read_text(encoding="utf-8"))
    assert data["finished"] == 2
    first = data["results"][0]
    assert first["tasks_completed"] >= 3
    assert first["metrics"]["events_total"] > 0
