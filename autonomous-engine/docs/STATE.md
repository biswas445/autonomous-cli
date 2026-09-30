# State, persistence, and recovery

Everything the system needs to survive a crash, a context reset, or a week of
runtime is on disk. Conversational context is never load-bearing.

## The Project Brain (`.agents/`)

```
.agents/
├── config.json                    ProjectConfig (budget, permissions, routes, mode)
├── project.json                   identity, objective, compiled intent, status
├── constitution.md                the project law (written once at init)
├── requirements/
│   ├── specification.md           the compiled intent as markdown
│   └── requirements.json          product agent output
├── architecture/
│   ├── architecture.md            the architecture document
│   └── decisions.md               binding decisions (every agent reads these)
├── planning/
│   ├── roadmap.json               milestones (reserved for milestone planning)
│   └── task_graph.json            the full TaskGraph (mirror of SQLite)
├── memory/
│   ├── facts.json                 permanent project facts
│   ├── discoveries.md             timestamped research/discovery log
│   └── failures.json              failure memory: attempt, cause, lesson
├── execution/
│   ├── current_run.json           live run doc: cycle, progress, budget, locks,
│   │                              approved task ids
│   ├── events.jsonl               append-only event log (the audit trail)
│   ├── control.json               cross-process control request (consumed once)
│   └── escalations.json           escalations with status pending/approved/rejected
├── verification/
│   ├── test_results/
│   ├── review_results/            per-task review + security reports
│   └── qa_gate.json               the final intent-vs-delivery verdict
├── release/release.json           release report (notes, version, checks)
├── checkpoints/checkpoint-NNN.json  restorable snapshot stubs
├── agent_logs/<agent>/            per-agent structured outputs
└── state.sqlite                   SQLite: tasks, runs, events, checkpoints,
                                   decisions, failures, unknowns, budget usage
```

Both the JSON files (human-inspectable) and SQLite (transactional) hold the
task graph; the orchestrator writes both. Loading prefers the JSON graph and
falls back to SQLite — each tolerates the other being ahead or torn.

## Task states and legal transitions (plan.md §11)

```
QUEUED -> READY -> ASSIGNED -> IMPLEMENTING -> VERIFYING -> REVIEWING -> COMPLETED
                     |             |              |             |
                     v             v              v             v
                  CANCELLED      FAILED  <------ + ---------- REPAIRING
                                    |                          ^
                                    v                          |
                          DIAGNOSING -> REPAIRING -> READY (requeue)
                                    |
                        ARCHITECTURE_REVIEW -> REPLAN -> QUEUED
```

- Only `Task.set_state` mutates status, and only along legal edges; everything
  else raises `IllegalTransition`.
- `READY -> REPLAN` exists so a schedulable task whose plan was invalidated is
  visibly re-planned rather than silently requeued.
- `VERIFYING -> COMPLETED` is allowed for simple tasks that skip review; the
  orchestrator decides when that applies (risk/complexity policy).
- Agents never transition state. The orchestrator does, after evidence.

## Runs

Each `auto run` creates a `RunRecord` (SQLite `runs`) and writes
`current_run.json` every cycle. A run ends with a named stop condition —
`PROJECT_COMPLETE`, `HUMAN_APPROVAL_REQUIRED`, `BUDGET_EXCEEDED`,
`RUNTIME_LIMIT`, `REPEATED_FAILURE`, `PAUSED`, `USER_REQUESTED`,
`NO_PROGRESS`, ... — or succeeds only via `PROJECT_COMPLETE`.

## Event sourcing (§9)

Every meaningful action appends to `execution/events.jsonl` and a SQLite
mirror: run lifecycle, intent, plan changes, task state changes, verification
results, agent starts/finishes/crashes, commits, checkpoints, escalations,
budget events, stop conditions. The log is the audit trail and the primary
debugging surface (`auto events`).

## Checkpoints (§25)

After every completed task the orchestrator:

1. creates a git commit `checkpoint(completed): <title> [<id>]`;
2. records a `CheckpointRecord` (SQLite) holding the commit, objective,
   outstanding failures, environment metadata, the **entire task graph**, and
   project state;
3. writes a stub `.agents/checkpoints/checkpoint-NNN.json`.

`auto rollback <id> --yes` restores the task graph from the record and (by
default) hard-resets the working tree to the recorded commit. This is the
documented destructive operation and therefore confirms first.

## Recovery (§24)

On startup, before the loop, `orchestrator.recover_stale_tasks()` finds tasks
left in active states (ASSIGNED/IMPLEMENTING/VERIFYING/REVIEWING/DIAGNOSING/
REPAIRING) by a dead process and returns them to READY: verification-stage
work is re-verified rather than trusted. The recovery is emitted as
`run.recovered` with the task ids.

Resuming after a stop is just `auto run` again: the graph loads, planning is
skipped when tasks exist, stale work is recovered, and approved-task
persistence (in `current_run.json`) keeps supervised approvals intact across
restarts.

## SQLite schema

Tables: `projects`, `tasks` (id, project, payload JSON, status, priority,
attempts, timestamps), `runs`, `events`, `checkpoints`, `decisions`,
`failures`, `unknowns`, `budget_usage`, `agent_activity`. All access is via
bound parameters (`core/store.py` is the only writer). WAL mode is on;
readers tolerate torn rows by design.
