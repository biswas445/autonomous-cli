"""Architecture Review Board (plan.md §39) with model disagreement (§20).

For major changes — a task that exhausted its attempts, or a director-declared
architecture problem — the board convenes:

    Researcher evidence  +  Architect options  +  Security constraints
                        ↓
          independent proposals (model disagreement: N calls)
                        ↓
                Decision Judge (majority or deciding vote)
                        ↓
        recorded Decision + applied action (replan or keep)

The board *advises*; the orchestrator still decides and applies. Everything is
recorded as a DecisionRecord so later agents are bound by it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..core.store import DecisionRecord
from ..core.task import Task, TaskGraph, new_id
from ..models.base import CompletionRequest, ModelError
from ..models.router import ModelRouter, extract_json

VERDICTS = ("keep_current", "revise_plan", "redesign_module")


@dataclass
class BoardEvidence:
    reason: str
    failing_task_ids: list[str] = field(default_factory=list)
    failure_lessons: list[str] = field(default_factory=list)
    security_findings: list[str] = field(default_factory=list)
    researcher_findings: str = ""


@dataclass
class BoardDecision:
    verdict: str
    rationale: str
    confidence: float
    agreed_by: int
    proposals: int
    judge_used: bool = False
    evidence: dict[str, Any] = field(default_factory=dict)


class ModelDisagreement:
    """Ask N independent proposals and let a judge resolve disagreement (§20).

    Agreement → the shared answer. Disagreement → one extra judge call that
    sees all candidates and picks. With no model at all, callers fall back to
    their deterministic path.
    """

    def __init__(self, router: ModelRouter, *, proposals: int = 2):
        self.router = router
        self.proposals = proposals

    async def decide(
        self,
        *,
        system: str,
        prompt: str,
        schema_hint: str,
        key: str,
        choices: list[str],
        role: str = "director",
    ) -> tuple[dict[str, Any], bool]:
        """Returns (verdict payload, judge_was_used)."""
        candidates: list[dict[str, Any]] = []
        for _ in range(self.proposals):
            try:
                response = await self.router.complete(
                    CompletionRequest(
                        system=system, prompt=prompt, schema_hint=schema_hint, temperature=0.4
                    ),
                    role,
                )
                candidates.append(extract_json(response.text))
            except ModelError:
                continue

        valid = [c for c in candidates if str(c.get(key, "")).strip() in choices]
        if not valid:
            raise ModelError("review board produced no valid proposal")
        votes = [str(c[key]) for c in valid]
        if len(set(votes)) == 1:
            return valid[0], False

        judge_prompt = (
            f"{prompt}\n\n# CANDIDATE PROPOSALS (they disagree)\n"
            + "\n".join(f"- {json.dumps(c, default=str)[:800]}" for c in valid)
            + f"\n\nDecide which '{key}' is correct and return the same JSON schema."
        )
        response = await self.router.complete(
            CompletionRequest(
                system=system + " You are the deciding judge.",
                prompt=judge_prompt,
                schema_hint=schema_hint,
                temperature=0.1,
            ),
            role,
        )
        verdict = extract_json(response.text)
        if str(verdict.get(key, "")).strip() not in choices:
            raise ModelError("judge returned an invalid verdict")
        return verdict, True


class ReviewBoard:
    """Convenes the board for one architecture question."""

    def __init__(self, orchestrator: Any):
        # Typed as Any to avoid an import cycle; this is the Orchestrator.
        self.orch = orchestrator

    async def convene(
        self, reason: str, failing_task_ids: list[str], graph: TaskGraph
    ) -> BoardDecision:
        orch = self.orch
        evidence = self._gather_evidence(reason, failing_task_ids)
        await self._research_evidence(evidence, graph)
        self._security_evidence(evidence)

        system = (
            "You are the Architecture Review Board of an autonomous engineering system. "
            "Given the evidence, decide: keep_current (the architecture stands; the plan "
            "needs better execution), revise_plan (the plan is wrong; rebuild the task "
            "graph), or redesign_module (a specific module is wrong; escalate for a "
            "focused redesign). Respond with a single JSON object."
        )
        schema_hint = (
            "BoardVerdict JSON with keys: decision ('keep_current'|'revise_plan'|"
            "'redesign_module'), rationale, confidence"
        )
        prompt = self._prompt(evidence, graph)

        deterministic = self._deterministic_verdict(evidence)
        try:
            payload, judge_used = await ModelDisagreement(orch.router, proposals=2).decide(
                system=system,
                prompt=prompt,
                schema_hint=schema_hint,
                key="decision",
                choices=list(VERDICTS),
            )
        except ModelError:
            payload, judge_used = deterministic.model_dump(mode="json"), False

        decision = BoardDecision(
            verdict=str(payload.get("decision", "keep_current")),
            rationale=str(payload.get("rationale", ""))[:800],
            confidence=float(payload.get("confidence", 0.5)),
            agreed_by=1 if judge_used else 2,
            proposals=2,
            judge_used=judge_used,
            evidence={
                "failing_tasks": evidence.failing_task_ids,
                "lessons": evidence.failure_lessons[:5],
                "security_findings": evidence.security_findings[:5],
                "research": evidence.researcher_findings[:300],
            },
        )
        self._record(decision, reason)
        return decision

    # ---- evidence gathering ----

    def _gather_evidence(self, reason: str, failing_task_ids: list[str]) -> BoardEvidence:
        lessons = [
            f"{f.get('summary', '')}: {f.get('lesson', '')}".strip(" :")
            for f in self.orch.workspace.load_failures()
        ]
        return BoardEvidence(
            reason=reason, failing_task_ids=failing_task_ids, failure_lessons=lessons
        )

    async def _research_evidence(self, evidence: BoardEvidence, graph: TaskGraph) -> None:
        researcher = self.orch.agents["researcher"]
        researcher.bind_tools(self.orch.tools_for(researcher.agent_class))
        question = (
            f"Architecture question: {evidence.reason}. "
            "What are the viable approaches and their trade-offs?"
        )
        probe = Task(title=question, role="research")
        context = self.orch.context_builder.build(
            task=None, role="researcher", repo_root=self.orch.repo_root
        )
        result = await self.orch.runner.run(researcher, probe, context)
        self.orch._record_budget(result, researcher.name)
        if result.ok:
            evidence.researcher_findings = str((result.output or {}).get("answer", ""))[:1200]

    def _security_evidence(self, evidence: BoardEvidence) -> None:
        security = self.orch.agents["security"]
        security.bind_tools(self.orch.tools_for(security.agent_class))
        try:
            report = security.static_scan()
        except Exception:
            return
        evidence.security_findings = [f"{f.location}: {f.issue}" for f in report.blocking()[:10]]

    def _prompt(self, evidence: BoardEvidence, graph: TaskGraph) -> str:
        progress = graph.progress()
        return (
            f"# REASON FOR REVIEW\n{evidence.reason}\n\n"
            f"# PROJECT STATE\n{progress}\n"
            f"# FAILING TASKS\n{', '.join(evidence.failing_task_ids) or 'none'}\n"
            "# FAILURE LESSONS (institutional memory)\n"
            + "\n".join(f"- {lesson}" for lesson in evidence.failure_lessons[-8:] or ["- none"])
            + "\n# SECURITY CONSTRAINTS\n"
            + "\n".join(f"- {f}" for f in evidence.security_findings or ["- none recorded"])
            + "\n# RESEARCH FINDINGS\n"
            + (evidence.researcher_findings or "none available")
            + "\n\nDecide the verdict."
        )

    def _deterministic_verdict(self, evidence: BoardEvidence) -> Any:
        """Offline fallback: failure lessons drive the verdict."""
        from pydantic import BaseModel

        class BoardVerdict(BaseModel):
            decision: str = "keep_current"
            rationale: str = ""
            confidence: float = 0.5

        architectural = any(
            word in lesson.lower()
            for lesson in evidence.failure_lessons
            for word in ("architecture", "redesign", "wrong abstraction", "incompatible")
        )
        if architectural or len(evidence.failing_task_ids) > 1:
            return BoardVerdict(
                decision="revise_plan",
                rationale="recorded failure lessons indicate the plan, not the execution, is wrong",
                confidence=0.5,
            )
        return BoardVerdict(
            decision="keep_current",
            rationale="no architectural evidence against the current plan",
            confidence=0.5,
        )

    def _record(self, decision: BoardDecision, reason: str) -> None:
        orch = self.orch
        record = DecisionRecord(
            id=new_id("BOARD"),
            project_id=orch.store.project_id,
            title=f"Architecture Review Board: {decision.verdict}",
            body=(
                f"reason: {reason}\n\nrationale: {decision.rationale}\n\n"
                f"proposals: {decision.proposals}, judge used: {decision.judge_used}"
            ),
            status="accepted",
            confidence=decision.confidence,
            evidence=[json.dumps(decision.evidence, default=str)[:1500]],
            decided_by="review-board",
        )
        orch.store.save_decision(record)
        orch.workspace.write_json_artifact(
            f"architecture/review_board_{record.id}.json", decision.__dict__ | {"id": record.id}
        )
        # Append (never overwrite) to the binding decisions document.
        decisions_path = orch.workspace.paths.architecture / "decisions.md"
        try:
            existing = (
                decisions_path.read_text(encoding="utf-8") if decisions_path.is_file() else ""
            )
            entry = (
                f"\n## {record.title}\n"
                f"- Reason: {reason[:200]}\n"
                f"- Rationale: {decision.rationale[:400]}\n"
                f"- Confidence: {decision.confidence:.2f} "
                f"({decision.agreed_by}/{decision.proposals} proposals agreed"
                f"{', judge decided' if decision.judge_used else ''})\n"
            )
            decisions_path.parent.mkdir(parents=True, exist_ok=True)
            decisions_path.write_text(existing + entry, encoding="utf-8")
        except OSError:
            pass
        orch.emit(
            "review_board.decided",
            verdict=decision.verdict,
            confidence=decision.confidence,
            judge_used=decision.judge_used,
        )
