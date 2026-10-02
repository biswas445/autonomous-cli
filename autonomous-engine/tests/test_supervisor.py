"""Process supervisor: restart-on-crash, backoff, graceful stop, single
instance (P1 #5). Tests use real child processes with tiny intervals."""

from __future__ import annotations

import os
import sys
import threading
import time

import pytest

from autonomous_engine.runtime.supervisor import (
    Supervisor,
    SupervisorError,
    SupervisorSettings,
)

CRASH = [sys.executable, "-c", "import sys; sys.exit(3)"]
CLEAN = [sys.executable, "-c", "import sys; sys.exit(0)"]
HANG = [sys.executable, "-c", "import time; time.sleep(60)"]


def _settings(command: list[str], **overrides: object) -> SupervisorSettings:
    defaults: dict[str, object] = {
        "poll_seconds": 0.05,
        "restart_delay_base": 0.01,
        "restart_delay_max": 0.05,
        "stop_grace_seconds": 5.0,
    }
    defaults.update(overrides)
    return SupervisorSettings(command=command, **defaults)  # type: ignore[arg-type]


def _run_in_thread(supervisor: Supervisor, results: dict[str, object]) -> threading.Thread:
    def target() -> None:
        results["report"] = supervisor.run()

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread


def _wait_for(predicate, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("timed out waiting for supervisor condition")


def test_crashing_child_is_restarted_then_gives_up(project):
    supervisor = Supervisor(project, _settings(CRASH, max_restarts=2))
    report = supervisor.run()
    assert report.status == "gave_up"
    assert report.restarts == 3  # 1 initial run + 2 restarts, then the budget is spent
    assert report.child_runs == 3
    assert report.last_exit_code == 3
    state = supervisor.read_state()
    assert state["status"] == "gave_up"
    assert state["last_heartbeat"]


def test_clean_exit_is_honored_not_restarted(project):
    supervisor = Supervisor(project, _settings(CLEAN))
    report = supervisor.run()
    assert report.status == "completed"
    assert report.restarts == 0
    assert report.child_runs == 1


def test_graceful_stop_terminates_a_hanging_child(project):
    supervisor = Supervisor(project, _settings(HANG))
    results: dict[str, object] = {}
    thread = _run_in_thread(supervisor, results)
    _wait_for(lambda: supervisor.read_state().get("child_pid"))
    supervisor.stop()
    thread.join(timeout=20)
    assert not thread.is_alive()
    report = results["report"]
    assert report.status == "stopped"
    assert report.child_runs == 1
    # the hanging child was actually terminated, not orphaned
    child_pid = int(supervisor.read_state().get("child_pid") or 0)
    time.sleep(0.2)
    from autonomous_engine.runtime.supervisor import _pid_alive

    assert not _pid_alive(child_pid)


def test_healthy_runs_reset_the_crash_budget(project):
    """A child that runs long enough is 'healthy': consecutive-crash give-up
    must not fire, and an operator stop still ends the loop cleanly."""
    slow_fail = [sys.executable, "-c", "import time, sys; time.sleep(0.4); sys.exit(1)"]
    supervisor = Supervisor(
        project, _settings(slow_fail, max_restarts=2, healthy_reset_seconds=0.3)
    )
    results: dict[str, object] = {}
    thread = _run_in_thread(supervisor, results)
    _wait_for(lambda: int(supervisor.read_state().get("restarts") or 0) >= 3)
    # the loop is still alive: a healthy stretch resets the crash budget, so
    # it never hit gave_up (status may momentarily read 'restarting')
    assert supervisor.read_state()["status"] in ("running", "restarting")
    supervisor.stop()
    thread.join(timeout=20)
    report = results["report"]
    assert report.status == "stopped"


def test_single_instance_lock_blocks_a_second_supervisor(project):
    """A lock held by a *live foreign* process must block a new supervisor."""
    from autonomous_engine.runtime.supervisor import _pid_alive

    foreign_pid = 4 if os.name == "nt" else 1  # System / init: alive, not ours
    if not _pid_alive(foreign_pid):  # pragma: no cover - platform paranoia
        pytest.skip("no known-foreign alive pid on this platform")
    supervisor = Supervisor(project, _settings(CLEAN))
    supervisor.lock_path.parent.mkdir(parents=True, exist_ok=True)
    supervisor.lock_path.write_text(str(foreign_pid), encoding="utf-8")
    with pytest.raises(SupervisorError, match="already watching"):
        supervisor.run()


def test_lock_file_survives_a_dead_supervisor(project):
    """A stale lock (dead pid) must not block a new supervisor forever."""
    lock = Supervisor(project).lock_path
    lock.parent.mkdir(parents=True, exist_ok=True)
    dead_pid = 999999  # nothing here runs with this pid
    if os.name == "nt":
        from autonomous_engine.runtime.supervisor import _pid_alive

        if _pid_alive(dead_pid):  # pragma: no cover - theoretically impossible
            dead_pid = 4  # System process on Windows: alive but not a supervisor
    lock.write_text(str(dead_pid), encoding="utf-8")
    supervisor = Supervisor(project, _settings(CLEAN))
    report = supervisor.run()  # must take over the stale lock and run
    assert report.status == "completed"


# ---- the supervisor <-> daemon exit contract ---------------------------------


def test_daemon_exit_code_contract():
    """`auto stop` must be a clean exit, or a supervisor would resurrect the
    daemon forever; dead ends must still be non-zero."""
    from autonomous_engine.runtime.daemon import DaemonReport, daemon_exit_code

    assert daemon_exit_code(DaemonReport(status="completed", reason="PROJECT_COMPLETE")) == 0
    # operator-requested stop is a clean exit even though status is 'stopped'
    assert daemon_exit_code(DaemonReport(status="stopped", reason="USER_REQUESTED")) == 0
    # everything else is a failure the supervisor may act on (restart budget)
    assert daemon_exit_code(DaemonReport(status="gave_up", reason="NO_PROGRESS")) == 2
    assert daemon_exit_code(DaemonReport(status="stopped", reason="BUDGET_EXCEEDED")) == 2
    assert daemon_exit_code(DaemonReport(status="stopped", reason="SAFETY_BLOCK")) == 2


def test_auto_stop_ends_a_supervised_daemon_cleanly(project):
    """End-to-end: `auto stop` written before `auto daemon` starts → the
    daemon stops with USER_REQUESTED and exits 0 — exactly what the process
    supervisor needs to treat the shutdown as intentional."""
    from typer.testing import CliRunner

    from autonomous_engine.cli.app import app
    from autonomous_engine.core.workspace import Workspace
    from autonomous_engine.runtime.control import ControlChannel

    ControlChannel(Workspace(project).paths.execution).request(
        stop=True, reason="supervisor contract test"
    )
    result = CliRunner().invoke(app, ["daemon", "--path", str(project)])
    assert result.exit_code == 0, result.output
    assert '"status": "stopped"' in result.output
    assert '"reason": "USER_REQUESTED"' in result.output


def test_supervise_cli_reports_a_live_single_instance_conflict(project):
    """`auto supervise` against an already-watched project must fail with a
    readable error, not a traceback."""
    from typer.testing import CliRunner

    from autonomous_engine.cli.app import app
    from autonomous_engine.runtime.supervisor import Supervisor, _pid_alive

    foreign_pid = 4 if os.name == "nt" else 1  # System / init: alive, not ours
    if not _pid_alive(foreign_pid):  # pragma: no cover - platform paranoia
        pytest.skip("no known-foreign alive pid on this platform")
    supervisor = Supervisor(project)
    supervisor.lock_path.parent.mkdir(parents=True, exist_ok=True)
    supervisor.lock_path.write_text(str(foreign_pid), encoding="utf-8")
    result = CliRunner().invoke(
        app, ["supervise", "--path", str(project), "--max-restarts", "1"]
    )
    assert result.exit_code == 1
    assert "already watching" in result.output
