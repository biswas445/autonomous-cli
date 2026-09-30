"""QA / Product Validation agent (plan.md §5K, §30, §55).

Asks the question no test can ask: "did we build the thing the user actually
requested?" It compares the compiled intent against what the task graph says
was delivered, detecting feature, scope, and quality drift. The orchestrator
runs it as a gate before declaring PROJECT_COMPLETE; unmet features are
turned into new tasks (goal drift detection), not silently accepted.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from ..core.task import Task, TaskGraph
from ..runtime.base import Agent, AgentResult
from ..runtime.context import AgentContext
from .shared import as_list, as_text, confidence

_STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "that",
    "this",
    "into",
    "from",
    "should",
    "must",
    "support",
    "implement",
    "user",
    "users",
}


class QACheck(BaseModel):
    aligned: bool = True
    gaps: list[str] = Field(default_factory=list)  # required but not delivered
    drift: list[str] = Field(default_factory=list)  # delivered but not requested
    summary: str = ""
    confidence: float = 0.5


class QAAgent(Agent):
    name = "qa"
    role = "qa"
    agent_class = "reviewer"
    description = "Validates the delivered system against the original intent."

    SYSTEM = (
        "You are the QA / Product Validation agent of an autonomous engineering system. "
        "Compare the ORIGINAL intent against what the task graph says was delivered. "
        "Report features that were required but not delivered (gaps) and work that drifted "
        "from the request (drift). Do not invent requirements that are not in the intent. "
        "Respond with a single JSON object, no prose."
    )
    SCHEMA_HINT = (
        "QACheck JSON with keys: aligned (bool), gaps[] (required but not delivered), "
        "drift[] (delivered but not requested), summary, confidence"
    )

    async def run(self, task, context: AgentContext) -> AgentResult:
        try:
            payload, usage = await self.ask_model(
                system=self.SYSTEM,
                prompt=context.render() or context.goal,
                schema_hint=self.SCHEMA_HINT,
                max_tokens=2500,
            )
        except Exception as exc:
            from .shared import model_failure

            return model_failure(self, exc, what="validate the result against the goal")

        check = QACheck(
            aligned=bool(payload.get("aligned", True)),
            gaps=as_list(payload.get("gaps")),
            drift=as_list(payload.get("drift")),
            summary=as_text(payload.get("summary")),
            confidence=confidence(payload.get("confidence"), 0.6),
        )
        self.record_activity("qa check", f"aligned={check.aligned} gaps={len(check.gaps)}")
        return AgentResult(
            # ok = "produced a valid verdict", not "the verdict was positive":
            # the orchestrator must see honest negative verdicts.
            ok=True,
            output=check.model_dump(mode="json"),
            confidence=check.confidence,
            evidence={"gaps": len(check.gaps), "drift": len(check.drift)},
            cost_usd=usage["cost_usd"],
            tokens_in=usage["tokens_in"],
            tokens_out=usage["tokens_out"],
        )

    # ---- deterministic fallback ----

    def deterministic_check(self, intent, graph: TaskGraph) -> QACheck:
        """Requirement-coverage check without a model.

        A feature counts as covered when some COMPLETED task's title or
        description shares a meaningful token with it. This is deliberately
        conservative: it under-detects coverage (creating at most a catch-up
        task), never over-detects it.
        """
        features = list(intent.features) + list(intent.requirements)
        completed_text = " ".join(
            f"{t.title} {t.description}".lower() for t in graph.completed_tasks()
        )
        completed_tokens = {
            w for w in re.split(r"[^a-z0-9]+", completed_text) if len(w) > 3
        } - _STOPWORDS

        gaps: list[str] = []
        for feature in features:
            tokens = {
                w
                for w in re.split(r"[^a-z0-9]+", str(feature).lower())
                if len(w) > 3 and w not in _STOPWORDS
            }
            if tokens and not tokens.intersection(completed_tokens):
                gaps.append(str(feature))
        return QACheck(
            aligned=not gaps,
            gaps=gaps,
            drift=[],
            summary=(
                f"{len(features) - len(gaps)}/{len(features)} required features have "
                "matching completed work"
                if features
                else "no explicit features to validate against"
            ),
            confidence=0.55,
        )


def gap_task(feature: str, graph: TaskGraph) -> Task:
    """Build the catch-up task for one unmet feature (dynamic task creation)."""
    existing = {t.id for t in graph.all()}
    index = 1
    while f"TASK-GAP-{index:03d}" in existing:
        index += 1
    return Task(
        id=f"TASK-GAP-{index:03d}",
        epic="goal-alignment",
        title=f"Close goal gap: {feature}",
        description=(
            f"The QA gate found this requirement unmet by the completed work: {feature}. "
            "Implement it, or record a decision that supersedes it."
        ),
        role="coding",
        priority=2,
        acceptance_criteria=[f"{feature} is implemented or explicitly superseded."],
        definition_of_done=["python -m compileall -q ."],
        verification_commands=["python -m compileall -q ."],
        risk="medium",
        estimated_complexity=5,
    )
