"""Agent message handling (comms spec §53, §62, §38).

Agents never write messages directly from model output: the runtime binds the
sender identity when an agent's handler runs, the agent interprets its model
output first, and only the resulting structured action becomes a message.
"""

from __future__ import annotations

from typing import Any

from .models import DIRECTOR, Message, MsgType
from .service import MessageService


class AgentMessenger:
    """The messaging surface one agent uses — bound to its runtime identity."""

    def __init__(self, service: MessageService, agent_name: str):
        if not agent_name:
            raise ValueError("agent identity is required")
        self.service = service
        self.agent_name = agent_name

    # ---- outbound (identity pre-bound; model text cannot change it) ----

    def send(
        self,
        msg_type: MsgType | str,
        recipient: str = DIRECTOR,
        *,
        payload: dict[str, Any] | None = None,
        task_id: str = "",
        parent: Message | None = None,
        **kwargs: Any,
    ) -> Message:
        return self.service.send(
            msg_type=msg_type,
            sender=self.agent_name,
            recipient=recipient,
            payload=payload,
            task_id=task_id,
            parent=parent,
            **kwargs,
        )

    # ---- the standard collaboration patterns (spec §20–§27, §75) ----

    def task_completed(self, task_id: str, summary: str, **extra: Any) -> Message:
        return self.send(
            MsgType.TASK_COMPLETED,
            payload={"task_id": task_id, "summary": summary, **extra},
            task_id=task_id,
        )

    def task_failed(
        self, task_id: str, failure_type: str, summary: str, *, attempt: int = 1,
        recommended_action: str = "", evidence_refs: list[str] | None = None, **extra: Any,
    ) -> Message:
        return self.send(
            MsgType.TASK_FAILED,
            payload={
                "task_id": task_id,
                "failure_type": failure_type.upper(),
                "summary": summary,
                "attempt": attempt,
                "recommended_action": recommended_action,
                **({"evidence_refs": evidence_refs} if evidence_refs else {}),
                **extra,
            },
            task_id=task_id,
            priority="high" if failure_type.upper() in ("SECURITY", "MODEL") else "normal",
        )

    def verification_request(self, tester: str, task_id: str, *, commit: str = "", **extra: Any) -> Message:
        return self.send(
            MsgType.VERIFICATION_REQUEST,
            recipient=tester,
            payload={"task_id": task_id, "commit": commit, **extra},
            task_id=task_id,
            requires_response=True,
        )

    def verification_result(self, task_id: str, *, passed: bool, summary: str, **extra: Any) -> Message:
        return self.send(
            MsgType.VERIFICATION_RESULT,
            payload={"task_id": task_id, "passed": passed, "summary": summary, **extra},
            task_id=task_id,
            priority="high" if not passed else "normal",
        )

    def review_request(self, task_id: str, *, commit: str = "", changed_files: list[str] | None = None) -> Message:
        return self.send(
            MsgType.REVIEW_REQUEST,
            recipient="reviewer",
            payload={"task_id": task_id, "commit": commit, "changed_files": changed_files or []},
            task_id=task_id,
            requires_response=True,
        )

    def discovery(self, discovery: str, *, confidence: float = 0.6, evidence_refs: list[str] | None = None, **extra: Any) -> Message:
        return self.send(
            MsgType.DISCOVERY_REPORTED,
            payload={"discovery": discovery, "confidence": confidence, **extra},
            priority="normal",
        )

    def blocker(self, task_id: str, blocker_type: str, description: str, *, severity: str = "medium", **extra: Any) -> Message:
        return self.send(
            MsgType.BLOCKER_REPORTED,
            payload={"task_id": task_id, "blocker_type": blocker_type, "description": description, "severity": severity, **extra},
            task_id=task_id,
            priority="high" if severity == "critical" else "normal",
        )

    def replan_request(self, reason: str, *, task_id: str = "", **extra: Any) -> Message:
        return self.send(
            MsgType.REPLAN_REQUEST,
            payload={"reason": reason, **extra},
            task_id=task_id,
            requires_response=True,
        )

    def help_request(self, question: str, *, recipient: str, task_id: str = "") -> Message:
        return self.send(
            MsgType.HELP_REQUEST,
            recipient=recipient,
            payload={"question": question},
            task_id=task_id,
            requires_response=True,
        )

    def handoff(self, task_id: str, to_agent: str, *, reason: str = "", **extra: Any) -> Message:
        return self.send(
            MsgType.AGENT_HANDOFF,
            recipient=to_agent,
            payload={"task_id": task_id, "to_agent": to_agent, "reason": reason, **extra},
            task_id=task_id,
            requires_response=True,
        )
