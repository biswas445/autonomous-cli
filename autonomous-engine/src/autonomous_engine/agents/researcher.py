"""Research agent (plan.md §5C, §14).

Investigates the Unknowns Queue instead of letting the main development flow
stall on open questions. It may read the repository (read-only permission
class); a resolved unknown records its answer in persistent memory, an
unresolved one stays open and visible rather than being silently guessed.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from ..runtime.base import Agent, AgentResult
from ..runtime.context import AgentContext
from ..runtime.tools import MEMORY_TOOLS, NETWORK_TOOLS, READ_ONLY_TOOLS
from .shared import as_text, confidence

# The researcher reads the repo, consults documentation over the network
# (permission class allows it), and records answers into project memory.
RESEARCH_TOOLS = (*READ_ONLY_TOOLS, *MEMORY_TOOLS, *NETWORK_TOOLS, "current_time")


class ResearchAnswer(BaseModel):
    question: str = ""
    answer: str = ""
    resolved: bool = False
    sources: list[str] = []
    confidence: float = 0.4


class ResearchAgent(Agent):
    name = "researcher"
    role = "researcher"
    agent_class = "researcher"
    description = "Investigates open questions and records evidence-based answers."

    SYSTEM = (
        "You are the Research agent of an autonomous engineering system. Investigate the "
        "question using the provided project context and repository. Answer from evidence: "
        "cite files or documentation you inspected. If the evidence is insufficient, say so "
        "and leave the question unresolved rather than guessing. "
        "Respond with a single JSON object, no prose."
    )
    SCHEMA_HINT = (
        "ResearchAnswer JSON with keys: question, answer, resolved (bool), sources[], confidence"
    )

    async def run(self, task, context: AgentContext) -> AgentResult:
        question = task.title if task is not None else context.goal
        prompt = (
            f"# QUESTION\n{question}\n\n"
            + (context.render() or "No additional context available.")
        )
        tools_enabled, max_iterations = _tool_settings(self)
        try:
            if tools_enabled:
                payload, usage = await self.ask_model_with_tools(
                    system=self.SYSTEM,
                    prompt=prompt,
                    schema_hint=self.SCHEMA_HINT,
                    tool_names=RESEARCH_TOOLS,
                    max_iterations=max_iterations,
                    max_tokens=2000,
                )
            else:
                payload, usage = await self.ask_model(
                    system=self.SYSTEM,
                    prompt=prompt,
                    schema_hint=self.SCHEMA_HINT,
                    max_tokens=2000,
                )
        except Exception as exc:
            from .shared import model_failure

            return model_failure(self, exc, what="research the question")

        answer = ResearchAnswer(
            question=as_text(payload.get("question"), question),
            answer=as_text(payload.get("answer")),
            resolved=bool(payload.get("resolved", False)),
            sources=[as_text(s) for s in payload.get("sources", []) or []],
            confidence=confidence(payload.get("confidence"), 0.4),
        )
        self.record_activity("researched", f"{question[:80]} -> resolved={answer.resolved}")
        return AgentResult(
            ok=bool(answer.answer),
            output=answer.model_dump(mode="json"),
            confidence=answer.confidence,
            evidence={
                "resolved": answer.resolved,
                "sources": len(answer.sources),
                "tool_loop": usage.get("tool_loop", {}),
            },
            cost_usd=usage["cost_usd"],
            tokens_in=usage["tokens_in"],
            tokens_out=usage["tokens_out"],
        )


def _tool_settings(agent: Any) -> tuple[bool, int]:
    """Read tools_enabled / max iterations from the agent's workspace config."""
    workspace = getattr(agent.deps, "workspace", None)
    if workspace is None or not hasattr(workspace, "load_config"):
        return True, 6
    try:
        config = workspace.load_config()
        return bool(config.tools_enabled), int(config.tool_loop_max_iterations)
    except Exception:
        return True, 6
