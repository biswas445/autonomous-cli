"""Agent-to-agent messaging subsystem.

Public API:
    MessageService  — send/route/deliver/ack/process/retry/dead-letter
    MessageStore    — durable persistence over the runtime SQLite store
    Message         — the protocol envelope+payload model
    MsgType         — the typed taxonomy
    AgentMessenger  — identity-bound outbound helpers for one agent
    DirectorInbox   — the Director's pending view + reply helper
"""

from .handlers import AgentMessenger
from .models import (
    DIRECTOR,
    ORCHESTRATOR,
    PROTOCOL_VERSION,
    SYSTEM,
    DeliveryState,
    IllegalTransition,
    Message,
    MessageServiceError,
    MsgPriority,
    MsgType,
    new_conversation_id,
    new_message_id,
)
from .service import (
    AgentDescriptor,
    DirectorInbox,
    LoopDetected,
    MailboxFull,
    MessageRejected,
    MessageService,
    default_agents,
)
from .store import MessageStore

__all__ = [
    "DIRECTOR",
    "ORCHESTRATOR",
    "SYSTEM",
    "PROTOCOL_VERSION",
    "AgentDescriptor",
    "AgentMessenger",
    "DeliveryState",
    "DirectorInbox",
    "IllegalTransition",
    "LoopDetected",
    "MailboxFull",
    "Message",
    "MessageRejected",
    "MessageService",
    "MessageServiceError",
    "MessageStore",
    "MsgPriority",
    "MsgType",
    "default_agents",
    "new_conversation_id",
    "new_message_id",
]
