"""OS-level registration for the supervisor (P1 #5).

The Python supervisor restarts a crashed *daemon*; this module makes the
supervisor itself survive terminal closure and machine reboots by registering
it with the operating system's service scheduler:

- Windows: Scheduled Task (at logon, restart-on-failure, no time limit)
- Linux:   systemd user unit
- macOS:   launchd LaunchAgent

All three run the same command: `<python> -m autonomous_engine.cli.app
supervise --path <project>`. `install` is idempotent (re-registering
overwrites), `uninstall` removes it, `status` reports whether the
registration exists.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

TASK_PREFIX = "AutoEngineSupervisor"


class RegistrationError(RuntimeError):
    pass


def _run(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(  # noqa: S603 - fixed argv
        command, capture_output=True, text=True, check=False, timeout=60
    )
    if check and result.returncode != 0:
        raise RegistrationError(
            f"command failed ({result.returncode}): {' '.join(command[:4])}…\n"
            f"{result.stderr.strip() or result.stdout.strip()[-400:]}"
        )
    return result


def task_name(root: Path) -> str:
    """One registration per project directory (slug from the path)."""
    slug = "".join(c if c.isalnum() else "-" for c in str(root).lower()).strip("-")[-40:]
    return f"{TASK_PREFIX}-{slug}"


def supervise_command(root: Path) -> list[str]:
    # -X utf8: an unattended child must never die on a console codepage that
    # cannot encode its output (Windows defaults to cp1252/cp850).
    return [
        sys.executable,
        "-X",
        "utf8",
        "-m",
        "autonomous_engine.cli.app",
        "supervise",
        "--path",
        str(root),
    ]


def _ps_quote(text: str) -> str:
    """Escape a value for interpolation into a PowerShell single-quoted string."""
    return text.replace("'", "''")


def _windows_argument_string(command: list[str]) -> str:
    """Render the child command for New-ScheduledTaskAction -Argument.

    Task Scheduler re-parses this string with the Windows argv rules, so a
    project path containing spaces (`D:\\autonomous twin coder\\...`) must be
    double-quoted — an unquoted --path value would be split and the daemon
    would watch the wrong directory (or fail to start).
    """
    args = [str(part) for part in command[1:]]
    if "--path" in args:
        index = args.index("--path")
        if index + 1 < len(args):
            args[index + 1] = f'"{args[index + 1]}"'
    return " ".join(args)


@dataclass(frozen=True)
class Registration:
    platform: str
    name: str
    detail: str  # where the registration lives


# ---- Windows: Scheduled Task -------------------------------------------------


def _windows_install(root: Path) -> Registration:
    name = task_name(root)
    command = supervise_command(root)
    # PowerShell's Register-ScheduledTask supports restart-on-failure and an
    # unlimited execution time; schtasks' CLI flags do not.
    # - RestartCount 124 is the Task Scheduler documented maximum.
    # - RestartInterval's minimum is one minute.
    # - The -AtLogOn trigger is pinned to the registering user: an unpinned
    #   trigger would fire at *every* account's logon.
    # - IgnoreNew stops a second logon trigger from doubling the supervisor.
    user = "$env:USERDOMAIN\\$env:USERNAME"
    ps = f"""
$action  = New-ScheduledTaskAction -Execute '{_ps_quote(str(command[0]))}' `
    -Argument '{_ps_quote(_windows_argument_string(command))}'
$trigger = New-ScheduledTaskTrigger -AtLogOn -User '{_ps_quote(user)}'
$settings = New-ScheduledTaskSettingsSet -RestartCount 124 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
Register-ScheduledTask -TaskName '{_ps_quote(name)}' -Action $action -Trigger $trigger `
    -Settings $settings -Force | Out-Null
"""
    result = _run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            ps,
        ]
    )
    return Registration("windows", name, result.stdout.strip() or "registered")


def _windows_uninstall(root: Path) -> None:
    _run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            f"Unregister-ScheduledTask -TaskName '{_ps_quote(task_name(root))}' -Confirm:$false",
        ]
    )


def _windows_status(root: Path) -> str:
    result = _run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            f"(Get-ScheduledTask -TaskName '{_ps_quote(task_name(root))}' -ErrorAction SilentlyContinue).State",
        ],
        check=False,
    )
    if result.returncode != 0:
        # The scheduler query itself failed: reporting "not installed" here
        # would mislead an operator debugging why nothing is running.
        raise RegistrationError(
            f"query failed ({result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip()[-200:]}"
        )
    state = result.stdout.strip()
    return state if state else "not installed"


# ---- Linux: systemd user unit ------------------------------------------------


def _systemd_unit_path(root: Path) -> Path:
    slug = task_name(root).lower()
    return Path.home() / ".config" / "systemd" / "user" / f"{slug}.service"


def _linux_install(root: Path) -> Registration:
    unit = _systemd_unit_path(root)
    unit.parent.mkdir(parents=True, exist_ok=True)
    command = supervise_command(root)
    unit.write_text(
        "[Unit]\n"
        f"Description=Autonomous Engineering supervisor ({root})\n"
        "After=network-online.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        "ExecStart=" + " ".join(command) + "\n"
        "Restart=always\n"
        "RestartSec=5\n\n"
        "[Install]\n"
        "WantedBy=default.target\n",
        encoding="utf-8",
    )
    _run(["systemctl", "--user", "daemon-reload"])
    _run(["systemctl", "--user", "enable", "--now", unit.name])
    return Registration("linux", unit.name, str(unit))


def _linux_uninstall(root: Path) -> None:
    unit = _systemd_unit_path(root)
    _run(["systemctl", "--user", "disable", "--now", unit.name], check=False)
    unit.unlink(missing_ok=True)
    _run(["systemctl", "--user", "daemon-reload"], check=False)


def _linux_status(root: Path) -> str:
    unit = _systemd_unit_path(root)
    result = _run(
        ["systemctl", "--user", "is-enabled", unit.name], check=False
    )
    return result.stdout.strip() or "not installed"


# ---- macOS: launchd LaunchAgent -----------------------------------------------


def _launchd_plist_path(root: Path) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{task_name(root)}.plist"


def _macos_install(root: Path) -> Registration:
    import plistlib

    plist = _launchd_plist_path(root)
    plist.parent.mkdir(parents=True, exist_ok=True)
    command = supervise_command(root)
    plist.write_bytes(
        plistlib.dumps(
            {
                "Label": task_name(root),
                "ProgramArguments": command,
                "RunAtLoad": True,
                "KeepAlive": True,
                "StandardOutPath": str(root / ".agents" / "supervisor.log"),
                "StandardErrorPath": str(root / ".agents" / "supervisor.log"),
            }
        )
    )
    _run(["launchctl", "load", str(plist)])
    return Registration("macos", task_name(root), str(plist))


def _macos_uninstall(root: Path) -> None:
    plist = _launchd_plist_path(root)
    _run(["launchctl", "unload", str(plist)], check=False)
    plist.unlink(missing_ok=True)


def _macos_status(root: Path) -> str:
    result = _run(["launchctl", "list", task_name(root)], check=False)
    return "loaded" if result.returncode == 0 else "not installed"


# ---- public facade ------------------------------------------------------------


def install(root: Path) -> Registration:
    root = Path(root).resolve()
    if sys.platform.startswith("win"):
        return _windows_install(root)
    if sys.platform == "darwin":
        return _macos_install(root)
    return _linux_install(root)


def uninstall(root: Path) -> None:
    root = Path(root).resolve()
    if sys.platform.startswith("win"):
        _windows_uninstall(root)
    elif sys.platform == "darwin":
        _macos_uninstall(root)
    else:
        _linux_uninstall(root)


def status(root: Path) -> str:
    root = Path(root).resolve()
    try:
        if sys.platform.startswith("win"):
            return _windows_status(root)
        if sys.platform == "darwin":
            return _macos_status(root)
        return _linux_status(root)
    except RegistrationError as exc:
        return f"unknown ({exc})"
