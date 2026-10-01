"""Deterministic quality-gate evaluator (verification spec §22, §21, §48, §49).

quality_gate(task evidence, policy) -> GateResult

Same inputs + same policy version => same verdict, always. An LLM may interpret
evidence; it never decides the gate. The most serious failure class is a false
positive — reporting success without evidence — so ambiguous states resolve to
INSUFFICIENT_EVIDENCE / FAILED, never PASSED.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from ..core.config import QualityGatePolicy
from ..core.task import now_iso
from .engine import CheckStatus, VerificationReport

GATE_POLICY_VERSION = 1

__all__ = [
    "GATE_POLICY_VERSION",
    "GateResult",
    "GateStatus",
    "QualityGatePolicy",
    "evaluate_quality_gate",
]


class GateStatus(StrEnum):
    PASSED = "PASSED"
    PASSED_WITH_WARNINGS = "PASSED_WITH_WARNINGS"
    FAILED = "FAILED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    BLOCKED = "BLOCKED"


class GateResult(BaseModel):
    """What the deterministic evaluator produced (spec §22)."""

    status: GateStatus
    policy_version: int = GATE_POLICY_VERSION
    checks_total: int = 0
    checks_passed: int = 0
    checks_failed: int = 0
    commands_executed: int = 0
    missing_evidence: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    blocking_reasons: list[str] = Field(default_factory=list)
    evaluated_at: str = Field(default_factory=now_iso)

    @property
    def ok(self) -> bool:
        return self.status in (GateStatus.PASSED, GateStatus.PASSED_WITH_WARNINGS)

    def as_dict(self) -> dict[str, Any]:
        data = self.model_dump(mode="json")
        data["ok"] = self.ok
        return data


def evaluate_quality_gate(
    report: VerificationReport,
    policy: QualityGatePolicy | None = None,
    *,
    task_declared_criteria: bool = False,
    flaky_history: bool = False,
) -> GateResult:
    """Evaluate one verification report against the gate policy.

    `task_declared_criteria` is True when the task declared DoD/acceptance
    criteria — a commands-only task is judged on command results alone,
    matching the engine's long-standing contract.
    """
    policy = policy or QualityGatePolicy()
    checks_total = len(report.checks)
    checks_passed = sum(1 for c in report.checks if c.status == CheckStatus.PASS)
    checks_failed = sum(1 for c in report.checks if c.status == CheckStatus.FAIL)
    manual = [c for c in report.checks if c.status == CheckStatus.UNKNOWN]
    commands_executed = len(report.evidence)

    gate = GateResult(
        status=GateStatus.PASSED,
        policy_version=policy.schema_version,
        checks_total=checks_total,
        checks_passed=checks_passed,
        checks_failed=checks_failed,
        commands_executed=commands_executed,
    )

    # 1. Evidence existence: a claim without executed checks is not evidence
    #    (spec §48/§49). This is the false-positive firewall.
    if policy.require_executable_evidence and commands_executed == 0 and checks_total == 0:
        gate.status = GateStatus.INSUFFICIENT_EVIDENCE
        gate.missing_evidence.append(
            "no executable verification ran and no machine-checkable criteria exist"
        )
        return gate

    # 2. Manual criteria: UNKNOWN is never a pass.
    if manual:
        detail = f"{len(manual)} manual criterion/criteria lack executable evidence"
        if policy.strict_manual_checks or task_declared_criteria:
            gate.status = GateStatus.INSUFFICIENT_EVIDENCE
            gate.missing_evidence.append(detail)
        else:
            gate.warnings.append(detail)

    # 3. Flaky history (spec §16): flip-flopping outcomes demand a clean run.
    if flaky_history and policy.block_flaky_history and not report.failures:
        gate.warnings.append(
            "verification history flip-flopped (fail→pass→fail); treat as flaky until a clean re-run"
        )

    # 4. Hard failures decide last: any failure — in the engine's interpreted
    #    list or in the raw evidence — fails the gate.
    failed_commands = [e for e in report.evidence if not e.ok]
    if report.failures or checks_failed or failed_commands:
        gate.status = GateStatus.FAILED
        gate.blocking_reasons = list(report.failures[:10]) or [
            f"command failed (exit {e.returncode}): {e.command}" for e in failed_commands[:5]
        ]

    if gate.status == GateStatus.PASSED and gate.warnings:
        gate.status = GateStatus.PASSED_WITH_WARNINGS
    return gate
