"""Message persistence over the runtime's SQLite store (comms spec §48, §49).

Messages survive restarts; delivery state and message content are written in
the same transaction boundary the Database wrapper provides (single UPDATE),
so the store never claims "delivered" for a message it failed to persist.
"""

from __future__ import annotations

from typing import Any

from ..core.database import Database, _dump, _loads
from ..core.task import now_iso
from .models import DeliveryState, Message, MsgPriority, MsgType


class MessageStore:
    """Typed persistence for the message protocol."""

    def __init__(self, db: Database, project_id: str):
        self.db = db
        self.project_id = project_id

    # ---- writes ----

    def save(self, message: Message) -> None:
        """Insert or update one message (metadata + state atomically)."""
        now = now_iso()
        if not message.timestamp:
            message.timestamp = now
        self.db.execute(
            """
            INSERT INTO messages (
                id, project_id, run_id, task_id, type, sender, recipient,
                conversation_id, parent_message_id, correlation_id, priority,
                state, attempts, requires_response, expires_at, payload_json,
                context_refs_json, artifact_refs_json, status_detail,
                protocol_version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                state=excluded.state,
                attempts=excluded.attempts,
                payload_json=excluded.payload_json,
                status_detail=excluded.status_detail,
                updated_at=excluded.updated_at
            """,
            (
                message.id,
                self.project_id,
                message.run_id,
                message.task_id,
                message.type.value,
                message.sender,
                message.recipient,
                message.conversation_id,
                message.parent_message_id,
                message.correlation_id,
                message.priority.value,
                message.state.value,
                message.attempts,
                int(message.requires_response),
                message.expires_at,
                _dump(message.payload),
                _dump(message.context_refs),
                _dump(message.artifact_refs),
                message.status_detail,
                message.protocol_version,
                message.timestamp,
                now,
            ),
        )

    def set_state(self, message_id: str, state: DeliveryState, detail: str = "") -> None:
        self.db.execute(
            "UPDATE messages SET state = ?, status_detail = ?, updated_at = ? WHERE id = ?",
            (state.value, detail, now_iso(), message_id),
        )

    # ---- reads ----

    def _row_to_message(self, row: dict[str, Any]) -> Message:
        return Message(
            id=row["id"],
            protocol_version=int(row["protocol_version"]),
            type=MsgType(row["type"]),
            sender=row["sender"],
            recipient=row["recipient"],
            project_id=row["project_id"],
            run_id=row["run_id"],
            task_id=row["task_id"],
            conversation_id=row["conversation_id"],
            parent_message_id=row["parent_message_id"],
            correlation_id=row["correlation_id"],
            priority=MsgPriority(row["priority"]),
            timestamp=row["created_at"],
            expires_at=row["expires_at"],
            requires_response=bool(row["requires_response"]),
            payload=_loads(row["payload_json"], {}),
            context_refs=_loads(row["context_refs_json"], []),
            artifact_refs=_loads(row["artifact_refs_json"], []),
            state=DeliveryState(row["state"]),
            attempts=int(row["attempts"]),
            status_detail=row["status_detail"],
        )

    def get(self, message_id: str) -> Message | None:
        row = self.db.query_one("SELECT * FROM messages WHERE id = ?", (message_id,))
        return self._row_to_message(row) if row else None

    def inbox(
        self,
        recipient: str,
        *,
        states: tuple[DeliveryState, ...] = (DeliveryState.QUEUED, DeliveryState.DELIVERED),
        limit: int = 50,
    ) -> list[Message]:
        """Pending messages for one mailbox, priority-then-age ordered (§11)."""
        placeholders = ", ".join("?" for _ in states)
        rows = self.db.query(
            f"""
            SELECT * FROM messages
            WHERE recipient = ? AND state IN ({placeholders})
            ORDER BY CASE priority
                WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END,
                created_at
            LIMIT ?
            """,
            (recipient, *[s.value for s in states], limit),
        )
        return [self._row_to_message(row) for row in rows]

    def pending_count(self, recipient: str) -> int:
        row = self.db.query_one(
            """
            SELECT COUNT(*) AS n FROM messages
            WHERE recipient = ? AND state IN ('QUEUED', 'DELIVERED')
            """,
            (recipient,),
        )
        return int(row["n"]) if row else 0

    def thread_length(self, conversation_id: str) -> int:
        if not conversation_id:
            return 0
        row = self.db.query_one(
            "SELECT COUNT(*) AS n FROM messages WHERE conversation_id = ?",
            (conversation_id,),
        )
        return int(row["n"]) if row else 0

    def dead_population(self) -> int:
        row = self.db.query_one(
            "SELECT COUNT(*) AS n FROM messages WHERE state = 'DEAD'"
        )
        return int(row["n"]) if row else 0

    def purge_dead(self, *, older_than: str = "", keep_newest: int = 0) -> int:
        """Retention policy for dead letters (§35): delete DEAD messages older
        than `older_than` (ISO timestamp) and/or trim to the `keep_newest`
        most recent. Returns the number of rows removed. Audited upstream by
        the caller via events; the DB rows themselves are gone for good, so
        callers must only purge beyond what the operator considers auditable.
        """
        removed = 0
        if older_than:
            cursor_before = self.dead_population()
            self.db.execute(
                "DELETE FROM messages WHERE state = 'DEAD' AND created_at < ?",
                (older_than,),
            )
            removed += cursor_before - self.dead_population()
        if keep_newest >= 0:
            overflow = self.dead_population() - keep_newest
            if overflow > 0:
                self.db.execute(
                    """
                    DELETE FROM messages WHERE state = 'DEAD' AND id IN (
                        SELECT id FROM messages WHERE state = 'DEAD'
                        ORDER BY created_at LIMIT ?
                    )
                    """,
                    (overflow,),
                )
                removed += overflow
        return removed

    def by_correlation(self, correlation_id: str) -> list[Message]:
        rows = self.db.query(
            "SELECT * FROM messages WHERE correlation_id = ? ORDER BY created_at",
            (correlation_id,),
        )
        return [self._row_to_message(row) for row in rows]

    def thread(self, conversation_id: str) -> list[Message]:
        rows = self.db.query(
            "SELECT * FROM messages WHERE conversation_id = ? ORDER BY created_at",
            (conversation_id,),
        )
        return [self._row_to_message(row) for row in rows]

    def list_messages(
        self,
        *,
        sender: str = "",
        recipient: str = "",
        msg_type: str = "",
        state: str = "",
        task_id: str = "",
        limit: int = 100,
    ) -> list[Message]:
        clauses = ["project_id = ?"]
        params: list[Any] = [self.project_id]
        if sender:
            clauses.append("sender = ?")
            params.append(sender)
        if recipient:
            clauses.append("recipient = ?")
            params.append(recipient)
        if msg_type:
            clauses.append("type = ?")
            params.append(msg_type)
        if state:
            clauses.append("state = ?")
            params.append(state)
        if task_id:
            clauses.append("task_id = ?")
            params.append(task_id)
        params.append(limit)
        rows = self.db.query(
            f"SELECT * FROM messages WHERE {' AND '.join(clauses)} "
            "ORDER BY created_at DESC LIMIT ?",
            tuple(params),
        )
        return [self._row_to_message(row) for row in rows]

    # ---- idempotency (§14) ----

    def find_duplicate(self, idempotency_key: str) -> Message | None:
        """A prior message carrying the same idempotency key, if any."""
        row = self.db.query_one(
            """
            SELECT * FROM messages
            WHERE project_id = ? AND json_extract(payload_json, '$.idempotency_key') = ?
            ORDER BY created_at DESC LIMIT 1
            """,
            (self.project_id, idempotency_key),
        )
        return self._row_to_message(row) if row else None

    # ---- stats (§66) ----

    def stats(self) -> dict[str, Any]:
        row = self.db.query_one(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN state IN ('QUEUED','DELIVERED') THEN 1 ELSE 0 END) AS pending,
                SUM(CASE WHEN state = 'COMPLETED' THEN 1 ELSE 0 END) AS completed,
                SUM(CASE WHEN state = 'FAILED' THEN 1 ELSE 0 END) AS failed,
                SUM(CASE WHEN state = 'DEAD' THEN 1 ELSE 0 END) AS dead,
                SUM(CASE WHEN state = 'EXPIRED' THEN 1 ELSE 0 END) AS expired,
                SUM(CASE WHEN attempts > 1 THEN 1 ELSE 0 END) AS retried
            FROM messages WHERE project_id = ?
            """,
            (self.project_id,),
        )
        if not row:
            return {"total": 0}
        return {key: int(row[key] or 0) for key in row}
