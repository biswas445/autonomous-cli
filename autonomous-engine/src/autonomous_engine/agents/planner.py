"""Planner agent (plan.md §5E, §54).

Breaks the architecture into a dependency graph of tasks. Every task gets a
purpose, dependencies, inputs, expected changes, acceptance criteria,
verification commands, risk, priority, complexity, and a rollback strategy.

The planner is also allowed to *revise* the graph: `replan()` creates, splits,
reprioritises, or re-links tasks when new evidence invalidates the plan.
"""

from __future__ import annotations

from typing import Any

from ..core.state_machine import TaskRole
from ..core.task import Task, TaskGraph, new_id, now_iso
from ..runtime.base import Agent, AgentResult
from ..runtime.context import AgentContext
from .shared import as_text, clamp, confidence, make_tasks_from_spec


class PlannerAgent(Agent):
    name = "planner"
    role = "planner"
    agent_class = "planner"
    description = "Decomposes the architecture into a dependency-ordered task graph."

    SYSTEM = (
        "You are the Planner of an autonomous engineering system. Break the project into "
        "an ordered set of concrete engineering tasks forming a dependency graph. "
        "Every task must have: title, description, dependencies, acceptance_criteria, "
        "definition_of_done, verification_commands, risk, priority, complexity, artifacts. "
        "Verification commands must be runnable and machine-checkable. "
        "Respond with a single JSON object, no prose."
    )
    SCHEMA_HINT = (
        "Plan JSON with keys: epics[] (each {name, tasks[]}), or tasks[] where each task is "
        "{id, title, description, epic, role, dependencies[], acceptance_criteria[], "
        "definition_of_done[], verification_commands[], risk, priority, complexity, artifacts[]}"
    )

    async def run(self, task, context: AgentContext) -> AgentResult:
        try:
            payload, usage = await self.ask_model(
                system=self.SYSTEM,
                prompt=context.render() or context.goal,
                schema_hint=self.SCHEMA_HINT,
                max_tokens=6000,
                complexity=6,
            )
        except Exception as exc:
            from .shared import model_failure

            return model_failure(self, exc, what="create a plan")

        tasks = self.build_tasks(payload)
        if not tasks:
            return AgentResult.failure("planner produced no usable tasks", agent=self.name)

        graph = TaskGraph()
        for item in tasks:
            graph.add_task(item)
        cycles = graph.detect_cycles()
        if cycles:
            # deterministic repair: drop the back-edges that create the cycle
            self._break_cycles(graph, cycles)

        self.record_activity("planned", f"{len(graph.tasks)} tasks")
        return AgentResult(
            ok=True,
            output={
                "tasks": [t.model_dump(mode="json") for t in graph.all()],
                "epics": sorted({t.epic for t in graph.all() if t.epic}),
            },
            confidence=confidence(payload.get("confidence"), 0.7),
            evidence={
                "tasks": len(graph.tasks),
                "dependency_edges": sum(len(t.dependencies) for t in graph.all()),
                "independent_ready": len(graph.ready_tasks()),
            },
            cost_usd=usage["cost_usd"],
            tokens_in=usage["tokens_in"],
            tokens_out=usage["tokens_out"],
        )

    # ---- deterministic helpers ----

    def build_tasks(self, payload: dict[str, Any]) -> list[Task]:
        """Normalise a plan payload into tasks, flattening epics if needed."""
        normalised: dict[str, Any] = {"tasks": []}
        for epic in payload.get("epics", []) or []:
            if not isinstance(epic, dict):
                continue
            for item in epic.get("tasks", []) or []:
                if isinstance(item, dict):
                    item = dict(item)
                    item.setdefault("epic", as_text(epic.get("name"), ""))
                    normalised["tasks"].append(item)
        for item in payload.get("tasks", []) or []:
            if isinstance(item, dict):
                normalised["tasks"].append(item)
        tasks = make_tasks_from_spec(normalised)
        # Guarantee a runnable global check on tasks that declared none.
        for task in tasks:
            if not task.definition_of_done and not task.acceptance_criteria:
                task.definition_of_done = [
                    "file exists: README.md",
                    "python -m compileall -q .",
                ]
        return tasks

    def _break_cycles(self, graph: TaskGraph, cycles: list[list[str]]) -> None:
        """Remove back-edges until the graph is acyclic (deterministic)."""
        for cycle in cycles:
            for task_id in cycle[1:]:
                task = graph.tasks.get(task_id)
                if task is None:
                    continue
                task.dependencies = [d for d in task.dependencies if d != cycle[0]]
                self.record_activity("broke dependency cycle", f"{task_id} -> {cycle[0]}")

        # If cycles persist (a model can emit tangled edges), linearise:
        # keep only dependencies that come earlier in a stable order.
        remaining = graph.detect_cycles()
        if remaining:
            order: list[str] = []
            seen: set[str] = set()

            def visit(tid: str) -> None:
                if tid in seen or tid not in graph.tasks:
                    return
                seen.add(tid)
                for dep in graph.tasks[tid].dependencies:
                    visit(dep)
                order.append(tid)

            for tid in sorted(graph.tasks):
                visit(tid)
            rank = {tid: i for i, tid in enumerate(order)}
            for task in graph.all():
                task.dependencies = [d for d in task.dependencies if rank.get(d, 0) < rank[task.id]]
            self.record_activity("linearised task graph", f"after {len(remaining)} cycle(s)")

    # ---- replanning ----

    def replan(
        self,
        graph: TaskGraph,
        *,
        add_tasks: list[Task] | None = None,
        cancel_task_ids: list[str] | None = None,
        reprioritise: dict[str, int] | None = None,
        relink: dict[str, list[str]] | None = None,
        reason: str = "",
    ) -> dict[str, Any]:
        """Deterministically mutate the task graph; returns a change summary."""
        summary: dict[str, Any] = {
            "reason": reason,
            "added": [],
            "cancelled": [],
            "reprioritised": [],
            "relinked": [],
        }

        for task in add_tasks or []:
            if task.id in graph.tasks:
                task.id = f"{task.id}.{new_id('r')[:4]}"
            graph.add_task(task)
            summary["added"].append(task.id)

        for task_id in cancel_task_ids or []:
            if task_id in graph.tasks and not graph.tasks[task_id].is_terminal():
                graph.remove_task(task_id)
                summary["cancelled"].append(task_id)

        for task_id, priority in (reprioritise or {}).items():
            if task_id in graph.tasks:
                graph.tasks[task_id].priority = clamp(priority, 1, 10, 5)
                summary["reprioritised"].append(task_id)

        for task_id, deps in (relink or {}).items():
            if task_id in graph.tasks:
                graph.tasks[task_id].dependencies = [
                    d for d in deps if d in graph.tasks and d != task_id
                ]
                summary["relinked"].append(task_id)

        if (
            summary["added"]
            or summary["cancelled"]
            or summary["reprioritised"]
            or summary["relinked"]
        ):
            graph.updated_at = now_iso()
            self.record_activity("replanned", str(summary))
        return summary

    def spawn_subtask(self, parent: Task, title: str, **kwargs: Any) -> Task:
        """Dynamic task creation: a task splits into new child tasks (§54)."""
        child = Task(
            id=f"{parent.id}.{new_id('s')[:4]}",
            epic=parent.epic,
            title=title,
            dependencies=[parent.id],
            created_by=parent.id,
            risk=parent.risk,
            **kwargs,
        )
        return child


def fallback_plan(objective: str, features: list[str]) -> list[Task]:
    """Deterministic plan used when no model is available.

    Produces the classic scaffold → implement → test → document shape so the
    loop still exercises real work instead of idling.
    """
    tasks: list[Task] = [
        Task(
            id="TASK-001",
            title="Scaffold project structure and toolchain",
            description=f"Create the repository layout and toolchain needed for: {objective}",
            role="coding",
            priority=1,
            definition_of_done=[
                "file exists: README.md",
                "python -m compileall -q .",
            ],
            verification_commands=["python -m compileall -q ."],
            risk="low",
            complexity=2,
            artifacts=["README.md"],
        )
    ]
    for index, feature in enumerate(features or ["core functionality"], start=2):
        tasks.append(
            Task(
                id=f"TASK-{index:03d}",
                title=f"Implement {feature}",
                description=f"Implement: {feature}",
                role=TaskRole.CODE,
                priority=min(2 + index, 9),
                dependencies=["TASK-001"],
                acceptance_criteria=[f"{feature} behaves as specified."],
                definition_of_done=["python -m compileall -q ."],
                verification_commands=["python -m compileall -q ."],
                risk="medium",
                complexity=5,
            )
        )
    final_index = len(tasks) + 1
    tasks.append(
        Task(
            id=f"TASK-{final_index:03d}",
            title="Write project documentation and runbook",
            description="Document architecture, how to run, and how to verify.",
            role=TaskRole.CODE,
            priority=8,
            dependencies=[t.id for t in tasks],
            acceptance_criteria=["README documents setup, usage and verification."],
            definition_of_done=[
                "file exists: README.md",
                "file contains: README.md: Verify",
            ],
            verification_commands=["python -m compileall -q ."],
            risk="low",
            complexity=2,
        )
    )
    return tasks
