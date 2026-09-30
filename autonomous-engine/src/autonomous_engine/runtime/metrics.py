"""Project metrics (plan.md §40): measure the system so improvements are scientific.

Everything here is derived from persisted evidence — the task graph, the
event log, and the SQLite store — never from agent self-reports.
"""

from __future__ import annotations

from typing import Any

from ..core.events import EventTypes
from ..core.store import Store
from ..core.workspace import Workspace

# Event names that count toward review/security findings.
_REVIEW_FINDING_EVENTS = ("review.non_blocking",)
_SECURITY_EVENTS = ("security.scan",)


def collect_metrics(workspace: Workspace, store: Store) -> dict[str, Any]:
    """Aggregate one project's operational metrics from persisted state."""
    graph = workspace.load_graph()
    progress = graph.progress()
    events = workspace.events.read_all()
    counts: dict[str, int] = {}
    for event in events:
        counts[event.get("event", "")] = counts.get(event.get("event", ""), 0) + 1

    attempts = sum(t.attempts for t in graph.all())
    retried = sum(1 for t in graph.all() if t.attempts > 1)
    verified = sum(1 for t in graph.completed_tasks() if t.verification.get("passed"))
    verification_events = counts.get(EventTypes.VERIFICATION_PASSED, 0) + counts.get(
        EventTypes.VERIFICATION_FAILED, 0
    )
    # Final-outcome pass rate: tasks with any verification record whose last
    # verification passed. Raw event counts include pre-adjudication runs
    # (manual criteria fail first, then the reviewer approves them), so the
    # honest success measure is taken from the task graph's final state.
    tasks_with_verification = [t for t in graph.all() if t.verification]
    final_passed = [t for t in tasks_with_verification if t.verification.get("passed")]

    escalations = workspace.load_escalations()
    unknowns_open = store.open_unknowns()
    usage = store.total_usage()

    security_findings = 0
    for event in events:
        if event.get("event") in _SECURITY_EVENTS:
            security_findings += int(event.get("findings", 0) or 0)
    review_findings = sum(counts.get(name, 0) for name in _REVIEW_FINDING_EVENTS)

    run_doc = workspace.load_run()
    runtime_seconds = 0.0
    budget = run_doc.get("budget") or {}
    if budget.get("elapsed_seconds"):
        runtime_seconds = float(budget["elapsed_seconds"])

    parallel_cycles = sum(
        1 for e in events if e.get("event") == "cycle.completed" and e.get("action") == "parallel"
    )

    return {
        # tasks (§40)
        "tasks_total": progress["total"],
        "tasks_completed": progress["completed"],
        "tasks_failed": progress["failed"],
        "tasks_cancelled": progress["cancelled"],
        "tasks_active": progress["active"],
        "tasks_pending": progress["pending"],
        "tasks_retried": retried,
        "total_attempts": attempts,
        "replanned_tasks": counts.get(EventTypes.TASK_REPLANNED, 0),
        "dynamically_created_tasks": counts.get(EventTypes.TASK_CREATED, 0),
        # verification quality
        "verification_runs": verification_events,
        "verification_final_pass_rate": round(len(final_passed) / len(tasks_with_verification), 3)
        if tasks_with_verification
        else None,
        "tasks_verified_completed": verified,
        # model usage + cost
        "model_calls": usage.calls,
        "tokens_in": usage.tokens_in,
        "tokens_out": usage.tokens_out,
        "cost_usd": round(usage.cost_usd, 4),
        # runtime
        "runtime_seconds": round(runtime_seconds, 1),
        "parallel_cycles": parallel_cycles,
        # engineering surface
        "commits": counts.get(EventTypes.COMMIT_CREATED, 0),
        "checkpoints": len(store.list_checkpoints()),
        "decisions": len(store.list_decisions()),
        "failures_recorded": len(store.list_failures()),
        "review_findings": review_findings,
        "security_findings": security_findings,
        "replans": counts.get(EventTypes.TASK_REPLANNED, 0) + counts.get("qa.gate_reopened", 0),
        # human involvement
        "human_interventions": len(escalations),
        "escalations_pending": len([e for e in escalations if e.get("status") == "pending"]),
        "unknowns_open": len(unknowns_open),
        "unknowns_total": len(unknowns_open) + counts.get(EventTypes.UNKNOWN_QUEUED, 0),
        "events_total": len(events),
    }


def render_metrics_markdown(metrics: dict[str, Any]) -> str:
    lines = ["# Project Metrics", ""]
    sections = {
        "Tasks": [
            "tasks_total",
            "tasks_completed",
            "tasks_failed",
            "tasks_cancelled",
            "tasks_active",
            "tasks_pending",
            "tasks_retried",
            "total_attempts",
            "replanned_tasks",
            "dynamically_created_tasks",
        ],
        "Verification": ["verification_runs", "verification_pass_rate", "tasks_verified_completed"],
        "Model usage": ["model_calls", "tokens_in", "tokens_out", "cost_usd"],
        "Runtime": ["runtime_seconds", "parallel_cycles"],
        "Engineering surface": [
            "commits",
            "checkpoints",
            "decisions",
            "failures_recorded",
            "review_findings",
            "security_findings",
            "replans",
        ],
        "Human involvement": [
            "human_interventions",
            "escalations_pending",
            "unknowns_open",
            "unknowns_total",
        ],
    }
    for title, keys in sections.items():
        lines.append(f"## {title}")
        lines.append("")
        for key in keys:
            value = metrics.get(key)
            if key == "cost_usd":
                value = f"${value}"
            lines.append(f"- {key}: {value}")
        lines.append("")
    return "\n".join(lines)
