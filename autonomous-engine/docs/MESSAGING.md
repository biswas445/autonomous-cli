# Agent Message Protocol (v1)

The agent-to-agent communication layer: typed, persistent, correlated
messages between the runtime's agents and the Engineering Director.
This is a protocol reference for future agents and maintainers.

The implementation lives in `src/autonomous_engine/messaging/`:

| module        | role                                                              |
|---------------|-------------------------------------------------------------------|
| `models.py`   | the `Message` envelope+payload model, types, priorities, states   |
| `store.py`    | durable persistence over the runtime SQLite database              |
| `service.py`  | routing, mailboxes, delivery policy, retries, loop/flood defense  |
| `handlers.py` | `AgentMessenger`: identity-bound outbound helpers for one agent   |

The orchestrator (`runtime/orchestrator.py`) publishes lifecycle messages on
behalf of agents and consumes the Director inbox once per cycle. The TUI
renders real traffic in the **AGENT COMMUNICATION** view (key `4`).

---

## 1. What a message is (and is not)

A MESSAGE is a structured, persistent, addressable unit of coordination. It is
deliberately distinct from:

- an **EVENT** — an observation in the event log (`events.jsonl`),
- a **TASK** — a unit of scheduled work in the task graph,
- an **AGENT RESULT** — the output of one agent invocation,
- **MODEL OUTPUT** — raw model text.

Agents interpret model output first; only the resulting structured action
becomes a message. Models never write messages directly, and message text can
never grant permissions (runtime policy stays authoritative).

## 2. Envelope

```
Message
├─ id                 "msg-<hex12>"       immutable, unique
├─ type               MsgType             see §3
├─ sender             str                 runtime-bound identity, never model text
├─ recipient          str                 resolved direct/role/capability address
├─ project_id / run_id / task_id            execution scope
├─ parent_message_id  str                 threading
├─ correlation_id     str                 request → response linkage
├─ conversation_id    str                 "conv-<hex12>" thread id
├─ priority           low|normal|high|critical
├─ requires_response  bool
├─ expires_at         ISO-8601 or None
├─ context_refs       ["task:TASK-042", "decision:DEC-07", …]
├─ artifact_refs      ["verification/TASK-042.json", "git:a81c…", …]
├─ payload            dict                type-specific, validated
├─ state              DeliveryState       see §5
├─ attempts           int                 handler retry counter
├─ status_detail      str                 last transition note / failure reason
└─ protocol_version   1
```

References point at durable state (context manager, artifacts); large content
is stored externally, never inlined. Payloads are capped at 20,000 chars.

## 3. Message types

`task.request` `task.accepted` `task.rejected` `task.completed` `task.failed`
`help.request` `help.response`
`review.request` `review.result`
`research.request` `research.result`
`architecture.request` `architecture.result`
`verification.request` `verification.result`
`blocker.reported` `blocker.resolved`
`discovery.reported` `decision.proposed`
`replan.request` `replan.result`
`approval.request` `approval.result`
`status.request` `status.response`
`cancellation.request` `cancellation.ack`
`agent.handoff` `agent.handoff.ack`
`health` `health.ack`
Director directives: `directive.continue` `directive.reassign`
`directive.create_task` `directive.escalate`

Semantics (§75 of the comms spec): `task.request` = "please perform this
work"; `help.request` = "I cannot safely continue without assistance";
`review.request` = "independent validation requested";
`discovery.reported` = "new information was discovered";
`replan.request` = "current execution strategy may no longer be valid";
`blocker.reported` = "task cannot safely progress".

Each type declares required payload keys (e.g. `task.failed` requires
`task_id`, `failure_type`, `summary`; failure types are whitelisted: CODE,
TEST, ENVIRONMENT, DEPENDENCY, TOOL, MODEL, ARCHITECTURE, REQUIREMENT,
SECURITY, CONCURRENCY, RESOURCE). Malformed messages are rejected and logged
(`message.rejected`), never silently repaired.

## 4. Identity, addressing, permissions

- Sender identity is **runtime-bound**: the caller (`AgentMessenger` or the
  orchestrator) fixes `sender`. A model claiming "I am engineering-director"
  obtains nothing.
- Addressing supports direct (`coder`), role (`debugger`), and capability
  (`capability="security"` → first capable agent). The resolved recipient is
  recorded on the message.
- Self-addressed messages are invalid; unknown recipients are rejected.
- Communication is coordination, not privilege escalation. A message asking to
  "run production deletion" is still subject to command policy.

## 5. Lifecycle

```
CREATED → QUEUED → DELIVERED → RECEIVED → ACKNOWLEDGED → PROCESSING
                                                       ├→ COMPLETED
                          FAILED ←─────────────────────┘
                            ├→ QUEUED (retry, up to max_retries)
                            └→ DEAD  (dead-letter, reason preserved)
any live state → EXPIRED (auditable, never executed)
any non-terminal → CANCELLED        DEAD is terminal
```

Transitions are deterministic; illegal ones raise `IllegalTransition`.
Delivery semantics: **at-least-once with idempotent handling** — duplicate
delivery is detected (message id, `idempotency_key`) and recorded
(`message.duplicate_suppressed`), never double-processed. This is not a
fake exactly-once guarantee.

**ACK means "received", never "done."** `complete()` is the "handled" marker.

## 6. Delivery, mailboxes, retries

- `MessageService.send()` validates → checks flood/loop guards → resolves the
  recipient → enforces mailbox backpressure → persists → QUEUED →
  `message.sent`.
- Each recipient has a mailbox (limit 25 default). Overflow rejects the send
  (`MailboxFull`) unless priority is CRITICAL, so queues cannot grow unbounded.
- `fetch()` pops in priority-then-age order, stepping QUEUED → DELIVERED →
  RECEIVED.
- Handler failures increment `attempts`; after `max_retries` (3) the message is
  dead-lettered with the preserved failure reason and a `message.dead_lettered`
  event.
- Expiration: `expire_stale()` runs each cycle; expired messages leave the
  queues but remain auditable.

## 7. Abuse protection

- **Rate limit** 60 sends/min/sender (`MessageRejected` beyond).
- **Loop detection**: ≥6 identical (sender, recipient, type, task) within 120 s
  raises `LoopDetected`.
- **Payload cap** 20,000 chars — large content becomes an artifact reference.

## 8. Orchestration integration

The orchestrator publishes the real conversation:

```
DIRECTOR → architect   architecture.request
architect → DIRECTOR   architecture.result        (correlated reply)
ORCHESTRATOR → coder   task.request               (on assignment)
coder → ORCHESTRATOR   task.accepted, task.completed
tester → ORCHESTRATOR  verification.result        (high priority on failure)
debugger → DIRECTOR    discovery.reported         (root cause)
researcher → DIRECTOR  research.result
ORCHESTRATOR → DIRECTOR status.response           (milestone broadcast)
```

Once per cycle the Director inbox is consumed (`_process_director_inbox`):
structured reports become management actions — continue, replan (real
`_replan` execution), create task (high-confidence discoveries), escalate
(attempt budget exhausted) — never raw state mutation by the sending agent.
Every interpretation is traced: message → decision record
(`decided_by=engineering-director`) → status_detail on the message → task
(`created_by=<message or task id>`).

## 9. Observability

Every transition emits to the event log: `message.sent`, `message.delivered`,
`message.acknowledged`, `message.completed`, `message.failed`,
`message.dead_lettered`, `message.cancelled`, `message.expired`,
`message.rejected`, `message.duplicate_suppressed`. The TUI live feed renders
these with sender→recipient; the AGENT COMMUNICATION view (key `4`) lists real
messages, `/inspect msg-…` opens the full envelope, and key `m` shows per-agent
sent/received/pending/failed counts. Query APIs: `store.list_messages`,
`store.inbox`, `store.thread`, `store.by_correlation`, `store.stats`.

## 10. Persistence

Messages live in the runtime SQLite database (`messages` table, WAL,
thread-local connections) with indexes on (project, state), (recipient,
state), correlation, and conversation. They survive restarts — a fresh
orchestrator sees every message with its settled state (proven by test).

## 11. Testing

`tests/test_messaging.py` covers the protocol semantics and the vertical
slices with **real agents** (echo provider, no mocks):

- typed envelope, validation, lifecycle, correlation/threads, identity
- expiration, retry → dead-letter, mailbox backpressure, loop detection,
  role/capability routing
- §82 success slice: Director→Architect→Coder→Tester conversation in one run
- §83 failure slice: TASK_FAILED → diagnosis discovery → repair → pass
- §84 restart: zero message loss across a runtime restart
- §85 duplicate delivery: idempotent, recorded
- §86 concurrency: 48 messages from 4 threads, none lost or duplicated
- Director interpretations: discovery→task, replan request→real replan,
  failure at budget→escalation

## 12. Extending the protocol

1. Add the type to `MsgType`; add required payload keys to `_PAYLOAD_REQUIRED`.
2. If it is a request, add it to `REQUEST_TYPES` and `RESPONSE_OF`.
3. Teach `_interpret_director_message` (or the relevant handler) its semantics.
4. Extend `_message_dict` in `ui/facade.py` if the envelope grew.
5. Version bump (`PROTOCOL_VERSION`) only for breaking envelope changes;
   additive payload keys stay v1.
