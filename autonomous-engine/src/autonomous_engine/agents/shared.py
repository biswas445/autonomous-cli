"""Shared agent helpers: deterministic fallbacks and validation.

Every model-driven agent pairs a probabilistic path (ask the model) with a
deterministic path (validate, fall back, or fail with evidence). That keeps
the runtime usable — and testable — without a provider, and keeps model
output from ever becoming unvalidated state.
"""

from __future__ import annotations

import re
from typing import Any

from ..core.task import Task, new_id
from ..models.base import ModelError
from ..runtime.base import Agent, AgentResult


def as_list(value: Any, *, limit: int = 50) -> list[str]:
    """Coerce messy model output into a clean list of non-empty strings."""
    if value is None:
        return []
    if isinstance(value, str):
        items = [line.strip(" -*\t") for line in value.splitlines()]
    elif isinstance(value, (list, tuple, set)):
        items = [str(v).strip() for v in value]
    else:
        items = [str(value).strip()]
    return [item for item in items if item][:limit]


def as_text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "\n".join(f"- {v}" for v in value)
    return str(value)


def clamp(value: Any, low: int, high: int, default: int) -> int:
    try:
        num = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, num))


def confidence(value: Any, default: float = 0.5) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, num))


_IDENT = re.compile(r"[^A-Za-z0-9._/-]+")


def safe_id(value: str, prefix: str = "TASK") -> str:
    """Normalize a model-supplied identifier to a filesystem/DB-safe id."""
    cleaned = _IDENT.sub("-", str(value).strip()).strip("-")
    cleaned = cleaned[:64]
    if not cleaned:
        cleaned = new_id(prefix)
    return cleaned


def pick_verification_command(acceptance: list[str], task_title: str) -> list[str]:
    """Infer verification commands from acceptance criteria when the planner
    did not specify any. Conservative: only obvious test/lint invocations."""
    commands: list[str] = []
    for criterion in acceptance:
        text = str(criterion).lower()
        for marker, command in (
            ("pytest", "pytest -q"),
            ("unit test", "pytest -q"),
            ("test passes", "pytest -q"),
            ("typecheck", "python -m compileall -q ."),
            ("lint", "python -m compileall -q ."),
        ):
            if marker in text and command not in commands:
                commands.append(command)
    return commands


def model_failure(agent: Agent, exc: Exception, *, what: str) -> AgentResult:
    """Uniform, honest result when a model call fails."""
    retriable = isinstance(exc, ModelError) and exc.retriable
    return AgentResult.failure(
        f"{agent.name} could not {what}: {exc}",
        agent=agent.name,
        metadata={"retriable": retriable, "stage": what},
    )


def make_tasks_from_spec(spec: dict[str, Any], *, epic: str = "") -> list[Task]:
    """Build tasks from a planner specification, validating every field.

    Rejects cycles later in the graph engine; here we only normalise input.
    """
    tasks: list[Task] = []
    id_map: dict[str, str] = {}

    for index, item in enumerate(spec.get("tasks", []), start=1):
        if not isinstance(item, dict):
            continue
        raw_id = str(item.get("id") or f"TASK-{index:03d}")
        task_id = safe_id(raw_id)
        while task_id in id_map.values():
            task_id = f"{task_id}-{index}"
        id_map[raw_id] = task_id
        id_map[task_id] = task_id

        title = as_text(item.get("title") or item.get("name"), "").strip()
        if not title:
            continue

        acceptance = as_list(item.get("acceptance_criteria") or item.get("acceptance"))
        dod = as_list(item.get("definition_of_done") or item.get("dod"))
        verify = as_list(item.get("verification_commands") or item.get("verification"))
        if not verify:
            verify = pick_verification_command(acceptance + dod, title)

        risk = str(item.get("risk", "medium")).lower()
        if risk not in ("low", "medium", "high"):
            risk = "medium"

        tasks.append(
            Task(
                id=task_id,
                epic=as_text(item.get("epic"), epic).strip(),
                title=title,
                description=as_text(item.get("description") or item.get("purpose")),
                role=str(item.get("role", "coding")),
                priority=clamp(item.get("priority"), 1, 10, 5),
                dependencies=[],
                acceptance_criteria=acceptance,
                definition_of_done=dod,
                verification_commands=verify,
                risk=risk,  # type: ignore[arg-type]
                estimated_complexity=clamp(
                    item.get("complexity") or item.get("estimated_complexity"), 1, 10, 5
                ),
                artifacts=as_list(item.get("artifacts") or item.get("files"), limit=20),
            )
        )

    # second pass: resolve dependency aliases
    for index, item in enumerate(spec.get("tasks", []), start=1):
        if not isinstance(item, dict):
            continue
        task_id = id_map.get(str(item.get("id") or f"TASK-{index:03d}"))
        if not task_id:
            continue
        task = next((t for t in tasks if t.id == task_id), None)
        if task is None:
            continue
        deps: list[str] = []
        for dep in as_list(item.get("dependencies") or item.get("depends_on")):
            resolved = id_map.get(dep)
            if resolved and resolved != task_id and resolved not in deps:
                deps.append(resolved)
        task.dependencies = deps
    return tasks
