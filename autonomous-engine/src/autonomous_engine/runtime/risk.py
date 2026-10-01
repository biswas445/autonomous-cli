"""Command risk analysis, ported from OpenHands' security analyzer.

OpenHands evaluates every action *before* execution and attaches a risk level
(`SecurityRisk`: LOW / MEDIUM / HIGH / UNKNOWN, see
`openhands/sdk/security/risk.py`), then a confirmation policy decides whether
a human must approve it (`ConfirmRisky(threshold=HIGH, confirm_unknown=True)`).

This module ports that model with deterministic invariants over the command
line — reviewed facts like "`git push --force` rewrites published history"
encoded once, checked on every execution. It is defense in depth on top of
the permission-class allowlist, not a replacement: the allowlist decides what
an agent *may* run; the risk analyzer decides what no agent may run without
explicit human approval, even if the allowlist is too broad.

Honest limitation, stated up front: invariant matching reads the command
line, so a hostile `python -c "<arbitrary code>"` cannot be classified. That
class of risk is what the Docker sandbox backend exists for; this module's
job is the commonplace destructive operation a language model emits by
accident.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

_RISK_ORDER = {"LOW": 1, "MEDIUM": 2, "HIGH": 3, "UNKNOWN": 0}


class CommandRisk(StrEnum):
    """Risk levels, ordered the way OpenHands orders them."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    UNKNOWN = "UNKNOWN"  # no invariant matched and the verb is not known-safe


def at_least(risk: CommandRisk | str, threshold: CommandRisk | str) -> bool:
    """True when `risk` is at or above `threshold` in the OpenHands ordering."""
    return (
        _RISK_ORDER[CommandRisk(str(risk)).value] >= _RISK_ORDER[CommandRisk(str(threshold)).value]
    )


# (pattern, risk, reason). Ordered strongest first so the first match wins.
_INVARIANTS: tuple[tuple[re.Pattern[str], CommandRisk, str], ...] = (
    # --- destructive to the machine or the wider world (HIGH) ---
    (
        re.compile(r"\brm\s+(-[a-zA-Z]*\s+)*-[a-zA-Z]*[rR][a-zA-Z]*f|rm\s+-fr"),
        CommandRisk.HIGH,
        "recursive force delete",
    ),
    (
        re.compile(r"\brm\s+(-[a-zA-Z]+\s+)*/(\s|$)|\brm\s+(-[a-zA-Z]+\s+)*~(\s|$)"),
        CommandRisk.HIGH,
        "delete of a root or home directory",
    ),
    (re.compile(r"\bmkfs(\.\w+)?\b"), CommandRisk.HIGH, "filesystem format"),
    (re.compile(r"\bdd\s+.*\bof=/dev/"), CommandRisk.HIGH, "raw write to a device"),
    (re.compile(r":\(\)\s*\{.*\};\s*:"), CommandRisk.HIGH, "fork bomb"),
    (
        re.compile(r"\bgit\s+push\b.*(--force|-f)\b"),
        CommandRisk.HIGH,
        "forcing a push rewrites published history",
    ),
    (re.compile(r"\bgit\s+filter-(branch|repo)\b"), CommandRisk.HIGH, "history rewrite"),
    (re.compile(r"\b(npm|pnpm|yarn)\s+publish\b"), CommandRisk.HIGH, "package publication"),
    (re.compile(r"\btwine\s+upload\b"), CommandRisk.HIGH, "package publication"),
    (re.compile(r"\bcargo\s+publish\b"), CommandRisk.HIGH, "package publication"),
    (re.compile(r"\bchmod\s+(-R\s+)?0?777\b"), CommandRisk.HIGH, "world-writable permissions"),
    (
        re.compile(r"\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?(ba)?sh\b"),
        CommandRisk.HIGH,
        "piping a download into a shell",
    ),
    (
        re.compile(r"\b(DROP|TRUNCATE)\s+(TABLE|DATABASE|SCHEMA)\b", re.IGNORECASE),
        CommandRisk.HIGH,
        "destructive SQL",
    ),
    (
        re.compile(r"\b(DELETE|UPDATE)\s+FROM?\s+\w+\s*(;|$)", re.IGNORECASE),
        CommandRisk.HIGH,
        "unbounded SQL delete/update (no WHERE clause)",
    ),
    (re.compile(r"\bkubectl\s+delete\b"), CommandRisk.HIGH, "cluster resource deletion"),
    (re.compile(r"\baws\s+s3\s+rm\b.*--recursive"), CommandRisk.HIGH, "bulk object deletion"),
    (
        re.compile(r"\bterraform\s+(destroy|apply\s+-auto-approve)\b"),
        CommandRisk.HIGH,
        "infrastructure destruction",
    ),
    (re.compile(r"\bdocker\s+system\s+prune\b.*-a"), CommandRisk.HIGH, "docker-wide prune"),
    (re.compile(r"\b(shutdown|reboot|halt|poweroff)\b"), CommandRisk.HIGH, "machine power state"),
    (
        re.compile(r"\bformat\s+[a-zA-Z]:", re.IGNORECASE),
        CommandRisk.HIGH,
        "drive format",
    ),
    # cmd.exe builtins and PowerShell: the flag must follow whitespace, so a
    # path like `del build/something` is not mistaken for a /s switch.
    (
        re.compile(r"\b(rd|rmdir)\b[^\n]*\s/s\b", re.IGNORECASE),
        CommandRisk.HIGH,
        "recursive directory removal (cmd rd /s)",
    ),
    (
        re.compile(r"\bdel\b[^\n]*\s/s\b", re.IGNORECASE),
        CommandRisk.HIGH,
        "recursive file deletion (cmd del /s)",
    ),
    (
        re.compile(r"\bRemove-Item\b[^\n]*\s-(Recurse|r)\b[^\n]*\s-(Force|f)\b", re.IGNORECASE),
        CommandRisk.HIGH,
        "recursive force delete",
    ),
    # --- risky but recoverable or scoped (MEDIUM) ---
    (re.compile(r"\bgit\s+reset\s+--hard\b"), CommandRisk.MEDIUM, "discards uncommitted work"),
    (
        re.compile(r"\bgit\s+clean\b.*-[a-zA-Z]*[xdf]"),
        CommandRisk.MEDIUM,
        "removes untracked files",
    ),
    (re.compile(r"\brm\b\s+-[a-zA-Z]*[rR]\b"), CommandRisk.MEDIUM, "recursive delete"),
    (
        re.compile(r"\bpip\s+uninstall\b|\bnpm\s+(uninstall|remove)\b"),
        CommandRisk.MEDIUM,
        "dependency removal",
    ),
    (re.compile(r"\bdocker\s+(rm|rmi|volume\s+rm)\b.*-f"), CommandRisk.MEDIUM, "forced deletion"),
    (re.compile(r"\bkill\s+-9\b"), CommandRisk.MEDIUM, "forced process termination"),
    (re.compile(r"\bchmod\s+-R\b"), CommandRisk.MEDIUM, "recursive permission change"),
    (
        re.compile(r"\bgit\s+checkout\s+--\s+\.|\bgit\s+restore\s+\.\s*$"),
        CommandRisk.MEDIUM,
        "discards working-tree changes",
    ),
    (re.compile(r"\bhistory\s+-c\b|\btruncate\b"), CommandRisk.MEDIUM, "data truncation"),
)

# Verbs whose worst case is bounded by the permission class and whose use is
# fully routine in development: verification, inspection, builds.
_SAFE_VERBS = {
    "python",
    "python3",
    "pytest",
    "ruff",
    "mypy",
    "uv",
    "pip",
    "npm",
    "npx",
    "pnpm",
    "yarn",
    "node",
    "tsc",
    "go",
    "cargo",
    "make",
    "cmake",
    "gcc",
    "g++",
    "clang",
    "java",
    "javac",
    "dotnet",
    "gradle",
    "mvn",
    "deno",
    "bun",
    "poetry",
    "hatch",
    "uvicorn",
    "flask",
    "ls",
    "cat",
    "head",
    "tail",
    "grep",
    "rg",
    "findstr",
    "wc",
    "sort",
    "diff",
    "echo",
    "pwd",
    "which",
    "where",
    "env",
    "date",
}
_SAFE_GIT_SUBCOMMANDS = {
    "status",
    "diff",
    "log",
    "show",
    "rev-parse",
    "branch",
    "add",
    "commit",
    "checkout",
    "switch",
    "stash",
    "tag",
    "describe",
    "shortlog",
    "blame",
    "ls-files",
    "worktree",
    "merge",
    "rebase",
    "cherry-pick",
    "init",
    "config",
    "fetch",
    "pull",
}

_PIPELINE_SPLIT = re.compile(r"[|;]|&&|\|\|")


@dataclass(frozen=True)
class RiskAssessment:
    risk: CommandRisk
    reason: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"risk": self.risk.value, "reason": self.reason}


def classify_command(command: str) -> RiskAssessment:
    """Classify one command line (strongest risk across pipeline segments)."""
    text = (command or "").strip()
    if not text:
        return RiskAssessment(CommandRisk.UNKNOWN, "empty command")

    # Whole-line invariants first: pipeline detection ("curl ... | sh")
    # spans separators that segmentation would remove.
    for pattern, risk, reason in _INVARIANTS:
        if pattern.search(text):
            return RiskAssessment(risk, reason)

    worst = RiskAssessment(CommandRisk.LOW)
    segments = [seg.strip() for seg in _PIPELINE_SPLIT.split(text) if seg.strip()]
    for segment in segments or [text]:
        assessment = _classify_segment(segment)
        if _RISK_ORDER[assessment.risk.value] > _RISK_ORDER[worst.risk.value] or (
            not worst.reason and assessment.reason
        ):
            worst = assessment
    return worst


def _rm_assessment(tokens: list[str]) -> RiskAssessment | None:
    """Flag-set analysis for `rm`: `-f -r`, `--force --recursive` and `-rf`
    are equally destructive, but a combined-flag regex only catches one
    spelling. Returns None when no destructive flag combination is present
    (plain `rm` then falls through to the UNKNOWN verb dispatch).
    """
    flags: set[str] = set()
    operands: list[str] = []
    for token in tokens[1:]:
        if token == "--":
            continue
        if token.startswith("--"):
            flags.add(token[2:].split("=", 1)[0].lower())
        elif token.startswith("-") and len(token) > 1:
            flags.update(token[1:])
        else:
            operands.append(token)
    recursive = bool(flags & {"r", "R", "recursive"})
    force = bool(flags & {"f", "force"})
    if recursive and force:
        return RiskAssessment(CommandRisk.HIGH, "recursive force delete")
    if any(op in ("/", "/*", "~", "~/*", "*/") for op in operands):
        return RiskAssessment(CommandRisk.HIGH, "delete of a root or home directory")
    if recursive:
        return RiskAssessment(CommandRisk.MEDIUM, "recursive delete")
    return None


def _classify_segment(segment: str) -> RiskAssessment:
    tokens = segment.split()
    verb = tokens[0].lower() if tokens else ""
    verb = verb.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]  # strip paths
    if verb.endswith(".exe"):
        verb = verb[:-4]
    if verb == "rm":
        assessment = _rm_assessment(tokens)
        if assessment is not None:
            return assessment

    for pattern, risk, reason in _INVARIANTS:
        if pattern.search(segment):
            return RiskAssessment(risk, reason)

    if verb == "git":
        subcommand = tokens[1].lower() if len(tokens) > 1 else ""
        if subcommand in _SAFE_GIT_SUBCOMMANDS:
            return RiskAssessment(CommandRisk.LOW, f"git {subcommand} is a routine subcommand")
        return RiskAssessment(CommandRisk.UNKNOWN, f"unclassified git subcommand: {subcommand}")
    if verb in _SAFE_VERBS:
        return RiskAssessment(CommandRisk.LOW, f"{verb} is a routine development command")
    return RiskAssessment(CommandRisk.UNKNOWN, f"unrecognised command: {verb or '?'}")


def should_require_confirmation(
    risk: CommandRisk,
    *,
    run_mode: str = "autonomous",
    confirm_unknown: bool = False,
    threshold: CommandRisk | str = CommandRisk.HIGH,
) -> bool:
    """OpenHands' `ConfirmRisky` policy, expressed for this runtime.

    HIGH always requires a human. UNKNOWN requires one only when the caller
    asks for it (`confirm_unknown`) — the permission-class allowlist already
    vetted which verbs may appear at all. Supervised mode lowers the
    threshold to MEDIUM: risky-but-recoverable work is exactly what an
    operator wants to see first.
    """
    threshold = CommandRisk(str(threshold))
    if risk == CommandRisk.UNKNOWN:
        return confirm_unknown
    effective = CommandRisk.MEDIUM if run_mode == "supervised" else threshold
    return _RISK_ORDER[risk.value] >= _RISK_ORDER[effective.value]


def high_risk_commands(commands: list[str]) -> list[tuple[str, str]]:
    """(command, reason) for every HIGH-risk command in a list."""
    found: list[tuple[str, str]] = []
    for command in commands:
        assessment = classify_command(command)
        if assessment.risk == CommandRisk.HIGH:
            found.append((command, assessment.reason))
    return found
