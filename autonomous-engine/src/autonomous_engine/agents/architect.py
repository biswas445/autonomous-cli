"""Architect agent (plan.md §5D, §39).

Produces the system architecture, data model, module boundaries, API design,
infrastructure, security model, technology choices, testing strategy, and the
decisions that every later coding agent is bound to follow.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..core.store import DecisionRecord
from ..core.task import new_id
from ..runtime.base import Agent, AgentResult
from ..runtime.context import AgentContext
from .shared import as_list, as_text, confidence


class ArchitectureProposal(BaseModel):
    summary: str = ""
    technology_choices: list[str] = Field(default_factory=list)
    modules: list[str] = Field(default_factory=list)
    data_model: list[str] = Field(default_factory=list)
    api_design: list[str] = Field(default_factory=list)
    security_model: list[str] = Field(default_factory=list)
    testing_strategy: list[str] = Field(default_factory=list)
    deployment: list[str] = Field(default_factory=list)
    decisions: list[dict[str, str]] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    confidence: float = 0.6

    def to_markdown(self) -> str:
        lines = [f"# Architecture\n\n{self.summary or 'No summary provided.'}\n"]
        sections = [
            ("Technology choices", self.technology_choices),
            ("Modules", self.modules),
            ("Data model", self.data_model),
            ("API design", self.api_design),
            ("Security model", self.security_model),
            ("Testing strategy", self.testing_strategy),
            ("Deployment", self.deployment),
            ("Open questions", self.open_questions),
        ]
        for title, items in sections:
            if items:
                lines.append(f"## {title}\n")
                lines.extend(f"- {item}" for item in items)
                lines.append("")
        if self.decisions:
            lines.append("## Binding decisions\n")
            for decision in self.decisions:
                lines.append(
                    f"- **{decision.get('title', 'decision')}**: {decision.get('decision', '')}\n"
                    f"  - rationale: {decision.get('rationale', 'n/a')}\n"
                    f"  - status: {decision.get('status', 'accepted')}"
                )
            lines.append("")
        return "\n".join(lines)


class ArchitectAgent(Agent):
    name = "architect"
    role = "architect"
    agent_class = "architect"
    description = "Designs the system architecture and records binding decisions."

    SYSTEM = (
        "You are the Software Architect of an autonomous engineering system. "
        "Design a system that satisfies the requirements. Be concrete about module "
        "boundaries, data model, interfaces, security, and how it will be tested. "
        "Record decisions that later coding agents must follow. "
        "Respond with a single JSON object, no prose."
    )
    SCHEMA_HINT = (
        "ArchitectureProposal JSON with keys: summary, technology_choices[], modules[], "
        "data_model[], api_design[], security_model[], testing_strategy[], deployment[], "
        "decisions[] (each {title, decision, rationale, status}), open_questions[], confidence"
    )

    async def run(self, task, context: AgentContext) -> AgentResult:
        try:
            payload, usage = await self.ask_model(
                system=self.SYSTEM,
                prompt=context.render() or context.goal,
                schema_hint=self.SCHEMA_HINT,
                max_tokens=4000,
                complexity=7,
            )
        except Exception as exc:
            from .shared import model_failure

            return model_failure(self, exc, what="design architecture")

        proposal = self._to_proposal(payload)
        self.record_activity("designed architecture", f"{len(proposal.modules)} modules")
        return AgentResult(
            ok=True,
            output=proposal.model_dump(mode="json"),
            confidence=confidence(payload.get("confidence"), 0.65),
            evidence={"modules": len(proposal.modules), "decisions": len(proposal.decisions)},
            cost_usd=usage["cost_usd"],
            tokens_in=usage["tokens_in"],
            tokens_out=usage["tokens_out"],
        )

    def _to_proposal(self, payload: dict) -> ArchitectureProposal:
        decisions: list[dict[str, str]] = []
        for item in payload.get("decisions", []) or []:
            if isinstance(item, str):
                decisions.append(
                    {"title": item[:60], "decision": item, "rationale": "", "status": "accepted"}
                )
            elif isinstance(item, dict):
                decisions.append(
                    {
                        "title": as_text(item.get("title"), "decision"),
                        "decision": as_text(item.get("decision")),
                        "rationale": as_text(item.get("rationale")),
                        "status": as_text(item.get("status"), "accepted") or "accepted",
                    }
                )
        return ArchitectureProposal(
            summary=as_text(payload.get("summary")),
            technology_choices=as_list(payload.get("technology_choices")),
            modules=as_list(payload.get("modules")),
            data_model=as_list(payload.get("data_model")),
            api_design=as_list(payload.get("api_design")),
            security_model=as_list(payload.get("security_model")),
            testing_strategy=as_list(payload.get("testing_strategy")),
            deployment=as_list(payload.get("deployment")),
            decisions=decisions,
            open_questions=as_list(payload.get("open_questions")),
            confidence=confidence(payload.get("confidence"), 0.65),
        )

    def persist(self, proposal: ArchitectureProposal) -> None:
        """Write architecture artifacts and binding decisions to persistent state."""
        ws = self.deps.workspace
        ws.write_artifact("architecture/architecture.md", proposal.to_markdown())
        for question in proposal.open_questions:
            ws.append_discovery(f"OPEN QUESTION: {question}")
        lines = ["# Architecture Decisions", ""]
        for decision in proposal.decisions:
            lines.append(f"## {decision['title']}")
            lines.append(f"- Decision: {decision['decision']}")
            lines.append(f"- Rationale: {decision['rationale'] or 'n/a'}")
            lines.append(f"- Status: {decision['status']}")
            lines.append("")
        ws.write_artifact("architecture/decisions.md", "\n".join(lines))
        for decision in proposal.decisions:
            self.deps.store.save_decision(
                DecisionRecord(
                    id=new_id("DEC"),
                    project_id=self.deps.store.project_id,
                    title=decision["title"][:120],
                    body=decision["decision"],
                    status=decision["status"],
                    confidence=proposal.confidence,
                    evidence=[decision["rationale"]] if decision["rationale"] else [],
                    decided_by=self.name,
                )
            )
