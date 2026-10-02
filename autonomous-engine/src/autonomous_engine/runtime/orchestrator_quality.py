"""Quality-gate methods of the orchestrator (verification spec §21-§22, §29).

Extracted from `orchestrator.py` (#6): everything that decides whether work
may be considered done — supervised approval, independent review, the
red-team pass, verification execution, evidence recording, and the QA gate.
The orchestrator inherits these as a mixin; `self` is the Orchestrator.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from ..agents.qa import gap_task
from ..core.events import EventTypes
from ..core.state_machine import TaskState
from ..core.store import AttemptRecord
from ..core.task import Task
from ..verification.engine import CheckStatus, VerificationEngine, VerificationReport
from ..verification.evidence import EvidenceRecord
from ..verification.gate import evaluate_quality_gate
from .risk import high_risk_commands

if TYPE_CHECKING:
    from .base import AgentResult
    from .orchestrator import CycleReport


class QualityGateMixin:
    """Verification / review / QA logic. Mixin over the Orchestrator."""

    def _needs_approval(self, task: Task) -> bool:
        if self.config.run_mode != "supervised" or task.id in self._approved_tasks:
            return False
        if task.risk == "high":
            return True
        # A task whose verification commands include HIGH-risk operations
        # needs a human to see them before they run (OpenHands ConfirmRisky).
        return bool(high_risk_commands(list(task.verification_commands)))

    async def _review_and_security(
        self,
        task: Task,
        attempt: AttemptRecord,
        report: VerificationReport,
        *,
        work_root: Path | None = None,
    ) -> CycleReport | None:
        """Independent review + red-team pass before completion.

        Shared by inline and worktree execution so a parallel task meets the
        same completion contract as a sequential one. Returns a failed
        CycleReport when a gate blocks the task, None to proceed.
        """
        repo = work_root if work_root is not None else self.repo_root
        if self._needs_review(task, report):
            self._transition(
                task, TaskState.REVIEWING, agent="orchestrator", note="independent review"
            )
            reviewer = self.reviewer
            reviewer.bind_tools(self.tools_for(reviewer.agent_class, work_root=repo))
            review_context = self.context_builder.build(
                task=task,
                role="reviewer",
                repo_root=repo,
                extra_sections={"change_diff": self._diff_section(None if repo is self.repo_root else repo)},
            )
            review_result = await self.runner.run(reviewer, task, review_context)
            self._record_budget(review_result, reviewer.name)
            review_findings = (review_result.output or {}).get("findings", [])
            review_blocking = [
                f for f in review_findings if f.get("severity") in ("critical", "high")
            ]
            self._msg_review_result(task, review_findings, len(review_blocking))
            self.workspace.write_json_artifact(
                f"verification/review_results/{task.id}.json", review_result.output or {}
            )
            findings = (review_result.output or {}).get("findings", [])
            blocking = [f for f in findings if f.get("severity") in ("critical", "high")]
            if (review_result.output or {}).get("blocking") and not blocking:
                blocking = [
                    {"severity": "high", "description": "reviewer marked the change blocking"}
                ]
            if blocking:
                return await self._fail_task(
                    task,
                    attempt,
                    f"review blocked by {len(blocking)} high-severity finding(s)",
                    agent="reviewer",
                    report=report,
                )
            if not review_result.ok:
                # A review without blocking findings is recorded evidence, not
                # a verdict: executable evidence decides completion.
                self.emit(
                    "review.non_blocking",
                    task_id=task.id,
                    findings=len(findings),
                    error=review_result.error[:200],
                )

        # ---- SECURITY: high-risk changes get a red-team pass before completion ----
        if task.risk == "high":
            security_result = await self._run_security(task, work_root=work_root)
            if security_result is not None and not security_result.ok:
                return await self._fail_task(
                    task,
                    attempt,
                    "security review found blocking findings; see the security report",
                    agent="security",
                    report=report,
                )
        return None

    async def _adjudicate_manual(
        self, task: Task, report: VerificationReport, *, work_root: Path | None = None
    ) -> bool:
        """Reviewer verdict on manual criteria; UNKNOWN is never auto-passed."""
        reviewer = self.reviewer
        reviewer.bind_tools(self.tools_for(reviewer.agent_class, work_root=work_root))
        manual = [c.criterion for c in report.checks if c.status == CheckStatus.UNKNOWN]
        context = self.context_builder.build(
            task=task,
            role="reviewer",
            repo_root=work_root or self.repo_root,
            extra_sections={
                "manual_criteria": (
                    "# MANUAL CRITERIA TO ADJUDICATE\n"
                    "Inspect the repository and decide whether each criterion holds.\n"
                    + "\n".join(f"- {c}" for c in manual)
                )
            },
        )
        result = await self.runner.run(reviewer, task, context)
        self._record_budget(result, reviewer.name)
        self.workspace.write_json_artifact(
            f"verification/review_results/{task.id}-manual.json", result.output or {}
        )
        if result.ok and (result.output or {}).get("approved"):
            for check in report.checks:
                if check.status == CheckStatus.UNKNOWN:
                    check.status = CheckStatus.PASS
                    check.detail = "approved by independent review (manual criterion)"
            self.emit("verification.manual_approved", task_id=task.id, criteria=len(manual))
            return True
        for check in report.checks:
            if check.status == CheckStatus.UNKNOWN:
                check.status = CheckStatus.FAIL
                check.detail = "reviewer could not approve this manual criterion"
            if check.status == CheckStatus.FAIL and not any(
                check.criterion in f for f in report.failures
            ):
                report.failures.append(f"{check.criterion} (not approved by review)")
        return False

    async def _run_security(self, task: Task, *, work_root: Path | None = None) -> AgentResult | None:
        """Red-team pass for high-risk work; static evidence plus model review."""
        repo = work_root if work_root is not None else self.repo_root
        security = self.agents["security"]
        security.bind_tools(self.tools_for(security.agent_class, work_root=repo))
        context = self.context_builder.build(task=task, role="security", repo_root=repo)
        result = await self.runner.run(security, task, context)
        self._record_budget(result, security.name)
        self.workspace.write_json_artifact(
            f"verification/review_results/{task.id}-security.json", result.output or {}
        )
        self.emit(
            "security.scan",
            task_id=task.id,
            ok=result.ok,
            findings=len((result.output or {}).get("findings", [])),
        )
        return result

    async def _run_qa_gate(self) -> bool:
        """Validate the finished graph against the original intent.

        Returns True when the gate found unmet requirements and reopened the
        graph with catch-up tasks; False when the objective is satisfied (or
        the QA verdict is not actionable).
        """
        self._qa_rounds += 1
        qa = self.agents["qa"]
        qa.bind_tools(self.tools_for(qa.agent_class))
        intent = self._current_intent()
        context = self.context_builder.build(
            task=None,
            role="qa",
            repo_root=self.repo_root,
            extra_sections={"intent": f"# ORIGINAL INTENT\n\n{intent.to_markdown()}"},
        )
        result = await self.runner.run(qa, None, context)
        self._record_budget(result, qa.name)
        if result.ok and result.output:
            gaps = [str(g) for g in result.output.get("gaps", []) or []]
            aligned = bool(result.output.get("aligned", True)) and not gaps
            summary = str(result.output.get("summary", ""))
        else:
            # No QA verdict available: fall back to the deterministic
            # coverage check so the gate never silently passes.
            check = qa.deterministic_check(intent, self.graph)
            gaps = check.gaps
            aligned = check.aligned
            summary = check.summary
        self.emit("qa.gate", round=self._qa_rounds, aligned=aligned, gaps=gaps[:10])
        self.workspace.write_json_artifact(
            "verification/qa_gate.json", {"aligned": aligned, "gaps": gaps, "summary": summary}
        )
        if aligned and not gaps:
            return False
        new_tasks = [gap_task(gap, self.graph) for gap in gaps[:5]]
        if not new_tasks:
            return False
        self.planner.replan(
            self.graph, add_tasks=new_tasks, reason=f"QA gate: unmet requirements ({summary[:120]})"
        )
        self._persist_graph()
        self.emit(EventTypes.TASK_CREATED, count=len(new_tasks), source="qa-gate")
        return True

    async def _verify(self, task: Task, *, work_root: Path) -> VerificationReport:
        risky = high_risk_commands(list(task.verification_commands))
        if risky:
            # Observability regardless of mode: the human (and the log) sees
            # exactly which high-risk operations verification will attempt.
            self.emit(
                "security.high_risk_command",
                task_id=task.id,
                commands=[c for c, _ in risky],
                reasons=[r for _, r in risky],
                approved=task.id in self._approved_tasks,
            )
        engine = VerificationEngine(
            self.tools_for("tester", work_root=work_root, approved=task.id in self._approved_tasks)
        )
        report = await asyncio.to_thread(engine.verify_task, task)
        for check in report.checks:
            if check.status == CheckStatus.UNKNOWN:
                self.emit(
                    "verification.unknown",
                    task_id=task.id,
                    criterion=check.criterion,
                    detail="a manual criterion is never counted as a pass",
                )
        if report.passed:
            self.emit(
                EventTypes.VERIFICATION_PASSED,
                task_id=task.id,
                summary=report.summary(),
                commands=[e.command for e in report.evidence],
            )
        else:
            self.emit(
                EventTypes.VERIFICATION_FAILED,
                task_id=task.id,
                summary=report.summary(),
                failures=report.failures[:5],
            )
        self._record_quality_gate(task, report)
        return report

    def _record_quality_gate(self, task: Task, report: VerificationReport) -> None:
        """Persist an evidence record and the deterministic gate verdict.

        The gate is the auditable answer to "what proves this task?" — same
        evidence + same policy always yields the same verdict, and an agent
        claim without executable evidence can never produce PASSED (§48/§49).
        """
        try:
            commit_sha = self.git.head_commit() if self.git.is_repo() else ""
            dirty = self.git.is_dirty()
        except Exception:
            commit_sha, dirty = "", False
        round_number = len(self.evidence_store.history(task.id)) + 1
        record, _classes = EvidenceRecord.from_report(
            report,
            task_id=task.id,
            round_number=round_number,
            commit_sha=commit_sha,
            workspace_dirty=dirty,
        )
        flaky = self.evidence_store.flaky_score(task.id)
        self.evidence_store.record(record)
        self.emit(
            EventTypes.EVIDENCE_RECORDED,
            task_id=task.id,
            evidence_id=record.evidence_id,
            commit=record.commit_sha[:12],
            passed=record.passed,
        )
        if flaky:
            self.emit(EventTypes.FLAKY_TEST_DETECTED, task_id=task.id)
        gate = evaluate_quality_gate(
            report,
            self.config.verification,
            task_declared_criteria=bool(task.definition_of_done or task.acceptance_criteria),
            flaky_history=flaky,
        )
        self.emit(
            EventTypes.QUALITY_GATE_EVALUATED,
            task_id=task.id,
            status=gate.status.value,
            checks_passed=gate.checks_passed,
            checks_total=gate.checks_total,
            missing_evidence=gate.missing_evidence[:3],
            commit=record.commit_sha[:12],
        )
