"""Code Reviewer agent (plan.md §5I, §21).

The model that wrote the code is not the final judge of its own work. The
reviewer inspects correctness, maintainability, architecture consistency,
security, performance, error handling, test quality, and side effects — and
returns a verdict that is weighed alongside executable evidence, never above
it.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..runtime.base import Agent, AgentResult
from ..runtime.context import AgentContext
from .shared import as_text, confidence

SEVERITIES = ("critical", "high", "medium", "low", "info")


class ReviewFinding(BaseModel):
    severity: str = "info"
    category: str = (
        "general"  # correctness | security | maintainability | performance | tests | architecture
    )
    location: str = ""
    description: str = ""
    recommendation: str = ""


class ReviewVerdict(BaseModel):
    approved: bool = False
    findings: list[ReviewFinding] = Field(default_factory=list)
    summary: str = ""
    blocking: bool = False
    confidence: float = 0.5

    def blocking_findings(self) -> list[ReviewFinding]:
        return [f for f in self.findings if f.severity in ("critical", "high")]


class ReviewerAgent(Agent):
    name = "reviewer"
    role = "reviewer"
    agent_class = "reviewer"
    description = "Independently reviews an implementation for correctness, security and quality."

    SYSTEM = (
        "You are an independent Code Reviewer. You did not write this code. Review it for "
        "correctness, maintainability, architecture consistency, security, performance, error "
        "handling, test quality and unexpected side effects. Be specific: cite files and "
        "lines. A finding is blocking only if it would make the change incorrect or unsafe. "
        "Respond with a single JSON object, no prose."
    )
    SCHEMA_HINT = (
        "ReviewVerdict JSON with keys: approved (bool), findings[] where each is "
        "{severity: 'critical'|'high'|'medium'|'low'|'info', category, location, description, "
        "recommendation}, summary, blocking (bool), confidence"
    )

    async def run(self, task, context: AgentContext) -> AgentResult:
        try:
            payload, usage = await self.ask_model(
                system=self.SYSTEM,
                prompt=context.render() or context.goal,
                schema_hint=self.SCHEMA_HINT,
                max_tokens=4000,
            )
        except Exception as exc:
            from .shared import model_failure

            return model_failure(self, exc, what="review the change")

        verdict = self._to_verdict(payload)
        self.record_activity(
            "reviewed",
            f"approved={verdict.approved} findings={len(verdict.findings)}",
        )
        return AgentResult(
            ok=verdict.approved,
            output=verdict.model_dump(mode="json"),
            confidence=confidence(payload.get("confidence"), 0.6),
            evidence={
                "findings": len(verdict.findings),
                "blocking": len(verdict.blocking_findings()),
            },
            cost_usd=usage["cost_usd"],
            tokens_in=usage["tokens_in"],
            tokens_out=usage["tokens_out"],
        )

    def _to_verdict(self, payload: dict) -> ReviewVerdict:
        findings: list[ReviewFinding] = []
        for item in payload.get("findings", []) or []:
            if not isinstance(item, dict):
                continue
            severity = str(item.get("severity", "info")).lower()
            if severity not in SEVERITIES:
                severity = "info"
            findings.append(
                ReviewFinding(
                    severity=severity,
                    category=as_text(item.get("category"), "general"),
                    location=as_text(item.get("location")),
                    description=as_text(item.get("description")),
                    recommendation=as_text(item.get("recommendation")),
                )
            )
        approved = bool(payload.get("approved", False))
        blocking = bool(payload.get("blocking", False)) or any(
            f.severity in ("critical", "high") for f in findings
        )
        return ReviewVerdict(
            approved=approved and not blocking,
            findings=findings,
            summary=as_text(payload.get("summary")),
            blocking=blocking,
            confidence=confidence(payload.get("confidence"), 0.6),
        )
