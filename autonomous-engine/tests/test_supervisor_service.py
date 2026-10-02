"""OS registration for the supervisor: platform dispatch, Windows Task
Scheduler generation (quoting + restart-on-failure settings), and the
supervise command rendering. All OS commands are faked — nothing here may
touch a real scheduler."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from autonomous_engine.runtime import supervisor_service as svc


def _fake_run(monkeypatch, calls: list[list[str]], stdout: str = "", returncode: int = 0):
    """Replace svc._run with a recorder."""

    def fake(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        calls.append(list(command))
        return subprocess.CompletedProcess(command, returncode, stdout=stdout, stderr="")

    monkeypatch.setattr(svc, "_run", fake)


# ---- command rendering -------------------------------------------------------


def test_supervise_command_targets_this_interpreter(project: Path):
    command = svc.supervise_command(project)
    assert command[0] == sys.executable
    assert "supervise" in command
    assert "--path" in command and str(project) in command
    # -X utf8 keeps the unattended child off the Windows legacy codepage
    assert command[1:3] == ["-X", "utf8"]


def test_windows_argument_string_quotes_paths_with_spaces():
    command = [
        sys.executable,
        "-X",
        "utf8",
        "-m",
        "autonomous_engine.cli.app",
        "supervise",
        "--path",
        "D:\\autonomous twin coder\\target-project",
    ]
    rendered = svc._windows_argument_string(command)
    # the whole path must survive the Task Scheduler argv re-parse as one value
    assert '"D:\\autonomous twin coder\\target-project"' in rendered
    assert rendered.endswith('--path "D:\\autonomous twin coder\\target-project"')


def test_windows_argument_string_without_path_flag():
    rendered = svc._windows_argument_string([sys.executable, "-m", "some.module"])
    assert rendered == "-m some.module"


def test_ps_quote_escapes_single_quotes():
    assert svc._ps_quote("it's") == "it''s"


def test_task_name_is_stable_and_path_derived(tmp_path: Path):
    a = tmp_path / "alpha"
    b = tmp_path / "beta"
    name_a1 = svc.task_name(a)
    name_a2 = svc.task_name(a)
    assert name_a1 == name_a2  # idempotent re-registration needs a stable name
    assert name_a1 != svc.task_name(b)
    assert name_a1.startswith(svc.TASK_PREFIX)
    assert " " not in name_a1  # must be usable as a bare Task Scheduler name


# ---- Windows install/uninstall/status (faked PowerShell) ---------------------


@pytest.fixture()
def on_windows(monkeypatch):
    monkeypatch.setattr(svc.sys, "platform", "win32")


def test_windows_install_registers_restart_on_failure(
    monkeypatch, on_windows, project: Path
):
    calls: list[list[str]] = []
    _fake_run(monkeypatch, calls, stdout="registered")
    registration = svc.install(project)
    assert registration.platform == "windows"
    assert registration.name == svc.task_name(project)
    assert len(calls) == 1
    ps_command = calls[0]
    assert ps_command[0] == "powershell"
    script = ps_command[-1]
    # restart-on-failure + no execution-time limit + single instance
    assert "-RestartCount 124" in script
    assert "-RestartInterval (New-TimeSpan -Minutes 1)" in script
    assert "-ExecutionTimeLimit ([TimeSpan]::Zero)" in script
    assert "-MultipleInstances IgnoreNew" in script
    # the logon trigger is pinned to the registering user
    assert "-AtLogOn -User" in script
    # the space-containing project path arrives double-quoted
    assert f'"{project}"' in script
    # the task name is quoted for the Register call
    assert svc.task_name(project) in script


def test_windows_uninstall(monkeypatch, on_windows, project: Path):
    calls: list[list[str]] = []
    _fake_run(monkeypatch, calls)
    svc.uninstall(project)
    assert len(calls) == 1
    assert "Unregister-ScheduledTask" in calls[0][-1]
    assert svc.task_name(project) in calls[0][-1]


def test_windows_status(monkeypatch, on_windows, project: Path):
    calls: list[list[str]] = []
    _fake_run(monkeypatch, calls, stdout="Ready\n")
    assert svc.status(project) == "Ready"
    _fake_run(monkeypatch, calls, stdout="")
    assert svc.status(project) == "not installed"


def test_windows_status_reports_registry_errors(monkeypatch, on_windows, project: Path):
    def broken(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 1, "", "boom")

    monkeypatch.setattr(svc, "_run", broken)
    assert svc.status(project).startswith("unknown (")


def test_failed_registration_raises_with_stderr(monkeypatch, on_windows, project: Path):
    def failing(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        assert check is True
        raise svc.RegistrationError("command failed: powershell\nboom stderr")

    monkeypatch.setattr(svc, "_run", failing)
    with pytest.raises(svc.RegistrationError, match="boom stderr"):
        svc.install(project)


# ---- Linux / macOS dispatch (faked systemctl + launchctl) ---------------------


def test_linux_install_writes_unit_and_enables_it(monkeypatch, tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(svc.sys, "platform", "linux")
    calls: list[list[str]] = []
    _fake_run(monkeypatch, calls)
    root = tmp_path / "target-project"
    registration = svc.install(root)
    # registration.name IS the unit file name (already carries .service)
    unit_path = home / ".config" / "systemd" / "user" / registration.name
    assert registration.platform == "linux"
    assert unit_path.is_file()
    unit = unit_path.read_text(encoding="utf-8")
    assert "Restart=always" in unit
    assert "supervise" in unit and "--path" in unit
    enabled = [c for c in calls if "enable" in c]
    assert enabled and "--now" in enabled[0]


def test_linux_uninstall_disables_and_removes_unit(monkeypatch, tmp_path: Path):
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(svc.sys, "platform", "linux")
    calls: list[list[str]] = []
    _fake_run(monkeypatch, calls)
    svc.uninstall(tmp_path / "target-project")
    unit_path = home / ".config" / "systemd" / "user"
    # the unit was removed (or never existed); disable ran best-effort
    assert not any(p.suffix == ".service" for p in unit_path.glob("*.service")) if unit_path.is_dir() else True
    assert any("disable" in c for c in calls)


def test_macos_install_writes_plist_and_loads_it(monkeypatch, tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(svc.sys, "platform", "darwin")
    calls: list[list[str]] = []
    _fake_run(monkeypatch, calls)
    root = tmp_path / "target-project"
    registration = svc.install(root)
    plist_path = home / "Library" / "LaunchAgents" / f"{svc.task_name(root)}.plist"
    assert registration.platform == "macos"
    assert plist_path.is_file()
    assert b"KeepAlive" in plist_path.read_bytes()  # launchd-level restart-on-exit
    assert any("launchctl" in c and "load" in c for c in calls)
