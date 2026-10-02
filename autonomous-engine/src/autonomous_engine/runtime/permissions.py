"""Permission enforcement and sandboxed tools (plan.md §61, §62).

The ToolBox is the only path from an agent to the machine. It enforces:
  - read/write path allowlists (globs relative to the work root)
  - command allowlists (first-token / full-command globs)
  - network policy
  - git-write policy
  - path traversal rejection
  - timeouts and output truncation

Model output is untrusted input: nothing an agent returns is executed without
passing these checks.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.config import PermissionClass
from .risk import CommandRisk, classify_command


class PermissionDenied(PermissionError):
    """Raised when an agent attempts an action outside its permission class."""


_GLOB_CACHE: dict[str, re.Pattern[str]] = {}


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Compile a write-policy glob with path semantics.

    fnmatch's `*` matches `/` too, so a class allowed `*.md` could write
    `.agents/microagents/persist.md` (persistent prompt injection) or
    `.git/hooks/x.md`. Here `**` crosses directories while `*` and `?`
    stay within one path segment, matching what the configured patterns
    mean. Case-insensitive on Windows, like fnmatch's normcase there.
    """
    cached = _GLOB_CACHE.get(pattern)
    if cached is not None:
        return cached
    out: list[str] = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "*":
            if pattern[i : i + 2] == "**":
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
            i += 1
        elif ch == "?":
            out.append("[^/]")
            i += 1
        elif ch == "[":
            end = pattern.find("]", i + 1)
            if end == -1:
                out.append(re.escape(ch))
                i += 1
            else:
                out.append(pattern[i : end + 1])
                i = end + 1
        else:
            out.append(re.escape(ch))
            i += 1
    flags = re.IGNORECASE if os.name == "nt" else re.NOFLAG
    compiled = re.compile("".join(out) + r"\Z", flags)
    _GLOB_CACHE[pattern] = compiled
    return compiled


def split_command(command: str) -> list[str]:
    """Split a command line into an argv list, safely on every platform.

    POSIX shlex treats backslashes as escapes, which corrupts Windows paths
    (a "C:/Python/python.exe" style token written with backslashes loses its separators).
    On Windows we split in
    non-POSIX mode and then strip the surrounding quotes shlex leaves behind.
    """
    import shlex as _shlex

    if os.name == "nt":
        tokens = _shlex.split(command, posix=False)
        return [
            token[1:-1]
            if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'"
            else token
            for token in tokens
        ]
    return _shlex.split(command)


@dataclass(frozen=True)
class CommandResult:
    command: str
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    duration_ms: float = 0.0

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def as_dict(self) -> dict[str, object]:
        return {
            "command": self.command,
            "returncode": self.returncode,
            "stdout": self.stdout[-8000:],
            "stderr": self.stderr[-4000:],
            "timed_out": self.timed_out,
            "duration_ms": round(self.duration_ms, 1),
        }


@dataclass
class ToolBox:
    """Sandboxed tool surface for a single agent invocation."""

    work_root: Path
    permissions: PermissionClass
    env_allowlist: list[str] = field(
        default_factory=lambda: ["PATH", "HOME", "LANG", "PYTHONPATH", "SYSTEMROOT"]
    )
    max_output_chars: int = 20_000
    default_timeout: int = 300
    # §49/§61: "process" enforces the allowlists on the host; "docker" runs
    # commands inside a container with the work root mounted at /workspace.
    sandbox_backend: str = "process"
    sandbox_image: str = "python:3.12-slim"
    # Risk gate (OpenHands analyzer pattern). HIGH-risk commands are refused
    # unless the permission class opts in, or a human approved this task.
    allow_high_risk: bool = False
    # Directive #10: when a HIGH-risk command would be refused but Docker is
    # available, run it inside the container instead — isolation instead of
    # refusal. No effect when sandbox_backend is already "docker".
    prefer_docker_high_risk: bool = False
    # Optional hooks for agent tools: the run budget (budget_status tool) and
    # the project workspace (save/recall memory tools). Absent in unit tests.
    budget: Any = None
    workspace: Any = None
    # Every path this sandbox wrote, in order. The orchestrator merges these
    # into the task's artifacts so tool-driven edits are tracked like EditPlan
    # edits (nothing an agent did escapes bookkeeping).
    written_paths: list[str] = field(default_factory=list)

    # ---- path policy ----

    def _resolve(self, path: str) -> Path:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.work_root / candidate
        resolved = candidate.resolve()
        try:
            resolved.relative_to(self.work_root)
        except ValueError as exc:
            raise PermissionDenied(f"path escapes the work root: {path} -> {resolved}") from exc
        return resolved

    def _matches_write_policy(self, resolved: Path) -> bool:
        if not self.permissions.write_paths:
            return False
        rel = resolved.relative_to(self.work_root).as_posix()
        # Git internals are never agent-writable: hooks/config in .git are
        # code execution waiting to happen. GitManager owns every git write.
        if rel == ".git" or rel.startswith(".git/"):
            return False
        for pattern in self.permissions.write_paths:
            if _glob_to_regex(pattern).match(rel):
                return True
            # a bare directory pattern (e.g. "src") should cover its subtree
            if not any(ch in pattern for ch in "*?[") and (
                rel == pattern or rel.startswith(pattern + "/")
            ):
                return True
        return False

    # ---- tools ----

    def read_file(self, path: str) -> str:
        if not self.permissions.read_repo:
            raise PermissionDenied(f"{self.permissions.name} may not read the repository")
        resolved = self._resolve(path)
        if not resolved.is_file():
            raise FileNotFoundError(str(resolved))
        text = resolved.read_text(encoding="utf-8", errors="replace")
        return text[: self.max_output_chars]

    def read_file_full(self, path: str) -> str:
        """Untruncated read used only for edit application (search/replace).

        Same permission rules as read_file; the content is not for a context
        window, so the display truncation would corrupt the edit.
        """
        if not self.permissions.read_repo:
            raise PermissionDenied(f"{self.permissions.name} may not read the repository")
        resolved = self._resolve(path)
        if not resolved.is_file():
            raise FileNotFoundError(str(resolved))
        return resolved.read_text(encoding="utf-8", errors="replace")

    def list_dir(self, path: str = ".") -> list[str]:
        if not self.permissions.read_repo:
            raise PermissionDenied(f"{self.permissions.name} may not read the repository")
        resolved = self._resolve(path)
        if not resolved.is_dir():
            raise NotADirectoryError(str(resolved))
        entries: list[str] = []
        for child in sorted(resolved.iterdir()):
            if child.name in {".git", "__pycache__", "node_modules", ".venv"}:
                continue
            entries.append(child.name + ("/" if child.is_dir() else ""))
            if len(entries) >= 500:
                break
        return entries

    def write_file(self, path: str, content: str) -> str:
        resolved = self._resolve(path)
        if not self._matches_write_policy(resolved):
            raise PermissionDenied(
                f"{self.permissions.name} may not write {resolved.relative_to(self.work_root).as_posix()}"
            )
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")
        rel = str(resolved.relative_to(self.work_root).as_posix())
        if rel not in self.written_paths:
            self.written_paths.append(rel)
        return rel

    def delete_file(self, path: str) -> str:
        resolved = self._resolve(path)
        if not self._matches_write_policy(resolved):
            raise PermissionDenied(
                f"{self.permissions.name} may not delete {resolved.relative_to(self.work_root).as_posix()}"
            )
        if resolved.is_dir():
            raise PermissionDenied("refusing to delete a directory recursively")
        if resolved.is_file():
            resolved.unlink()
        rel = str(resolved.relative_to(self.work_root).as_posix())
        if rel not in self.written_paths:
            self.written_paths.append(rel)
        return rel

    def run_command(self, command: str, *, timeout: int | None = None) -> CommandResult:
        if not self.permissions.run_commands:
            raise PermissionDenied(f"{self.permissions.name} may not run commands")
        if not self.permissions.can_run_command(command):
            raise PermissionDenied(
                f"command not allowed for {self.permissions.name}: {command.split()[0] if command.split() else command!r}"
            )
        allowed_high = self.allow_high_risk or self.permissions.allow_high_risk
        assessment = classify_command(command)
        isolate_high_risk = (
            assessment.risk == CommandRisk.HIGH
            and not allowed_high
            and self.prefer_docker_high_risk
            and self.sandbox_backend == "process"
            and self._docker_available()
        )
        if assessment.risk == CommandRisk.HIGH and not allowed_high and not isolate_high_risk:
            raise PermissionDenied(
                f"high-risk command refused by the risk analyzer ({assessment.reason}): {command}"
            )
        # shell=False: the command string is split into an argv list and
        # executed without a shell, so model output cannot inject
        # shell metacharacters (split_command is Windows-path-safe).
        import time as _time

        try:
            argv = split_command(command)
        except ValueError as exc:
            raise PermissionDenied(f"unparseable command: {exc}") from exc
        if not argv:
            raise PermissionDenied("empty command")

        if self.sandbox_backend == "docker" or isolate_high_risk:
            argv = self._docker_argv(argv)

        env = {k: v for k, v in os.environ.items() if k in set(self.env_allowlist)}
        env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
        started = _time.perf_counter()
        try:
            proc = subprocess.run(  # noqa: S603 - argv list, shell=False
                argv,
                cwd=None if (self.sandbox_backend == "docker" or isolate_high_risk) else str(self.work_root),
                capture_output=True,
                text=True,
                timeout=timeout or self.default_timeout,
                env=env,
                shell=False,
                check=False,
            )
        except FileNotFoundError:
            if self.sandbox_backend == "docker":
                return CommandResult(
                    command,
                    127,
                    "",
                    "docker not found; install Docker or set sandbox_backend=process",
                )
            return CommandResult(command, 127, "", f"command not found: {argv[0]}")
        except subprocess.TimeoutExpired:
            return CommandResult(
                command,
                124,
                "",
                f"timed out after {timeout or self.default_timeout}s",
                timed_out=True,
                duration_ms=(_time.perf_counter() - started) * 1000,
            )
        except PermissionError as exc:
            raise PermissionDenied(f"OS refused command execution: {exc}") from exc

        duration = (_time.perf_counter() - started) * 1000
        return CommandResult(
            command=command,
            returncode=proc.returncode,
            stdout=proc.stdout[-self.max_output_chars :],
            stderr=proc.stderr[-self.max_output_chars :],
            duration_ms=duration,
        )

    _docker_available_cache: bool | None = None

    @classmethod
    def _docker_available(cls) -> bool:
        """Is Docker usable? Checked once per process (directive #10)."""
        if cls._docker_available_cache is None:
            try:
                proc = subprocess.run(  # noqa: S603 - fixed argv
                    ["docker", "version", "--format", "ok"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                cls._docker_available_cache = proc.returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                cls._docker_available_cache = False
        return cls._docker_available_cache

    def _docker_argv(self, command_argv: list[str]) -> list[str]:
        """Wrap the command for container execution (§61).

        The work root is mounted at /workspace, the container starts there,
        and the network is disabled unless the agent's permission class
        explicitly allows it. The command stays an argv list — never a shell
        string — so model output still cannot inject metacharacters.
        """
        argv = [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{self.work_root}:/workspace",
            "-w",
            "/workspace",
        ]
        if not self.permissions.network:
            argv += ["--network", "none"]
        return [*argv, self.sandbox_image, *command_argv]

    # The only git mutations a sandbox may perform via git_write; everything
    # else (push, config, filter-branch…) goes through GitManager with its own
    # orchestrator-controlled policy.
    _GIT_WRITE_SUBCOMMANDS = {
        "add", "branch", "checkout", "cherry-pick", "commit", "merge", "mv",
        "rebase", "restore", "rm", "stash", "switch", "tag",
    }

    def git_write(self, args: list[str]) -> str:
        if not self.permissions.git_write:
            raise PermissionDenied(f"{self.permissions.name} may not perform git writes")
        if not args or args[0] not in self._GIT_WRITE_SUBCOMMANDS:
            subcommand = args[0] if args else "<none>"
            raise PermissionDenied(f"git {subcommand} is not an approved write subcommand")
        # Same defense in depth as run_command: the risk analyzer decides
        # what no agent may run, even inside an allowlisted subcommand.
        assessment = classify_command("git " + " ".join(args))
        if assessment.risk is CommandRisk.HIGH:
            raise PermissionDenied(f"high-risk git operation refused: {assessment.reason}")
        argv = ["git", *args]
        proc = subprocess.run(  # noqa: S603 - argv list, shell=False
            argv, cwd=str(self.work_root), capture_output=True, text=True, check=False, shell=False
        )
        return (proc.stdout + proc.stderr).strip()

    def network_allowed(self) -> bool:
        return self.permissions.network

    def snapshot(self) -> dict[str, object]:
        return {
            "agent_class": self.permissions.name,
            "work_root": str(self.work_root),
            "read_repo": self.permissions.read_repo,
            "write_paths": list(self.permissions.write_paths),
            "run_commands": self.permissions.run_commands,
            "network": self.permissions.network,
            "git_write": self.permissions.git_write,
        }
