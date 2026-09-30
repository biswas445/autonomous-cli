"""Security agent (plan.md §5J, §57 red team).

Checks secrets, authentication, authorization, injection, unsafe dependencies,
data exposure, filesystem access, command execution, network access, and
configuration mistakes. It combines deterministic static checks (which produce
hard evidence) with a model pass over the diff.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ..core.security import find_high_entropy_strings, find_secrets, is_safe_relative_path
from ..runtime.base import Agent, AgentResult
from ..runtime.context import AgentContext
from .shared import as_text, confidence

# Deterministic static signals: (pattern, severity, message)
_STATIC_RULES: list[tuple[re.Pattern[str], str, str]] = [
    (
        re.compile(r"\beval\s*\(", re.IGNORECASE),
        "critical",
        "use of eval() can execute attacker-controlled code",
    ),
    (
        re.compile(r"\bexec\s*\(", re.IGNORECASE),
        "critical",
        "use of exec() can execute attacker-controlled code",
    ),
    (re.compile(r"shell\s*=\s*True"), "critical", "shell=True allows command injection"),
    (re.compile(r"os\.system\s*\("), "high", "os.system() invokes a shell"),
    (
        re.compile(r"subprocess\.[a-z_]+\([^)]*f[\"']", re.IGNORECASE),
        "high",
        "f-string in a subprocess call may inject arguments",
    ),
    (re.compile(r"pickle\.loads?\s*\("), "high", "pickle deserialisation executes arbitrary code"),
    (
        re.compile(r"yaml\.load\s*\((?![^)]*Safe)", re.IGNORECASE),
        "high",
        "yaml.load without SafeLoader can execute arbitrary code",
    ),
    (re.compile(r"verify\s*=\s*False"), "high", "TLS verification disabled"),
    (
        re.compile(r"random\.(random|randint)\s*\("),
        "medium",
        "non-cryptographic randomness used where secrets may be involved",
    ),
    (re.compile(r"md5|sha1", re.IGNORECASE), "low", "weak hash algorithm"),
    (re.compile(r"chmod\s*\(?\s*0?777"), "high", "world-writable permissions"),
]

_DANGEROUS_FILES = (
    ".env",
    "id_rsa",
    "id_ed25519",
    ".npmrc",
    ".pypirc",
    "credentials.json",
    "secrets.yaml",
)


class SecurityFinding(BaseModel):
    severity: str = "info"
    location: str = ""
    issue: str = ""
    remediation: str = ""


class SecurityReport(BaseModel):
    passed: bool = True
    findings: list[SecurityFinding] = Field(default_factory=list)
    scanned_files: int = 0
    confidence: float = 0.7

    def blocking(self) -> list[SecurityFinding]:
        return [f for f in self.findings if f.severity in ("critical", "high")]


class SecurityAgent(Agent):
    name = "security"
    role = "security"
    agent_class = "security"
    description = "Scans changes for security defects; part of the red team before release."

    SYSTEM = (
        "You are a security engineer performing a red-team review of a code change. "
        "Look for: leaked secrets, missing authentication/authorization, injection "
        "(SQL/command/template/path), unsafe dependencies, data exposure, unsafe filesystem "
        "access, unsafe command execution, unsafe network calls, and configuration mistakes. "
        "Only report real, specific issues; cite the file. Respond with a single JSON object."
    )
    SCHEMA_HINT = (
        "SecurityReport JSON with keys: passed (bool), findings[] where each is "
        "{severity: 'critical'|'high'|'medium'|'low'|'info', location, issue, remediation}, confidence"
    )

    SCAN_SUFFIXES = (
        ".py",
        ".ts",
        ".js",
        ".tsx",
        ".jsx",
        ".sh",
        ".yaml",
        ".yml",
        ".toml",
        ".json",
        ".env",
    )
    MAX_SCAN_FILES = 400
    MAX_SCAN_BYTES = 100_000

    async def run(self, task, context: AgentContext) -> AgentResult:
        static_report = self.static_scan()
        model_findings: list[SecurityFinding] = []
        model_confidence = 0.5
        cost = 0.0
        tokens_in = 0
        tokens_out = 0

        try:
            payload, usage = await self.ask_model(
                system=self.SYSTEM,
                prompt=context.render() or context.goal,
                schema_hint=self.SCHEMA_HINT,
                max_tokens=3000,
                security_sensitivity="high",
            )
            cost = usage["cost_usd"]
            tokens_in = usage["tokens_in"]
            tokens_out = usage["tokens_out"]
            model_confidence = confidence(payload.get("confidence"), 0.65)
            for item in payload.get("findings", []) or []:
                if not isinstance(item, dict):
                    continue
                severity = str(item.get("severity", "info")).lower()
                if severity not in ("critical", "high", "medium", "low", "info"):
                    severity = "info"
                model_findings.append(
                    SecurityFinding(
                        severity=severity,
                        location=as_text(item.get("location")),
                        issue=as_text(item.get("issue")),
                        remediation=as_text(item.get("remediation")),
                    )
                )
        except Exception:
            # Static evidence alone is still useful; say so honestly.
            model_confidence = 0.4

        report = SecurityReport(
            passed=not static_report.blocking()
            and not any(f.severity in ("critical", "high") for f in model_findings),
            findings=static_report.findings + model_findings,
            scanned_files=static_report.scanned_files,
            confidence=max(static_report.confidence, model_confidence)
            if static_report.scanned_files
            else model_confidence,
        )
        self.record_activity(
            "security scan", f"{len(report.findings)} findings, passed={report.passed}"
        )
        return AgentResult(
            ok=report.passed,
            output=report.model_dump(mode="json"),
            confidence=report.confidence,
            evidence={
                "blocking": len(report.blocking()),
                "scanned_files": report.scanned_files,
                "static_only": not model_findings,
            },
            cost_usd=cost,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )

    # ---- deterministic static analysis ----

    def static_scan(self) -> SecurityReport:
        findings: list[SecurityFinding] = []
        scanned = 0
        root = self.tools.work_root

        for path in self._iter_files(root):
            scanned += 1
            if scanned > self.MAX_SCAN_FILES:
                break
            rel = path.relative_to(root).as_posix()
            name = path.name.lower()

            if name in _DANGEROUS_FILES or name.endswith((".pem", ".key", ".p12")):
                findings.append(
                    SecurityFinding(
                        severity="high",
                        location=rel,
                        issue="sensitive file present in the repository",
                        remediation="remove it from version control and rotate any exposed credentials",
                    )
                )
                continue

            if not is_safe_relative_path(rel):
                findings.append(
                    SecurityFinding(
                        severity="high",
                        location=rel,
                        issue="path escapes the project root",
                        remediation="use only project-relative paths",
                    )
                )
                continue

            try:
                if path.stat().st_size > self.MAX_SCAN_BYTES:
                    continue
                content = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue

            for pattern in find_secrets(content):
                findings.append(
                    SecurityFinding(
                        severity="critical",
                        location=rel,
                        issue=f"possible hardcoded secret matching /{pattern}/",
                        remediation="move the value to an environment variable and rotate it",
                    )
                )

            for descriptor in find_high_entropy_strings(content):
                findings.append(
                    SecurityFinding(
                        severity="high",
                        location=rel,
                        issue=f"possible hardcoded credential: {descriptor}",
                        remediation=(
                            "if this is a secret, move it to the environment and rotate it; "
                            "if it is test data, mark it with 'example'/'dummy'"
                        ),
                    )
                )

            for pattern, severity, message in _STATIC_RULES:
                for match in re.finditer(pattern, content):
                    line_no = content[: match.start()].count("\n") + 1
                    findings.append(
                        SecurityFinding(
                            severity=severity,
                            location=f"{rel}:{line_no}",
                            issue=message,
                            remediation="remove or replace with a safe alternative",
                        )
                    )
                    break  # one hit per rule per file keeps reports readable

        return SecurityReport(
            passed=not any(f.severity in ("critical", "high") for f in findings),
            findings=findings,
            scanned_files=scanned,
            confidence=0.75,
        )

    def _iter_files(self, root: Path):
        skip_dirs = {
            ".git",
            "node_modules",
            "__pycache__",
            ".venv",
            "venv",
            "dist",
            "build",
            ".agents",
        }
        for path in root.rglob("*"):
            if any(part in skip_dirs for part in path.parts):
                continue
            if path.is_file() and path.suffix.lower() in self.SCAN_SUFFIXES:
                yield path

    def report_markdown(self, report: dict[str, Any]) -> str:
        lines = [f"# Security Report\n\npassed: {report.get('passed')}\n"]
        for finding in report.get("findings", []):
            lines.append(
                f"- **{finding.get('severity')}** `{finding.get('location')}`: "
                f"{finding.get('issue')} -> {finding.get('remediation')}"
            )
        if not report.get("findings"):
            lines.append("No findings.")
        return "\n".join(lines)
