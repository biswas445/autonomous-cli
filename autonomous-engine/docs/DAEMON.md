# The daemon and its supervisor

`auto daemon` (plan.md §45) runs the orchestrator repeatedly until the project
is done, resolving human gates as they open. But a daemon that can survive its
*own* crashes is not enough for unattended operation: if the whole Python
process dies — terminal closed, machine rebooted, process killed by the OS —
nothing brought it back. The supervisor closes that gap with three layers:

| Layer | What it survives | Where |
| --- | --- | --- |
| `auto daemon` | its own crashed runs (bounded restarts, persisted state) | `runtime/daemon.py` |
| `auto supervise` | a dead *daemon process* (child restart, exponential backoff) | `runtime/supervisor.py` |
| `auto supervisor install` | terminal closure, logoff, machine reboot (OS registration) | `runtime/supervisor_service.py` |

## The process supervisor (`auto supervise`)

`auto supervise` runs the daemon as a **child process** (never in-process: a
supervised crash must be survivable, which is the entire point):

- Any non-zero child exit is a crash → restart with exponential backoff
  (base `--restart-delay`, default 2s, doubling up to 60s).
- **Exit-code contract** (`daemon_exit_code` in `runtime/daemon.py`): the
  daemon exits `0` when the project completed *or* the operator ran
  `auto stop` — a clean exit is honored and **not** restarted, otherwise
  `auto stop` could never end a supervised daemon. Dead ends (`gave_up`,
  budget walls, safety blocks) exit `2` and burn the restart budget.
- Consecutive crashes are bounded (`--max-restarts`, default 10). A child
  that runs longer than `--healthy-reset` (default 600s) counts as healthy
  and resets the budget, so one bad hour cannot permanently stop a project.
- A graceful supervisor shutdown (Ctrl+C) first asks the daemon to stop
  through the control channel (`auto stop` semantics), then terminates and
  kills after a grace period — no orphaned children.
- State is in `.agents/execution/supervisor.json` (pid, child pid, restarts,
  heartbeat); a pid lock (`supervisor.lock`) prevents two supervisors on one
  project while tolerating stale locks from dead processes. Lifecycle events
  (`supervisor.started`, `supervisor.child_started`, `supervisor.child_exited`,
  `supervisor.restarting`, `supervisor.completed/stopped/gave_up`) land in the
  project event log.

```bash
auto supervise --max-restarts 10 --restart-delay 2
```

## OS registration (`auto supervisor install`)

The supervisor is itself a process; the OS registration makes it survive
logoff and reboot by running `... supervise --path <project>` under the
system's service scheduler:

- **Windows**: a Scheduled Task created via PowerShell
  `Register-ScheduledTask` — trigger `AtLogOn` pinned to the registering
  user, `RestartCount 124` (the Task Scheduler max) with a 1-minute
  `RestartInterval` (restart-on-failure), `ExecutionTimeLimit Zero` (no time
  limit), `MultipleInstances IgnoreNew`. Project paths with spaces are
  quoted into the task's argument string.
- **Linux**: a systemd user unit (`Restart=always`, `RestartSec=5`).
- **macOS**: a launchd LaunchAgent (`RunAtLoad` + `KeepAlive`).

```bash
auto supervisor install      # register (idempotent; re-running overwrites)
auto supervisor status       # registration state + live supervisor/daemon pids
auto supervisor uninstall    # deregister
```

Task name: `AutoEngineSupervisor-<path-slug>` — one registration per project
directory. Registration does **not** start the daemon immediately on Windows
(the trigger fires at next logon; start it once with `auto supervise` or
`Start-ScheduledTask` if you need it now).

For machines where the daemon must run with **no user logged in**, register a
Windows *service* instead (the supervisor command is service-agnostic):

```powershell
nssm install AutoEngineSupervisor "<python.exe>" -X utf8 -m autonomous_engine.cli.app supervise --path "D:\projects\demo"
nssm set AutoEngineSupervisor AppDirectory "D:\projects\demo"
nssm set AutoEngineSupervisor AppStdout "D:\projects\demo\.agents\supervisor.log"
nssm set AutoEngineSupervisor AppStderr "D:\projects\demo\.agents\supervisor.log"
nssm start AutoEngineSupervisor
```

## Stop contract

| Operator action | Daemon stop | Daemon exit | Supervisor |
| --- | --- | --- | --- |
| `auto stop` | `USER_REQUESTED` | `0` | honors it, ends cleanly |
| `auto pause` → `auto resume` | — (daemon never stops) | — | keeps supervising |
| budget wall / safety block | reason recorded | `2` | restarts within budget |
| crash / kill -9 of the daemon | — | non-zero | restarts with backoff |
| crash / kill -9 of the supervisor | daemon keeps running (orphaned) | — | next logon / service restart brings a new supervisor |

## Operational notes

- `auto supervisor status` shows the OS registration, the live supervisor pid
  and daemon pid, restart count, and heartbeat — the first stop when
  something is not running.
- Events are the audit trail: `grep supervisor .agents/execution/events.jsonl`.
- The registration stores the *absolute* python and project paths; moving a
  venv or the project requires re-running `auto supervisor install`.
