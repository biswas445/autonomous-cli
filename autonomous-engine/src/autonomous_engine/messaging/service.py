"""Message service: routing, mailboxes, and delivery policy (comms spec §10,
§29–§35, §67–§69).

The service is the only way agents exchange messages. It binds runtime-owned
sender identity (a model cannot claim to be the director, §37/§38), validates
every message before delivery (§36), routes to role or direct recipients
(§30/§31), keeps per-recipient mailboxes with backpressure (§34), retries
failed handlers and dead-letters after the retry budget (§35), expires stale
messages (§13), and watches for floods and pathological loops (§67/§68).

Delivery semantics (§50): at-least-once with idempotent handling — duplicate
delivery is detected by message id and recorded, never double-processed.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Any

from ..core.events import EventLog
from ..core.task import now_iso
from .models import (
    DIRECTOR,
    ORCHESTRATOR,
    SYSTEM,
    DeliveryState,
    IllegalTransition,
    Message,
    MessageServiceError,
    MsgPriority,
    MsgType,
    new_conversation_id,
)
from .store import MessageStore

MAX_PAYLOAD_CHARS = 20_000  # large content belongs in artifacts (§69)
DEFAULT_MAILBOX_LIMIT = 25
DEFAULT_MAX_RETRIES = 3
_RATE_WINDOW_SECONDS = 60.0


class MailboxFull(MessageServiceError):
    pass


class MessageRejected(MessageServiceError):
    pass


class LoopDetected(MessageServiceError):
    pass


class AgentDescriptor:
    """What the router knows about one agent (§29, §33)."""

    def __init__(self, name: str, role: str, capabilities: set[str], mailbox_limit: int = DEFAULT_MAILBOX_LIMIT):
        self.name = name
        self.role = role
        self.capabilities = set(capabilities)
        self.mailbox_limit = mailbox_limit
        self.availability = "AVAILABLE"  # AVAILABLE|BUSY|PAUSED|OFFLINE|FAILED|BLOCKED

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "role": self.role,
            "capabilities": sorted(self.capabilities),
            "availability": self.availability,
            "mailbox_limit": self.mailbox_limit,
        }


def default_agents() -> dict[str, AgentDescriptor]:
    """Runtime-known agent classes and their capabilities (spec §29)."""
    roles = {
        "orchestrator": {"scheduling", "state", "reports"},
        "intent-compiler": {"intent", "planning"},
        "product": {"requirements"},
        "researcher": {"research", "web"},
        "architect": {"architecture", "review"},
        "planner": {"planning", "task-graph"},
        "coder": {"coding", "filesystem", "shell"},
        "tester": {"verification", "testing"},
        "debugger": {"debugging", "diagnosis"},
        "reviewer": {"review"},
        "security": {"security", "review"},
        "qa": {"qa", "review"},
        "release": {"release"},
    }
    registry = {name: AgentDescriptor(name, name, caps) for name, caps in roles.items()}
    # The director's runtime identity is DIRECTOR ("engineering-director"):
    # register it so mailbox backpressure also guards the hottest mailbox,
    # with role "director" so role-addressed mail resolves to the mailbox the
    # DirectorInbox actually reads. SYSTEM likewise.
    registry[DIRECTOR] = AgentDescriptor(
        DIRECTOR, "director", {"coordination", "scheduling", "review"}
    )
    registry[SYSTEM] = AgentDescriptor(SYSTEM, "system", {"system"})
    return registry


class MessageService:
    """Central facade for agent-to-agent messaging (§46)."""

    def __init__(
        self,
        store: MessageStore,
        events: EventLog,
        *,
        run_id: str = "",
        agents: dict[str, AgentDescriptor] | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        rate_limit_per_minute: int = 60,
        max_thread_length: int = 100,
        dead_retention_days: int = 7,
        max_dead_messages: int = 200,
    ):
        self.store = store
        self.events = events
        self.run_id = run_id
        self.agents = agents or default_agents()
        self.max_retries = max_retries
        self.rate_limit_per_minute = rate_limit_per_minute
        self.max_thread_length = max_thread_length
        self.dead_retention_days = dead_retention_days
        self.max_dead_messages = max_dead_messages
        self._send_times: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=rate_limit_per_minute * 2))
        self._pending: dict[str, deque[str]] = defaultdict(lambda: deque(maxlen=200))
        self._loop_watcher: dict[str, deque[tuple[str, str]]] = defaultdict(lambda: deque(maxlen=12))

    def _emit(self, event: str, *, message: Message, **extra: Any) -> None:
        """Communication observability through the runtime event log (§66)."""
        import contextlib

        with contextlib.suppress(Exception):
            self.events.append(
                event,
                message_id=message.id,
                msg_type=message.type.value,
                sender=message.sender,
                recipient=message.recipient,
                task_id=message.task_id,
                correlation_id=message.correlation_id,
                state=message.state.value,
                priority=message.priority.value,
                **extra,
            )

    # ---- registry ----

    def register_agent(self, descriptor: AgentDescriptor) -> None:
        self.agents[descriptor.name] = descriptor

    def resolve_recipient(self, recipient: str, *, capability: str = "") -> str:
        """Resolve direct / role / capability addressing to one agent (§30, §31)."""
        if recipient in self.agents:
            return recipient
        if recipient in (DIRECTOR, SYSTEM):
            return recipient
        # role-based: any agent whose role matches
        role_matches = [name for name, desc in self.agents.items() if desc.role == recipient]
        if role_matches:
            return sorted(role_matches)[0]
        if capability:
            capable = [
                name
                for name, desc in self.agents.items()
                if capability in desc.capabilities and desc.availability != "OFFLINE"
            ]
            if capable:
                return sorted(capable)[0]
        raise MessageRejected(f"no agent for recipient {recipient!r}")

    # ---- sending ----

    def send(
        self,
        *,
        msg_type: MsgType | str,
        sender: str,
        recipient: str,
        payload: dict[str, Any] | None = None,
        task_id: str = "",
        parent: Message | None = None,
        correlation_id: str = "",
        priority: MsgPriority | str = MsgPriority.NORMAL,
        requires_response: bool = False,
        expires_in_seconds: float | None = None,
        context_refs: list[str] | None = None,
        artifact_refs: list[str] | None = None,
        conversation_id: str = "",
        capability: str = "",
    ) -> Message:
        """Build, validate, persist, and route one message (§4, §10)."""
        # identity binding: the CALLER supplies the sender; a model never does.
        if not sender:
            raise MessageRejected("sender identity is required (runtime-bound)")
        msg_type = MsgType(msg_type)
        priority = MsgPriority(priority)
        payload = dict(payload or {})

        content_chars = len(str(payload))
        if content_chars > MAX_PAYLOAD_CHARS:
            raise MessageRejected(
                f"payload too large ({content_chars} chars > {MAX_PAYLOAD_CHARS}); "
                "store large content as an artifact and reference it"
            )

        # flood protection (§68)
        bucket = self._send_times[sender]
        now = time.monotonic()
        while bucket and now - bucket[0] > _RATE_WINDOW_SECONDS:
            bucket.popleft()
        if len(bucket) >= self.rate_limit_per_minute:
            raise MessageRejected(f"rate limit exceeded for {sender}")
        bucket.append(now)

        if parent is not None:
            correlation_id = correlation_id or parent.correlation_id or parent.id
            conversation_id = conversation_id or parent.conversation_id
            task_id = task_id or parent.task_id
        if not conversation_id:
            conversation_id = new_conversation_id()

        expires_at = None
        if expires_in_seconds is not None:
            import time as _time

            expires_at = _time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", _time.gmtime(_time.time() + expires_in_seconds)
            )

        message = Message(
            type=msg_type,
            sender=sender,
            recipient=recipient,
            project_id=self.store.project_id,
            run_id=self.run_id,
            task_id=task_id,
            conversation_id=conversation_id,
            parent_message_id=parent.id if parent else "",
            correlation_id=correlation_id,
            priority=priority,
            requires_response=requires_response,
            expires_at=expires_at,
            payload=payload,
            context_refs=list(context_refs or []),
            artifact_refs=list(artifact_refs or []),
        )
        errors = message.validate_for_delivery()
        if errors:
            self._emit("message.rejected", message=message, reason="; ".join(errors))
            raise MessageRejected(f"invalid message: {'; '.join(errors)}")

        # loop detection (§67): same pair + type + task repeating rapidly
        self._check_loop(message)

        resolved = self.resolve_recipient(recipient, capability=capability)
        message.recipient = resolved

        # mailbox backpressure (§34): an overloaded recipient has new work
        # rejected (except CRITICAL, which always gets through) so the queue
        # cannot grow without bound and the sender learns immediately.
        descriptor = self.agents.get(resolved)
        if (
            descriptor is not None
            and priority is not MsgPriority.CRITICAL
            and self.store.pending_count(resolved) >= descriptor.mailbox_limit
        ):
            self._emit("message.rejected", message=message, reason="mailbox full")
            raise MailboxFull(f"mailbox for {resolved} is full ({descriptor.mailbox_limit} pending)")

        # thread limit (§68): an unbounded conversation is an unbounded
        # memory leak; CRITICAL always gets through, everything else is
        # rejected once the thread hits the cap.
        if (
            priority is not MsgPriority.CRITICAL
            and self.store.thread_length(conversation_id) >= self.max_thread_length
        ):
            self._emit("message.rejected", message=message, reason="thread too long")
            raise MessageRejected(
                f"thread {conversation_id} reached {self.max_thread_length} messages; "
                "start a new conversation or escalate"
            )

        # idempotency: same key returns the original message (§14)
        key = payload.get("idempotency_key")
        if key:
            existing = self.store.find_duplicate(str(key))
            if existing is not None:
                self._emit("message.duplicate_suppressed", message=message, original=existing.id)
                return existing

        self.store.save(message)
        message.set_state(DeliveryState.QUEUED)
        self.store.save(message)
        self._pending[message.recipient].append(message.id)
        self._emit("message.sent", message=message)
        return message

    def _check_loop(self, message: Message) -> None:
        signature = (message.sender, message.recipient, message.type.value, message.task_id)
        seen = self._loop_watcher[signature]
        cutoff = time.monotonic() - 120.0
        while seen and seen[0][1] < cutoff:
            seen.popleft()
        if len(seen) >= 6:
            raise LoopDetected(
                f"possible communication loop: {message.sender}->{message.recipient} "
                f"{message.type.value} x{len(seen) + 1} in 120s"
            )
        seen.append((message.id, time.monotonic()))

    # ---- delivery / processing (§10, §53) ----

    def deliver(self, message: Message) -> None:
        """Mark queued work as delivered to its mailbox."""
        if message.state == DeliveryState.QUEUED:
            message.set_state(DeliveryState.DELIVERED)
            self.store.save(message)

    def fetch(self, recipient: str, limit: int = 10) -> list[Message]:
        """Pop the highest-priority pending messages for one mailbox (§11).

        Delivery moves QUEUED -> DELIVERED -> RECEIVED deterministically;
        a message whose transition is no longer legal (e.g. cancelled in
        flight) is skipped, never silently dropped.
        """
        messages = self.store.inbox(recipient, limit=limit)
        out: list[Message] = []
        for message in messages:
            try:
                if message.state == DeliveryState.QUEUED:
                    message.set_state(DeliveryState.DELIVERED)
                if message.state == DeliveryState.DELIVERED:
                    message.set_state(DeliveryState.RECEIVED)
            except IllegalTransition:
                self.store.save(message)
                continue
            self.store.save(message)
            self._emit("message.delivered", message=message)
            out.append(message)
        return out

    def acknowledge(self, message: Message, note: str = "") -> None:
        """ACK is 'received', never 'done' (§55). Coerces QUEUED/DELIVERED
        through their legal intermediate states instead of raising."""
        if message.state == DeliveryState.QUEUED:
            message.set_state(DeliveryState.DELIVERED)
            self.store.save(message)
        if message.state == DeliveryState.DELIVERED:
            message.set_state(DeliveryState.RECEIVED)
            self.store.save(message)
        if message.state in (DeliveryState.RECEIVED, DeliveryState.ACKNOWLEDGED):
            if message.state == DeliveryState.RECEIVED:
                message.set_state(DeliveryState.ACKNOWLEDGED)
            message.status_detail = note
            self.store.save(message)
            self._emit("message.acknowledged", message=message)

    def start_processing(self, message: Message) -> None:
        if message.state in (DeliveryState.RECEIVED, DeliveryState.ACKNOWLEDGED):
            message.set_state(DeliveryState.PROCESSING)
            self.store.save(message)

    def complete(self, message: Message, summary: str = "") -> None:
        """Mark the message handled, coercing earlier states deterministically.

        Idempotent (§85): completing an already-completed message is a no-op,
        never a second side effect. QUEUED/DELIVERED are walked forward along
        legal edges rather than raising IllegalTransition.
        """
        if message.state == DeliveryState.COMPLETED:
            return
        _forward = {
            DeliveryState.CREATED: DeliveryState.QUEUED,
            DeliveryState.QUEUED: DeliveryState.DELIVERED,
            DeliveryState.DELIVERED: DeliveryState.RECEIVED,
            DeliveryState.RECEIVED: DeliveryState.ACKNOWLEDGED,
            DeliveryState.ACKNOWLEDGED: DeliveryState.PROCESSING,
        }
        while message.state in _forward:
            message.set_state(_forward[message.state])
            self.store.save(message)
        if message.state == DeliveryState.PROCESSING:
            message.set_state(DeliveryState.COMPLETED)
        message.status_detail = summary[:500]
        self.store.save(message)
        self._emit("message.completed", message=message)

    def fail_processing(self, message: Message, reason: str) -> None:
        """Record the failure; retry up to budget, then dead-letter (§35, §54)."""
        # Coerce to PROCESSING so FAILED/DEAD are legal from any live state.
        _forward = {
            DeliveryState.CREATED: DeliveryState.QUEUED,
            DeliveryState.QUEUED: DeliveryState.DELIVERED,
            DeliveryState.DELIVERED: DeliveryState.RECEIVED,
            DeliveryState.RECEIVED: DeliveryState.PROCESSING,
            DeliveryState.ACKNOWLEDGED: DeliveryState.PROCESSING,
        }
        while message.state in _forward:
            message.set_state(_forward[message.state])
            self.store.save(message)
        message.attempts += 1
        if message.attempts >= self.max_retries:
            message.set_state(DeliveryState.DEAD)
            message.status_detail = f"dead-lettered after {message.attempts} attempts: {reason[:300]}"
            self.store.save(message)
            self._emit("message.dead_lettered", message=message)
        else:
            message.set_state(DeliveryState.FAILED)
            message.status_detail = reason[:500]
            self.store.save(message)
            self._emit("message.failed", message=message, reason=reason[:300])

    def cancel(self, message: Message, reason: str = "") -> bool:
        if message.is_terminal():
            return False
        message.set_state(DeliveryState.CANCELLED)
        message.status_detail = reason[:500]
        self.store.save(message)
        self._emit("message.cancelled", message=message)
        return True

    def expire_stale(self) -> int:
        """EXPIRE messages past their expires_at (§13); auditable, not executed.

        Also applies the dead-letter retention policy (§35): DEAD messages
        older than `dead_retention_days` are purged, and the dead population
        is trimmed to `max_dead_messages` — a months-long runtime must not
        accumulate dead letters forever.
        """
        now = now_iso()
        rows: list[Message] = []
        for state in (DeliveryState.QUEUED, DeliveryState.DELIVERED, DeliveryState.RECEIVED):
            rows.extend(self.store.list_messages(state=state.value, limit=500))
        expired = 0
        for message in rows:
            if message.expires_at and message.expires_at < now:
                message.state = DeliveryState.EXPIRED  # direct set: EXPIRED is reachable from any live state
                self.store.save(message)
                self._emit("message.expired", message=message)
                expired += 1
        import datetime as _dt

        cutoff = (
            _dt.datetime.now(_dt.UTC) - _dt.timedelta(days=self.dead_retention_days)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        purged = self.store.purge_dead(
            older_than=cutoff, keep_newest=self.max_dead_messages
        )
        if purged:
            # not tied to one message: audit through the event log directly
            import contextlib

            with contextlib.suppress(Exception):
                self.events.append("message.dead_purged", count=purged)
        return expired


# Director-facing helpers (§76): interpret a message into a runtime directive.


class DirectorInbox:
    """The Director's view of pending messages, with reply helpers."""

    def __init__(self, service: MessageService):
        self.service = service

    def pending(self) -> list[Message]:
        inbox = self.service.store.inbox(DIRECTOR, limit=50)
        inbox += self.service.store.inbox(ORCHESTRATOR, limit=50)
        return inbox

    def reply(
        self,
        request: Message,
        *,
        msg_type: MsgType | str,
        payload: dict[str, Any] | None = None,
        recipient: str = "",
        requires_response: bool = False,
        priority: MsgPriority | str = MsgPriority.NORMAL,
    ) -> Message:
        return self.service.send(
            msg_type=msg_type,
            sender=DIRECTOR,
            recipient=recipient or request.sender,
            payload=payload or {},
            task_id=request.task_id,
            parent=request,
            requires_response=requires_response,
            priority=priority,
        )
