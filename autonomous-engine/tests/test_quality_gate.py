"""Quality gate, evidence store, staleness, and failure classification
(verification spec §13–§18, §48, §49, §59)."""

from __future__ import annotations

from pathlib import Path

from autonomous_engine.core.config import PermissionClass, QualityGatePolicy
from autonomous_engine.core.task import Task
from autonomous_engine.runtime.permissions import ToolBox
from autonomous_engine.verification.engine import (
    CheckStatus,
    CommandEvidence,
    DoDCheck,
    VerificationEngine,
    VerificationReport,
)
from autonomous_engine.verification.evidence import (
    EvidenceRecord,
    EvidenceStore,
    FailureClass,
    classify_failure,
)
from autonomous_engine.verification.gate import (
    GateStatus,
    evaluate_quality_gate,
)


def _evidence(command: str, returncode: int, ok: bool, stderr: str = "") -> CommandEvidence:
    return CommandEvidence(
        command=command,
        returncode=returncode,
        ok=ok,
        stdout_tail="",
        stderr_tail=stderr,
        duration_ms=12.0,
    )


def _report(
    *, evidence: list[CommandEvidence] | None = None, checks: list[DoDCheck] | None = None
) -> VerificationReport:
    report = VerificationReport(task_id="TASK-1")
    report.evidence = evidence or []
    report.checks = checks or []
    return report


# ---- failure classification (deterministic, §18) -----------------------------


def test_classification_of_common_failures():
    assert classify_failure(_evidence("pytest -q", 1, False, "AssertionError: expected 2")) == (
        FailureClass.TEST_FAILURE
    )
    assert classify_failure(
        _evidence("pytest -q", 1, False, "ModuleNotFoundError: No module named 'httpx'")
    ) == (FailureClass.ENVIRONMENT_FAILURE)
    assert classify_failure(_evidence("make build", 2, False, "*** timed out after 300s")) == (
        FailureClass.TIMEOUT
    )
    assert classify_failure(_evidence("pytest -q", 1, False, "permission denied: secrets")) == (
        FailureClass.PERMISSION_DENIED
    )


# ---- quality gate (deterministic, §22, §48, §49) -----------------------------


def test_gate_passes_with_executable_evidence():
    report = _report(evidence=[_evidence("pytest -q", 0, True)])
    gate = evaluate_quality_gate(report)
    assert gate.status == GateStatus.PASSED
    assert gate.ok


def test_agent_claim_without_evidence_cannot_pass():
    """The false-positive firewall: no commands, no checks -> INSUFFICIENT."""
    report = _report()
    gate = evaluate_quality_gate(report)
    assert gate.status == GateStatus.INSUFFICIENT_EVIDENCE
    assert not gate.ok
    assert gate.missing_evidence


def test_gate_fails_on_failing_command():
    report = _report(evidence=[_evidence("pytest -q", 1, False, "1 failed")])
    gate = evaluate_quality_gate(report)
    assert gate.status == GateStatus.FAILED
    assert gate.blocking_reasons


def test_manual_check_is_never_a_pass():
    manual = DoDCheck(
        criterion="looks good", kind="manual", spec="looks good", status=CheckStatus.UNKNOWN
    )
    report = _report(evidence=[_evidence("pytest -q", 0, True)], checks=[manual])
    gate = evaluate_quality_gate(report)
    assert gate.status == GateStatus.PASSED_WITH_WARNINGS
    strict = evaluate_quality_gate(report, QualityGatePolicy(strict_manual_checks=True))
    assert strict.status == GateStatus.INSUFFICIENT_EVIDENCE


def test_declared_criteria_with_unknown_check_block_the_gate():
    manual = DoDCheck(
        criterion="documented", kind="manual", spec="documented", status=CheckStatus.UNKNOWN
    )
    report = _report(evidence=[_evidence("pytest -q", 0, True)], checks=[manual])
    gate = evaluate_quality_gate(report, task_declared_criteria=True)
    assert gate.status == GateStatus.INSUFFICIENT_EVIDENCE


def test_gate_is_deterministic():
    report = _report(evidence=[_evidence("pytest -q", 0, True)])
    one = evaluate_quality_gate(report)
    two = evaluate_quality_gate(report)
    assert one.status == two.status and one.policy_version == two.policy_version


# ---- evidence store (§13, §14) -----------------------------------------------


def test_evidence_store_roundtrip_and_staleness(tmp_path: Path):
    store = EvidenceStore(tmp_path)
    report = _report(evidence=[_evidence("pytest -q", 0, True)])
    record, _ = EvidenceRecord.from_report(
        report, task_id="TASK-9", round_number=1, commit_sha="a" * 40
    )
    store.record(record)
    history = store.history("TASK-9")
    assert len(history) == 1
    assert history[0].commit_sha == "a" * 40
    assert not history[0].is_stale("a" * 40)
    assert history[0].is_stale("b" * 40), "evidence from commit A cannot prove commit B"
    assert store.stale_tasks(["TASK-9"], "b" * 40) == [("TASK-9", "a" * 40)]
    assert store.stale_tasks(["TASK-9"], "a" * 40) == []


def test_evidence_store_survives_torn_line(tmp_path: Path):
    store = EvidenceStore(tmp_path)
    report = _report(evidence=[_evidence("pytest -q", 0, True)])
    store.record(EvidenceRecord.from_report(report, task_id="TASK-5", round_number=1)[0])
    path = tmp_path / "verification" / "evidence" / "TASK-5.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"evidence_id": "TORN")\n')  # malformed JSON line
    assert len(store.history("TASK-5")) == 1


def test_flaky_history_detection(tmp_path: Path):
    """fail -> pass -> fail is flaky (§16); a clean streak is not."""
    store = EvidenceStore(tmp_path)
    for round_number, passed in enumerate([False, True, False], start=1):
        report = _report(
            evidence=[_evidence("pytest -q", 0 if passed else 1, passed, "" if passed else "1 failed")]
        )
        report.passed = passed
        record, _ = EvidenceRecord.from_report(
            report, task_id="TASK-F", round_number=round_number, commit_sha="c" * 40
        )
        store.record(record)
    assert store.flaky_score("TASK-F")

    steady = EvidenceStore(tmp_path / "steady")
    for round_number in (1, 2, 3):
        report = _report(evidence=[_evidence("pytest -q", 0, True)])
        record, _ = EvidenceRecord.from_report(
            report, task_id="TASK-F", round_number=round_number, commit_sha="c" * 40
        )
        steady.record(record)
    assert not steady.flaky_score("TASK-F")


def test_flaky_history_flags_gate_warning_but_clean_run_passes():
    report = _report(evidence=[_evidence("pytest -q", 0, True)])
    gate = evaluate_quality_gate(report, flaky_history=True)
    assert gate.status == GateStatus.PASSED_WITH_WARNINGS
    assert any("flaky" in w for w in gate.warnings)


# ---- end-to-end through the real engine (§59 happy path) ---------------------


def test_engine_evidence_flows_into_the_gate(tmp_path: Path):
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    tools = ToolBox(
        work_root=tmp_path,
        permissions=PermissionClass(
            name="tester", read_repo=True, write_paths=["**"], run_commands=True
        ),
    )
    task = Task(
        id="TASK-77",
        title="demo",
        verification_commands=['python -c "import sys; sys.exit(0)"'],
        definition_of_done=["file exists: app.py"],
    )
    report = VerificationEngine(tools).verify_task(task)
    assert report.passed
    gate = evaluate_quality_gate(report, task_declared_criteria=True)
    assert gate.status == GateStatus.PASSED
    assert gate.commands_executed == 1
