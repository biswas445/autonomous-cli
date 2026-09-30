"""Release agent (plan.md §5L).

Runs once the project reaches a release candidate: verifies the release
criteria, produces release notes from the completed work, and proposes the
version/tag. The tag itself is applied by the orchestrator (git writes are
infrastructure, and the release agent's proposal is checked first).
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..core.task import TaskGraph, now_iso
from ..runtime.base import Agent, AgentResult
from ..runtime.context import AgentContext
from .shared import as_list, as_text, confidence


class ReleaseReport(BaseModel):
    ready: bool = False
    version: str = ""
    notes: str = ""
    checks: list[str] = Field(default_factory=list)
    blocking: list[str] = Field(default_factory=list)
    confidence: float = 0.5


class ReleaseAgent(Agent):
    name = "release"
    role = "release"
    agent_class = "release"
    description = "Prepares release notes and verifies release readiness."

    SYSTEM = (
        "You are the Release agent of an autonomous engineering system. Given the completed "
        "task graph and verification evidence, decide whether the project is release-ready: "
        "all work completed and verified, no outstanding failures, documentation present. "
        "Write release notes summarising what was built and how to verify it. "
        "Respond with a single JSON object, no prose."
    )
    SCHEMA_HINT = (
        "ReleaseReport JSON with keys: ready (bool), version, notes (markdown), checks[], "
        "blocking[], confidence"
    )

    async def run(self, task, context: AgentContext) -> AgentResult:
        try:
            payload, usage = await self.ask_model(
                system=self.SYSTEM,
                prompt=context.render() or context.goal,
                schema_hint=self.SCHEMA_HINT,
                max_tokens=3000,
            )
        except Exception as exc:
            from .shared import model_failure

            return model_failure(self, exc, what="assess release readiness")

        report = ReleaseReport(
            ready=bool(payload.get("ready", False)),
            version=as_text(payload.get("version")),
            notes=as_text(payload.get("notes")),
            checks=as_list(payload.get("checks")),
            blocking=as_list(payload.get("blocking")),
            confidence=confidence(payload.get("confidence"), 0.5),
        )
        self.record_activity("release assessment", f"ready={report.ready}")
        return AgentResult(
            ok=report.ready,
            output=report.model_dump(mode="json"),
            confidence=report.confidence,
            evidence={"blocking": len(report.blocking)},
            cost_usd=usage["cost_usd"],
            tokens_in=usage["tokens_in"],
            tokens_out=usage["tokens_out"],
        )

    def deterministic_report(self, graph: TaskGraph, objective: str) -> ReleaseReport:
        """Offline fallback: derive release readiness and notes from evidence."""
        progress = graph.progress()
        blocking: list[str] = []
        if progress["failed"]:
            blocking.append(f"{progress['failed']} failed task(s)")
        if progress["active"] or progress["pending"]:
            blocking.append(f"{progress['pending'] + progress['active']} unfinished task(s)")
        unverified = [
            t.id
            for t in graph.completed_tasks()
            if not t.verification or not t.verification.get("passed")
        ]
        if unverified:
            blocking.append(f"completed tasks without passing verification: {unverified[:5]}")

        completed = sorted(graph.completed_tasks(), key=lambda t: t.created_at)
        lines = [
            "# Release Notes",
            "",
            f"Generated: {now_iso()}",
            "",
            "## Objective",
            "",
            objective or "(no objective recorded)",
            "",
            "## What was built",
            "",
        ]
        for task in completed:
            lines.append(
                f"- {task.title} ({task.id}) — {task.verification.get('summary', 'verified')}"
            )
        lines += [
            "",
            "## How to verify",
            "",
            "Run the project's own verification commands; every completed task carries "
            "recorded command evidence in `.agents/verification/`.",
            "",
        ]
        ready = not blocking
        version = now_iso()[:10].replace("-", ".")
        return ReleaseReport(
            ready=ready,
            version=version,
            notes="\n".join(lines),
            checks=[
                f"tasks completed: {progress['completed']}",
                f"tasks failed: {progress['failed']}",
                f"verification evidence present for all completed tasks: {not unverified}",
            ],
            blocking=blocking,
            confidence=0.6,
        )
