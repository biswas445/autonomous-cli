"""RuntimeFacade: the UI's only door into the runtime (spec §69).

Every piece of displayed state is read from the runtime's own persistence
(workspace JSON, event log, SQLite store); every control action goes through
the runtime's existing mechanisms (ControlChannel file, escalation records,
config). The facade holds no business logic and no competing state — it is a
read/act adapter, so the TUI can never silently disagree with the runtime.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..core.config import ProjectConfig
from ..core.events import EventLog
from ..core.store import Store
from ..core.workspace import Workspace
from ..runtime.control import ControlChannel
from ..runtime.memory import memory_store


def _open_context(root: Path):  # typed loosely to avoid an import cycle at module load
    from ..runtime.context_setup import open_context

    return open_context(root)


class RuntimeFacade:
    """Read/act adapter over one project's runtime state."""

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.workspace = Workspace(self.root)
        if not self.workspace.exists():
            raise FileNotFoundError(
                f"no autonomous-engine project at {self.root}; run `auto init` first"
            )
        self.ctx = _open_context(self.root)
        self.store: Store = self.ctx.store
        self.config: ProjectConfig = self.ctx.config
        self.events: EventLog = self.workspace.events
        self.control = ControlChannel(self.workspace.paths.execution)
        self._last_event_ts = ""

    # ---- project / run state (all reads from runtime persistence) ----

    def project(self) -> dict[str, Any]:
        return self.workspace.load_project()

    def objective(self) -> str:
        return str(self.project().get("objective", ""))

    def current_run(self) -> dict[str, Any]:
        return self.workspace.load_run()

    def graph(self):
        return self.workspace.load_graph()

    def progress(self) -> dict[str, int]:
        return self.graph().progress()

    def budget_snapshot(self) -> dict[str, Any]:
        run = self.current_run()
        return run.get("budget") or {}

    def milestones(self) -> list[dict[str, Any]]:
        return list(self.workspace.load_roadmap().get("milestones", []))

    def escalations(self, pending_only: bool = True) -> list[dict[str, Any]]:
        items = self.workspace.load_escalations()
        if pending_only:
            return [e for e in items if e.get("status") == "pending"]
        return items

    def checkpoints(self) -> list[dict[str, Any]]:
        rows = self.store.list_checkpoints()
        return [
            {
                "id": r["id"],
                "run_id": r["run_id"],
                "git_commit": r["git_commit"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    def failures(self) -> list[dict[str, Any]]:
        return [
            {
                "id": f.id,
                "task_id": f.task_id,
                "agent": f.agent,
                "summary": f.summary,
                "root_cause": f.root_cause,
                "lesson": f.lesson,
                "created_at": f.created_at,
            }
            for f in self.store.list_failures()
        ]

    def memory(self) -> list[dict[str, Any]]:
        return [i.as_dict() for i in memory_store(self.workspace).all()]

    def unknowns(self) -> list[dict[str, Any]]:
        return [
            {"id": u.id, "question": u.question, "status": u.status, "answer": u.answer}
            for u in self.store.open_unknowns()
        ]

    def verification_health(self) -> dict[str, Any]:
        """Aggregate real verification evidence across the graph (no invention)."""
        graph = self.graph()
        passed = failed = unknown = 0
        for task in graph.all():
            verdict = task.verification or {}
            if not verdict:
                continue
            if verdict.get("passed"):
                passed += 1
            elif verdict.get("passed") is False:
                failed += 1
            else:
                unknown += 1
        return {"passed": passed, "failed": failed, "unknown": unknown}

    def stale_evidence_tasks(self) -> list[tuple[str, str]]:
        """(task_id, evidence commit) where latest evidence predates HEAD (§14)."""
        from ..git.manager import GitManager
        from ..verification.evidence import EvidenceStore

        try:
            git = GitManager(self.root)
            current = git.head_commit() if git.is_repo() else ""
        except Exception:
            return []
        if not current:
            return []
        store = EvidenceStore(self.workspace.paths.state)
        try:
            return store.stale_tasks(list(self.graph().tasks), current)
        except Exception:
            return []

    # ---- tasks / agents / models ----

    def tasks(self) -> list[dict[str, Any]]:
        graph = self.graph()
        active_ids = {t.id for t in graph.active_tasks()}
        out: list[dict[str, Any]] = []
        for task in graph.all():
            out.append(
                {
                    "id": task.id,
                    "title": task.title,
                    "status": task.status.value,
                    "priority": task.priority,
                    "risk": task.risk,
                    "epic": task.epic,
                    "attempts": task.attempts,
                    "assigned_agent": task.assigned_agent,
                    "dependencies": list(task.dependencies),
                    "active": task.id in active_ids,
                    "artifacts": list(task.artifacts),
                    "verification_passed": (task.verification or {}).get("passed"),
                }
            )
        out.sort(key=lambda t: (not t["active"], t["status"] != "IMPLEMENTING", t["priority"], t["id"]))
        return out

    def task_detail(self, task_id: str) -> dict[str, Any] | None:
        try:
            task = self.graph().get(task_id)
        except KeyError:
            return None
        return {
            "id": task.id,
            "title": task.title,
            "description": task.description,
            "status": task.status.value,
            "priority": task.priority,
            "risk": task.risk,
            "complexity": task.estimated_complexity,
            "epic": task.epic,
            "assigned_agent": task.assigned_agent,
            "dependencies": list(task.dependencies),
            "acceptance_criteria": list(task.acceptance_criteria),
            "definition_of_done": list(task.definition_of_done),
            "verification_commands": list(task.verification_commands),
            "attempts": task.attempts,
            "attempts_history": [a.model_dump(mode="json") for a in task.attempts_history],
            "verification": task.verification or {},
            "artifacts": list(task.artifacts),
            "created_by": task.created_by,
            "created_at": task.created_at,
            "completed_at": task.completed_at,
            "history": [h.model_dump(mode="json") for h in task.history],
        }

    def active_agents(self) -> list[dict[str, Any]]:
        """Agents derive from real task assignments; nothing is invented."""
        graph = self.graph()
        run = self.current_run()
        agents: list[dict[str, Any]] = []
        for task in graph.active_tasks():
            agents.append(
                {
                    "agent": task.assigned_agent or f"unassigned[{task.id}]",
                    "role": task.role.value,
                    "task_id": task.id,
                    "task_title": task.title,
                    "status": task.status.value,
                    "attempt": task.attempts,
                    "risk": task.risk,
                }
            )
        idle = not agents and not graph.ready_tasks() and run.get("status") == "running"
        return agents if not idle else []

    def models(self) -> list[dict[str, Any]]:
        """Configured routes + real usage from the store's budget_usage table."""
        usage = self._usage_by_model()
        out: list[dict[str, Any]] = []
        for route in self.config.model_routes:
            # record_usage stores the bare model id (no provider prefix), so
            # the lookup key must match or the panel always shows zeros.
            stats = usage.get(route.model, {})
            out.append(
                {
                    "provider": route.provider,
                    "model": route.model,
                    "role": route.role,
                    "fallbacks": list(route.fallbacks),
                    "calls": int(stats.get("calls", 0)),
                    "tokens_in": int(stats.get("tokens_in", 0)),
                    "tokens_out": int(stats.get("tokens_out", 0)),
                    "cost_usd": float(stats.get("cost_usd", 0.0)),
                    "health": self._health_for(route),
                }
            )
        return out

    def _usage_by_model(self) -> dict[str, dict[str, Any]]:
        try:
            rows = self.db().query(
                """
                SELECT model, COUNT(*) AS calls,
                       COALESCE(SUM(tokens_in), 0) AS tokens_in,
                       COALESCE(SUM(tokens_out), 0) AS tokens_out,
                       COALESCE(SUM(cost_usd), 0) AS cost_usd
                FROM budget_usage WHERE project_id = ?
                GROUP BY model
                """,
                (self.store.project_id,),
            )
        except Exception:
            return {}
        out: dict[str, dict[str, Any]] = {}
        for row in rows:
            key = str(row["model"])
            out[key] = {
                "calls": int(row["calls"]),
                "tokens_in": int(row["tokens_in"]),
                "tokens_out": int(row["tokens_out"]),
                "cost_usd": float(row["cost_usd"]),
            }
        return out

    # ---- agent communication (comms spec §57–§60, §73) ----

    def _message_store(self):
        from ..messaging import MessageStore

        return MessageStore(self.ctx.store.db, self.ctx.store.project_id)

    @staticmethod
    def _message_dict(message) -> dict[str, Any]:
        """One real persisted message as display data (§58)."""
        payload = dict(message.payload or {})
        summary = ""
        for key in (
            "summary", "discovery", "question", "reason", "findings", "description",
            "title", "note", "status", "blocker_type", "answer",
        ):
            value = payload.get(key)
            if value:
                if isinstance(value, list):
                    value = "; ".join(str(v) for v in value[:3])
                summary = str(value).replace("\n", " ")[:160]
                break
        return {
            "id": message.id,
            "type": message.type.value,
            "sender": message.sender,
            "recipient": message.recipient,
            "project_id": message.project_id,
            "run_id": message.run_id,
            "task_id": message.task_id,
            "conversation_id": message.conversation_id,
            "parent_message_id": message.parent_message_id,
            "correlation_id": message.correlation_id,
            "priority": message.priority.value,
            "state": message.state.value,
            "created_at": message.timestamp,
            "requires_response": message.requires_response,
            "expires_at": message.expires_at,
            "attempts": message.attempts,
            "status_detail": message.status_detail,
            "payload": payload,
            "summary": summary,
            "context_refs": list(message.context_refs),
            "artifact_refs": list(message.artifact_refs),
        }

    def messages(self, limit: int = 60) -> list[dict[str, Any]]:
        """Recent traffic, oldest first, for the AGENT COMMUNICATION view."""
        store = self._message_store()
        rows = store.list_messages(limit=limit)
        return [self._message_dict(m) for m in reversed(rows)]

    def message_detail(self, message_id: str) -> dict[str, Any] | None:
        store = self._message_store()
        message = store.get(message_id)
        return self._message_dict(message) if message is not None else None

    def agent_comm_stats(self) -> list[dict[str, Any]]:
        """Per-agent sent/received/pending/failed from real messages (§59)."""
        store = self._message_store()
        rows = store.list_messages(limit=1000)
        stats: dict[str, dict[str, int]] = {}
        for m in rows:
            for name, field_name in ((m.sender, "sent"), (m.recipient, "received")):
                entry = stats.setdefault(name, {"sent": 0, "received": 0, "pending": 0, "failed": 0})
                entry[field_name] += 1
                if field_name == "received" and m.state in ("QUEUED", "DELIVERED", "RECEIVED"):
                    entry["pending"] += 1
                if field_name == "received" and m.state in ("FAILED", "DEAD"):
                    entry["failed"] += 1
        return [
            {"agent": name, **counts}
            for name, counts in sorted(stats.items(), key=lambda kv: -(kv[1]["sent"] + kv[1]["received"]))
        ]

    def communication_graph(self, *, limit: int = 14) -> list[dict[str, Any]]:
        """Who talked to whom, as directed edges with counts (§73/§74)."""
        store = self._message_store()
        rows = store.list_messages(limit=1000)
        edges: dict[tuple[str, str], dict[str, int]] = {}
        for m in rows:
            edge = edges.setdefault(
                (m.sender, m.recipient), {"messages": 0, "failed": 0}
            )
            edge["messages"] += 1
            if m.state in ("FAILED", "DEAD", "EXPIRED"):
                edge["failed"] += 1
        return [
            {"sender": sender, "recipient": recipient, **counts}
            for (sender, recipient), counts in sorted(
                edges.items(), key=lambda kv: -kv[1]["messages"]
            )[:limit]
        ]

    def db(self):
        return self.ctx.db

    def _health_for(self, route) -> str:
        from ..models.capabilities import REGISTRY

        profile = REGISTRY.profile(route.provider, route.model)
        if profile is None:
            return "unknown"
        return REGISTRY.health(route.provider, route.model).value

    # ---- events ----

    def recent_events(self, limit: int = 200) -> list[dict[str, Any]]:
        events = self.events.read_last(limit)
        if events:
            self._last_event_ts = str(events[-1].get("timestamp", ""))
        return events

    def events_since(self) -> list[dict[str, Any]]:
        """Incremental tail since the last poll (push-like, cheap)."""
        fresh = self.events.tail_since(self._last_event_ts) if self._last_event_ts else []
        if fresh:
            self._last_event_ts = str(fresh[-1].get("timestamp", ""))
        return fresh

    # ---- controls (all through existing runtime mechanisms) ----

    def pause(self, reason: str = "paused from the UI") -> None:
        self.control.request(pause=True, reason=reason)

    def resume(self) -> None:
        self.control.request(resume=True, reason="resumed from the UI")

    def cancel(self, reason: str = "cancelled from the UI") -> None:
        self.control.request(stop=True, reason=reason)

    def approve(self, escalation_id: str | None = None) -> bool:
        items = self.escalations()
        if escalation_id is None:
            if not items:
                return False
            escalation_id = items[0]["id"]
        self.control.request(approvals=[escalation_id])
        return True

    def reject(self, escalation_id: str | None = None) -> bool:
        items = self.escalations()
        if escalation_id is None:
            if not items:
                return False
            escalation_id = items[0]["id"]
        self.control.request(rejections=[escalation_id])
        return True

    def remember(self, text: str, kind: str = "fact", tags: list[str] | None = None) -> bool:
        """Persist an operator instruction through the runtime's memory API."""
        text = " ".join(str(text).split())
        if not text:
            return False
        from ..runtime.memory import remember

        remember(self.workspace, text, kind=kind, source="ui-operator", tags=tags or [])
        return True

    def set_enhance_prompt(self, enabled: bool) -> bool:
        """Toggle the Intent Compiler through the project config (§18)."""
        try:
            self.config.enhance_prompt = bool(enabled)
            self.workspace.save_config(self.config)
            return True
        except Exception:
            return False

    def enhance_prompt_enabled(self) -> bool:
        return bool(self.config.enhance_prompt)

    def close(self) -> None:
        import contextlib

        with contextlib.suppress(Exception):
            self.ctx.db.close()
