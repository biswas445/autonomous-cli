"""Escalation and checkpoint methods of the orchestrator (plan.md §15/§16, §25).

Extracted from `orchestrator.py` (#6). The orchestrator inherits these as a
mixin; `self` is the Orchestrator.
"""

from __future__ import annotations

from typing import Any

from ..core.events import EventTypes
from ..core.store import CheckpointRecord
from ..core.task import Task, TaskGraph, new_id, now_iso
from ..git.manager import GitError


class LifecycleMixin:
    """Human escalation + checkpoint/restore logic. Mixin over the Orchestrator."""

    def _escalate(
        self, task_id: str, reason: str, *, kind: str, evidence: str = ""
    ) -> dict[str, Any]:
        escalation = {
            "id": new_id("ESC"),
            "task_id": task_id,
            "kind": kind,
            "reason": reason,
            "evidence": evidence,
            "status": "pending",
            "created_at": now_iso(),
        }
        self.workspace.add_escalation(escalation)
        self.emit(EventTypes.ESCALATION_RAISED, task_id=task_id, kind=kind, reason=reason[:300])
        return escalation

    def _checkpoint(self, task: Task, label: str) -> None:
        """Commit verified work and record a restorable checkpoint."""
        commit = ""
        if self.config.git_checkpoints:
            try:
                commit = self.git.create_checkpoint_commit(
                    task.id, f"checkpoint({label}): {task.title} [{task.id}]"
                )
                if commit:
                    self.emit(EventTypes.COMMIT_CREATED, task_id=task.id, commit=commit[:12])
            except GitError as exc:
                self.emit("git.checkpoint_failed", task_id=task.id, detail=str(exc))

        self._checkpoint_counter = (
            max(
                self._checkpoint_counter,
                len(self.workspace.list_checkpoints()),
            )
            + 1
        )
        checkpoint_id = f"checkpoint-{self._checkpoint_counter:04d}"
        record = CheckpointRecord(
            id=checkpoint_id,
            project_id=self.store.project_id,
            run_id=self.run.id,
            git_commit=commit,
            objective=self.ctx.objective[:500],
            outstanding_failures=[f.id for f in self.store.list_failures()][-5:],
            environment={"python": self._python_version(), "cwd": str(self.repo_root)},
            task_graph=self.graph.model_dump(mode="json"),
            project_state={"progress": self.graph.progress(), "budget": self.budget.snapshot()},
        )
        try:
            self.store.save_checkpoint(record)
            self.workspace.save_checkpoint(
                checkpoint_id,
                {
                    "id": checkpoint_id,
                    "git_commit": commit,
                    "label": label,
                    "task": task.id,
                    "created_at": record.created_at,
                },
            )
            self.emit(EventTypes.CHECKPOINT_CREATED, checkpoint=checkpoint_id, commit=commit[:12])
        except Exception as exc:
            self.emit("checkpoint.error", checkpoint=checkpoint_id, detail=str(exc))

    def restore_checkpoint(self, checkpoint_id: str) -> dict[str, Any]:
        """Restore a checkpoint's task graph (git is restored separately)."""
        record = self.store.get_checkpoint(checkpoint_id)
        if record is None:
            raise KeyError(f"unknown checkpoint: {checkpoint_id}")
        self.graph = TaskGraph.model_validate(record.task_graph)
        self.workspace.save_graph(self.graph)
        self.store.save_graph(self.graph)
        self.emit(
            EventTypes.CHECKPOINT_RESTORED, checkpoint=checkpoint_id, commit=record.git_commit[:12]
        )
        return {
            "checkpoint": checkpoint_id,
            "git_commit": record.git_commit,
            "progress": self.graph.progress(),
        }
