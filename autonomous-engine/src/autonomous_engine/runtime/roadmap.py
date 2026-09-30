"""Milestone planning (plan.md §31): the roadmap as a first-class artifact.

Milestones group the task graph into stable delivery units. The roadmap is a
derived view — rebuilt from the graph deterministically — so replanning,
dynamic task creation, and reprioritisation can never desynchronise it. Each
milestone's completion is tracked, and a milestone completion triggers a
self-evaluation record (§30).
"""

from __future__ import annotations

from typing import Any

from ..core.state_machine import TaskState
from ..core.task import Task, TaskGraph, now_iso
from ..core.workspace import Workspace

TERMINAL = (TaskState.COMPLETED, TaskState.CANCELLED)


def build_roadmap(graph: TaskGraph) -> dict[str, Any]:
    """Derive the roadmap from the graph: one milestone per epic, ordered.

    Tasks without an epic land in a single "Delivery" milestone. Status is
    computed, never stored as source-of-truth:
      done        every task terminal and at least one completed
      in_progress any task completed or active
      open        everything else
    """
    groups: dict[str, list[Task]] = {}
    for task in graph.all():
        name = task.epic.strip() or "Delivery"
        groups.setdefault(name, []).append(task)

    milestones: list[dict[str, Any]] = []
    for index, (name, tasks) in enumerate(
        sorted(groups.items(), key=lambda kv: (min(t.priority for t in kv[1]), kv[0])), start=1
    ):
        terminal = all(t.status in TERMINAL for t in tasks)
        completed = sum(1 for t in tasks if t.status == TaskState.COMPLETED)
        active = any(t.status not in TERMINAL and t.status != TaskState.QUEUED for t in tasks)
        if terminal and completed:
            status = "done"
        elif completed or active:
            status = "in_progress"
        else:
            status = "open"
        milestones.append(
            {
                "id": f"M-{index:02d}",
                "name": name,
                "task_ids": sorted(t.id for t in tasks),
                "tasks_total": len(tasks),
                "tasks_completed": completed,
                "status": status,
                "updated_at": now_iso(),
            }
        )
    return {"milestones": milestones, "updated_at": now_iso()}


def refresh_roadmap(workspace: Workspace, graph: TaskGraph) -> dict[str, Any]:
    """Rebuild and persist the roadmap; returns the milestones that just finished.

    A milestone counts as newly completed when its previous recorded status
    was anything other than done (including "never seen") — the first refresh
    after the last task completes must still report the transition.
    """
    previous = {
        m.get("name"): m.get("status") for m in workspace.load_roadmap().get("milestones", [])
    }
    roadmap = build_roadmap(graph)
    workspace.save_roadmap(roadmap)
    completed: list[dict[str, Any]] = []
    for milestone in roadmap["milestones"]:
        if milestone["status"] == "done" and previous.get(milestone["name"]) != "done":
            completed.append(milestone)
    return {"roadmap": roadmap, "newly_completed": completed}


def milestone_self_evaluation(
    workspace: Workspace, milestone: dict[str, Any], graph: TaskGraph
) -> dict[str, Any]:
    """Self-evaluation at a milestone boundary (plan.md §30).

    Deterministic and evidence-first: how far the delivered milestone is from
    the original objective, what remains, and which risks the recorded
    failures raise. Written to verification/ and returned for the event log.
    """
    project = workspace.load_project()
    intent = project.get("intent") or {}
    features = intent.get("features") or []
    tasks = [graph.get(tid) for tid in milestone["task_ids"] if tid in graph.tasks]
    unverified = [
        t.id for t in tasks if t.status == TaskState.COMPLETED and not t.verification.get("passed")
    ]
    failures = workspace.load_failures()
    evaluation = {
        "milestone": milestone["id"],
        "name": milestone["name"],
        "completed_at": now_iso(),
        "objective_distance": {
            "features_delivered_so_far": None,
            "features_total": len(features),
            "note": (
                f"counted project-wide at the next QA gate; milestone scope: {milestone['name']}"
            ),
        },
        "tasks": {
            "total": milestone["tasks_total"],
            "completed": milestone["tasks_completed"],
            "unverified_completed": unverified,
        },
        "remaining_risks": [
            f["root_cause"] or f["summary"] for f in failures[-5:] if f.get("root_cause")
        ],
        "assessments": {
            "what_changed_since_last_milestone": [
                t.title for t in tasks if t.status == TaskState.COMPLETED
            ],
            "what_remains": [t.title for t in tasks if t.status not in TERMINAL],
        },
    }
    workspace.write_json_artifact(f"verification/milestone_{milestone['id']}.json", evaluation)
    return evaluation
