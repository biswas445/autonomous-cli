"""OS-independent process supervisor for the autonomous daemon (P1 #5).

`DaemonLoop` survives its own crashes (bounded restarts, state on disk), but
if the whole Python process dies — terminal closed, machine reboot, OOM kill —
nothing brought it back. The supervisor owns the daemon as a *child process*:
it restarts it after crashes with exponential backoff, honors clean exits,
writes a heartbeat state file, and can be registered with the OS (Windows
Scheduled Task, systemd, launchd) so autonomy survives reboots.

Design rules:
- The supervisor never imports and runs the daemon in-process: a crash of the
  supervised process must be survivable, which is the entire point.
- Graceful stop goes through the existing control channel (`auto stop`
  semantics), then escalates to terminate/kill after a grace period.
- All state is on disk (`.agents/execution/supervisor.json`): the supervisor
  itself is disposable, the record of what it did is not.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.events import EventLog
from ..core.workspace import Workspace, atomic_write_json, now_iso


class SupervisorError(RuntimeError):
    pass


_PROCESS_SYNCHRONIZE = 0x00100000


def _pid_alive(pid: int) -> bool:
    """Best-effort process liveness without extra dependencies."""
    if pid <= 0:
        return False
    if os.name == "nt":
        handle = ctypes.windll.kernel32.OpenProcess(_PROCESS_SYNCHRONIZE, False, pid)  # type: ignore[attr-defined]
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore[attr-defined]
            return True
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return False
    return True


@dataclass
class SupervisorSettings:
    """Knobs for the supervisor loop; `command` is injectable for tests."""

    command: list[str] = field(default_factory=list)
    poll_seconds: float = 1.0
    restart_delay_base: float = 2.0
    restart_delay_max: float = 60.0
    # Consecutive crashes (without a healthy stretch) before giving up.
    max_restarts: int = 10
    # A child that ran this long counts as healthy: restart budget resets.
    healthy_reset_seconds: float = 600.0
    # Grace period for a clean stop before terminating the child.
    stop_grace_seconds: float = 15.0


@dataclass
class SupervisorReport:
    status: str  # completed | stopped | gave_up
    restarts: int = 0
    child_runs: int = 0
    last_exit_code: int | None = None
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "restarts": self.restarts,
            "child_runs": self.child_runs,
            "last_exit_code": self.last_exit_code,
            "reason": self.reason,
        }


class Supervisor:
    """Runs the daemon as a child process and brings it back after crashes."""

    def __init__(self, root: Path, settings: SupervisorSettings | None = None):
        self.root = Path(root).resolve()
        self.workspace = Workspace(self.root)
        if not self.workspace.exists():
            raise SupervisorError(
                f"no autonomous-engine project at {self.root}; run `auto init` first"
            )
        self.settings = settings or self._default_command(self.root)
        self.events: EventLog = self.workspace.events
        self.execution_dir = self.workspace.paths.execution
        self.state_path = self.execution_dir / "supervisor.json"
        self.lock_path = self.execution_dir / "supervisor.lock"
        self._stop_requested = threading.Event()

    @staticmethod
    def _default_command(root: Path) -> SupervisorSettings:
        # -X utf8: the daemon's console output (rich panels, event detail)
        # must survive a Windows codepage that cannot encode unicode.
        return SupervisorSettings(
            command=[
                sys.executable,
                "-X",
                "utf8",
                "-m",
                "autonomous_engine.cli.app",
                "daemon",
                "--path",
                str(root),
            ]
        )

    # ---- public control ----

    def stop(self) -> None:
        """Request a graceful shutdown (safe to call from another thread)."""
        self._stop_requested.set()

    def is_stop_requested(self) -> bool:
        return self._stop_requested.is_set()

    # ---- state on disk ----

    _HEARTBEAT_INTERVAL = 1.0

    def _write_state(self, status: str, *, restarts: int = 0, child_pid: int = 0,
                     last_exit: int | None = None, note: str = "") -> None:
        # State is best-effort observability: a failed write must never kill
        # supervision itself (Windows sharing violations can survive retries).
        import contextlib

        with contextlib.suppress(OSError):
            atomic_write_json(
                self.state_path,
                {
                    "status": status,
                    "supervisor_pid": os.getpid(),
                    "child_pid": child_pid,
                    "restarts": restarts,
                    "last_exit_code": last_exit,
                    "project": str(self.root),
                    "note": note,
                    "started_at": getattr(self, "_started_at", ""),
                    "last_heartbeat": now_iso(),
                },
            )
        self._last_beat = time.monotonic()

    def _heartbeat(self, status: str, *, restarts: int, child_pid: int) -> None:
        """Throttled liveness write: at most once per interval."""
        if time.monotonic() - getattr(self, "_last_beat", 0.0) < self._HEARTBEAT_INTERVAL:
            return
        self._write_state(status, restarts=restarts, child_pid=child_pid)

    def read_state(self) -> dict[str, Any]:
        import json

        if not self.state_path.is_file():
            return {}
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _emit(self, event: str, **fields: Any) -> None:
        """Supervisor lifecycle into the project's event log (auditability)."""
        import contextlib

        with contextlib.suppress(Exception):
            self.events.append(event, **fields)

    # ---- single instance ----

    def _acquire_lock(self) -> None:
        self.execution_dir.mkdir(parents=True, exist_ok=True)
        if self.lock_path.is_file():
            try:
                existing = int(self.lock_path.read_text(encoding="utf-8").strip() or 0)
            except (ValueError, OSError):
                existing = 0
            if existing and existing != os.getpid() and _pid_alive(existing):
                raise SupervisorError(
                    f"another supervisor (pid {existing}) is already watching {self.root}"
                )
        self.lock_path.write_text(str(os.getpid()), encoding="utf-8")

    def _release_lock(self) -> None:
        try:
            if self.lock_path.is_file() and self.lock_path.read_text(
                encoding="utf-8"
            ).strip() == str(os.getpid()):
                self.lock_path.unlink()
        except OSError:
            pass

    # ---- child lifecycle ----

    def _spawn(self) -> subprocess.Popen[None]:
        return subprocess.Popen(  # noqa: S603 - fixed argv from settings
            list(self.settings.command),
            cwd=str(self.root),
        )

    def _graceful_child_stop(self, child: subprocess.Popen[None]) -> None:
        """Ask the daemon to stop through the control channel, then escalate."""
        from .control import ControlChannel

        ControlChannel(self.execution_dir).request(
            stop=True, reason="supervisor shutting down"
        )
        try:
            child.wait(timeout=self.settings.stop_grace_seconds)
            return
        except subprocess.TimeoutExpired:
            pass
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()

    # ---- main loop ----

    def run(self) -> SupervisorReport:
        self._acquire_lock()
        self._started_at = now_iso()
        report = SupervisorReport(status="stopped")
        consecutive_crashes = 0
        child: subprocess.Popen[None] | None = None

        def finish(status: str, reason: str) -> SupervisorReport:
            report.status = status
            report.reason = reason
            self._write_state(status, restarts=report.restarts, last_exit=report.last_exit_code,
                              note=reason)
            self._emit(f"supervisor.{status}", reason=reason, restarts=report.restarts)
            self._release_lock()
            return report

        self._write_state("running")
        self._emit("supervisor.started", command=" ".join(self.settings.command)[:200])
        try:
            while True:
                if self.is_stop_requested():
                    if child is not None and child.poll() is None:
                        self._graceful_child_stop(child)
                        report.last_exit_code = child.returncode
                    return finish("stopped", "operator requested shutdown")

                child = self._spawn()
                report.child_runs += 1
                self._write_state("running", restarts=report.restarts, child_pid=child.pid)
                self._emit("supervisor.child_started", pid=child.pid, run=report.child_runs)

                started_at = time.monotonic()
                exited = False
                while not exited:
                    try:
                        code = child.wait(timeout=self.settings.poll_seconds)
                        exited = True
                        report.last_exit_code = code
                    except subprocess.TimeoutExpired:
                        self._heartbeat(
                            "running", restarts=report.restarts, child_pid=child.pid
                        )
                        if self.is_stop_requested():
                            self._graceful_child_stop(child)
                            report.last_exit_code = child.returncode
                            return finish("stopped", "operator requested shutdown")

                runtime_seconds = time.monotonic() - started_at
                if runtime_seconds >= self.settings.healthy_reset_seconds:
                    consecutive_crashes = 0
                exit_code = report.last_exit_code or 0
                self._emit(
                    "supervisor.child_exited",
                    exit_code=exit_code,
                    runtime_seconds=round(runtime_seconds, 1),
                )
                if exit_code == 0:
                    # A clean exit is the daemon's own decision (project
                    # complete, budget spent, `auto stop`): honor it.
                    return finish("completed", f"daemon exited cleanly ({exit_code})")

                consecutive_crashes += 1
                report.restarts += 1
                if consecutive_crashes > self.settings.max_restarts:
                    return finish(
                        "gave_up",
                        f"{consecutive_crashes} consecutive crashes "
                        f"(last exit {exit_code})",
                    )
                delay = min(
                    self.settings.restart_delay_base * (2 ** (consecutive_crashes - 1)),
                    self.settings.restart_delay_max,
                )
                self._write_state(
                    "restarting", restarts=report.restarts, last_exit=exit_code
                )
                self._emit("supervisor.restarting", restart=report.restarts, delay_s=round(delay, 1))
                self._sleep_with_stop_check(delay)
        except KeyboardInterrupt:
            if child is not None and child.poll() is None:
                self._graceful_child_stop(child)
            return finish("stopped", "interrupted")

    def _sleep_with_stop_check(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.is_stop_requested():
                return
            time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))
