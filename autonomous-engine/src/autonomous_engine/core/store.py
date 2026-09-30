"""Store: transactional, typed access to persistent runtime state.

This is the deterministic persistence API used by the orchestrator and the
CLI. All statements use bound parameters; identifiers used for row selection
come from code-owned query constants, never from model output.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .database import Database, _dump, _loads
from .task import AttemptRecord, Task, TaskGraph, now_iso


class ProjectRecord(BaseModel):
    id: str
    name: str
    objective: str = ""
    intent: dict[str, Any] = Field(default_factory=dict)
    status: str = "active"
    created_at: str = now_iso()
    updated_at: str = now_iso()


class RunRecord(BaseModel):
    id: str
    project_id: str
    started_at: str = now_iso()
    finished_at: str | None = None
    status: str = "running"  # running | paused | stopped
    stop_reason: str = ""
    stats: dict[str, Any] = Field(default_factory=dict)


class DecisionRecord(BaseModel):
    id: str
    project_id: str
    title: str
    body: str = ""
    status: str = "accepted"  # accepted | rejected | superseded
    confidence: float = 0.5
    evidence: list[str] = Field(default_factory=list)
    decided_by: str = ""
    created_at: str = now_iso()


class FailureRecord(BaseModel):
    id: str
    project_id: str
    task_id: str = ""
    agent: str = ""
    summary: str
    root_cause: str = ""
    evidence: dict[str, Any] = Field(default_factory=dict)
    lesson: str = ""
    created_at: str = now_iso()


class UnknownRecord(BaseModel):
    id: str
    project_id: str
    question: str
    status: str = "open"  # open | resolved
    answer: str = ""
    raised_by: str = ""
    created_at: str = now_iso()
    resolved_at: str | None = None


class BudgetUsage(BaseModel):
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    calls: int = 0


class CheckpointRecord(BaseModel):
    id: str
    project_id: str
    run_id: str = ""
    git_commit: str = ""
    objective: str = ""
    outstanding_failures: list[str] = Field(default_factory=list)
    environment: dict[str, Any] = Field(default_factory=dict)
    task_graph: dict[str, Any] = Field(default_factory=dict)
    project_state: dict[str, Any] = Field(default_factory=dict)
    created_at: str = now_iso()


_INSERT_UPSERT_TASK = """
INSERT INTO tasks (id, project_id, payload, status, priority, epic, assigned_agent, attempts, created_at, updated_at, completed_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(id) DO UPDATE SET
    payload=excluded.payload, status=excluded.status, priority=excluded.priority,
    epic=excluded.epic, assigned_agent=excluded.assigned_agent, attempts=excluded.attempts,
    updated_at=excluded.updated_at, completed_at=excluded.completed_at
"""

_SELECT_TASKS = "SELECT payload FROM tasks WHERE project_id = ? ORDER BY priority, created_at"


class Store:
    """Typed persistence API over :class:`Database`."""

    def __init__(self, db: Database, project_id: str):
        self.db = db
        self.project_id = project_id
        self.db.initialize()

    # ---- projects ----

    def upsert_project(self, project: ProjectRecord) -> None:
        self.db.execute(
            """
            INSERT INTO projects (id, name, objective, intent_json, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name, objective=excluded.objective,
                intent_json=excluded.intent_json, status=excluded.status,
                updated_at=excluded.updated_at
            """,
            (
                project.id,
                project.name,
                project.objective,
                _dump(project.intent),
                project.status,
                project.created_at,
                project.updated_at,
            ),
        )

    def get_project(self) -> ProjectRecord | None:
        row = self.db.query_one("SELECT * FROM projects WHERE id = ?", (self.project_id,))
        if not row:
            return None
        return ProjectRecord(
            id=row["id"],
            name=row["name"],
            objective=row["objective"],
            intent=_loads(row["intent_json"], {}),
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    # ---- tasks ----

    def save_task(self, task: Task) -> None:
        self.db.execute(
            _INSERT_UPSERT_TASK,
            (
                task.id,
                self.project_id,
                _dump(task.model_dump(mode="json")),
                task.status.value,
                task.priority,
                task.epic,
                task.assigned_agent,
                task.attempts,
                task.created_at,
                task.updated_at,
                task.completed_at,
            ),
        )

    def save_graph(self, graph: TaskGraph) -> None:
        for task in graph.all():
            self.save_task(task)

    def load_graph(self) -> TaskGraph:
        rows = self.db.query(_SELECT_TASKS, (self.project_id,))
        graph = TaskGraph()
        for row in rows:
            payload = _loads(row["payload"], None)
            if not payload:
                continue
            try:
                graph.tasks[payload["id"]] = Task.model_validate(payload)
            except Exception:
                continue  # tolerate a schema-drifted row rather than losing the run
        return graph

    # ---- runs ----

    def start_run(self, run: RunRecord) -> None:
        self.db.execute(
            """
            INSERT INTO runs (id, project_id, started_at, finished_at, status, stop_reason, stats_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                finished_at=excluded.finished_at, status=excluded.status,
                stop_reason=excluded.stop_reason, stats_json=excluded.stats_json
            """,
            (
                run.id,
                run.project_id,
                run.started_at,
                run.finished_at,
                run.status,
                run.stop_reason,
                _dump(run.stats),
            ),
        )

    def finish_run(self, run_id: str, status: str, stop_reason: str, stats: dict[str, Any]) -> None:
        self.db.execute(
            "UPDATE runs SET finished_at = ?, status = ?, stop_reason = ?, stats_json = ? WHERE id = ?",
            (now_iso(), status, stop_reason, _dump(stats), run_id),
        )

    def latest_run(self) -> RunRecord | None:
        row = self.db.query_one(
            "SELECT * FROM runs WHERE project_id = ? ORDER BY started_at DESC LIMIT 1",
            (self.project_id,),
        )
        if not row:
            return None
        return RunRecord(
            id=row["id"],
            project_id=row["project_id"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            status=row["status"],
            stop_reason=row["stop_reason"],
            stats=_loads(row["stats_json"], {}),
        )

    # ---- events (SQLite mirror of the JSONL log) ----

    def append_event(self, event: str, payload: dict[str, Any]) -> None:
        self.db.execute(
            "INSERT INTO events (project_id, event, payload, timestamp) VALUES (?, ?, ?, ?)",
            (self.project_id, event, _dump(payload), payload.get("timestamp", now_iso())),
        )

    def recent_events(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT event, payload, timestamp FROM events WHERE project_id = ? ORDER BY id DESC LIMIT ?",
            (self.project_id, limit),
        )
        out: list[dict[str, Any]] = []
        for row in rows:
            record = _loads(row["payload"], {})
            record["event"] = row["event"]
            record["timestamp"] = row["timestamp"]
            out.append(record)
        return list(reversed(out))

    # ---- checkpoints ----

    def save_checkpoint(self, checkpoint: CheckpointRecord) -> None:
        self.db.execute(
            """
            INSERT INTO checkpoints (id, project_id, run_id, git_commit, payload, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                run_id=excluded.run_id, git_commit=excluded.git_commit,
                payload=excluded.payload, created_at=excluded.created_at
            """,
            (
                checkpoint.id,
                checkpoint.project_id,
                checkpoint.run_id,
                checkpoint.git_commit,
                _dump(checkpoint.model_dump(mode="json")),
                checkpoint.created_at,
            ),
        )

    def list_checkpoints(self) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT id, run_id, git_commit, created_at FROM checkpoints WHERE project_id = ? ORDER BY created_at",
            (self.project_id,),
        )

    def get_checkpoint(self, checkpoint_id: str) -> CheckpointRecord | None:
        row = self.db.query_one("SELECT payload FROM checkpoints WHERE id = ?", (checkpoint_id,))
        if not row:
            return None
        return CheckpointRecord.model_validate(_loads(row["payload"], {}))

    # ---- decisions / failures / unknowns ----

    def save_decision(self, decision: DecisionRecord) -> None:
        self.db.execute(
            """
            INSERT INTO decisions (id, project_id, title, body, status, confidence, evidence_json, decided_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                body=excluded.body, status=excluded.status, confidence=excluded.confidence,
                evidence_json=excluded.evidence_json, decided_by=excluded.decided_by
            """,
            (
                decision.id,
                decision.project_id,
                decision.title,
                decision.body,
                decision.status,
                decision.confidence,
                _dump(decision.evidence),
                decision.decided_by,
                decision.created_at,
            ),
        )

    def list_decisions(self) -> list[DecisionRecord]:
        rows = self.db.query(
            "SELECT * FROM decisions WHERE project_id = ? ORDER BY created_at", (self.project_id,)
        )
        return [
            DecisionRecord(
                id=r["id"],
                project_id=r["project_id"],
                title=r["title"],
                body=r["body"],
                status=r["status"],
                confidence=r["confidence"],
                evidence=_loads(r["evidence_json"], []),
                decided_by=r["decided_by"],
                created_at=r["created_at"],
            )
            for r in rows
        ]

    def save_failure(self, failure: FailureRecord) -> None:
        self.db.execute(
            """
            INSERT INTO failures (id, project_id, task_id, agent, summary, root_cause, evidence_json, lesson, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                failure.id,
                failure.project_id,
                failure.task_id,
                failure.agent,
                failure.summary,
                failure.root_cause,
                _dump(failure.evidence),
                failure.lesson,
                failure.created_at,
            ),
        )

    def list_failures(self) -> list[FailureRecord]:
        rows = self.db.query(
            "SELECT * FROM failures WHERE project_id = ? ORDER BY created_at", (self.project_id,)
        )
        return [
            FailureRecord(
                id=r["id"],
                project_id=r["project_id"],
                task_id=r["task_id"],
                agent=r["agent"],
                summary=r["summary"],
                root_cause=r["root_cause"],
                evidence=_loads(r["evidence_json"], {}),
                lesson=r["lesson"],
                created_at=r["created_at"],
            )
            for r in rows
        ]

    def save_unknown(self, unknown: UnknownRecord) -> None:
        self.db.execute(
            """
            INSERT INTO unknowns (id, project_id, question, status, answer, raised_by, created_at, resolved_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                status=excluded.status, answer=excluded.answer, resolved_at=excluded.resolved_at
            """,
            (
                unknown.id,
                unknown.project_id,
                unknown.question,
                unknown.status,
                unknown.answer,
                unknown.raised_by,
                unknown.created_at,
                unknown.resolved_at,
            ),
        )

    def open_unknowns(self) -> list[UnknownRecord]:
        rows = self.db.query(
            "SELECT * FROM unknowns WHERE project_id = ? AND status = ? ORDER BY created_at",
            (self.project_id, "open"),
        )
        return [
            UnknownRecord(
                id=r["id"],
                project_id=r["project_id"],
                question=r["question"],
                status=r["status"],
                answer=r["answer"],
                raised_by=r["raised_by"],
                created_at=r["created_at"],
                resolved_at=r["resolved_at"],
            )
            for r in rows
        ]

    # ---- budget ----

    def record_usage(
        self, run_id: str, agent: str, model: str, tokens_in: int, tokens_out: int, cost_usd: float
    ) -> None:
        self.db.execute(
            """
            INSERT INTO budget_usage (project_id, run_id, tokens_in, tokens_out, cost_usd, agent, model, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (self.project_id, run_id, tokens_in, tokens_out, cost_usd, agent, model, now_iso()),
        )

    def total_usage(self) -> BudgetUsage:
        row = self.db.query_one(
            """
            SELECT COALESCE(SUM(tokens_in), 0) AS ti, COALESCE(SUM(tokens_out), 0) AS to_,
                   COALESCE(SUM(cost_usd), 0) AS cost, COUNT(*) AS calls
            FROM budget_usage WHERE project_id = ?
            """,
            (self.project_id,),
        )
        if not row:
            return BudgetUsage()
        return BudgetUsage(
            tokens_in=int(row["ti"]),
            tokens_out=int(row["to_"]),
            cost_usd=float(row["cost"]),
            calls=int(row["calls"]),
        )

    # ---- agent activity ----

    def record_activity(self, agent: str, action: str, detail: str = "") -> None:
        self.db.execute(
            "INSERT INTO agent_activity (project_id, agent, action, detail, timestamp) VALUES (?, ?, ?, ?, ?)",
            (self.project_id, agent, action, detail, now_iso()),
        )

    def recent_activity(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT agent, action, detail, timestamp FROM agent_activity WHERE project_id = ? ORDER BY id DESC LIMIT ?",
            (self.project_id, limit),
        )


def attempts_from_graph(graph: TaskGraph) -> list[AttemptRecord]:
    out: list[AttemptRecord] = []
    for task in graph.all():
        out.extend(task.attempts_history)
    return out
