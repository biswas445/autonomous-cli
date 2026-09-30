"""Debugger agent (plan.md §5H).

Receives actual evidence — diff, stack trace, failing test output, logs,
environment — and determines root cause, candidate fixes, risk, affected
files, and the tests required afterwards. It proposes; the orchestrator
dispatches the repair.
"""

from __future__ import annotations

import contextlib
from typing import Any

from pydantic import BaseModel, Field

from ..core.store import FailureRecord
from ..core.task import new_id
from ..runtime.base import Agent, AgentResult
from ..runtime.context import AgentContext
from ..runtime.tools import BUILDER_TOOLS
from .shared import as_list, as_text, confidence


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


class Diagnosis(BaseModel):
    root_cause: str = ""
    evidence: list[str] = Field(default_factory=list)
    candidate_fixes: list[str] = Field(default_factory=list)
    risk_of_fix: str = "medium"
    files_affected: list[str] = Field(default_factory=list)
    tests_required: list[str] = Field(default_factory=list)
    architecture_issue: bool = False
    lesson: str = ""
    confidence: float = 0.5


class DebuggerAgent(Agent):
    name = "debugger"
    role = "debugger"
    agent_class = "debugger"
    description = "Diagnoses failures from real evidence and proposes repairs."

    SYSTEM = (
        "You are the Debugger of an autonomous engineering system. You are given a failing "
        "task, the code diff, real command output, and the project state. Diagnose the ROOT "
        "CAUSE (not the symptom), propose concrete fixes, state the risk, list the affected "
        "files, and specify the tests that must pass afterwards. If the failure reveals that "
        "the architecture or plan is wrong, set architecture_issue to true. "
        "Respond with a single JSON object, no prose."
    )
    SCHEMA_HINT = (
        "Diagnosis JSON with keys: root_cause, evidence[], candidate_fixes[], risk_of_fix, "
        "files_affected[], tests_required[], architecture_issue (bool), lesson, confidence"
    )

    async def run(self, task, context: AgentContext) -> AgentResult:
        prompt = self._build_prompt(task, context)
        try:
            payload, usage = await self._ask_with_tools(
                system=self.SYSTEM,
                prompt=prompt,
                schema_hint=self.SCHEMA_HINT,
                max_tokens=4000,
                complexity=7,
            )
        except Exception as exc:
            from .shared import model_failure

            return model_failure(self, exc, what="diagnose the failure")

        diagnosis = Diagnosis(
            root_cause=as_text(payload.get("root_cause"), "root cause not established"),
            evidence=as_list(payload.get("evidence")),
            candidate_fixes=as_list(payload.get("candidate_fixes")),
            risk_of_fix=str(payload.get("risk_of_fix", "medium")).lower(),
            files_affected=as_list(payload.get("files_affected")),
            tests_required=as_list(payload.get("tests_required")),
            architecture_issue=bool(payload.get("architecture_issue", False)),
            lesson=as_text(payload.get("lesson")),
            confidence=confidence(payload.get("confidence"), 0.55),
        )
        if diagnosis.risk_of_fix not in ("low", "medium", "high"):
            diagnosis.risk_of_fix = "medium"

        self.record_activity(
            "diagnosed failure",
            diagnosis.root_cause[:200] + (" [ARCH ISSUE]" if diagnosis.architecture_issue else ""),
        )
        self._persist_failure(task, diagnosis)
        return AgentResult(
            ok=bool(diagnosis.root_cause),
            output=diagnosis.model_dump(mode="json"),
            confidence=diagnosis.confidence,
            evidence={
                "architecture_issue": diagnosis.architecture_issue,
                "fixes": len(diagnosis.candidate_fixes),
                "tool_loop": usage.get("tool_loop", {}),
            },
            cost_usd=usage["cost_usd"],
            tokens_in=usage["tokens_in"],
            tokens_out=usage["tokens_out"],
        )

    async def _ask_with_tools(self, **kwargs: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        """Re-run the failing command / inspect the code before diagnosing."""
        tools_enabled, max_iterations = _tool_settings(self)
        if tools_enabled:
            payload, usage = await self.ask_model_with_tools(
                tool_names=BUILDER_TOOLS, max_iterations=max_iterations, **kwargs
            )
            transcript = usage.get("tool_loop", {}).get("transcript") or []
            if transcript:
                self.record_activity(
                    "investigated with tools",
                    ", ".join(sorted({str(e.get("tool", "")) for e in transcript})),
                )
            return payload, usage
        return await self.ask_model(**kwargs)

    def _build_prompt(self, task, context: AgentContext) -> str:
        parts = [context.render()]
        if task is not None:
            verification = task.verification or {}
            if verification:
                parts.append("# FAILURE EVIDENCE\n" + str(verification)[:6000])
            if task.attempts_history:
                parts.append(
                    "# PRIOR ATTEMPTS (do not repeat)\n"
                    + "\n".join(
                        f"- attempt {a.attempt_number}: {a.outcome} | {a.failure_summary} | lesson: {a.lesson}"
                        for a in task.attempts_history
                    )
                )
        diff = self._safe_diff()
        if diff:
            parts.append("# RECENT CODE DIFF\n```diff\n" + diff[:12000] + "\n```")
        return "\n\n".join(parts)

    def _safe_diff(self) -> str:
        try:
            return self.deps.git.diff("HEAD")
        except Exception:
            return ""

    def _persist_failure(self, task, diagnosis: Diagnosis) -> None:
        record = FailureRecord(
            id=new_id("FAIL"),
            project_id=self.deps.store.project_id,
            task_id=task.id if task else "",
            agent=self.name,
            summary=diagnosis.root_cause,
            root_cause=diagnosis.root_cause,
            evidence={"files": diagnosis.files_affected, "evidence": diagnosis.evidence},
            lesson=diagnosis.lesson,
        )
        with contextlib.suppress(Exception):
            self.deps.store.save_failure(record)
        with contextlib.suppress(Exception):
            self.deps.workspace.add_failure(record.model_dump(mode="json"))

    def first_fix(self, diagnosis: dict[str, Any]) -> str:
        fixes = diagnosis.get("candidate_fixes") or []
        return as_text(fixes[0]) if fixes else ""
