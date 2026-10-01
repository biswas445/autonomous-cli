"""Tester agent (plan.md §5G, §56).

Responsible for producing *executable* verification. It runs the project's
test/build/lint/typecheck commands and reports structured evidence — never
"looks good". For requirements with no existing test, it generates one
(self-generated tests), so natural-language requirements become executable
verification.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

from ..models.base import Usage
from ..runtime.base import Agent, AgentResult
from ..runtime.context import AgentContext
from ..runtime.permissions import PermissionDenied
from ..verification.engine import CommandEvidence, parse_test_counts
from .shared import as_text

# Commands considered "the project's own verification" when a task names none.
DEFAULT_VERIFICATION_CANDIDATES: list[tuple[str, tuple[str, ...]]] = [
    ("pytest -q", ("pytest", "test", "tests")),
    ("python -m pytest -q", ("pytest",)),
    ("npm test", ("npm",)),
    ("npm run build", ("npm", "build")),
    ("python -m compileall -q .", ("python",)),
]


class GeneratedTest(BaseModel):
    path: str
    content: str
    rationale: str = ""


class TestReport(BaseModel):
    status: str = "unknown"  # passed | failed | error
    commands_run: list[dict[str, Any]] = Field(default_factory=list)
    total: int = 0
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    failure: str = ""
    suspected_root_cause: str = ""
    recommended_next_action: str = ""
    generated_tests: list[str] = Field(default_factory=list)
    confidence: float = 0.5


class TesterAgent(Agent):
    # not a pytest test class, despite the name
    __test__ = False

    name = "tester"
    role = "tester"
    agent_class = "tester"
    description = "Runs executable verification and reports structured evidence."

    SYSTEM = (
        "You are the Verification engineer of an autonomous engineering system. "
        "You do not fix code; you produce evidence. If a requirement has no executable "
        "test, write one. Report structured results, never impressions. "
        "Respond with a single JSON object, no prose."
    )
    SCHEMA_HINT = (
        "TestReport JSON with keys: status ('passed'|'failed'), commands_run[], total, passed, "
        "failed, errors, failure, suspected_root_cause, recommended_next_action, "
        "generated_tests[] where each is {path, content, rationale}, confidence"
    )

    async def run(self, task, context: AgentContext) -> AgentResult:
        commands = self._commands_to_run(task)
        executed: list[dict[str, Any]] = []
        failures: list[str] = []
        totals = {"total": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0}

        for command in commands:
            try:
                result = self.tools.run_command(command, timeout=900)
            except PermissionError as exc:
                failures.append(f"{command}: not permitted ({exc})")
                executed.append({"command": command, "error": str(exc)})
                continue
            evidence = CommandEvidence.from_result(result)
            executed.append(evidence.model_dump())
            counts = parse_test_counts(result.stdout + "\n" + result.stderr)
            for key in totals:
                totals[key] += counts.get(key, 0)
            if not result.ok:
                failures.append(
                    f"{command} exited {result.returncode}: "
                    f"{(result.stderr or result.stdout)[-1500:]}"
                )

        generated: list[str] = []
        generated_usage: Usage | None = None
        if task is not None and not self._has_existing_test_evidence(executed):
            generated, generated_usage = await self._generate_missing_test(task, context, failures)

        status = "passed" if executed and not failures else ("failed" if failures else "unknown")
        report = TestReport(
            status=status,
            commands_run=executed,
            total=totals["total"],
            passed=totals["passed"],
            failed=totals["failed"],
            errors=totals["errors"],
            skipped=totals["skipped"],
            failure="\n".join(failures)[:6000],
            generated_tests=generated,
            confidence=0.8 if executed else 0.3,
        )
        self.record_activity("ran verification", f"{len(executed)} commands, status={status}")
        return AgentResult(
            ok=status == "passed",
            output=report.model_dump(mode="json"),
            confidence=report.confidence,
            evidence={"commands": len(executed), "failures": len(failures)},
            artifacts=generated,
            cost_usd=generated_usage.cost_usd if generated_usage else 0.0,
            tokens_in=generated_usage.tokens_in if generated_usage else 0,
            tokens_out=generated_usage.tokens_out if generated_usage else 0,
        )

    # ---- helpers ----

    def _commands_to_run(self, task) -> list[str]:
        if task is not None and task.verification_commands:
            return list(task.verification_commands)
        # fall back to the project's conventional verification commands
        try:
            entries = set(self.tools.list_dir("."))
        except Exception:
            entries = set()
        commands: list[str] = []
        for command, markers in DEFAULT_VERIFICATION_CANDIDATES:
            if any(marker in entries for marker in markers):
                commands.append(command)
        if not commands:
            commands = ["python -m compileall -q ."]
        return commands[:4]

    def _has_existing_test_evidence(self, executed: list[dict[str, Any]]) -> bool:
        for item in executed:
            if item.get("returncode") is not None and not item.get("error"):
                return True
        return False

    async def _generate_missing_test(
        self, task, context: AgentContext, failures: list[str]
    ) -> tuple[list[str], Usage | None]:
        """Self-generated tests (plan.md §56): make the requirement executable.

        Returns the written paths and the model usage — the caller adds the
        usage to the AgentResult so this real spend reaches the budget.
        """
        try:
            payload, usage = await self.ask_model(
                system=self.SYSTEM,
                prompt=(
                    context.render()
                    + "\n\nNo executable test covers this task. Write the smallest pytest "
                    "test file that verifies its acceptance criteria, and return it as a "
                    "generated test."
                ),
                schema_hint=(
                    "JSON with key generated_tests[] where each item is {path, content, rationale}"
                ),
                max_tokens=3000,
            )
        except Exception:
            return [], None

        written: list[str] = []
        for item in payload.get("generated_tests", []) or []:
            if not isinstance(item, dict):
                continue
            path = as_text(item.get("path")).strip()
            content = item.get("content")
            content = content if isinstance(content, str) else as_text(content)
            if not path or not content:
                continue
            if not re.search(r"\.py$", path) and "test" not in path.lower():
                continue
            try:
                self.tools.write_file(path, content)
                written.append(path)
            except PermissionDenied:
                continue
            except Exception as exc:
                failures.append(f"generated test {path} could not be written: {exc}")
        if written:
            self.record_activity("generated tests", ", ".join(written))
        return written, usage

    def summarise(self, result: AgentResult) -> str:
        """Human-readable, evidence-first summary for the CLI."""
        payload = result.output or {}
        lines = [f"status: {payload.get('status', 'unknown')}"]
        for command in payload.get("commands_run", []):
            if command.get("error"):
                lines.append(f"  ! {command.get('command')}: {command['error']}")
            else:
                mark = "ok" if command.get("ok") else "FAILED"
                lines.append(
                    f"  [{mark}] {command.get('command')} (exit {command.get('returncode')})"
                )
        if payload.get("failure"):
            lines.append(f"  failure: {str(payload['failure'])[:600]}")
        if payload.get("generated_tests"):
            lines.append(f"  generated tests: {', '.join(payload['generated_tests'])}")
        return "\n".join(lines)

    def commands_from_report(self, report: dict[str, Any]) -> list[str]:
        return [
            str(c.get("command", "")) for c in report.get("commands_run", []) if c.get("command")
        ]
