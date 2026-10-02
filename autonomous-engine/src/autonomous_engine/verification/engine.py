"""Definition-of-Done engine and command-based verification (plan.md §13)."""

from __future__ import annotations

import contextlib
import fnmatch
import json
import re
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ..core.task import Task, now_iso
from ..runtime.permissions import CommandResult, ToolBox


class CheckStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    SKIP = "skip"
    UNKNOWN = "unknown"


class CommandEvidence(BaseModel):
    """Evidence produced by actually running something."""

    command: str
    returncode: int
    ok: bool
    stdout_tail: str = ""
    stderr_tail: str = ""
    duration_ms: float = 0.0

    @classmethod
    def from_result(cls, result: CommandResult) -> CommandEvidence:
        return cls(
            command=result.command,
            returncode=result.returncode,
            ok=result.ok,
            stdout_tail=result.stdout[-4000:],
            stderr_tail=result.stderr[-2000:],
            duration_ms=result.duration_ms,
        )


class DoDCheck(BaseModel):
    """One Definition-of-Done item evaluated against real evidence."""

    criterion: str
    kind: str  # command | file_exists | file_contains | file_absent | manual
    spec: str
    status: CheckStatus = CheckStatus.UNKNOWN
    detail: str = ""


class VerificationReport(BaseModel):
    task_id: str = ""
    passed: bool = False
    checks: list[DoDCheck] = Field(default_factory=list)
    evidence: list[CommandEvidence] = Field(default_factory=list)
    failures: list[str] = Field(default_factory=list)
    unverified: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=now_iso)

    def summary(self) -> str:
        return f"{sum(1 for c in self.checks if c.status == CheckStatus.PASS)}/{len(self.checks)} checks passed"

    def as_task_verification(self) -> dict[str, Any]:
        """Compact form persisted on the task itself."""
        return {
            "passed": self.passed,
            "summary": self.summary(),
            "failures": self.failures[:10],
            "commands": [
                {"command": e.command, "returncode": e.returncode, "ok": e.ok}
                for e in self.evidence
            ],
            "created_at": self.created_at,
        }


class DefinitionOfDone:
    """Parses Definition-of-Done lines into executable checks.

    Supported line syntaxes (all optional prefix ``[ ]`` markers allowed):

        pytest tests/
        run: npm test
        file exists: src/app.py
        file contains: "def main"
        file absent: secrets.txt
        (anything else) -> manual check, reported as UNKNOWN rather than PASS
    """

    _COMMANDS = {"run:", "command:", "cmd:", "exec:"}
    _FILE_EXISTS = {"file exists:", "file exists ", "exists:"}
    _FILE_CONTAINS = {"file contains:", "contains:"}
    _FILE_ABSENT = {"file absent:", "absent:", "no file:"}
    # A bare line is only treated as a command when it starts with a known
    # runner; the old "first token looks path-like" heuristic classified prose
    # such as "Handles empty input gracefully" as a shell command, which then
    # failed verification on a PermissionError. Explicit `run:` prefixes are
    # the escape hatch for anything else.
    _RUNNERS = {
        "bash", "bun", "cargo", "cmd", "deno", "dotnet", "go", "gradle", "java",
        "ls", "make", "mypy", "mvn", "node", "npm", "npx", "pip", "pnpm",
        "poetry", "py", "python", "python3", "pytest", "ruff", "sh", "test",
        "tox", "tsc", "uv", "yarn",
    }

    def __init__(self, criteria: list[str]):
        self.criteria = criteria

    def parse(self, line: str) -> DoDCheck:
        raw = line.strip()
        # strip checkbox markers
        cleaned = re.sub(r"^\[[ xX?]\]\s*", "", raw).strip()
        cleaned_lower = cleaned.lower()

        for prefix in self._COMMANDS:
            if cleaned_lower.startswith(prefix):
                return DoDCheck(criterion=raw, kind="command", spec=cleaned[len(prefix) :].strip())
        for prefix in self._FILE_EXISTS:
            if cleaned_lower.startswith(prefix):
                return DoDCheck(
                    criterion=raw, kind="file_exists", spec=cleaned[len(prefix) :].strip()
                )
        for prefix in self._FILE_CONTAINS:
            if cleaned_lower.startswith(prefix):
                return DoDCheck(
                    criterion=raw, kind="file_contains", spec=cleaned[len(prefix) :].strip()
                )
        for prefix in self._FILE_ABSENT:
            if cleaned_lower.startswith(prefix):
                return DoDCheck(
                    criterion=raw, kind="file_absent", spec=cleaned[len(prefix) :].strip()
                )

        first = cleaned.split(" ", 1)[0].lower() if cleaned else ""
        if first in self._RUNNERS:
            return DoDCheck(criterion=raw, kind="command", spec=cleaned)
        return DoDCheck(criterion=raw, kind="manual", spec=cleaned)

    def checks(self) -> list[DoDCheck]:
        return [self.parse(line) for line in self.criteria if line.strip()]


class VerificationEngine:
    """Executes a task's verification commands and Definition of Done."""

    def __init__(self, tools: ToolBox, *, command_timeout: int = 600):
        self.tools = tools
        self.command_timeout = command_timeout

    def verify_task(
        self, task: Task, *, extra_commands: list[str] | None = None
    ) -> VerificationReport:
        report = VerificationReport(task_id=task.id)

        # 1. task-level verification commands
        for command in list(task.verification_commands) + list(extra_commands or []):
            if not command.strip():
                continue
            try:
                result = self.tools.run_command(command, timeout=self.command_timeout)
            except PermissionError as exc:
                report.failures.append(f"command not permitted: {command} ({exc})")
                continue
            evidence = CommandEvidence.from_result(result)
            report.evidence.append(evidence)
            if not evidence.ok:
                report.failures.append(
                    f"command failed ({evidence.returncode}): {command}\n"
                    f"{(evidence.stderr_tail or evidence.stdout_tail)[-1500:]}"
                )

        # 2. Definition of Done checks
        dod = DefinitionOfDone(list(task.definition_of_done) + list(task.acceptance_criteria))
        for check in dod.checks():
            self._evaluate(check, report)
            report.checks.append(check)

        if not report.checks and not report.evidence:
            report.unverified.append("no machine-checkable criteria were defined for this task")
            report.passed = False
        else:
            hard_fail = bool(report.failures) or any(
                c.status in (CheckStatus.FAIL, CheckStatus.UNKNOWN) for c in report.checks
            )
            if not task.definition_of_done and not task.acceptance_criteria:
                # A task with commands but no DoD is judged on command results only.
                hard_fail = bool(report.failures)
            report.passed = not hard_fail
        return report

    def _evaluate(self, check: DoDCheck, report: VerificationReport) -> None:
        if check.kind == "manual":
            check.status = CheckStatus.UNKNOWN
            check.detail = "manual criterion requires a review agent verdict"
            report.unverified.append(check.criterion)
            return

        if check.kind == "command":
            try:
                result = self.tools.run_command(check.spec, timeout=self.command_timeout)
            except PermissionError as exc:
                check.status = CheckStatus.FAIL
                check.detail = f"command not permitted: {exc}"
                report.failures.append(check.criterion)
                return
            except Exception as exc:
                check.status = CheckStatus.FAIL
                check.detail = f"command error: {exc}"
                report.failures.append(check.criterion)
                return
            evidence = CommandEvidence.from_result(result)
            report.evidence.append(evidence)
            check.status = CheckStatus.PASS if evidence.ok else CheckStatus.FAIL
            check.detail = (evidence.stdout_tail or evidence.stderr_tail)[-800:]
            if check.status == CheckStatus.FAIL:
                report.failures.append(f"{check.criterion} -> exit {evidence.returncode}")
            return

        if check.kind in ("file_exists", "file_absent"):
            try:
                exists = self._path_exists(check.spec)
            except PermissionError as exc:
                check.status = CheckStatus.FAIL
                check.detail = str(exc)
                report.failures.append(check.criterion)
                return
            want_exists = check.kind == "file_exists"
            check.status = CheckStatus.PASS if exists == want_exists else CheckStatus.FAIL
            check.detail = f"{check.spec} exists={exists}"
            if check.status == CheckStatus.FAIL:
                report.failures.append(
                    f"{check.criterion} (exists={exists}, expected={want_exists})"
                )
            return

        if check.kind == "file_contains":
            # The path may be a Windows absolute path ("C:\repo\app.py"), so a
            # naive first-colon split would yield path "C". Split on the last
            # colon that still leaves a plausible path, by splitting on ": "
            # first and falling back to a drive-letter-aware split.
            spec = check.spec
            if re.match(r"^[A-Za-z]:", spec):
                # drive-letter path: the separator is the first colon after
                # the drive ("C:\path: needle")
                drive, _, rest = spec.partition(":")
                path, sep, needle = rest.partition(":")
                path = drive + ":" + path
            else:
                path, sep, needle = spec.partition(":")
            if not sep:
                check.status = CheckStatus.FAIL
                check.detail = (
                    "invalid file-contains syntax; expected 'file contains: <path>: <needle>'"
                )
                report.failures.append(f"{check.criterion} ({check.detail})")
                return
            needle = needle.strip().strip('"').strip("'")
            try:
                content = self.tools.read_file(path.strip())
            except Exception as exc:
                check.status = CheckStatus.FAIL
                check.detail = f"cannot read {path}: {exc}"
                report.failures.append(check.criterion)
                return
            found = needle in content
            check.status = CheckStatus.PASS if found else CheckStatus.FAIL
            check.detail = f"needle {needle!r} {'found' if found else 'not found'} in {path}"
            if not found:
                report.failures.append(check.criterion)

    def _path_exists(self, spec: str) -> bool:
        pattern = spec.strip()
        if not pattern:
            return False
        glob_chars = any(c in pattern for c in "*?[")
        if glob_chars:
            root = self.tools.work_root
            return any(root.glob(pattern))
        return (self.tools.work_root / pattern).exists()


def parse_json_field(text: str, field_name: str) -> Any:
    """Helper for agents: pull one field out of a JSON-ish model response."""
    try:
        data = json.loads(text)
        return data.get(field_name)
    except (json.JSONDecodeError, AttributeError):
        return None


def glob_matches_any(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, p) for p in patterns)


def parse_test_counts(output: str) -> dict[str, int]:
    """Best-effort test count extraction from common runners' output."""
    counts: dict[str, int] = {"total": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    patterns = [
        (r"(\d+)\s+passed", "passed"),
        (r"(\d+)\s+failed", "failed"),
        (r"(\d+)\s+error", "errors"),
        (r"(\d+)\s+skipped", "skipped"),
        (r"(\d+)\s+tests?", "total"),
    ]
    for pattern, key in patterns:
        match = re.search(pattern, output, re.IGNORECASE)
        if match:
            with contextlib.suppress(ValueError):
                counts[key] = int(match.group(1))
    if not counts["total"]:
        counts["total"] = counts["passed"] + counts["failed"] + counts["errors"]
    return counts


def write_report(path: Path, report: VerificationReport) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
