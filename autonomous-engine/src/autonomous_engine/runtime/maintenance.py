"""Continuous Objective Mode (directive #1): maintenance work after completion.

When the Engineering Director's graph is done, the runtime must not go idle or
pong between NO_PROGRESS stops for months of unattended operation. This module
generates *real* maintenance work from *real* repository signals — never
fabricated busywork:

    dependency updates      pyproject dependencies worth reviewing
    flaky-test quarantine   verification history that flip-flopped (§16)
    TODO cleanup            TODO/FIXME/HACK markers in the source tree
    coverage gaps           modules missing from the coverage baseline
    regression checks       tasks whose evidence predates the current HEAD
    technical debt          debt items recorded in project memory

Everything is deterministic: the same repository state yields the same
backlog. Tasks are deduplicated against the live graph by (kind, subject) so
re-generating after every completion cannot flood the plan — each generator
fires at most once per subject until its task completes.
"""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.task import Task, TaskGraph, TaskRole
from ..core.workspace import Workspace
from ..verification.evidence import EvidenceStore

MAX_BACKLOG_PER_GENERATION = 5
_TODO_PATTERN = re.compile(r"\b(TODO|FIXME|HACK|XXX)\b[ :](.{0,120})")
_SKIP_DIRS = {".git", ".agents", ".venv", "node_modules", "__pycache__", ".pytest_cache"}


@dataclass(frozen=True)
class BacklogItem:
    """One unit of proposed maintenance work (kind identifies the generator)."""

    kind: str  # dependency_update | flaky_test | todo_cleanup | coverage_gap | regression_check | tech_debt
    subject: str  # deduplication key within the kind
    title: str
    description: str
    role: TaskRole = TaskRole.CODE
    priority: int = 3
    commands: tuple[str, ...] = ()

    def dedup_key(self) -> str:
        return f"{self.kind}:{self.subject.lower().strip()}"


def _task_to_item_key(task: Task) -> str:
    """The backlog dedup key a task was created from ('' when not maintenance)."""
    created_by = str(task.created_by or "")
    if not created_by.startswith("maintenance:"):
        return ""
    return created_by.removeprefix("maintenance:")


def item_key_for(item: BacklogItem) -> str:
    return f"maintenance:{item.dedup_key()}"


# ---- generators ----------------------------------------------------------------


def dependency_updates(root: Path) -> list[BacklogItem]:
    """Pyproject dependencies that deserve a periodic review bump."""
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        return []
    try:
        text = pyproject.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    deps = re.findall(r'^\s*"([A-Za-z0-9_.\-]+)[><=~!^]', text, re.MULTILINE)
    if not deps:
        return []
    subject = ",".join(sorted(deps)[:8])
    return [
        BacklogItem(
            kind="dependency_update",
            subject=subject,
            title=f"Review and update dependencies ({len(deps)} declared)",
            description=(
                "Continuous-objective maintenance: check the declared dependencies "
                f"({', '.join(sorted(deps)[:8])}) for available updates, security "
                "advisories and deprecations; bump conservatively and run the test suite."
            ),
            role=TaskRole.CODE,
            priority=4,
            commands=("python -m pytest tests/ -q",),
        )
    ]


def flaky_tests(workspace: Workspace, graph: TaskGraph) -> list[BacklogItem]:
    """Tasks whose verification history flip-flopped get a quarantine/fix task."""
    store = EvidenceStore(workspace.paths.state)
    items: list[BacklogItem] = []
    for task in graph.all():
        if task.status.value != "COMPLETED":
            continue
        with contextlib.suppress(Exception):
            if store.flaky_score(task.id):
                items.append(
                    BacklogItem(
                        kind="flaky_test",
                        subject=task.id,
                        title=f"Quarantine/fix flaky verification for {task.id}",
                        description=(
                            f"Verification history for task {task.id} flip-flopped "
                            "(fail→pass→fail). Reproduce, fix the test or the code, and "
                            "re-verify with a clean run before unquarantining."
                        ),
                        role=TaskRole.TEST,
                        priority=2,
                        commands=tuple(task.verification_commands[:2]),
                    )
                )
    return items


def todo_cleanup(root: Path, *, max_markers: int = 12) -> list[BacklogItem]:
    """TODO/FIXME markers in the source tree, batched one task per file."""
    findings: list[tuple[str, int, str]] = []
    for path in sorted(root.rglob("*.py")):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for index, line in enumerate(text.splitlines(), start=1):
            match = _TODO_PATTERN.search(line)
            if match:
                findings.append((path.relative_to(root).as_posix(), index, match.group(0)[:120]))
            if len(findings) >= max_markers:
                break
        if len(findings) >= max_markers:
            break
    by_file: dict[str, list[str]] = {}
    for rel, line, marker in findings:
        by_file.setdefault(rel, []).append(f"line {line}: {marker}")
    return [
        BacklogItem(
            kind="todo_cleanup",
            subject=rel,
            title=f"Clear TODO/FIXME markers in {rel}",
            description="Continuous-objective maintenance: resolve or justify these markers:\n"
            + "\n".join(by_file[rel][:6]),
            role=TaskRole.CODE,
            priority=5,
        )
        for rel in sorted(by_file)
    ]


def coverage_gaps(workspace: Workspace) -> list[BacklogItem]:
    """Modules missing from (or below) the persisted coverage baseline."""
    baseline_path = workspace.paths.verification / "coverage_baseline.json"
    if not baseline_path.is_file():
        return []
    import json

    try:
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    files = baseline.get("files") or {}
    gaps = [
        (rel, int(info.get("covered_lines", 0)))
        for rel, info in files.items()
        if int(info.get("covered_lines", 0)) == 0
    ]
    return [
        BacklogItem(
            kind="coverage_gap",
            subject=rel,
            title=f"Add tests for uncovered module {rel}",
            description=(
                "Continuous-objective maintenance: this module has no covered lines in "
                "the persisted coverage baseline; add focused tests for its behaviour."
            ),
            role=TaskRole.TEST,
            priority=4,
            commands=("python -m pytest tests/ -q",),
        )
        for rel, _ in sorted(gaps)[:3]
    ]


def regression_checks(workspace: Workspace, graph: TaskGraph) -> list[BacklogItem]:
    """Completed tasks whose evidence predates the current HEAD (spec §14)."""
    from ..git.manager import GitManager

    root = workspace.paths.root
    try:
        git = GitManager(root)
        if not git.is_repo():
            return []
        head = git.head_commit()
    except Exception:
        return []
    if not head:
        return []
    store = EvidenceStore(workspace.paths.state)
    with contextlib.suppress(Exception):
        stale = store.stale_tasks(list(graph.tasks.keys()), head)
    if not stale:
        return []
    return [
        BacklogItem(
            kind="regression_check",
            subject=task_id,
            title=f"Re-verify {task_id} against the current HEAD",
            description=(
                f"Recorded evidence for {task_id} was produced at an older commit "
                f"({sha[:12]}) and no longer proves the current tree. Re-run its "
                "verification commands to confirm no regression."
            ),
            role=TaskRole.TEST,
            priority=3,
        )
        for task_id, sha in stale[:3]
    ]


def tech_debt(workspace: Workspace) -> list[BacklogItem]:
    """Debt items the project itself recorded in memory (kind=debt)."""
    from .memory import memory_store

    items: list[BacklogItem] = []
    for entry in memory_store(workspace).all():
        if entry.kind != "debt":
            continue
        items.append(
            BacklogItem(
                kind="tech_debt",
                subject=entry.text[:80],
                title=f"Address debt: {entry.text[:70]}",
                description=(
                    "Continuous-objective maintenance: recorded debt item — "
                    f"{entry.text} (source: {entry.source or 'memory'})."
                ),
                role=TaskRole.CODE,
                priority=5,
            )
        )
    return items


# ---- facade --------------------------------------------------------------------


def scan_backlog(workspace: Workspace, *, graph: TaskGraph | None = None) -> list[BacklogItem]:
    """All generators, in priority order (regressions first)."""
    root = workspace.paths.root
    graph = graph or workspace.load_graph()
    generators = (
        lambda: regression_checks(workspace, graph),
        lambda: flaky_tests(workspace, graph),
        lambda: coverage_gaps(workspace),
        lambda: dependency_updates(root),
        lambda: todo_cleanup(root),
        lambda: tech_debt(workspace),
    )
    items: list[BacklogItem] = []
    for generator in generators:
        with contextlib.suppress(Exception):  # one broken generator never stops the rest
            items.extend(generator())
    return items


def ensure_backlog(
    workspace: Workspace,
    graph: TaskGraph,
    *,
    max_tasks: int = MAX_BACKLOG_PER_GENERATION,
) -> list[Task]:
    """Add unseen maintenance tasks to the graph; returns what was added."""
    live_keys = {key for key in (_task_to_item_key(t) for t in graph.all()) if key}
    added: list[Task] = []
    for item in scan_backlog(workspace, graph=graph):
        if len(added) >= max_tasks:
            break
        key = item.dedup_key()
        if key in live_keys:
            continue
        task = Task(
            title=item.title,
            description=item.description,
            role=item.role,
            priority=item.priority,
            verification_commands=list(item.commands),
            created_by=item_key_for(item),
        )
        graph.add_task(task)
        live_keys.add(key)
        added.append(task)
    return added


def maintenance_round(workspace: Workspace, graph: TaskGraph) -> dict[str, Any]:
    """One generation round for the orchestrator: add backlog, report counts."""
    added = ensure_backlog(workspace, graph)
    if added:
        workspace.save_graph(graph)
    return {
        "added": len(added),
        "tasks": [t.id for t in added],
        "kinds": sorted({str(t.created_by).split(":", 1)[1].split(":", 1)[0] for t in added}),
    }
