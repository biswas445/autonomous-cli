"""SQLite persistence layer.

The runtime is built to survive crashes, context resets, and restarts, so
task state, events, checkpoints, decisions, failures, budget usage and agent
activity are all persisted here. Writes are atomic; readers tolerate torn
state.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    objective TEXT NOT NULL DEFAULT '',
    intent_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 5,
    epic TEXT NOT NULL DEFAULT '',
    assigned_agent TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_project_status ON tasks(project_id, status);
CREATE INDEX IF NOT EXISTS idx_tasks_priority ON tasks(priority);

CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    stop_reason TEXT NOT NULL DEFAULT '',
    stats_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    event TEXT NOT NULL,
    payload TEXT NOT NULL,
    timestamp TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_project ON events(project_id, id);

CREATE TABLE IF NOT EXISTS checkpoints (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    run_id TEXT NOT NULL DEFAULT '',
    git_commit TEXT NOT NULL DEFAULT '',
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decisions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'accepted',
    confidence REAL NOT NULL DEFAULT 0.5,
    evidence_json TEXT NOT NULL DEFAULT '[]',
    decided_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS failures (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    task_id TEXT NOT NULL DEFAULT '',
    agent TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL,
    root_cause TEXT NOT NULL DEFAULT '',
    evidence_json TEXT NOT NULL DEFAULT '{}',
    lesson TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS unknowns (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    question TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    answer TEXT NOT NULL DEFAULT '',
    raised_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS budget_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    run_id TEXT NOT NULL DEFAULT '',
    tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    agent TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    agent TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    timestamp TEXT NOT NULL
);

-- Agent-to-agent message transport (durable queue; survives restarts).
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    run_id TEXT NOT NULL DEFAULT '',
    task_id TEXT NOT NULL DEFAULT '',
    type TEXT NOT NULL,
    sender TEXT NOT NULL,
    recipient TEXT NOT NULL,
    conversation_id TEXT NOT NULL DEFAULT '',
    parent_message_id TEXT NOT NULL DEFAULT '',
    correlation_id TEXT NOT NULL DEFAULT '',
    priority TEXT NOT NULL DEFAULT 'normal',
    state TEXT NOT NULL DEFAULT 'CREATED',
    attempts INTEGER NOT NULL DEFAULT 0,
    requires_response INTEGER NOT NULL DEFAULT 0,
    expires_at TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    context_refs_json TEXT NOT NULL DEFAULT '[]',
    artifact_refs_json TEXT NOT NULL DEFAULT '[]',
    status_detail TEXT NOT NULL DEFAULT '',
    protocol_version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_project_state ON messages(project_id, state);
CREATE INDEX IF NOT EXISTS idx_messages_recipient ON messages(recipient, state);
CREATE INDEX IF NOT EXISTS idx_messages_correlation ON messages(correlation_id);
CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id);
"""


class Database:
    """Thin, explicit SQLite wrapper. One connection per thread.

    All statements run with bound parameters; caller-supplied values are
    never interpolated into SQL text, so they cannot alter query logic.
    The schema is a module constant, not user input.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_lock = threading.Lock()
        self._initialized = False

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    def initialize(self) -> None:
        with self._init_lock:
            if self._initialized:
                return
            conn = self._conn()
            conn.executescript(_SCHEMA)
            conn.commit()
            self._initialized = True

    def execute(self, sql: str, params: tuple | dict = ()) -> None:
        """Run a write statement. Callers MUST bind values via ?/:name placeholders."""
        self.initialize()
        conn = self._conn()
        conn.execute(sql, params)
        conn.commit()

    def query(self, sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
        """Run a read statement. Callers MUST bind values via ?/:name placeholders."""
        self.initialize()
        conn = self._conn()
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def query_one(self, sql: str, params: tuple | dict = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None


def _dump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def _loads(text: str, default: Any) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return default
