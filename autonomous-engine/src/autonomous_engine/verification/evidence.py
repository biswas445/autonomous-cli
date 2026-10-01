"""Evidence records, staleness, and failure classification (verification spec
§3, §13, §14, §18, §16).

An agent saying "done" is not evidence. This store persists what was actually
executed, against which commit, with what outcome — so quality gates, the
Director, and the operator can distinguish "implemented" from "proven".
"""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ..core.task import now_iso
from .engine import CheckStatus, CommandEvidence, VerificationReport

EVIDENCE_SCHEMA_VERSION = 1


class FailureClass(StrEnum):
    """Deterministic failure classification (spec §18): what kind of failure
    the evidence shows — never a model opinion."""

    TEST_FAILURE = "TEST_FAILURE"
    BUILD_FAILURE = "BUILD_FAILURE"
    ENVIRONMENT_FAILURE = "ENVIRONMENT_FAILURE"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    TIMEOUT = "TIMEOUT"
    NO_EVIDENCE = "NO_EVIDENCE"
    UNKNOWN = "UNKNOWN_FAILURE"


_ENVIRONMENT_MARKERS = (
    "modulenotfounderror",
    "importerror",
    "no such file or directory",
    "is not recognized as an internal or external command",
    "command not found",
    "no module named",
    "connection refused",
    "econnrefused",
    "could not connect",
)
_BUILD_MARKERS = ("syntaxerror", "compileall", "error:c", "fatal error:", "build failed")
_PERMISSION_MARKERS = ("permission denied", "not permitted", "access is denied")
_TIMEOUT_MARKERS = ("timed out", "timeout", "deadline exceeded")


def classify_failure(evidence: CommandEvidence) -> FailureClass:
    """Classify one failed command's evidence deterministically."""
    if evidence.returncode == 0 and evidence.ok:
        return FailureClass.UNKNOWN  # not a failure; caller should not ask
    haystack = (evidence.stderr_tail + "\n" + evidence.stdout_tail).lower()
    if any(marker in haystack for marker in _PERMISSION_MARKERS):
        return FailureClass.PERMISSION_DENIED
    if any(marker in haystack for marker in _TIMEOUT_MARKERS):
        return FailureClass.TIMEOUT
    if any(marker in haystack for marker in _ENVIRONMENT_MARKERS):
        return FailureClass.ENVIRONMENT_FAILURE
    if any(marker in haystack for marker in _BUILD_MARKERS):
        return FailureClass.BUILD_FAILURE
    if "failed" in haystack or "assert" in haystack or "traceback" in haystack:
        return FailureClass.TEST_FAILURE
    return FailureClass.UNKNOWN


class EvidenceRecord(BaseModel):
    """One persisted verification run against a specific commit (spec §13)."""

    schema_version: int = EVIDENCE_SCHEMA_VERSION
    evidence_id: str
    task_id: str
    round: int = 1
    commit_sha: str = ""  # empty = not a git repo / unknown freshness
    workspace_dirty: bool = False
    passed: bool = False
    checks_total: int = 0
    checks_passed: int = 0
    commands: list[dict[str, Any]] = Field(default_factory=list)
    failure_classes: list[str] = Field(default_factory=list)
    summary: str = ""
    created_at: str = Field(default_factory=now_iso)

    def is_stale(self, current_sha: str) -> bool:
        """Evidence produced against commit A does not prove commit B."""
        if not self.commit_sha or not current_sha:
            return False  # freshness unknown, not provably stale
        return self.commit_sha != current_sha

    @classmethod
    def from_report(
        cls,
        report: VerificationReport,
        *,
        task_id: str,
        round_number: int,
        commit_sha: str = "",
        workspace_dirty: bool = False,
    ) -> tuple[EvidenceRecord, list[FailureClass]]:
        classes = [classify_failure(e) for e in report.evidence if not e.ok]
        return (
            cls(
                evidence_id=f"EV-{task_id}-{round_number}",
                task_id=task_id,
                round=round_number,
                commit_sha=commit_sha,
                workspace_dirty=workspace_dirty,
                passed=report.passed,
                checks_total=len(report.checks),
                checks_passed=sum(1 for c in report.checks if c.status == CheckStatus.PASS),
                commands=[
                    {
                        "command": e.command,
                        "returncode": e.returncode,
                        "ok": e.ok,
                        "duration_ms": e.duration_ms,
                        "classification": classify_failure(e).value if not e.ok else "",
                    }
                    for e in report.evidence
                ],
                failure_classes=sorted({c.value for c in classes}),
                summary=report.summary(),
            ),
            classes,
        )


class EvidenceStore:
    """Append-only JSONL evidence store under `.agents/verification/evidence/`.

    Large command output lives in the engine's tail fields and verification
    artifacts; the store keeps structured, queryable metadata (spec §13).
    """

    def __init__(self, state_dir: Path):
        self.root = state_dir / "verification" / "evidence"
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, task_id: str) -> Path:
        return self.root / f"{task_id}.jsonl"

    def record(self, record: EvidenceRecord) -> None:
        with self._path(record.task_id).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record.model_dump(mode="json"), ensure_ascii=False) + "\n")

    def history(self, task_id: str) -> list[EvidenceRecord]:
        path = self._path(task_id)
        if not path.is_file():
            return []
        records: list[EvidenceRecord] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                records.append(EvidenceRecord.model_validate(json.loads(line)))
            except (json.JSONDecodeError, ValueError):
                continue  # tolerate a torn line after a crash
        return records

    def latest(self, task_id: str) -> EvidenceRecord | None:
        records = self.history(task_id)
        return records[-1] if records else None

    def stale_tasks(self, task_ids: list[str], current_sha: str) -> list[tuple[str, str]]:
        """(task_id, evidence commit) for tasks whose latest evidence is stale."""
        stale: list[tuple[str, str]] = []
        for task_id in task_ids:
            latest = self.latest(task_id)
            if latest is not None and latest.is_stale(current_sha):
                stale.append((task_id, latest.commit_sha))
        return stale

    def flaky_score(self, task_id: str) -> bool:
        """True when pass/fail history flip-flopped (fail→pass→fail, spec §16).

        A flip-flopping sequence means the check is not deterministic; the
        quality policy decides how flakiness affects the gate, and this flag
        is the persisted evidence for that decision.
        """
        outcomes = [r.passed for r in self.history(task_id)]
        flips = sum(1 for a, b in zip(outcomes, outcomes[1:], strict=False) if a != b)
        return len(outcomes) >= 3 and flips >= 2
