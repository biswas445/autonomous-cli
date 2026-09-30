"""Agent message protocol models (comms spec §2–§16, §64).

A MESSAGE is a structured, persistent, addressable unit of agent-to-agent
coordination. It is deliberately distinct from an EVENT (an observation in
the event log), a TASK (a unit of scheduled work), an AGENT RESULT (the
output of one agent invocation), and MODEL OUTPUT (raw model text). Agents
interpret model output and then speak in messages; the model never writes
messages directly (spec §92, §62).

Envelope vs payload (§6): the envelope carries routing and lifecycle
metadata; the payload carries type-specific data validated per type.
"""

from __future__ import annotations

import uuid
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator

PROTOCOL_VERSION = 1

# Sender identities the runtime owns. A model claiming one of these in its
# output can never obtain it — identity is bound by the caller, not the text
# (spec §37, §38).
DIRECTOR = "engineering-director"
SYSTEM = "runtime"
ORCHESTRATOR = "orchestrator"


class MsgType(StrEnum):
    """Typed message taxonomy (spec §5). Each has one semantic purpose."""

    # delegation / lifecycle of delegated work
    TASK_REQUEST = "task.request"
    TASK_ACCEPTED = "task.accepted"
    TASK_REJECTED = "task.rejected"
    TASK_COMPLETED = "task.completed"
    TASK_FAILED = "task.failed"
    # help
    HELP_REQUEST = "help.request"
    HELP_RESPONSE = "help.response"
    # independent validation
    REVIEW_REQUEST = "review.request"
    REVIEW_RESULT = "review.result"
    RESEARCH_REQUEST = "research.request"
    RESEARCH_RESULT = "research.result"
    ARCHITECTURE_REQUEST = "architecture.request"
    ARCHITECTURE_RESULT = "architecture.result"
    VERIFICATION_REQUEST = "verification.request"
    VERIFICATION_RESULT = "verification.result"
    # impediments
    BLOCKER_REPORTED = "blocker.reported"
    BLOCKER_RESOLVED = "blocker.resolved"
    # knowledge
    DISCOVERY_REPORTED = "discovery.reported"
    DECISION_PROPOSED = "decision.proposed"
    # direction
    REPLAN_REQUEST = "replan.request"
    REPLAN_RESULT = "replan.result"
    APPROVAL_REQUEST = "approval.request"
    APPROVAL_RESULT = "approval.result"
    # bookkeeping
    STATUS_REQUEST = "status.request"
    STATUS_RESPONSE = "status.response"
    CANCELLATION_REQUEST = "cancellation.request"
    CANCELLATION_ACK = "cancellation.ack"
    AGENT_HANDOFF = "agent.handoff"
    AGENT_HANDOFF_ACK = "agent.handoff.ack"
    HEALTH = "health"
    HEALTH_ACK = "health.ack"
    # director interpretations back to the runtime (§76)
    DIRECTIVE_CONTINUE = "directive.continue"
    DIRECTIVE_REASSIGN = "directive.reassign"
    DIRECTIVE_CREATE_TASK = "directive.create_task"
    DIRECTIVE_ESCALATE = "directive.escalate"


# Message types that request work and therefore expect a correlated response.
REQUEST_TYPES: frozenset[str] = frozenset(
    {
        MsgType.TASK_REQUEST,
        MsgType.HELP_REQUEST,
        MsgType.REVIEW_REQUEST,
        MsgType.RESEARCH_REQUEST,
        MsgType.ARCHITECTURE_REQUEST,
        MsgType.VERIFICATION_REQUEST,
        MsgType.REPLAN_REQUEST,
        MsgType.STATUS_REQUEST,
        MsgType.APPROVAL_REQUEST,
        MsgType.CANCELLATION_REQUEST,
        MsgType.AGENT_HANDOFF,
        MsgType.HEALTH,
    }
)

# The correlated response type for each request type (§7).
RESPONSE_OF: dict[str, MsgType] = {
    MsgType.TASK_REQUEST: MsgType.TASK_ACCEPTED,
    MsgType.HELP_REQUEST: MsgType.HELP_RESPONSE,
    MsgType.REVIEW_REQUEST: MsgType.REVIEW_RESULT,
    MsgType.RESEARCH_REQUEST: MsgType.RESEARCH_RESULT,
    MsgType.ARCHITECTURE_REQUEST: MsgType.ARCHITECTURE_RESULT,
    MsgType.VERIFICATION_REQUEST: MsgType.VERIFICATION_RESULT,
    MsgType.REPLAN_REQUEST: MsgType.REPLAN_RESULT,
    MsgType.STATUS_REQUEST: MsgType.STATUS_RESPONSE,
    MsgType.APPROVAL_REQUEST: MsgType.APPROVAL_RESULT,
    MsgType.CANCELLATION_REQUEST: MsgType.CANCELLATION_ACK,
    MsgType.AGENT_HANDOFF: MsgType.AGENT_HANDOFF_ACK,
    MsgType.HEALTH: MsgType.HEALTH_ACK,
}


class MsgPriority(StrEnum):
    """Urgency of the COMMUNICATION, distinct from task priority (§80)."""

    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"


_PRIORITY_ORDER: dict[str, int] = {
    MsgPriority.LOW: 0,
    MsgPriority.NORMAL: 1,
    MsgPriority.HIGH: 2,
    MsgPriority.CRITICAL: 3,
}


class DeliveryState(StrEnum):
    """Deterministic message lifecycle (spec §9)."""

    CREATED = "CREATED"
    QUEUED = "QUEUED"
    DELIVERED = "DELIVERED"
    RECEIVED = "RECEIVED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"
    DEAD = "DEAD"  # dead-lettered (§35)


_TERMINAL_STATES = {
    DeliveryState.COMPLETED,
    DeliveryState.FAILED,
    DeliveryState.EXPIRED,
    DeliveryState.CANCELLED,
    DeliveryState.DEAD,
}

# Deterministic lifecycle transitions; anything else is rejected (§9).
_TRANSITIONS: dict[DeliveryState, set[DeliveryState]] = {
    DeliveryState.CREATED: {DeliveryState.QUEUED, DeliveryState.CANCELLED, DeliveryState.EXPIRED},
    DeliveryState.QUEUED: {
        DeliveryState.DELIVERED,
        DeliveryState.EXPIRED,
        DeliveryState.CANCELLED,
        DeliveryState.DEAD,
    },
    DeliveryState.DELIVERED: {DeliveryState.RECEIVED, DeliveryState.EXPIRED, DeliveryState.DEAD},
    DeliveryState.RECEIVED: {
        DeliveryState.ACKNOWLEDGED,
        DeliveryState.PROCESSING,
        DeliveryState.EXPIRED,
        DeliveryState.DEAD,
    },
    DeliveryState.ACKNOWLEDGED: {DeliveryState.PROCESSING, DeliveryState.COMPLETED, DeliveryState.EXPIRED},
    DeliveryState.PROCESSING: {
        DeliveryState.COMPLETED,
        DeliveryState.FAILED,
        DeliveryState.EXPIRED,
        DeliveryState.DEAD,
    },
    DeliveryState.FAILED: {DeliveryState.QUEUED, DeliveryState.DEAD, DeliveryState.CANCELLED},
    DeliveryState.DEAD: set(),
}
for _terminal in _TERMINAL_STATES:
    _TRANSITIONS.setdefault(_terminal, set())


def can_transition(current: DeliveryState | str, target: DeliveryState | str) -> bool:
    return DeliveryState(target) in _TRANSITIONS.get(DeliveryState(current), set())


class IllegalTransition(ValueError):
    pass


# Payload requirements per message type: which keys must be present. Keys are
# the minimal contract; handlers validate deeper semantics (§36).
_PAYLOAD_REQUIRED: dict[str, tuple[str, ...]] = {
    MsgType.TASK_REQUEST: ("task_id",),
    MsgType.TASK_ACCEPTED: ("task_id",),
    MsgType.TASK_REJECTED: ("task_id", "reason"),
    MsgType.TASK_COMPLETED: ("task_id", "summary"),
    MsgType.TASK_FAILED: ("task_id", "failure_type", "summary"),
    MsgType.BLOCKER_REPORTED: ("task_id", "blocker_type", "description"),
    MsgType.DISCOVERY_REPORTED: ("discovery",),
    MsgType.REPLAN_REQUEST: ("reason",),
    MsgType.REVIEW_REQUEST: ("task_id",),
    MsgType.RESEARCH_REQUEST: ("question",),
    MsgType.ARCHITECTURE_REQUEST: ("question",),
    MsgType.VERIFICATION_REQUEST: ("task_id",),
    MsgType.HELP_REQUEST: ("question",),
    MsgType.APPROVAL_REQUEST: ("reason",),
    MsgType.CANCELLATION_REQUEST: ("target_message_id",),
    MsgType.AGENT_HANDOFF: ("task_id", "to_agent"),
}

_FAILURE_TYPES = {
    "CODE",
    "TEST",
    "ENVIRONMENT",
    "DEPENDENCY",
    "TOOL",
    "MODEL",
    "ARCHITECTURE",
    "REQUIREMENT",
    "SECURITY",
    "CONCURRENCY",
    "RESOURCE",
}


def new_message_id() -> str:
    return f"msg-{uuid.uuid4().hex[:12]}"


def new_conversation_id() -> str:
    return f"conv-{uuid.uuid4().hex[:12]}"


class Message(BaseModel):
    """A persistent, addressable agent-to-agent message (spec §4)."""

    id: str = Field(default_factory=new_message_id)
    protocol_version: int = PROTOCOL_VERSION
    type: MsgType
    sender: str
    recipient: str
    project_id: str = ""
    run_id: str = ""
    task_id: str = ""
    conversation_id: str = ""
    parent_message_id: str = ""
    correlation_id: str = ""  # the request this message answers (§7)
    priority: MsgPriority = MsgPriority.NORMAL
    timestamp: str = ""
    expires_at: str | None = None
    requires_response: bool = False
    payload: dict[str, Any] = Field(default_factory=dict)
    context_refs: list[str] = Field(default_factory=list)  # requirement:..., decision:... (§18)
    artifact_refs: list[str] = Field(default_factory=list)  # file/commit/report refs (§19)
    state: DeliveryState = DeliveryState.CREATED
    attempts: int = 0
    status_detail: str = ""  # delivery/handler failure reason (§35)

    @field_validator("type", mode="before")
    @classmethod
    def _coerce_type(cls, value: Any) -> Any:
        return MsgType(value)

    # ---- lifecycle ----

    def set_state(self, target: DeliveryState | str) -> DeliveryState:
        target = DeliveryState(target)
        if target == self.state:
            return target
        if not can_transition(self.state, target):
            raise IllegalTransition(f"{self.id}: {self.state.value} -> {target.value}")
        self.state = target
        return target

    def is_terminal(self) -> bool:
        return self.state in _TERMINAL_STATES

    # ---- helpers ----

    def response_type(self) -> MsgType | None:
        return RESPONSE_OF.get(self.type)

    def priority_rank(self) -> int:
        return _PRIORITY_ORDER.get(self.priority, 1)

    def validate_for_delivery(self) -> list[str]:
        """Structural validation before routing (spec §36)."""
        errors: list[str] = []
        if not self.sender:
            errors.append("missing sender")
        if not self.recipient:
            errors.append("missing recipient")
        if self.sender == self.recipient:
            errors.append("sender and recipient must differ")
        required = _PAYLOAD_REQUIRED.get(self.type, ())
        for key in required:
            if key not in self.payload or self.payload[key] in (None, ""):
                errors.append(f"{self.type.value} payload missing {key!r}")
        if self.type == MsgType.TASK_FAILED:
            failure_type = str(self.payload.get("failure_type", "")).upper()
            if failure_type and failure_type not in _FAILURE_TYPES:
                errors.append(f"unknown failure_type: {failure_type}")
        if self.requires_response and self.type not in REQUEST_TYPES:
            errors.append(f"{self.type.value} cannot require a response")
        if self.type in REQUEST_TYPES and not self.correlation_id:
            # requests get their own id as correlation root so responses can link
            self.correlation_id = self.id
        return errors


class MessageServiceError(ValueError):
    pass
