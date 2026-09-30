"""Engineering Director / Delegation Agent (plan.md §4, §36, §51).

The Director is not the boss of a fixed pipeline; it is the system's manager.
Each cycle it answers:

    What is the project trying to become?  What is complete?  What remains?
    What is blocking progress?  Which agent should work next?
    Does the architecture still make sense?  Did the implementation satisfy
    the requirement?  Should more tasks be created?  Should the approach be
    modified or abandoned?  Can work run in parallel?  Are we done?

Important boundary: the Director *proposes*; the orchestrator decides. Every
Director action is a structured proposal that deterministic code validates and
applies (or rejects) with recorded justification.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from ..core.task import TaskGraph
from ..runtime.base import Agent, AgentResult
from ..runtime.context import AgentContext
from .shared import as_list, as_text, clamp, confidence

ActionKind = Literal[
    "continue",  # keep executing the current plan
    "select_task",  # run this specific task next
    "parallelize",  # run an independent wave together
    "create_tasks",  # the plan was incomplete: add tasks
    "reprioritise",  # priorities changed because of new evidence
    "replan",  # the plan is invalid: rewrite the graph
    "architecture_review",  # a failure suggests the architecture is wrong
    "request_research",  # an unknown blocks progress
    "escalate",  # a human must decide
    "stop",  # a stop condition is met
]


class DirectorProposal(BaseModel):
    """A structured, validated proposal — never free-form instructions."""

    action: ActionKind = "continue"
    rationale: str = ""
    # payload semantics depend on `action`
    task_ids: list[str] = Field(default_factory=list)
    new_tasks: list[dict[str, Any]] = Field(default_factory=list)
    reprioritise: dict[str, int] = Field(default_factory=dict)
    cancel_task_ids: list[str] = Field(default_factory=list)
    research_questions: list[str] = Field(default_factory=list)
    escalation: str = ""
    stop_reason: str = ""
    goal_progress: float = 0.0  # 0..1 self-assessment vs the ORIGINAL objective
    assumptions_changed: list[str] = Field(default_factory=list)
    new_risks: list[str] = Field(default_factory=list)
    confidence: float = 0.5

    def validate_against(self, graph: TaskGraph) -> tuple[bool, str]:
        """Deterministic validation. The orchestrator applies only valid proposals."""
        known = set(graph.tasks)
        for task_id in list(self.task_ids) + list(self.cancel_task_ids):
            if task_id not in known:
                return False, f"unknown task referenced: {task_id}"
        for task_id in self.reprioritise:
            if task_id not in known:
                return False, f"unknown task in reprioritise: {task_id}"
        for item in self.new_tasks:
            if not isinstance(item, dict) or not str(item.get("title", "")).strip():
                return False, "new task without a title"
        if self.action == "stop" and not self.stop_reason.strip():
            return False, "stop requires a reason"
        if self.action == "escalate" and not self.escalation.strip():
            return False, "escalation requires a description"
        return True, ""


class DirectorAgent(Agent):
    name = "director"
    role = "director"
    agent_class = "director"
    description = "Autonomous manager: decides what happens next across the whole project."

    SYSTEM = (
        "You are the Engineering Director of an autonomous engineering system. You are not a "
        "coder; you decide what should happen next. Given the project goal, the task graph, "
        "recent events, verification evidence, and the current budget, decide which action the "
        "orchestrator should take. Treat the plan as a hypothesis: if evidence invalidated it, "
        "say so and propose a replan. Never claim a task is done without evidence. "
        "Respond with a single JSON object, no prose."
    )
    SCHEMA_HINT = (
        "DirectorProposal JSON with keys: action (one of 'continue','select_task','parallelize',"
        "'create_tasks','reprioritise','replan','architecture_review','request_research',"
        "'escalate','stop'), rationale, task_ids[], new_tasks[] (each a task object), "
        "reprioritise{} (task_id->priority), cancel_task_ids[], research_questions[], "
        "escalation, stop_reason, goal_progress (0..1), assumptions_changed[], new_risks[], confidence"
    )

    async def run(self, task, context: AgentContext) -> AgentResult:
        try:
            payload, usage = await self.ask_model(
                system=self.SYSTEM,
                prompt=context.render() or context.goal,
                schema_hint=self.SCHEMA_HINT,
                max_tokens=3000,
                temperature=0.25,
                complexity=6,
            )
        except Exception as exc:
            from .shared import model_failure

            return model_failure(self, exc, what="propose the next action")

        proposal = self._to_proposal(payload)
        graph = self.deps.workspace.load_graph()
        valid, reason = proposal.validate_against(graph)
        if not valid:
            # An invalid proposal is evidence of a bad decision, not a crash.
            proposal = DirectorProposal(
                action="continue",
                rationale=f"Director proposal rejected by deterministic validation ({reason}); falling back to the current plan.",
                confidence=0.3,
            )

        self.record_activity("proposed action", f"{proposal.action}: {proposal.rationale[:160]}")
        return AgentResult(
            ok=True,
            output=proposal.model_dump(mode="json"),
            confidence=proposal.confidence,
            evidence={"action": proposal.action, "validated": valid},
            cost_usd=usage["cost_usd"],
            tokens_in=usage["tokens_in"],
            tokens_out=usage["tokens_out"],
        )

    def _to_proposal(self, payload: dict[str, Any]) -> DirectorProposal:
        allowed = {
            "continue",
            "select_task",
            "parallelize",
            "create_tasks",
            "reprioritise",
            "replan",
            "architecture_review",
            "request_research",
            "escalate",
            "stop",
        }
        action = str(payload.get("action", "continue")).strip().lower()
        if action not in allowed:
            action = "continue"

        reprioritise: dict[str, int] = {}
        raw_reprioritise = payload.get("reprioritise")
        if isinstance(raw_reprioritise, dict):
            for task_id, priority in raw_reprioritise.items():
                reprioritise[str(task_id)] = clamp(priority, 1, 10, 5)

        new_tasks = [t for t in (payload.get("new_tasks") or []) if isinstance(t, dict)]

        return DirectorProposal(
            action=action,  # type: ignore[arg-type]
            rationale=as_text(payload.get("rationale")),
            task_ids=as_list(payload.get("task_ids")),
            new_tasks=new_tasks,
            reprioritise=reprioritise,
            cancel_task_ids=as_list(payload.get("cancel_task_ids")),
            research_questions=as_list(payload.get("research_questions")),
            escalation=as_text(payload.get("escalation")),
            stop_reason=as_text(payload.get("stop_reason")),
            goal_progress=max(0.0, min(1.0, float(payload.get("goal_progress") or 0.0))),
            assumptions_changed=as_list(payload.get("assumptions_changed")),
            new_risks=as_list(payload.get("new_risks")),
            confidence=confidence(payload.get("confidence"), 0.55),
        )

    # ---- deterministic director helpers (used when no model is available) ----

    def deterministic_proposal(self, graph: TaskGraph, reason: str) -> DirectorProposal:
        """Rule-based decision used on every cycle as the baseline.

        The model's proposal is an *override* on top of this, never a
        replacement for the hard stop conditions.
        """
        ready = graph.ready_tasks()
        if not ready:
            active = graph.active_tasks()
            if active:
                return DirectorProposal(
                    action="continue",
                    rationale=f"{len(active)} task(s) in flight; waiting for evidence.",
                    task_ids=[t.id for t in active],
                    confidence=0.7,
                )
            failed = graph.failed_tasks()
            if failed:
                return DirectorProposal(
                    action="replan",
                    rationale=f"{len(failed)} failed task(s) with nothing ready to run.",
                    task_ids=[t.id for t in failed],
                    confidence=0.5,
                )
            return DirectorProposal(
                action="continue",
                rationale="No ready and no active tasks; re-evaluating the graph.",
                confidence=0.4,
            )

        wave = graph.independent_wave(max_tasks=4)
        if len(wave) > 1:
            return DirectorProposal(
                action="parallelize",
                rationale=f"{len(wave)} independent tasks can proceed together.",
                task_ids=wave,
                confidence=0.7,
            )
        return DirectorProposal(
            action="select_task",
            rationale="Highest-priority ready task.",
            task_ids=wave,
            confidence=0.7,
        )

    def status_report(self, graph: TaskGraph) -> dict[str, Any]:
        """Operational transparency: state, not hidden reasoning (§17)."""
        progress = graph.progress()
        return {
            "progress": progress,
            "ready": [t.id for t in graph.ready_tasks()],
            "active": [
                {"id": t.id, "state": t.status.value, "agent": t.assigned_agent}
                for t in graph.active_tasks()
            ],
            "blocked": [t.id for t in graph.blocked_tasks()][:10],
            "failed": [t.id for t in graph.failed_tasks()],
        }
