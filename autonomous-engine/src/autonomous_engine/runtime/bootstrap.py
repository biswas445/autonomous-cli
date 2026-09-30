"""Project bootstrap: `auto init` in one function.

Initialisation is deterministic and offline. It creates the Project Brain,
the constitution, the configuration, the SQLite schema, and a Git repository
when one is missing. It never calls a model and never touches the network —
the objective is recorded, not interpreted.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..core.config import BudgetConfig, PermissionClass, ProjectConfig, RunMode
from ..core.database import Database
from ..core.store import ProjectRecord, Store
from ..core.task import TaskGraph, new_id, now_iso
from ..core.workspace import Workspace, slugify
from ..git.manager import GitManager
from .constitution import ensure_constitution
from .context_setup import RuntimeContext, open_context
from .microagents import ensure_microagents_readme


def init_project(
    root: Path,
    *,
    project_name: str = "",
    objective: str = "",
    run_mode: RunMode = "autonomous",
    budget: BudgetConfig | None = None,
    git: bool = True,
    force: bool = False,
) -> RuntimeContext:
    """Create or adopt a project workspace at `root` and return its context."""
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)

    workspace = Workspace(root)
    name = project_name.strip() or slugify(root.name)

    if workspace.exists() and not force:
        # Adopt the existing workspace; only refresh configuration defaults.
        context = open_context(root)
        if objective:
            context.workspace.update_project(objective=objective)
            context.project.objective = objective
            context.store.upsert_project(context.project)
            context.objective = objective
        ensure_constitution(context.workspace, context.config, context.objective)
        return context

    config = ProjectConfig(
        project_name=name,
        run_mode=run_mode,
        budget=budget or BudgetConfig(),
        permission_classes=dict(_default_permissions()),
        enhance_prompt=True,
    )
    workspace = Workspace.initialize(root, name, config)
    project_id = new_id("PROJ")
    project_doc = workspace.load_project()
    project_doc["id"] = project_id
    project_doc["objective"] = objective
    workspace.save_project(project_doc)

    db = Database(workspace.paths.database)
    store = Store(db, project_id)
    project = ProjectRecord(id=project_id, name=name, objective=objective)
    store.upsert_project(project)
    store.save_graph(TaskGraph())

    ensure_constitution(workspace, config, objective)
    ensure_microagents_readme(workspace)

    if git:
        manager = GitManager(root)
        try:
            manager.ensure_repo()
            _ensure_gitignore(root)
        except Exception:
            # Git is an accelerator, not a prerequisite: a project without a
            # repository still runs, it just loses checkpoint commits.
            workspace.append_discovery(
                "git initialisation failed; checkpoints are disabled for this project"
            )

    workspace.events.append(
        "project.initialized",
        project=name,
        project_id=project_id,
        objective=objective[:500],
        run_mode=run_mode,
    )
    workspace.update_project(status="initialized")

    return RuntimeContext(
        root=root,
        workspace=workspace,
        config=config,
        store=store,
        project=project,
        project_id=project_id,
        db=db,
        objective=objective,
    )


GITIGNORE_CONTENT = """# Written by `auto init`
__pycache__/
*.py[cod]
.worktrees/
.agents/state.sqlite*

# Hot runtime state is persisted on disk (and mirrored in SQLite), not
# versioned — otherwise every cycle dirties the repository (the same choice
# aider and Claude Code make for their own state).
.agents/execution/

# Append-only episode log churns every task; the durable knowledge lives in
# memory.json / MEMORY.md, which stay versioned.
.agents/memory/episodes.jsonl
"""


def _ensure_gitignore(root: Path) -> None:
    """Keep generated caches and local control files out of checkpoint commits."""
    target = root / ".gitignore"
    if target.exists():
        return
    target.write_text(GITIGNORE_CONTENT, encoding="utf-8")


def _default_permissions() -> dict[str, PermissionClass]:
    from ..core.config import DEFAULT_PERMISSION_CLASSES

    return {name: perm.model_copy(deep=True) for name, perm in DEFAULT_PERMISSION_CLASSES.items()}


def set_objective(context: RuntimeContext, objective: str) -> RuntimeContext:
    """Record the objective without running anything."""
    context.workspace.update_project(objective=objective)
    context.project.objective = objective
    context.objective = objective
    context.store.upsert_project(context.project)
    return context


def project_summary(context: RuntimeContext) -> dict[str, Any]:
    workspace = context.workspace
    graph = workspace.load_graph()
    project_doc = workspace.load_project()
    run_doc = workspace.load_run()
    return {
        "name": context.project.name,
        "project_id": context.store.project_id,
        "root": str(context.repo_root),
        "status": run_doc.get("status", "") or project_doc.get("status", ""),
        "objective": context.objective,
        "run_mode": context.config.run_mode,
        "progress": graph.progress(),
        "pending_escalations": len(workspace.pending_escalations()),
        "checkpoints": workspace.list_checkpoints(),
        "events": len(workspace.events.read_all()),
        "current_run": run_doc.get("run_id", ""),
        "constitution_written": bool(project_doc.get("constitution_written")),
        "intent_source": "enhanced"
        if (project_doc.get("intent") or {}).get("enhanced")
        else "literal",
        "created_at": project_doc.get("created_at", now_iso()),
    }


def dump_summary(summary: dict[str, Any]) -> str:
    return json.dumps(summary, ensure_ascii=False, indent=2, default=str)
