"""User-defined event hooks (the Claude Code hooks pattern).

A running autonomous system is only useful if it can reach the outside world
at meaningful moments: post to Slack when a task fails, run a formatter after
every completed task, page someone when a human gate opens. Claude Code
documents exactly this shape: config-declared commands bound to lifecycle
events, invoked with structured JSON on stdin, whose failures are reported but
never break the session.

    .agents/hooks.json
    {
      "hooks": [
        {"event": "task.failed",       "command": "python notify.py", "timeout": 10},
        {"event": "escalation.*",      "command": "python page.py"},
        {"event": "task.completed",    "command": "ruff format src"}
      ]
    }

Semantics:
- `event` is an exact name ("task.failed") or a prefix glob ("task.*").
- Commands are argv lists executed without a shell (shlex.split), so hook
  configuration cannot inject shell metacharacters.
- The event JSON is written to the hook's stdin; stdout/stderr are captured
  into `.agents/agent_logs/hooks.log`.
- A hook that fails, times out, or is missing is recorded as `hook.failed`
  and never interrupts the run. `hook.*` events are never dispatched
  (no recursion).
- Prefer specific events: `task.*` also matches the frequent
  `task.state_changed`, so a broad glob runs a dozen times per task.
"""

from __future__ import annotations

import contextlib
import fnmatch
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .permissions import split_command

HOOKS_FILENAME = "hooks.json"
HOOKS_LOG = "hooks.log"
_MAX_OUTPUT = 4000


@dataclass(frozen=True)
class Hook:
    event: str
    command: str
    timeout: int = 10

    def matches(self, event: str) -> bool:
        if self.event == event:
            return True
        return fnmatch.fnmatch(event, self.event)


def load_hooks(workspace) -> list[Hook]:
    """Read .agents/hooks.json; malformed entries are skipped, not fatal."""
    hooks, _error = load_hooks_with_error(workspace)
    return hooks


def load_hooks_with_error(workspace) -> tuple[list[Hook], str]:
    """Load hooks and report *why* an existing file produced none.

    A silently ignored hooks.json is a misconfiguration the operator would
    never notice; the error string lets HookRunner surface it.
    """
    path = workspace.paths.state / HOOKS_FILENAME
    if not path.is_file():
        return [], ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [], f"{HOOKS_FILENAME} is not valid JSON: {exc}"
    except OSError as exc:
        return [], f"{HOOKS_FILENAME} could not be read: {exc}"
    hooks: list[Hook] = []
    for item in payload.get("hooks", []) if isinstance(payload, dict) else []:
        if not isinstance(item, dict):
            continue
        event = str(item.get("event", "")).strip()
        command = str(item.get("command", "")).strip()
        if not event or not command:
            continue
        with contextlib.suppress(TypeError, ValueError):
            hooks.append(Hook(event=event, command=command, timeout=int(item.get("timeout", 10))))
    if not hooks and payload.get("hooks"):
        return [], f"{HOOKS_FILENAME} defines no usable hooks (check event/command fields)"
    return hooks, ""


class HookRunner:
    """Dispatches events to configured hooks; never raises, never blocks long."""

    def __init__(self, workspace, *, enabled: bool = True):
        self.workspace = workspace
        self.enabled = enabled
        self._hooks: list[Hook] | None = None
        self._config_error: str = ""
        self._config_error_reported = False

    def hooks(self) -> list[Hook]:
        if self._hooks is None:
            self._hooks, self._config_error = load_hooks_with_error(self.workspace)
            self._report_config_error()
        return self._hooks

    def reload(self) -> None:
        self._hooks = None
        self._config_error_reported = False

    def _report_config_error(self) -> None:
        if not self._config_error or self._config_error_reported:
            return
        self._config_error_reported = True
        with contextlib.suppress(Exception):
            self.workspace.events.append("hook.invalid_config", detail=self._config_error[:300])

    def log_path(self) -> Path:
        directory = self.workspace.paths.agent_logs / "hooks"
        directory.mkdir(parents=True, exist_ok=True)
        return directory / HOOKS_LOG

    def dispatch(self, event: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Run matching hooks; returns their outcomes (for the caller's log)."""
        if not self.enabled or event.startswith("hook."):
            return []
        matched = [hook for hook in self.hooks() if hook.matches(event)]
        if not matched:
            return []
        outcomes: list[dict[str, Any]] = []
        for hook in matched:
            outcome = self._run(hook, event, payload)
            outcomes.append(outcome)
            self._record(outcome)
        return outcomes

    def _run(self, hook: Hook, event: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            argv = split_command(hook.command)
        except ValueError as exc:
            return {
                "event": event,
                "hook": hook.event,
                "command": hook.command,
                "ok": False,
                "detail": f"unparseable hook command: {exc}",
            }
        if not argv:
            return {
                "event": event,
                "hook": hook.event,
                "command": hook.command,
                "ok": False,
                "detail": "empty hook command",
            }
        envelope = json.dumps({"event": event, "payload": payload}, default=str)
        try:
            proc = subprocess.run(  # noqa: S603 - argv list, no shell
                argv,
                cwd=str(self.workspace.paths.root),
                input=envelope,
                capture_output=True,
                text=True,
                timeout=hook.timeout,
                shell=False,
                check=False,
            )
        except FileNotFoundError as exc:
            return {
                "event": event,
                "hook": hook.event,
                "command": hook.command,
                "ok": False,
                "detail": f"hook command not found: {exc}",
            }
        except subprocess.TimeoutExpired:
            return {
                "event": event,
                "hook": hook.event,
                "command": hook.command,
                "ok": False,
                "detail": f"hook timed out after {hook.timeout}s",
            }
        except OSError as exc:
            return {
                "event": event,
                "hook": hook.event,
                "command": hook.command,
                "ok": False,
                "detail": str(exc),
            }
        return {
            "event": event,
            "hook": hook.event,
            "command": hook.command,
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": proc.stdout[-_MAX_OUTPUT:],
            "stderr": proc.stderr[-_MAX_OUTPUT:],
        }

    def _record(self, outcome: dict[str, Any]) -> None:
        """Append to the hook log and the event stream (never recursively)."""
        with contextlib.suppress(OSError), self.log_path().open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(outcome, ensure_ascii=False, default=str) + "\n")
        with contextlib.suppress(Exception):
            # Note: the field is `source_event` — `event` is the positional
            # parameter of EventLog.append and would raise TypeError.
            self.workspace.events.append(
                "hook.completed" if outcome.get("ok") else "hook.failed",
                hook=outcome.get("hook"),
                source_event=outcome.get("event"),
                command=outcome.get("command"),
                detail=str(outcome.get("detail") or outcome.get("stderr") or "")[:300],
            )


def default_hooks_template() -> str:
    return (
        json.dumps(
            {
                "hooks": [
                    {
                        "event": "task.failed",
                        "command": "python -c \"import sys; print('task failed', file=sys.stderr)\"",
                        "timeout": 10,
                        "_comment": "example: replace with your own notifier; delete to disable",
                    }
                ]
            },
            indent=2,
        )
        + "\n"
    )
