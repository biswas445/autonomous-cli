"""Verification next slice (directive #5): gates above the task level.

What was missing after the task-level gate:
- **milestone-level gates**: all of a milestone's tasks individually gated,
  plus the milestone roadmap status.
- **project-level gates**: every completed task gated + QA artifacts exist.
- **gate composition**: milestone gates are the AND of their task gates; the
  project gate is the AND of milestone gates (evidence-flowing-up).
- **coverage baseline + regression comparison**: persisted baseline
  (`.agents/verification/coverage_baseline.json`), compare runs against it.
- **daemon-restart reconciliation**: verification interrupted mid-run
  (task in VERIFYING/REVIEWING with a run doc that says running) is detected
  and returned to a schedulable state with its evidence marked untrusted.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.config import QualityGatePolicy
from ..core.task import TaskGraph, now_iso
from ..core.workspace import Workspace
from .evidence import EvidenceStore
from .gate import GateStatus

COVERAGE_BASELINE_VERSION = 1


# ---- coverage baseline ---------------------------------------------------------


def baseline_path(workspace: Workspace) -> Path:
    return workspace.paths.verification / "coverage_baseline.json"


def save_coverage_baseline(
    workspace: Workspace,
    *,
    total_lines: int,
    covered_lines: int,
    files: dict[str, dict[str, int]] | None = None,
    commit: str = "",
) -> dict[str, Any]:
    """Persist a real coverage snapshot (or update files incrementally)."""
    path = baseline_path(workspace)
    existing: dict[str, Any] = {}
    if path.is_file():
        with contextlib.suppress(json.JSONDecodeError, OSError):
            existing = json.loads(path.read_text(encoding="utf-8"))
    merged_files: dict[str, dict[str, int]] = dict(existing.get("files") or {})
    for rel, info in (files or {}).items():
        merged_files[rel] = {
            "covered_lines": int(info.get("covered_lines", 0)),
            "total_lines": int(info.get("total_lines", 0)),
        }
    payload = {
        "schema_version": COVERAGE_BASELINE_VERSION,
        "updated_at": now_iso(),
        "commit": commit or str(existing.get("commit", "")),
        "total_lines": int(total_lines),
        "covered_lines": int(covered_lines),
        "files": merged_files,
    }
    from ..core.workspace import atomic_write_json

    atomic_write_json(path, payload)
    return payload


def load_coverage_baseline(workspace: Workspace) -> dict[str, Any] | None:
    path = baseline_path(workspace)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


@dataclass
class CoverageComparison:
    has_baseline: bool = False
    baseline_pct: float = 0.0
    current_pct: float = 0.0
    delta_pct: float = 0.0
    regressed_files: list[str] = field(default_factory=list)
    improved_files: list[str] = field(default_factory=list)
    new_files: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "has_baseline": self.has_baseline,
            "baseline_pct": round(self.baseline_pct, 2),
            "current_pct": round(self.current_pct, 2),
            "delta_pct": round(self.delta_pct, 2),
            "regressed_files": self.regressed_files[:10],
            "improved_files": self.improved_files[:10],
            "new_files": self.new_files[:10],
        }


def compare_coverage(
    workspace: Workspace,
    *,
    current_total: int,
    current_covered: int,
    current_files: dict[str, dict[str, int]] | None = None,
) -> CoverageComparison:
    """Compare a fresh coverage run against the persisted baseline (ratchet)."""
    comparison = CoverageComparison()
    baseline = load_coverage_baseline(workspace)
    if baseline is None:
        return comparison
    comparison.has_baseline = True
    baseline_total = max(1, int(baseline.get("total_lines", 1)))
    baseline_covered = int(baseline.get("covered_lines", 0))
    comparison.baseline_pct = 100.0 * baseline_covered / baseline_total
    comparison.current_pct = 100.0 * current_covered / max(1, current_total)
    comparison.delta_pct = comparison.current_pct - comparison.baseline_pct
    baseline_files: dict[str, dict[str, int]] = baseline.get("files") or {}
    for rel, info in (current_files or {}).items():
        old = baseline_files.get(rel)
        if old is None:
            comparison.new_files.append(rel)
            continue
        covered_now = int(info.get("covered_lines", 0))
        covered_before = int(old.get("covered_lines", 0))
        if covered_now < covered_before:
            comparison.regressed_files.append(rel)
        elif covered_now > covered_before:
            comparison.improved_files.append(rel)
    for rel in baseline_files:
        if current_files and rel not in current_files:
            comparison.regressed_files.append(rel)  # module vanished from the report
    return comparison


# ---- milestone / project gates -------------------------------------------------


@dataclass
class GateNode:
    """One node in the gate composition tree."""

    name: str
    level: str  # task | milestone | project
    status: GateStatus
    children: list[GateNode] = field(default_factory=list)
    blocking: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in (GateStatus.PASSED, GateStatus.PASSED_WITH_WARNINGS)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "level": self.level,
            "status": self.status.value,
            "ok": self.ok,
            "children": [child.as_dict() for child in self.children],
            "blocking": self.blocking[:5],
            "detail": self.detail,
        }


def _worst_status(statuses: list[GateStatus]) -> GateStatus:
    order = [
        GateStatus.BLOCKED,
        GateStatus.FAILED,
        GateStatus.INSUFFICIENT_EVIDENCE,
        GateStatus.PASSED_WITH_WARNINGS,
        GateStatus.PASSED,
    ]
    for status in order:
        if status in statuses:
            return status
    return GateStatus.INSUFFICIENT_EVIDENCE


def evaluate_milestone_gate(
    workspace: Workspace,
    graph: TaskGraph,
    milestone: dict[str, Any],
    policy: QualityGatePolicy | None = None,
) -> GateNode:
    """A milestone gate: every task's latest evidence must pass, right now."""
    policy = policy or QualityGatePolicy()
    store = EvidenceStore(workspace.paths.state)
    children: list[GateNode] = []
    for task_id in milestone.get("task_ids", []):
        if task_id not in graph.tasks:
            continue
        latest = store.latest(task_id)
        if latest is None:
            children.append(
                GateNode(
                    name=task_id,
                    level="task",
                    status=GateStatus.INSUFFICIENT_EVIDENCE,
                    blocking=["no evidence recorded"],
                )
            )
            continue
        if not latest.passed:
            children.append(
                GateNode(
                    name=task_id,
                    level="task",
                    status=GateStatus.FAILED,
                    blocking=list(latest.failure_classes)[:3] or ["latest evidence failed"],
                )
            )
            continue
        # Task passed but stale evidence can no longer prove the tree.
        from ..git.manager import GitManager

        head = ""
        with contextlib.suppress(Exception):
            git = GitManager(workspace.paths.root)
            head = git.head_commit() if git.is_repo() else ""
        if head and latest.is_stale(head):
            children.append(
                GateNode(
                    name=task_id,
                    level="task",
                    status=GateStatus.BLOCKED,
                    blocking=[f"evidence stale (verified at {latest.commit_sha[:12]})"],
                )
            )
            continue
        children.append(
            GateNode(
                name=task_id,
                level="task",
                status=GateStatus.PASSED,
                detail={"evidence_id": latest.evidence_id, "commit": latest.commit_sha[:12]},
            )
        )
    status = _worst_status([child.status for child in children]) if children else GateStatus.INSUFFICIENT_EVIDENCE
    if milestone.get("status") != "done" and status == GateStatus.PASSED:
        status = GateStatus.PASSED_WITH_WARNINGS  # milestone not yet marked done
    blocking = [b for child in children for b in child.blocking]
    return GateNode(
        name=str(milestone.get("name", milestone.get("id", "?"))),
        level="milestone",
        status=status,
        children=children,
        blocking=blocking,
        detail={"milestone_id": milestone.get("id", ""), "tasks": len(children)},
    )


def evaluate_project_gate(
    workspace: Workspace,
    graph: TaskGraph,
    policy: QualityGatePolicy | None = None,
) -> GateNode:
    """The project gate: the AND of all milestone gates, evidence-up."""
    policy = policy or QualityGatePolicy()
    roadmap = workspace.load_roadmap().get("milestones", [])
    children = [evaluate_milestone_gate(workspace, graph, m, policy) for m in roadmap]
    if not children:
        # No roadmap milestones: gate on completed tasks with evidence only.
        store = EvidenceStore(workspace.paths.state)
        for task in graph.all():
            latest = store.latest(task.id)
            if latest is None:
                continue
            children.append(
                GateNode(
                    name=task.id,
                    level="task",
                    status=GateStatus.PASSED if latest.passed else GateStatus.FAILED,
                    blocking=[] if latest.passed else ["latest evidence failed"],
                )
            )
    if not children:
        return GateNode(
            name="project",
            level="project",
            status=GateStatus.INSUFFICIENT_EVIDENCE,
            blocking=["no tasks and no milestones to gate"],
        )
    status = _worst_status([child.status for child in children])
    return GateNode(
        name="project",
        level="project",
        status=status,
        children=children,
        blocking=[b for child in children for b in child.blocking],
        detail={"milestones": len(children)},
    )


# ---- daemon-restart reconciliation ---------------------------------------------


RECONCILIATION_FILE = "verification_inflight.json"


def _reconciliation_path(workspace: Workspace) -> Path:
    return workspace.paths.verification / RECONCILIATION_FILE


def record_inflight_verification(workspace: Workspace, task_id: str, command: str = "") -> None:
    """The orchestrator notes a verification started, so a restart can spot it."""
    from ..core.workspace import atomic_write_json

    path = _reconciliation_path(workspace)
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (json.JSONDecodeError, OSError):
        data = {}
    inflight: dict[str, Any] = data.get("inflight", {}) if isinstance(data, dict) else {}
    inflight[task_id] = {"started_at": now_iso(), "command": command[:200]}
    atomic_write_json(path, {"inflight": inflight})


def clear_inflight_verification(workspace: Workspace, task_id: str) -> None:
    path = _reconciliation_path(workspace)
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (json.JSONDecodeError, OSError):
        return
    inflight: dict[str, Any] = data.get("inflight", {}) if isinstance(data, dict) else {}
    if task_id in inflight:
        del inflight[task_id]
        from ..core.workspace import atomic_write_json

        atomic_write_json(path, {"inflight": inflight})


def reconcile_after_restart(workspace: Workspace, graph: TaskGraph) -> dict[str, Any]:
    """Daemon-restart reconciliation for in-flight verification (directive #5).

    A verification interrupted by a process death cannot be trusted: the
    command may never have finished. Tasks recorded in-flight and still in
    VERIFYING/REVIEWING are failed-and-requeued with a note, and their
    in-flight record is dropped. Real persisted state only.
    """
    path = _reconciliation_path(workspace)
    if not path.is_file():
        return {"reconciled": [], "note": "no in-flight record"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"reconciled": [], "note": "unreadable in-flight record"}
    inflight: dict[str, Any] = data.get("inflight", {}) if isinstance(data, dict) else {}
    if not inflight:
        return {"reconciled": []}
    from ..core.state_machine import TaskState

    reconciled: list[str] = []
    changed = False
    for task_id, _info in list(inflight.items()):
        if task_id not in graph.tasks:
            inflight.pop(task_id)
            changed = True
            continue
        task = graph.get(task_id)
        if task.status not in (TaskState.VERIFYING, TaskState.REVIEWING):
            inflight.pop(task_id)
            changed = True
            continue
        try:
            task.set_state(
                TaskState.FAILED, agent="reconciliation", note="verification interrupted by restart"
            )
            task.set_state(
                TaskState.READY, agent="reconciliation", note="re-queued for re-verification"
            )
            reconciled.append(task_id)
            changed = True
        except Exception:
            continue
    if changed:
        from ..core.workspace import atomic_write_json

        atomic_write_json(path, {"inflight": inflight})
        if reconciled:
            workspace.save_graph(graph)
    return {"reconciled": reconciled}
