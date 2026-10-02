# Autonomous Engineering Runtime (`auto`)

> Give the system a software goal once, and let an autonomous AI engineering
> organization convert that goal into a plan, implement it, verify it, repair
> failures, re-plan when reality changes, and continue working until the
> project reaches a defined completion state.

This repository is the **runtime**, not a prompt: a deterministic orchestration
core that coordinates AI agents. The CLI is only the control surface.

## Status

Implemented per `plan.md` MVP v0.1 → v0.5 scope (see `docs/ARCHITECTURE.md`
for the full capability map). Covered: the autonomous loop, the Engineering
Director, twelve specialist agents, intent compilation, task-graph planning
with dynamic replanning, executable evidence (Definition-of-Done engine),
independent review and security passes, a QA gate before completion, release,
git checkpoints and worktree parallelism, event sourcing, budgets, resource
locks, supervised approval gates, and crash recovery. The default provider is
an offline `echo` model used for testing; real providers (OpenAI-compatible,
Anthropic, local) plug in via configuration with no orchestrator changes.
For unattended runs, `auto supervise` keeps the daemon alive across process
crashes and `auto supervisor install` registers it with the OS (Windows Task
Scheduler with restart-on-failure, systemd, or launchd) so autonomy survives
logoffs and reboots — see `docs/DAEMON.md`.

## Quick start

```bash
pip install -e ".[dev]" pre-commit
pre-commit install        # ruff + secret-scan on every commit

# 1. Create a project (offline, deterministic — no model call)
auto init --objective "Build a small REST API for managing notes"

# 2. Point agents at a real model (choose ONE of the two):
#    a) standard env vars:
export OPENAI_API_KEY=sk-...          # or ANTHROPIC_API_KEY
#    b) a .env file at the project root or above, grouped per provider:
#       baseurl = https://your-provider.com/v1
#       apikey  = sk-...
#       models  = your-model-id
#       (blocks are blank-line separated; each becomes a named provider the
#       routes can reference — see docs/CONFIGURATION.md)

# 3. Run autonomously until completion criteria are met
auto run

# ... or fully offline first: the echo provider exercises the whole loop
auto run --provider echo --no-director

# Long-running mode (days/months): daemon under a crash-restarting supervisor
auto daemon                            # in-terminal long-running mode
auto supervise                         # supervisor restarts the daemon on crash
auto supervisor install                # register with the OS (survives reboot)

# Supervised mode: high-risk tasks require explicit approval
auto run --supervised

# Optional: tell the agents how you want work done (CLAUDE.md-style memory)
$EDITOR .agents/instructions.md
auto remember "PostgreSQL is the production database"
```

## CLI

```text
auto init [path] --objective "..."   Create project state (.agents/) + constitution
auto run ["objective"]               Run the autonomous engineering loop
auto status                          Show project state, task graph progress
auto inspect <TASK-id|graph|run>     Detailed view of state / events / evidence
auto pause | resume | stop           Control a running orchestration
auto approve | reject <id|all>       Resolve a pending escalation (supervised mode)
auto rollback <checkpoint-id> --yes  Restore a checkpoint
auto daemon                          Long-running mode: wait on gates, auto-resume (§45)
auto metrics [--save]                Project metrics from persisted evidence (§40)
auto roadmap                         Milestone roadmap (§31)
auto benchmark [--out DIR]           Offline evaluation harness (§41)
auto remember <fact>                 Persist a fact for every future agent
auto memory [-q TEXT] [--add TEXT]   Inspect/curate the typed memory store
auto tools -c coder                  Show an agent class's sandboxed tool surface
auto ui [-o "objective"] [--observe] Full interactive terminal UI (Textual)
auto instructions                    View operator instructions (project + global)
auto microagents [--match TEXT]      List trigger-matched repo guidance
auto risk "command line"             Classify a command's risk level
auto hooks                           List event hooks and recent runs
auto events [-n 50]                  Tail the event log
auto config --set budget.max_token_budget=500
auto reset --hard --yes              Destroy project state (keeps repository)
auto supervise                       Daemon under a crash-restarting supervisor
auto supervisor install|status|uninstall
                                     OS registration (Task Scheduler/systemd/launchd)
auto verify task <id>|history <id>|stale|policy
                                     Verification evidence + quality-gate inspection
auto runtime start|status|stop|attach
                                     Persistent runtime daemon over local IPC
```

## What the runtime owns vs. what models own

| Deterministic infrastructure (this codebase) | Probabilistic intelligence (models) |
| --- | --- |
| Task state machine, dependency checks, DAG scheduling | Planning, architecture |
| Git checkpoints, worktrees, merges | Code generation |
| Locks, retries, timeouts, budgets | Diagnosis, research |
| Persistence (SQLite + JSONL events), recovery | Review, decision proposals |
| Permissions, sandbox policies, process management | Classification |
| Definition-of-Done evaluation, evidence hierarchy | |

## Terminal UI

`auto ui` opens a full interactive TUI over the runtime: live activity feed,
tasks/agents/models panels, task inspector, failures, verification, memory,
checkpoints, approval keys (A/J), pause/resume (p), cancel (x), Intent
Compiler toggle (e), slash commands (/status /tasks /filter /inspect
/remember /pause /resume /stop), and a help screen (?). Without a TTY it
falls back to plain line output automatically, so scripts and CI are safe.

The UI is a client of the runtime: all state is read from runtime
persistence, all controls go through the run control channel, and all events
come from the runtime event log — nothing simulated. With `--observe` it
attaches read-only to a runtime owned by another process (e.g. `auto daemon`).

## Agent tools

Agents work through a sandboxed tool surface (the same pattern as every
serious coding CLI), executed inside the permission policy mid-reasoning:

```text
read_file  read_files  list_dir  search  glob  git_diff    read the repository
write_file  edit_file                                      write / SEARCH-REPLACE inside write_paths
run_command                                                allowlisted dev commands
save_memory  recall_memory                                 typed project memory (secrets refused)
current_time  budget_status                                environment awareness
web_fetch  web_search                                      public docs, network policy gated
```

Every tool call is permission-checked, observations come back to the model,
and writes are tracked into the task's artifacts. `auto tools -c <class>`
shows the surface for one agent class.

## Agent communication

Agents coordinate through a typed, persistent message protocol (`messaging/`):
 envelopes with runtime-bound sender identity, per-type validated payloads,
 request/response correlation, threads, priorities, and delivery states from
 QUEUED to COMPLETED/DEAD. The orchestrator publishes the real conversation
 (Director → Architect → Coder → Tester) and consumes the Director inbox once
 per cycle, turning structured reports into continue/replan/create-task/
 escalate decisions with full traceability. Delivery is at-least-once with
 idempotent handling; flood, loop, and mailbox-overload protection are built
 in. See `docs/MESSAGING.md` and press `4` in the TUI to watch the live
 AGENT COMMUNICATION view.

## Safety

- Every agent class has explicit least-privilege permissions (filesystem
  globs, command allowlists, network, git-write).
- Model output is treated as untrusted; work happens in isolated worktrees
  when parallelism is enabled and merges only validated work.
- Agent messages never grant permissions: communication is coordination, not
  privilege escalation.
- Secrets are never hardcoded; providers read credentials from the
  environment.

## Documentation

- `docs/ARCHITECTURE.md` — architecture, module map, plan.md capability coverage
- `docs/MESSAGING.md` — agent message protocol: types, lifecycle, identity, delivery semantics
- `docs/STATE.md` — task state machine, persistence schema, recovery
- `docs/CONFIGURATION.md` — models/providers, budgets, permissions, modes
- `docs/RUNBOOK.md` — operating the loop, recovery, evaluation harness
