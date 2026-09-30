"""Intent Compiler (plan.md §5A, §34, §35).

Turns a messy natural-language request into a machine-operational project
specification. This is the "Prompt Enhance" toggle: OFF means the request is
executed literally; ON means the intent is compiled into explicit goals,
requirements, constraints, assumptions, unknowns, risks, acceptance criteria
and a definition of done.

Critically: it does not silently invent product requirements. Every inferred
item is labelled with a source and a confidence, and assumptions are visible
so a human can contradict them.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ..runtime.base import Agent, AgentResult
from ..runtime.context import AgentContext
from .shared import as_list, as_text, confidence


class Assumption(BaseModel):
    statement: str
    source: str = "implied by request"
    confidence: float = 0.5
    action: str = "proceeding unless contradicted"


class ProjectIntent(BaseModel):
    """The machine-readable project definition produced by the compiler."""

    objective: str
    users: list[str] = Field(default_factory=list)
    features: list[str] = Field(default_factory=list)
    requirements: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    non_goals: list[str] = Field(default_factory=list)
    quality_requirements: list[str] = Field(default_factory=list)
    deployment_target: str = ""
    unknowns: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    definition_of_done: list[str] = Field(default_factory=list)
    assumptions: list[Assumption] = Field(default_factory=list)
    enhanced: bool = True
    source_prompt: str = ""

    def to_markdown(self) -> str:
        lines = [f"# Objective\n\n{self.objective}\n"]
        sections = [
            ("Users", self.users),
            ("Features", self.features),
            ("Requirements", self.requirements),
            ("Constraints", self.constraints),
            ("Non-goals", self.non_goals),
            ("Quality requirements", self.quality_requirements),
            ("Unknowns", self.unknowns),
            ("Risks", self.risks),
            ("Acceptance criteria", self.acceptance_criteria),
            ("Definition of done", self.definition_of_done),
        ]
        for title, items in sections:
            if items:
                lines.append(f"## {title}\n")
                lines.extend(f"- {item}" for item in items)
                lines.append("")
        if self.assumptions:
            lines.append("## Assumptions (labelled, not invented silently)\n")
            for assumption in self.assumptions:
                lines.append(
                    f"- **{assumption.statement}**\n"
                    f"  - source: {assumption.source}\n"
                    f"  - confidence: {assumption.confidence:.2f}\n"
                    f"  - action: {assumption.action}"
                )
            lines.append("")
        if self.deployment_target:
            lines.append(f"## Deployment target\n\n{self.deployment_target}\n")
        return "\n".join(lines)


class IntentCompiler(Agent):
    name = "intent-compiler"
    role = "intent_compiler"
    agent_class = "planner"
    description = (
        "Compiles a natural-language goal into a machine-operational project specification."
    )

    SYSTEM = (
        "You are the Intent Compiler of an autonomous engineering system. "
        "Transform the user's request into a machine-operational project specification. "
        "Rules: (1) never invent product requirements silently — every inferred item is an "
        "assumption with a source and confidence; (2) capture goals, requirements, implied "
        "requirements, constraints, non-goals, quality expectations, risks, unknowns, "
        "acceptance criteria and a definition of done; (3) keep every item concrete and "
        "testable; (4) respond with a single JSON object and no prose."
    )

    SCHEMA_HINT = (
        "ProjectIntent JSON with keys: objective, users[], features[], requirements[], "
        "constraints[], non_goals[], quality_requirements[], deployment_target, unknowns[], "
        "risks[], acceptance_criteria[], definition_of_done[], assumptions[] "
        "where each assumption is {statement, source, confidence, action}"
    )

    async def run(self, task, context: AgentContext) -> AgentResult:
        prompt = context.render() or context.goal
        try:
            payload, usage = await self.ask_model(
                system=self.SYSTEM,
                prompt=self._build_prompt(prompt),
                schema_hint=self.SCHEMA_HINT,
                max_tokens=3000,
                temperature=0.3,
            )
        except Exception as exc:
            from .shared import model_failure

            return model_failure(self, exc, what="compile intent")

        intent = self._to_intent(
            payload,
            objective_fallback=context.goal or prompt.strip()[:500],
            source_prompt=prompt,
        )
        self.record_activity("compiled intent", f"{len(intent.requirements)} requirements")
        return AgentResult(
            ok=True,
            output=intent.model_dump(mode="json"),
            confidence=min([a.confidence for a in intent.assumptions] or [0.6]),
            evidence={"assumptions": len(intent.assumptions), "unknowns": len(intent.unknowns)},
            cost_usd=usage["cost_usd"],
            tokens_in=usage["tokens_in"],
            tokens_out=usage["tokens_out"],
        )

    def _build_prompt(self, raw_request: str) -> str:
        return (
            "Compile the following user request into a project specification.\n"
            "User request:\n"
            f"{raw_request}\n\n"
            "Return JSON matching this schema:\n"
            f"{self.SCHEMA_HINT}"
        )

    def _to_intent(
        self,
        payload: dict[str, Any],
        *,
        objective_fallback: str = "",
        source_prompt: str = "",
    ) -> ProjectIntent:
        assumptions = []
        for item in payload.get("assumptions", []) or []:
            if isinstance(item, str):
                assumptions.append(Assumption(statement=item, confidence=0.5))
            elif isinstance(item, dict):
                assumptions.append(
                    Assumption(
                        statement=as_text(item.get("statement"), "unspecified assumption"),
                        source=as_text(item.get("source"), "implied by request"),
                        confidence=confidence(item.get("confidence"), 0.5),
                        action=as_text(item.get("action"), "proceeding unless contradicted"),
                    )
                )
        return ProjectIntent(
            objective=as_text(payload.get("objective")) or objective_fallback,
            users=as_list(payload.get("users")),
            features=as_list(payload.get("features")),
            requirements=as_list(payload.get("requirements")),
            constraints=as_list(payload.get("constraints")),
            non_goals=as_list(payload.get("non_goals")),
            quality_requirements=as_list(payload.get("quality_requirements")),
            deployment_target=as_text(payload.get("deployment_target")),
            unknowns=as_list(payload.get("unknowns")),
            risks=as_list(payload.get("risks")),
            acceptance_criteria=as_list(payload.get("acceptance_criteria")),
            definition_of_done=as_list(payload.get("definition_of_done")),
            assumptions=assumptions,
            enhanced=bool(payload.get("enhanced", True)),
            source_prompt=source_prompt,
        )


def literal_intent(request: str) -> ProjectIntent:
    """Enhance OFF: execute the request literally, with no interpretation."""
    return ProjectIntent(
        objective=request.strip(),
        requirements=[request.strip()],
        acceptance_criteria=["The delivered system addresses the request as written."],
        definition_of_done=[
            "file exists: README.md",
            "python -m compileall -q .",
        ],
        enhanced=False,
        source_prompt=request,
    )
