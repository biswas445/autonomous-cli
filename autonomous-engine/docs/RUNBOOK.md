# Runbook

Operating the runtime: first run, real models, supervision, failure handling,
recovery, and evaluation.

## First run (no credentials needed)

```bash
pip install -e ".[dev]"

mkdir my-project && cd my-project
auto init --objective "Build a small REST API for managing notes"

# fully offline dry run: the echo provider exercises the whole loop
auto run --provider echo --no-director

auto status
auto tasks --all
auto events -n 40
auto checkpoints
```

The offline run compiles intent, plans a task graph, implements each task with
a deterministic fallback coder, runs real verification commands (the
Definition-of-Done engine executes them), records review/security evidence,
passes the QA gate, and finishes with release notes and a git tag.

## Using real models

```bash
export OPENAI_API_KEY=sk-...
# or
export ANTHROPIC_API_KEY=sk-ant-...
```

Edit `.agents/config.json` and point the roles you want at real providers:

```json
"model_routes": [
  {"role": "coder", "provider": "openai", "model": "gpt-4.1",
   "fallbacks": ["anthropic/claude-sonnet-4"]},
  {"role": "director", "provider": "anthropic", "model": "claude-sonnet-4"}
]
```

Then `auto run`. Everything else — verification, evidence, commits, stops —
is unchanged; models only ever propose.

Local models (Ollama/vLLM/LM Studio) work via the OpenAI-compatible provider:
set `OPENAI_BASE_URL` to your endpoint and export
`AUTO_ALLOW_PRIVATE_ENDPOINTS=true` to acknowledge the local endpoint.

## Supervised operation (§16)

```bash
auto run --supervised          # high-risk tasks need explicit approval
# ... run stops with HUMAN_APPROVAL_REQUIRED ...
auto status                    # shows pending escalations
auto inspect TASK-007
auto approve ESC-xxxx          # or: auto approve all
auto run                       # resumes; approval persists across restarts
```

## Pausing and stopping a live run

From a second terminal (the loop polls the control file each cycle):

```bash
auto pause --reason "hold for review"
auto resume
auto stop --reason "end of day"
```

State is persisted every cycle; stopping loses nothing.

## When a task keeps failing

The failure path is: `FAILED -> DIAGNOSING -> REPAIRING -> READY` with the
debugger's root cause, affected files, and required tests recorded each time.
After `budget.max_task_attempts` failures the task goes to
`ARCHITECTURE_REVIEW`, a human escalation is raised, and the run stops with
`REPEATED_FAILURE`/`HUMAN_APPROVAL_REQUIRED`. Failure memory (`.agents/memory/
failures.json`) records the lesson so later agents do not retry the same
approach.

```bash
auto inspect TASK-007 --json     # attempts, evidence, verification output
auto events -n 100               # the exact failure chain
```

To resume after changing the plan or architecture: edit the graph via
`auto inspect graph --json` (or let the director replan), resolve the
escalation (`auto approve all`), and `auto run`.

## Crash recovery (§24, §25)

A crash (kill -9, terminal closed, machine reboot) loses at most the current
cycle:

```bash
auto run            # recovers stale tasks, re-verifies untrusted work, continues
auto checkpoints    # restorable snapshots exist after every completed task
auto rollback checkpoint-0004 --yes   # restore graph + tree (destructive)
```

## Telling the agents what you want (memory hierarchy)

Agent memory has three layers, all read into every session:

1. `.agents/constitution.md` — generated project law (never hand-edited).
2. `.agents/instructions.md` — **yours**: how you want work done in this project
   (the CLAUDE.md pattern). `auto init` writes a template; edit it freely.
3. `~/.auto_engine/instructions.md` — personal preferences across all projects.

```bash
auto instructions            # view both instruction scopes
auto instructions --paths    # just the file paths
auto remember "PostgreSQL is the production database"   # persist a fact
```

On top of that, the coding agent receives a **repository map** — the key
symbols per file ranked by relevance to the task and git recency (the aider
repomap idea, dependency-free) — and proposes `replace` edits (search text +
replacement) instead of rewriting whole files, like aider's editblock format.
A search that matches nothing is rejected and reported, never guessed.

## Situational guidance: microagents

`instructions.md` loads for every task; microagents load **only when they
match**. Put project knowledge next to the code it describes:

```markdown
<!-- .agents/microagents/billing.md -->
---
triggers: billing, invoice, payment
---
- Money amounts are integers in cents; never floats.
```

```bash
auto microagents                          # list what exists
auto microagents --match "invoice refund" # check which would fire
```

A file with no `triggers:` loads for every task (use sparingly). Matching is a
case-insensitive substring test on the task title and description.

## Wiring the runtime to your tools: event hooks

```json
// .agents/hooks.json
{"hooks": [
  {"event": "task.failed",  "command": "python notify.py", "timeout": 10},
  {"event": "escalation.*", "command": "python page_oncall.py"}
]}
```

Hooks receive `{"event": ..., "payload": {...}}` on stdin and run without a
shell (argv lists only). Failures, timeouts, missing binaries, and malformed
config are recorded (`hook.failed` / `hook.invalid_config`) and never
interrupt the run. `auto hooks` shows the configuration and recent runs;
`.agents/agent_logs/hooks/hooks.log` has the full history. Prefer specific
events — `task.*` also matches the frequent `task.state_changed`.

## Command risk: what the sandbox refuses

Every command an agent runs passes two gates: the permission-class allowlist,
then the risk analyzer (`auto risk "git push --force"` to inspect). HIGH-risk
operations — force pushes, package publishes, recursive force deletes,
destructive SQL, cluster/infra deletion — are **refused** even when the verb is
allowlisted, unless the permission class sets `allow_high_risk` or a human
approved the task in supervised mode (where such tasks are gated and the
commands are shown in the escalation). MEDIUM-risk commands (hard resets,
recursive deletes, uninstalls) run normally in autonomous mode and require
approval in supervised mode.

## Long-running mode (§45)

```bash
auto daemon --poll-seconds 5        # keeps going until the project is done
```

The daemon resolves human gates by waiting: approve with `auto approve <id|all>`
(a rejected escalation cancels its task), resume a pause with `auto resume`,
stop everything with `auto stop`. Dead ends (no runnable task, environment
failure) get bounded automatic restarts (`--max-restarts`, default 5). The
daemon's liveness is in `.agents/execution/daemon.json`; every cycle state is
persisted, so killing the daemon and re-running it loses nothing.

## Unattended operation: process supervisor + OS registration

`auto daemon` is one Python process; the supervisor keeps that process alive
across crashes, and the OS registration keeps the supervisor alive across
logoffs and reboots (full reference: `docs/DAEMON.md`):

```bash
auto supervise               # daemon as a child process: crash → restart, forever
auto supervisor install      # Windows Scheduled Task / systemd / launchd at logon
auto supervisor status       # registration + live supervisor/daemon pids
auto supervisor uninstall    # deregister
```

`auto stop` ends a supervised daemon cleanly (exit 0 — the supervisor honors
it); a crashed daemon is restarted with exponential backoff within a bounded
budget that healthy runs reset.

## Inspecting the system

```bash
auto status          # progress + operational view: phase, current task, next action
auto metrics         # §40 metrics: tasks, retries, pass rate, cost, commits, escalations
auto metrics --save  # also writes .agents/metrics.json + metrics.md
auto roadmap         # §31 milestone roadmap derived from the task graph
auto inspect run     # raw run doc, escalations, budget usage
```

## Evaluating the system (§41)

The offline echo provider turns any objective into a reproducible benchmark:

```bash
for goal in "Build a REST API" "Build a CLI tool"; do
  mkdir -p eval/$i && (cd eval/$i && auto init --objective "$goal" && \
    auto run --provider echo --no-director --json > result.json)
done
```

Measure: did it finish (`status: completed`), cycles, cost
(`budget.cost_usd`), retries (task attempts), escalations, and replans — all
recorded in the run stats and event log. This is the harness for judging
whether an orchestration change actually helps.

## Development

```bash
# Isolated environment (recommended): the runtime itself needs only
# typer/rich/pydantic/httpx — everything else it uses is the standard library.
python -m venv .venv && . .venv/Scripts/activate   # (.venv/bin/activate on POSIX)
pip install -e ".[dev]"

pytest                     # 220 tests, fully offline, no model calls
ruff check src tests
ruff format src tests
```

Dependency policy: every new capability must justify a dependency. Everything
ported from other CLIs (repo map, microagents, risk analyzer, hooks, entropy
scanner, search/replace edits) is stdlib-only by design; heavier optional
tools stay behind explicit opt-ins (e.g. `sandbox_backend: "docker"`).

Test layout mirrors the architecture: core state machine/graph, security and
sandbox, events/store, DoD engine, git, providers/router, agents, orchestrator
end-to-end (completion, failure loop, escalation, supervision, pause, QA gate,
worktree parallelism, recovery), the OSS-inspired features (repo map,
instructions, microagents, risk analyzer, hooks, entropy secrets), and the CLI.
