"""Task and task-graph domain models (plan.md §5, §11, §66)."""

from __future__ import annotations

import re
import time
import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field

from .state_machine import IllegalTransition, TaskRole, TaskState, can_transition

Risk = Literal["low", "medium", "high"]

_STOPWORDS = {
    "implement",
    "add",
    "create",
    "build",
    "make",
    "write",
    "update",
    "support",
    "handling",
    "into",
    "from",
    "with",
    "that",
    "this",
    "the",
    "and",
    "for",
    "using",
    "should",
    "must",
    "task",
    "todo",
}

# Extensions grouped by what a task touching them likely cares about.
_EXT_HINTS: dict[str, tuple[str, ...]] = {
    "api": (".ts", ".js", ".py", ".go", ".java", ".rb"),
    "database": (".sql", ".prisma", ".db"),
    "frontend": (".tsx", ".jsx", ".css", ".html", ".vue", ".svelte"),
    "docs": (".md", ".rst", ".txt"),
    "test": ("test", "spec"),
}


def _tokens(text: str) -> set[str]:
    words = re.split(r"[^A-Za-z0-9]+", text.lower())
    return {w for w in words if len(w) > 3 and w not in _STOPWORDS}


def relevant_paths(task: Task, graph: TaskGraph | None = None, limit: int = 12) -> list[str]:
    """Heuristically rank repository files relevant to a task.

    Used only for context reconstruction; the planner can attach explicit
    paths via ``Task.artifacts`` and those always win.
    """
    scored: dict[str, int] = {}

    def add(path: str, score: int) -> None:
        if path:
            scored[path] = max(scored.get(path, 0), score)

    for artifact in task.artifacts:
        add(artifact, 100)

    keywords = _tokens(task.title) | _tokens(task.description)
    epic = _tokens(task.epic)
    keywords |= epic

    deps = ""
    if graph is not None:
        parts: list[str] = []
        for dep_id in task.dependencies:
            try:
                parts.append(f"{graph.get(dep_id).title} {graph.get(dep_id).description}")
            except KeyError:
                continue
        deps = " ".join(parts)
    dep_tokens = _tokens(deps)
    keywords |= dep_tokens

    # Explicit hints from the planner, e.g. verification commands hint at the
    # test/tooling files involved.
    for cmd in task.verification_commands:
        keywords |= _tokens(cmd)

    for path, base_score in _enumerate_candidate_files(task):
        lowered = path.lower()
        path_tokens = _tokens(path_name(path))
        overlap = len(keywords & path_tokens)
        score = base_score + overlap * 5
        if any(hint in lowered for hint in _EXT_HINTS["test"]) and "test" in keywords:
            score += 3
        for domain, extensions in _EXT_HINTS.items():
            if domain not in keywords and domain not in task.title.lower():
                continue
            if lowered.endswith(extensions):
                score += 4
        if overlap or base_score:
            add(path, score)

    ranked = sorted(scored.items(), key=lambda kv: (-kv[1], kv[0]))
    return [path for path, _ in ranked[:limit]]


def path_name(path: str) -> str:
    return path.replace("\\", "/").rsplit("/", 1)[-1]


def _enumerate_candidate_files(task: Task) -> list[tuple[str, int]]:
    """List files named in the task or likely to exist for it, with base scores.

    The task itself does not know the repository layout, so this yields
    spec-declared paths plus conventional source roots that ContextBuilder
    filters against the real filesystem.
    """
    candidates: list[tuple[str, int]] = [(p, 20) for p in task.artifacts]
    conventional = (
        "src",
        "app",
        "lib",
        "tests",
        "docs",
        "README.md",
        "pyproject.toml",
        "package.json",
    )
    for name in conventional:
        candidates.append((name, 1))
    return candidates


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class TaskHistoryEntry(BaseModel):
    timestamp: str
    from_state: str
    to_state: str
    agent: str = ""
    note: str = ""


class AttemptRecord(BaseModel):
    """One execution attempt against this task (failure memory, plan.md §29)."""

    attempt_number: int
    agent: str
    started_at: str
    finished_at: str | None = None
    outcome: Literal["success", "failed", "aborted"] = "failed"
    failure_summary: str = ""
    root_cause: str = ""
    evidence: dict[str, Any] = Field(default_factory=dict)
    commit: str = ""
    lesson: str = ""  # "Do not retry unless assumptions change."


class Task(BaseModel):
    """A unit of work in the task graph.

    Every task carries purpose, dependencies, acceptance criteria,
    verification commands, risk, priority, complexity, and rollback strategy
    (plan.md §5).
    """

    id: str = Field(default_factory=lambda: new_id("TASK"))
    epic: str = ""
    title: str
    description: str = ""
    role: TaskRole = TaskRole.CODE

    status: TaskState = TaskState.QUEUED
    priority: int = 5  # 1 (highest) .. 10 (lowest)
    dependencies: list[str] = Field(default_factory=list)

    # Definition of Done engine (plan.md §13): machine-checkable criteria.
    acceptance_criteria: list[str] = Field(default_factory=list)
    definition_of_done: list[str] = Field(default_factory=list)
    verification_commands: list[str] = Field(default_factory=list)

    risk: Risk = "medium"
    estimated_complexity: int = 5  # 1..10
    assigned_agent: str = ""

    # Mutable execution bookkeeping
    attempts: int = 0
    attempts_history: list[AttemptRecord] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)
    verification: dict[str, Any] = Field(default_factory=dict)
    worktree: str = ""  # non-empty => task runs in its own git worktree
    locked_paths: list[str] = Field(default_factory=list)
    history: list[TaskHistoryEntry] = Field(default_factory=list)
    created_at: str = Field(default_factory=now_iso)
    updated_at: str = Field(default_factory=now_iso)
    completed_at: str | None = None
    created_by: str = ""  # e.g. "TASK-17" for dynamically spawned subtasks

    # ---- state machine (deterministic; agents never mutate status directly) ----

    def set_state(self, target: TaskState | str, agent: str = "", note: str = "") -> TaskState:
        target = TaskState(target)
        if target == self.status:
            return target
        if not can_transition(self.status, target):
            raise IllegalTransition(self.status, target)
        entry = TaskHistoryEntry(
            timestamp=now_iso(),
            from_state=self.status.value,
            to_state=target.value,
            agent=agent,
            note=note,
        )
        self.status = target
        self.updated_at = now_iso()
        self.history.append(entry)
        if target == TaskState.COMPLETED:
            self.completed_at = now_iso()
        return target

    def is_terminal(self) -> bool:
        return self.status in (TaskState.COMPLETED, TaskState.CANCELLED)

    def is_active(self) -> bool:
        return self.status in (
            TaskState.ASSIGNED,
            TaskState.IMPLEMENTING,
            TaskState.VERIFYING,
            TaskState.REVIEWING,
            TaskState.DIAGNOSING,
            TaskState.REPAIRING,
            TaskState.ARCHITECTURE_REVIEW,
        )


class TaskGraph(BaseModel):
    """The dependency graph of tasks; supports dynamic task creation (§54)."""

    version: int = 1
    tasks: dict[str, Task] = Field(default_factory=dict)
    updated_at: str = Field(default_factory=now_iso)

    # ---- mutation ----

    def add_task(self, task: Task) -> Task:
        if task.id in self.tasks:
            raise ValueError(f"duplicate task id: {task.id}")
        self.tasks[task.id] = task
        self.updated_at = now_iso()
        return task

    def remove_task(self, task_id: str) -> None:
        self.tasks.pop(task_id, None)
        for task in self.tasks.values():
            task.dependencies = [d for d in task.dependencies if d != task_id]
        self.updated_at = now_iso()

    def cascade_cancel(
        self, task_id: str, *, agent: str = "cascade", note: str = ""
    ) -> list[str]:
        """Cancel a task and every non-terminal task that transitively
        depends on it.

        Cancelled work can never complete, so its dependents would stay
        BLOCKED forever and the project could never report completion.
        Returns every cancelled task id, starting with `task_id` itself.
        """
        if task_id not in self.tasks:
            return []
        cancelled: list[str] = [task_id]
        self.tasks[task_id].set_state(TaskState.CANCELLED, agent=agent, note=note)
        frontier = [task_id]
        while frontier:
            current = frontier.pop()
            for task in self.tasks.values():
                if task.id in cancelled or task.is_terminal():
                    continue
                if current in task.dependencies:
                    task.set_state(
                        TaskState.CANCELLED,
                        agent=agent,
                        note=note or f"dependency {current} was cancelled",
                    )
                    cancelled.append(task.id)
                    frontier.append(task.id)
        self.updated_at = now_iso()
        return cancelled

    # ---- queries ----

    def get(self, task_id: str) -> Task:
        if task_id not in self.tasks:
            raise KeyError(f"unknown task: {task_id}")
        return self.tasks[task_id]

    def all(self) -> list[Task]:
        return list(self.tasks.values())

    def ready_tasks(self) -> list[Task]:
        """Tasks whose dependencies are all COMPLETED and that are schedulable."""
        ready: list[Task] = []
        for task in self.tasks.values():
            if task.status != TaskState.QUEUED and task.status != TaskState.READY:
                continue
            deps_ok = all(
                self.tasks[d].status == TaskState.COMPLETED
                for d in task.dependencies
                if d in self.tasks
            )
            missing = [d for d in task.dependencies if d not in self.tasks]
            if deps_ok and not missing:
                ready.append(task)
        ready.sort(key=lambda t: (t.priority, t.created_at))
        return ready

    def blocked_tasks(self) -> list[Task]:
        blocked: list[Task] = []
        for task in self.tasks.values():
            if task.is_terminal() or task.is_active():
                continue
            unsatisfied = [
                d
                for d in task.dependencies
                if d not in self.tasks or self.tasks[d].status != TaskState.COMPLETED
            ]
            if unsatisfied:
                blocked.append(task)
        blocked.sort(key=lambda t: (t.priority, t.created_at))
        return blocked

    def active_tasks(self) -> list[Task]:
        return [t for t in self.tasks.values() if t.is_active()]

    def completed_tasks(self) -> list[Task]:
        return [t for t in self.tasks.values() if t.status == TaskState.COMPLETED]

    def failed_tasks(self) -> list[Task]:
        return [t for t in self.tasks.values() if t.status == TaskState.FAILED]

    def progress(self) -> dict[str, int]:
        total = len(self.tasks)
        completed = len(self.completed_tasks())
        cancelled = len([t for t in self.tasks.values() if t.status == TaskState.CANCELLED])
        return {
            "total": total,
            "completed": completed,
            "cancelled": cancelled,
            "active": len(self.active_tasks()),
            "failed": len(self.failed_tasks()),
            "pending": total - completed - cancelled - len(self.active_tasks()),
        }

    def detect_cycles(self) -> list[list[str]]:
        """Return dependency cycles (empty list = acyclic)."""
        white, gray, black = 0, 1, 2
        color = {tid: white for tid in self.tasks}
        cycles: list[list[str]] = []

        def visit(tid: str, stack: list[str]) -> None:
            color[tid] = gray
            stack.append(tid)
            for dep in self.tasks[tid].dependencies:
                if dep not in self.tasks:
                    continue
                if color[dep] == gray:
                    idx = stack.index(dep)
                    cycles.append(list(stack[idx:]))
                elif color[dep] == white:
                    visit(dep, stack)
            stack.pop()
            color[tid] = black

        for tid in self.tasks:
            if color[tid] == white:
                visit(tid, [])
        return cycles

    def topological_order(self) -> list[str]:
        """Kahn's algorithm; raises ValueError on cycles."""
        from collections import deque

        indegree: dict[str, int] = {tid: 0 for tid in self.tasks}
        for task in self.tasks.values():
            for dep in task.dependencies:
                if dep in indegree:
                    indegree[task.id] += 1
        queue = deque(sorted([tid for tid, d in indegree.items() if d == 0]))
        order: list[str] = []
        while queue:
            tid = queue.popleft()
            order.append(tid)
            for task in self.tasks.values():
                if tid in task.dependencies:
                    indegree[task.id] -= 1
                    if indegree[task.id] == 0:
                        queue.append(task.id)
        if len(order) != len(self.tasks):
            raise ValueError("task graph contains a dependency cycle")
        return order

    def independent_wave(self, max_tasks: int) -> list[str]:
        """The next wave of mutually independent, ready task ids (§22).

        Only *unfinished* dependencies are claimed as shared resources: two
        tasks that both depend on the same completed task can run together.
        """
        ready = self.ready_tasks()
        wave: list[str] = []
        claimed: set[str] = set()
        for task in ready:
            if len(wave) >= max_tasks:
                break
            pending_deps = {
                d
                for d in task.dependencies
                if d in self.tasks and self.tasks[d].status != TaskState.COMPLETED
            }
            if claimed.intersection(pending_deps):
                continue
            wave.append(task.id)
            claimed.update(pending_deps)
        return wave
