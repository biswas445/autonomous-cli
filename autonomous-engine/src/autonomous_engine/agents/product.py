"""Product / Requirements agent (plan.md §5B).

Converts a compiled intent into requirements, user stories, acceptance
criteria, non-functional requirements, edge cases, constraints, and a project
level Definition of Done.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..runtime.base import Agent, AgentResult
from ..runtime.context import AgentContext
from .shared import as_list, confidence


class Requirements(BaseModel):
    functional: list[str] = Field(default_factory=list)
    user_stories: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    non_functional: list[str] = Field(default_factory=list)
    edge_cases: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    definition_of_done: list[str] = Field(default_factory=list)


class ProductAgent(Agent):
    name = "product"
    role = "product"
    agent_class = "planner"
    description = (
        "Turns compiled intent into requirements, stories, and a project Definition of Done."
    )

    SYSTEM = (
        "You are the Product/Requirements analyst of an autonomous engineering system. "
        "Convert the project intent into concrete, testable requirements. Each requirement "
        "must be verifiable. Respond with a single JSON object, no prose."
    )
    SCHEMA_HINT = (
        "Requirements JSON with keys: functional[], user_stories[], acceptance_criteria[], "
        "non_functional[], edge_cases[], constraints[], definition_of_done[]"
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

            return model_failure(self, exc, what="derive requirements")

        requirements = Requirements(
            functional=as_list(payload.get("functional")),
            user_stories=as_list(payload.get("user_stories")),
            acceptance_criteria=as_list(payload.get("acceptance_criteria")),
            non_functional=as_list(payload.get("non_functional")),
            edge_cases=as_list(payload.get("edge_cases")),
            constraints=as_list(payload.get("constraints")),
            definition_of_done=as_list(payload.get("definition_of_done")),
        )
        self.record_activity("derived requirements", f"{len(requirements.functional)} functional")
        return AgentResult(
            ok=True,
            output=requirements.model_dump(mode="json"),
            confidence=confidence(payload.get("confidence"), 0.7),
            evidence={"requirements": len(requirements.functional)},
            cost_usd=usage["cost_usd"],
            tokens_in=usage["tokens_in"],
            tokens_out=usage["tokens_out"],
        )


def fallback_requirements(intent_features: list[str], objective: str) -> Requirements:
    """Deterministic requirements used when the model is unavailable."""
    functional = list(intent_features) or [objective]
    return Requirements(
        functional=functional,
        user_stories=[f"As a user, I need {f}." for f in functional],
        acceptance_criteria=[f"{f} is implemented and tested." for f in functional],
        non_functional=[
            "All code is syntactically valid and importable.",
            "The delivered system has runnable verification commands.",
        ],
        edge_cases=["Empty input", "Malformed input", "Concurrent access"],
        constraints=[],
        definition_of_done=[
            "file exists: README.md",
            "python -m compileall -q .",
            "The project README documents how to run and verify the system.",
        ],
    )
