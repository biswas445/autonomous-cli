"""Runtime context: paths and configuration for one engine instance.

A single `RuntimeContext` is built by the CLI (or by an embedding program) and
handed to the orchestrator. It owns no state of its own — it is the seam that
keeps the orchestration core independent of how it was launched.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.config import ProjectConfig
from ..core.database import Database
from ..core.store import ProjectRecord, Store
from ..core.task import new_id
from ..core.workspace import Workspace, slugify


@dataclass
class RuntimeContext:
    """Everything the orchestrator needs, assembled once."""

    root: Path
    workspace: Workspace
    config: ProjectConfig
    store: Store
    project: ProjectRecord
    project_id: str
    db: Database
    objective: str = ""
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def repo_root(self) -> Path:
        return self.workspace.paths.root

    def snapshot(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "project": self.project.name,
            "root": str(self.repo_root),
            "objective": self.objective[:200],
            "run_mode": self.config.run_mode,
        }


def open_context(
    root: Path,
    *,
    config: ProjectConfig | None = None,
    project_id: str = "",
) -> RuntimeContext:
    """Open (or create) the persistent state for the project at `root`."""
    root = Path(root).resolve()
    workspace = Workspace(root)
    if not workspace.exists():
        raise FileNotFoundError(f"no autonomous-engine project at {root}; run `auto init` first")
    cfg = config or workspace.load_config()
    project_doc = workspace.load_project()

    project_id = project_id or str(project_doc.get("id") or "") or new_id("PROJ")
    db = Database(workspace.paths.database)
    store = Store(db, project_id)

    project = store.get_project()
    if project is None:
        project = ProjectRecord(
            id=project_id,
            name=str(project_doc.get("name") or cfg.project_name or slugify(root.name)),
            objective=str(project_doc.get("objective") or ""),
            intent=project_doc.get("intent", {}) or {},
        )
        store.upsert_project(project)
        project_doc.setdefault("id", project_id)
        workspace.save_project(project_doc)

    return RuntimeContext(
        root=root,
        workspace=workspace,
        config=cfg,
        store=store,
        project=project,
        project_id=project_id,
        db=db,
        objective=project.objective or str(project_doc.get("objective") or ""),
    )
