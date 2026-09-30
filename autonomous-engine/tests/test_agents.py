"""Agent-level behaviour: deterministic fallbacks and validation."""

from __future__ import annotations

from pathlib import Path

from autonomous_engine.agents.coder import CoderAgent
from autonomous_engine.agents.director import DirectorAgent, DirectorProposal
from autonomous_engine.agents.intent_compiler import ProjectIntent, literal_intent
from autonomous_engine.agents.planner import PlannerAgent, fallback_plan
from autonomous_engine.agents.qa import QAAgent, gap_task
from autonomous_engine.agents.release import ReleaseAgent
from autonomous_engine.agents.shared import make_tasks_from_spec
from autonomous_engine.agents.tester import TesterAgent
from autonomous_engine.core.task import Task, TaskGraph
from autonomous_engine.runtime.base import AgentDeps
from autonomous_engine.runtime.permissions import ToolBox


def _deps(context) -> AgentDeps:
    return AgentDeps(
        router=context.router if hasattr(context, "router") else None,
        tools=ToolBox(
            work_root=context.repo_root, permissions=context.config.permission_for("coder")
        ),
        workspace=context.workspace,
        store=context.store,
        git=None,
    )


# ---- coder fallback --------------------------------------------------------


def test_coder_fallback_satisfies_dod(context, tmp_path: Path):
    """The deterministic fallback writes exactly what the DoD demands."""
    coder = CoderAgent(_deps(context))
    task = Task(
        id="TASK-001",
        title="Scaffold",
        epic="setup",
        artifacts=["README.md"],
        definition_of_done=[
            "file exists: README.md",
            "file contains: README.md: Verify",
            "python -m compileall -q .",
        ],
        verification_commands=["python -m compileall -q ."],
    )
    from autonomous_engine.runtime.context import AgentContext

    ctx = AgentContext(goal="test goal")
    plan = coder._fallback_plan(task, ctx)
    paths = {edit.path for edit in plan.edits}
    assert "README.md" in paths
    assert any(p.startswith("src/") and p.endswith(".py") for p in paths)
    applied, rejected, errors = coder._apply(plan.edits)
    assert not rejected and not errors
    readme = (context.repo_root / "README.md").read_text(encoding="utf-8")
    assert "Verify" in readme
    assert (context.repo_root / "README.md").exists()


def test_coder_fallback_generates_pytest_file_when_needed(context):
    coder = CoderAgent(_deps(context))
    task = Task(
        id="TASK-009",
        title="Add feature",
        definition_of_done=["pytest -q"],
        verification_commands=["pytest -q"],
    )
    from autonomous_engine.runtime.context import AgentContext

    plan = coder._fallback_plan(task, AgentContext(goal="g"))
    assert any(p.startswith("tests/test_") for p in (e.path for e in plan.edits))


# ---- planner ---------------------------------------------------------------


def test_planner_fallback_plan_shape():
    tasks = fallback_plan("Build a widget", ["frobnicate the widget"])
    assert tasks[0].id == "TASK-001"
    assert any("frobnicate" in t.title for t in tasks)
    final = tasks[-1]
    assert final.dependencies  # final task depends on the earlier work
    assert set(final.dependencies).issubset({t.id for t in tasks[:-1]})


def test_planner_build_tasks_breaks_cycles():
    planner = PlannerAgent(AgentDeps(router=None, tools=None, workspace=None, store=None, git=None))
    payload = {
        "tasks": [
            {"id": "A", "title": "a", "dependencies": ["B"]},
            {"id": "B", "title": "b", "dependencies": ["A"]},
        ]
    }
    tasks = planner.build_tasks(payload)
    graph = TaskGraph()
    for t in tasks:
        graph.add_task(t)
    planner._break_cycles(graph, graph.detect_cycles())
    assert graph.detect_cycles() == []


def test_make_tasks_from_spec_normalises_and_links():
    spec = {
        "tasks": [
            {"id": "weird id!", "title": "First", "priority": 99, "risk": "extreme"},
            {"id": "T2", "title": "Second", "depends_on": ["weird id!"]},
        ]
    }
    tasks = make_tasks_from_spec(spec)
    assert len(tasks) == 2
    first = tasks[0]
    assert "!" not in first.id
    assert first.priority == 10  # clamped
    assert first.risk == "medium"  # normalised
    assert tasks[1].dependencies == [first.id]


def test_planner_replan_operations():
    planner = PlannerAgent(AgentDeps(router=None, tools=None, workspace=None, store=None, git=None))
    graph = TaskGraph()
    graph.add_task(Task(id="A", title="a"))
    graph.add_task(Task(id="B", title="b"))
    summary = planner.replan(
        graph,
        add_tasks=[Task(id="C", title="c")],
        cancel_task_ids=["B"],
        reprioritise={"A": 2},
        relink={"C": ["A"]},
        reason="test",
    )
    assert summary["added"] == ["C"]
    assert summary["cancelled"] == ["B"]
    assert graph.get("A").priority == 2
    assert graph.get("C").dependencies == ["A"]
    assert "B" not in graph.tasks


def test_planner_spawn_subtask():
    parent = Task(id="TASK-017", title="parent")
    child = PlannerAgent(
        AgentDeps(router=None, tools=None, workspace=None, store=None, git=None)
    ).spawn_subtask(parent, "child work")
    assert child.id.startswith("TASK-017.")
    assert child.created_by == "TASK-017"
    assert parent.id in child.dependencies


# ---- director --------------------------------------------------------------


def test_director_deterministic_proposal_priority():
    director = DirectorAgent(
        AgentDeps(router=None, tools=None, workspace=None, store=None, git=None)
    )
    graph = TaskGraph()
    graph.add_task(Task(id="A", title="a", priority=1))
    graph.add_task(Task(id="B", title="b", priority=5, dependencies=["A"]))
    proposal = director.deterministic_proposal(graph, "test")
    assert proposal.action == "select_task"
    assert proposal.task_ids == ["A"]


def test_director_proposal_validation():
    graph = TaskGraph()
    graph.add_task(Task(id="A", title="a"))
    ok = DirectorProposal(action="select_task", task_ids=["A"])
    valid, reason = ok.validate_against(graph)
    assert valid and not reason

    bad = DirectorProposal(action="select_task", task_ids=["GHOST"])
    valid, reason = bad.validate_against(graph)
    assert not valid and "unknown task" in reason

    stop_without_reason = DirectorProposal(action="stop")
    valid, reason = stop_without_reason.validate_against(graph)
    assert not valid


# ---- QA / release deterministic paths -------------------------------------


def test_qa_deterministic_check_detects_gaps():
    qa = QAAgent(AgentDeps(router=None, tools=None, workspace=None, store=None, git=None))
    intent = literal_intent("Build a thing")
    intent.features = ["authentication with login", "billing invoices"]
    graph = TaskGraph()
    graph.add_task(Task(id="T1", title="Implement login authentication", status=_completed()))
    check = qa.deterministic_check(intent, graph)
    assert not check.aligned
    assert any("billing" in gap for gap in check.gaps)


def _completed():
    from autonomous_engine.core.state_machine import TaskState

    return TaskState.COMPLETED


def test_gap_task_creates_catchup_work():
    graph = TaskGraph()
    task = gap_task("billing invoices", graph)
    assert task.id == "TASK-GAP-001"
    assert "billing" in task.title
    graph.add_task(task)
    assert gap_task("another", graph).id == "TASK-GAP-002"


def test_release_deterministic_report_blocks_on_failures():
    release = ReleaseAgent(AgentDeps(router=None, tools=None, workspace=None, store=None, git=None))
    graph = TaskGraph()
    graph.add_task(Task(id="T1", title="t", status=_completed(), verification={"passed": True}))
    report = release.deterministic_report(graph, "objective")
    assert report.ready
    assert "objective" in report.notes

    graph.add_task(
        Task(id="T2", title="broken", status=_completed(), verification={"passed": False})
    )
    report = release.deterministic_report(graph, "objective")
    assert not report.ready
    assert any("verification" in b for b in report.blocking)


# ---- tester ----------------------------------------------------------------


def test_tester_commands_from_report():
    tester = TesterAgent(AgentDeps(router=None, tools=None, workspace=None, store=None, git=None))
    report = {
        "commands_run": [
            {"command": "pytest -q", "returncode": 0},
            {"command": "", "returncode": 1},
        ]
    }
    assert tester.commands_from_report(report) == ["pytest -q"]


# ---- intent ----------------------------------------------------------------


def test_literal_intent_is_explicit():
    intent = literal_intent("Build a REST API for notes")
    assert not intent.enhanced
    assert intent.objective == "Build a REST API for notes"
    assert any("README.md" in d for d in intent.definition_of_done)


def test_project_intent_markdown_labels_assumptions():
    from autonomous_engine.agents.intent_compiler import Assumption

    intent = ProjectIntent(
        objective="obj",
        assumptions=[Assumption(statement="uses postgres", confidence=0.4)],
        features=["f1"],
    )
    markdown = intent.to_markdown()
    assert "Assumptions" in markdown
    assert "0.40" in markdown
    assert "- f1" in markdown
