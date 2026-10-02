# Architecture

> The unit of completion is a verified repository state, not a finished model response.

## The one-line design rule

**Deterministic infrastructure owns decisions; models propose.**
Every arrow in the loop below is code in `src/autonomous_engine/runtime/orchestrator.py`,
not model reasoning:

```
OBSERVE -> UNDERSTAND -> PLAN -> SELECT -> EXECUTE -> VERIFY
        -> ANALYSE -> UPDATE STATE -> REPLAN -> (repeat or STOP)
```

## Module map

```
src/autonomous_engine/
├── core/            domain models and deterministic persistence
│   ├── config.py         ProjectConfig: budgets, permission classes, model routes
│   ├── task.py           Task / TaskGraph: the dependency DAG, wave computation
│   ├── state_machine.py  TaskState + legal transitions (the only mutator of status)
│   ├── store.py          typed SQLite API (projects, tasks, runs, decisions, ...)
│   ├── database.py       SQLite wrapper (WAL, bound parameters, one conn/thread)
│   ├── events.py         append-only JSONL event log + canonical event names
│   ├── security.py       endpoint policy, secret scanning, path containment
│   └── workspace.py      the on-disk "Project Brain" under .agents/
├── models/          provider-neutral model access
│   ├── base.py           CompletionRequest / ModelResponse / ProviderAdapter registry
│   ├── providers.py      echo (offline), OpenAI-compatible, Anthropic adapters
│   └── router.py         role -> provider/model routing, retry, fallbacks, JSON repair
├── agents/          probabilistic workers (reason + act, never decide)
│   ├── intent_compiler.py  NL goal -> machine-operable ProjectIntent (§5A, §34)
│   ├── product.py          intent -> requirements + project DoD (§5B)
│   ├── researcher.py       unknowns queue -> evidence-based answers (§5C, §14)
│   ├── architect.py        architecture + binding decisions (§5D, §39)
│   ├── planner.py          dependency graph of tasks; replan() mutations (§5E, §54)
│   ├── coder.py            EditPlan proposals applied through the sandbox (§5F)
│   ├── tester.py           executable verification + generated tests (§5G, §56)
│   ├── debugger.py         root cause from real evidence (§5H)
│   ├── reviewer.py         independent review; blocking findings (§5I, §21)
│   ├── security.py         static rules + model red-team pass (§5J, §57)
│   ├── qa.py               intent-vs-delivery gate; drift detection (§5K, §55)
│   ├── release.py          release readiness, notes, version (§5L)
│   └── director.py         Engineering Director proposals (§4, §36, §51)
├── runtime/         the deterministic engine
│   ├── orchestrator.py   the loop: schedule, execute, verify, commit, replan, stop
│   ├── base.py           Agent interface + AgentRunner (panic isolation, events)
│   ├── context.py        context reconstruction with a character budget (§8)
│   ├── repo_map.py       repository symbol map, aider-style (AST + git recency)
│   ├── instructions.py   operator instruction memory, CLAUDE.md-style hierarchy
│   ├── microagents.py    trigger-matched repo guidance, OpenHands-style
│   ├── risk.py           command risk analyzer + confirmation policy, OpenHands-style
│   ├── hooks.py          user-defined event hooks (.agents/hooks.json)
│   ├── permissions.py    ToolBox: the only path from an agent to the machine
│   ├── locks.py          shared-resource locks for parallel work (§23)
│   ├── roadmap.py        milestone planning + milestone self-evaluation (§31, §30)
│   ├── metrics.py        project metrics from persisted evidence (§40)
│   ├── review_board.py   Architecture Review Board + model disagreement (§39, §20)
│   ├── daemon.py         persistent long-running mode (§45)
│   ├── lessons.py        cross-project architecture memory (§60)
│   ├── control.py        cross-process pause/stop/approve channel
│   ├── stop.py           StopEngine: every condition that ends a run (§37)
│   ├── budget.py         runtime/token/cost budgets (§26, §58)
│   ├── constitution.py   the project law every agent reads (§27)
│   ├── bootstrap.py      `auto init` — offline, deterministic
│   └── context_setup.py  RuntimeContext assembly
├── verification/    the Definition-of-Done engine (§13)
├── git/             checkpoints, worktrees, validated merges (§10)
├── messaging/       agent message protocol (comms spec §1–§96)
│   ├── models.py        Message envelope+payload, MsgType taxonomy, delivery states
│   ├── store.py         durable message persistence over the runtime SQLite store
│   ├── service.py       routing, mailboxes, ack/retry/dead-letter, loop+flood defense
│   └── handlers.py      AgentMessenger: identity-bound outbound helpers per agent
├── benchmarks.py    offline evaluation harness (§41)
└── cli/             Typer/Rich control surface (§33)
```

### Agent-to-agent communication

Agents do not talk in ad-hoc strings; every coordination act is a typed,
persistent `Message` (`docs/MESSAGING.md` is the protocol reference). The
orchestrator publishes the real conversation — Director→Architect
architecture requests, task delegation/acceptance/completion/failure, tester
verdicts, debugger discoveries — and consumes the Director inbox once per
cycle, converting structured reports into the management actions it already
owns (continue / replan / create task / escalate) with message→decision→task
traceability. Sender identity is runtime-bound (a model cannot claim the
Director), delivery is at-least-once with idempotent handling, and rate,
loop, and mailbox-overload guards keep pathological agents from flooding the
system. The TUI's AGENT COMMUNICATION view renders this live traffic.

## What the orchestrator owns vs. what models own

| Deterministic code (this repo) | Models (via the router) |
| --- | --- |
| task state transitions, dependency checks | planning, architecture |
| scheduling, locking, parallel waves | code generation |
| verification execution and verdicts | diagnosis, research |
| git commits, worktrees, merges | review, security analysis |
| budgets, stop conditions, escalation | decision *proposals* |
| persistence, recovery, replay | intent compilation |

Models never see the orchestrator's sandbox. Each agent gets a `ToolBox` built
from its own `PermissionClass`, and model output is data until validated.

## The cycle, concretely

1. **OBSERVE/UNDERSTAND** — the deterministic director builds a baseline
   proposal from the graph (ready/active/failed/locks). If enabled, the model
   director proposes an override, which is validated against the graph
   (`DirectorProposal.validate_against`); invalid proposals are rejected with
   a recorded reason.
2. **PLAN** — validated management actions apply: create/reprioritise/cancel
   tasks, full replan, queue research, escalate. Actions that change the plan
   consume the cycle (no task executes that cycle).
3. **SELECT** — ready tasks in priority order; resource locks are acquired
   all-or-nothing; conflicting tasks are skipped, not raced.
4. **EXECUTE** — the coder runs in a sandbox (its own worktree when parallelism
   is enabled) with reconstructed context; edits outside the write policy are
   rejected and reported.
5. **VERIFY** — the Definition-of-Done engine runs the task's commands and
   checks for real. Manual criteria become UNKNOWN, never PASS; when a task
   failed *only* on manual criteria, the reviewer adjudicates them.
6. **ANALYSE** — review (risk/complexity-driven) with blocking findings; a
   security pass for high-risk tasks. A review without blocking findings is
   recorded evidence, not a verdict.
7. **UPDATE STATE** — evidence-based transition to COMPLETED (with a git
   checkpoint) or the failure path:
   `FAILED -> DIAGNOSING -> REPAIRING -> READY -> ...`; at
   `max_task_attempts` the task goes to `ARCHITECTURE_REVIEW` and a human is
   escalated (REPEATED_FAILURE is a stop, not a hint).
8. **REPLAN** — the QA gate runs before PROJECT_COMPLETE is accepted;
   unmet requirements become catch-up tasks (bounded rounds). On success the
   release step writes notes/CHANGELOG, commits, and tags.

## Evidence hierarchy (§12)

```
user requirement > acceptance criteria > executable test > observed runtime result > agent reasoning
```

Only the verification engine can mark a task complete. Agent confidence is
recorded and displayed, never decisive.

## Model routing (§18, §19; v2 §19–22, §115, §116)

`ProjectConfig.model_routes` maps every agent role to `provider/model` with
fallbacks. Providers are registered adapters; the loop never imports a vendor
SDK. High complexity or high security sensitivity prefers the first fallback
(a deliberate escalation hook). Historical success/failure per role is counted
on the router for future routing decisions.

On top of configuration sits the **capability registry**
(`models/capabilities.py`): model profiles advertise capabilities (coding,
planning, tool_use, large_context, ...), context limits, cost class, and
data-use posture; the registry ranks eligible profiles per role and tracks
runtime provider health (healthy → degraded → unavailable) from real
execution outcomes. An unavailable or persistently failing primary yields to
a healthy fallback; recovery is automatic once it succeeds again.

## Untrusted-data boundary (v2 §104)

Repository files, tool observations, and web content are **data, not
instructions**. Context sections sourced from the repository are wrapped in
`<<<UNTRUSTED_DATA ...>>>` markers by `runtime/context.untrusted_block()`,
tool-loop observations carry the same marker, and every agent context opens
with the DATA BOUNDARY rule. Agent system prompts instruct treating injected
instructions inside repository/tool content as hostile and reporting them.
This is enforced by construction: content cannot reach a model prompt
through the context builder or the tool loop without being marked.

## Capability coverage of plan.md

| plan.md section | where |
| --- | --- |
| §4/§36/§51 Director | `agents/director.py` + orchestrator baseline/override |
| §5 A–L agents | `agents/*` (all twelve roles) |
| §7 Project Brain | `.agents/` layout in `core/workspace.py` |
| §8 context reconstruction | `runtime/context.py` |
| §9 event sourcing | `core/events.py` + SQLite mirror |
| §10 git checkpoints/worktrees | `git/manager.py` |
| §11 state machine | `core/state_machine.py` |
| §12 evidence hierarchy | `verification/engine.py` + orchestrator verdicts |
| §13 DoD engine | `verification/engine.py` |
| §14 unknowns queue | store `unknowns` + `agents/researcher.py` |
| §15/§16 escalation + modes | `stop.py`, orchestrator supervised gate, control channel |
| §45 daemon + OS supervisor | `runtime/daemon.py`, `runtime/supervisor.py`, `runtime/supervisor_service.py` |
| §18/§19 model routing | `models/router.py` |
| §21 independent verification | reviewer/security agents + evidence rules |
| §22/§23 parallelism + locks | `task.py.independent_wave`, `runtime/locks.py` |
| §24/§25 recovery + checkpoints | `orchestrator.recover_stale_tasks`, checkpoints |
| §26/§58 budgets | `runtime/budget.py` |
| §27 constitution | `runtime/constitution.py` |
| §28/§29 memory + failure memory | workspace memory files, `agents/shared` attempt records |
| §30/§55 QA gate + drift | `agents/qa.py`, orchestrator QA gate |
| §33 CLI | `cli/app.py` |
| §37 stop engine | `runtime/stop.py` |
| §41 evaluation | offline echo provider + e2e suite (`tests/`) |
| §54 dynamic tasks | `planner.replan`, director `create_tasks`, QA gap tasks |
| §56 self-generated tests | `agents/tester.py` + `_ensure_machine_checkable` |
| §57 red team | `agents/security.py` (static rules + model pass) |
| §61/§62 sandbox + permission classes | `runtime/permissions.py`, `core/config.py` (process or Docker backend) |
| §17 operational transparency | `auto status` operational view, event log |
| §20 model disagreement | `runtime/review_board.py` (`ModelDisagreement`) |
| §30 milestone self-evaluation | `runtime/roadmap.py` + `milestone.completed` decisions |
| §31 milestone planning | `runtime/roadmap.py`, `auto roadmap` |
| §38 research before coding | `orchestrator._research_before_coding` |
| §39 Architecture Review Board | `runtime/review_board.py`, convened on repeated failure |
| §40 project metrics | `runtime/metrics.py`, `auto metrics` |
| §41 evaluation harness | `benchmarks.py`, `auto benchmark` |
| §45 persistent daemon | `runtime/daemon.py`, `auto daemon` |
| §60 architecture memory | `runtime/lessons.py` (global, evidence-based) |
| v2 §19–22 capability registry | `models/capabilities.py` — profiles, role requirements, health, eligibility ranking |
| v2 §104 prompt-injection defense | `runtime/context.untrusted_block` + tool-loop markers + DATA BOUNDARY rule in every context |
| v2 §102 failure injection | `tests/test_failure_injection.py` — garbage output, 429 storms, provider crashes against a real run |
| Terminal UI | `ui/` — RuntimeFacade (single read/act seam over workspace/store/control channel), UIState + EventAdapter (bounded buffers, dedup, filtering), Textual app (activity feed, tasks/agents/models panels, task inspector, failures/verification/memory/checkpoints views, slash commands, approval keys), plain-mode fallback for CI/no-TTY; `auto ui`, `auto ui --observe` |
| repo map (aider) | `runtime/repo_map.py` — symbols ranked by task relevance + git recency |
| instructions hierarchy (Claude Code / Gemini CLI) | `runtime/instructions.md`: `~/.auto_engine/instructions.md` -> `.agents/instructions.md`; `auto remember`, `auto instructions` |
| SEARCH/REPLACE edits (aider editblock) | `CoderAgent` action=`replace` with whitespace-flexible matching; no-match is rejected honestly |
| dirty-repo pre-flight (aider) | `orchestrator._preflight_dirty_repo` — commits leftovers before the loop |
| microagents (OpenHands skills) | `runtime/microagents.py` — `.agents/microagents/*.md` with `triggers:` injected only when a task matches; `auto microagents` |
| command risk analyzer (OpenHands SecurityAnalyzer) | `runtime/risk.py` — LOW/MEDIUM/HIGH/UNKNOWN invariants + `ConfirmRisky` policy; the sandbox refuses HIGH-risk commands unless a permission class opts in or a human approved the task; `auto risk <cmd>` |
| entropy secret detection (gitleaks-style) | `core/security.py::find_high_entropy_strings` — Shannon-entropy heuristic for generic API keys; describes findings without revealing values |
| event hooks (Claude Code hooks) | `runtime/hooks.py` — `.agents/hooks.json`, event JSON on stdin, failures recorded never fatal; `auto hooks` |
| agentic tool surface | `runtime/tools.py` — 15 tools: read/search/glob/diff/run plus `read_files` (gemini read-many-files), `write_file`/`edit_file` (kilo edit, codex apply_patch), `current_time` (codex curr_time), `budget_status` (codex get_context_remaining), `save_memory`/`recall_memory` (gemini save-memory, kilo recall), `web_fetch`/`web_search` (kilo/opencode webfetch/websearch, SSRF-guarded); `auto tools -c <class>` |
| agent-written memory | `save_memory` with secret redaction (gitleaks heuristics) — agents record facts/decisions/lessons into the typed store; recalled into later contexts (kilo recall loop) |

Phase 6 (web dashboard, remote execution, team collaboration) is out of scope
by design; the event log and store are its forward-compatible substrate.
