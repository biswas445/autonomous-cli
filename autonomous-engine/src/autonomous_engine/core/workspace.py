"""Project workspace: the persistent "Project Brain" on disk (plan.md §7).

Layout (created by `auto init`):

    .agents/
        config.json                     ProjectConfig (budget, permissions, routes)
        project.json                    project identity + intent + status
        constitution.md                 project law (coding/arch/security/testing rules)
        requirements/specification.md
        architecture/architecture.md, decisions.md
        planning/roadmap.json, task_graph.json
        memory/facts.json, discoveries.md, failures.json
        execution/current_run.json, events.jsonl
        verification/test_results/, review_results/
        checkpoints/checkpoint-NNN.json
        agent_logs/<agent>/...
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..core.config import ProjectConfig
from ..core.events import EventLog
from ..core.task import TaskGraph, now_iso

STATE_DIRNAME = ".agents"


@dataclass(frozen=True)
class WorkspacePaths:
    root: Path
    state: Path

    @property
    def config(self) -> Path:
        return self.state / "config.json"

    @property
    def project(self) -> Path:
        return self.state / "project.json"

    @property
    def constitution(self) -> Path:
        return self.state / "constitution.md"

    @property
    def requirements(self) -> Path:
        return self.state / "requirements"

    @property
    def architecture(self) -> Path:
        return self.state / "architecture"

    @property
    def planning(self) -> Path:
        return self.state / "planning"

    @property
    def memory(self) -> Path:
        return self.state / "memory"

    @property
    def execution(self) -> Path:
        return self.state / "execution"

    @property
    def verification(self) -> Path:
        return self.state / "verification"

    @property
    def checkpoints(self) -> Path:
        return self.state / "checkpoints"

    @property
    def agent_logs(self) -> Path:
        return self.state / "agent_logs"

    @property
    def database(self) -> Path:
        return self.state / "state.sqlite"

    @property
    def events_file(self) -> Path:
        return self.execution / "events.jsonl"

    @property
    def current_run(self) -> Path:
        return self.execution / "current_run.json"

    @property
    def escalations(self) -> Path:
        return self.execution / "escalations.json"

    def all_dirs(self) -> list[Path]:
        return [
            self.requirements,
            self.architecture,
            self.planning,
            self.memory,
            self.execution,
            self.verification,
            self.checkpoints,
            self.agent_logs,
        ]


def find_workspace(start: Path | None = None) -> WorkspacePaths | None:
    """Walk up from `start` looking for a `.agents/` directory."""
    current = (start or Path.cwd()).resolve()
    for candidate in [current, *current.parents]:
        state = candidate / STATE_DIRNAME
        if state.is_dir():
            return WorkspacePaths(root=candidate, state=state)
    return None


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # A unique tmp name per writer: a shared ".tmp" makes two concurrent
    # writers clobber each other's replace() on Windows. The fsync keeps the
    # documented crash-safety promise (rename durable, contents too).
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid4().hex[:8]}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def atomic_write_json(path: Path, payload: Any) -> None:
    atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def slugify(text: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower()).strip("-")
    return (slug[:max_len] or "project").rstrip("-")


class Workspace:
    """Typed access to the on-disk project brain."""

    def __init__(self, root: Path | str):
        root = Path(root)
        self.paths = WorkspacePaths(root=root.resolve(), state=(root / STATE_DIRNAME).resolve())
        self.events = EventLog(self.paths.events_file)

    # ---- lifecycle ----

    @classmethod
    def initialize(cls, root: Path, project_name: str, config: ProjectConfig) -> Workspace:
        ws = cls(root)
        ws.paths.state.mkdir(parents=True, exist_ok=True)
        for directory in ws.paths.all_dirs():
            directory.mkdir(parents=True, exist_ok=True)
        (ws.paths.verification / "test_results").mkdir(parents=True, exist_ok=True)
        (ws.paths.verification / "review_results").mkdir(parents=True, exist_ok=True)
        config.save(ws.paths.config)
        ws.save_project(
            {
                "schema_version": 1,
                "name": project_name,
                "created_at": now_iso(),
                "updated_at": now_iso(),
                "status": "active",
                "objective": "",
                "intent": {},
                "constitution_written": False,
            }
        )
        ws.save_graph(TaskGraph())
        ws.save_run({})
        ws.save_escalations([])
        return ws

    @classmethod
    def open(cls, root: Path) -> Workspace:
        return cls(root)

    def exists(self) -> bool:
        return self.paths.state.is_dir() and self.paths.project.is_file()

    # ---- config ----

    def load_config(self) -> ProjectConfig:
        if not self.paths.config.is_file():
            return ProjectConfig()
        try:
            return ProjectConfig.load(self.paths.config)
        except Exception as exc:
            # A malformed config must not silently downgrade the run: the
            # defaults carry a much larger budget and looser policy than an
            # operator who pinned them down would expect. Fail loudly instead.
            raise ValueError(
                f"invalid config file {self.paths.config}: {exc}; "
                "fix or remove it — defaults are deliberately NOT substituted"
            ) from exc

    def save_config(self, config: ProjectConfig) -> None:
        config.save(self.paths.config)

    # ---- project ----

    def load_project(self) -> dict[str, Any]:
        if not self.paths.project.is_file():
            return {}
        return json.loads(self.paths.project.read_text(encoding="utf-8"))

    def save_project(self, payload: dict[str, Any]) -> None:
        payload["updated_at"] = now_iso()
        atomic_write_json(self.paths.project, payload)

    def update_project(self, **fields: Any) -> dict[str, Any]:
        """Merge fields; a no-op update does not touch the file.

        Skipping identical writes keeps the repository clean between runs —
        the dirty-repo pre-flight only fires for real changes.
        """
        project = self.load_project()
        changed = {key: value for key, value in fields.items() if project.get(key) != value}
        if not changed:
            return project
        project.update(changed)
        self.save_project(project)
        return project

    # ---- task graph ----

    def load_graph(self) -> TaskGraph:
        path = self.paths.planning / "task_graph.json"
        if not path.is_file():
            return TaskGraph()
        try:
            return TaskGraph.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception:
            return TaskGraph()

    def save_graph(self, graph: TaskGraph) -> None:
        atomic_write_json(self.paths.planning / "task_graph.json", graph.model_dump(mode="json"))

    def load_roadmap(self) -> dict[str, Any]:
        path = self.paths.planning / "roadmap.json"
        if not path.is_file():
            return {"milestones": []}
        return json.loads(path.read_text(encoding="utf-8"))

    def save_roadmap(self, roadmap: dict[str, Any]) -> None:
        atomic_write_json(self.paths.planning / "roadmap.json", roadmap)

    # ---- run / control ----

    def load_run(self) -> dict[str, Any]:
        if not self.paths.current_run.is_file():
            return {}
        try:
            return json.loads(self.paths.current_run.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}

    def save_run(self, payload: dict[str, Any]) -> None:
        atomic_write_json(self.paths.current_run, payload)

    def load_escalations(self) -> list[dict[str, Any]]:
        if not self.paths.escalations.is_file():
            return []
        try:
            return json.loads(self.paths.escalations.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []

    def save_escalations(self, payload: list[dict[str, Any]]) -> None:
        atomic_write_json(self.paths.escalations, payload)

    def add_escalation(self, escalation: dict[str, Any]) -> None:
        current = self.load_escalations()
        current.append(escalation)
        self.save_escalations(current)

    def resolve_escalation(
        self, escalation_id: str, resolution: str, note: str = ""
    ) -> dict[str, Any] | None:
        escalations = self.load_escalations()
        for item in escalations:
            if item.get("id") == escalation_id:
                item["status"] = resolution
                item["resolution_note"] = note
                item["resolved_at"] = now_iso()
                self.save_escalations(escalations)
                return item
        return None

    def pending_escalations(self) -> list[dict[str, Any]]:
        return [e for e in self.load_escalations() if e.get("status") == "pending"]

    # ---- artifacts ----

    def write_artifact(self, relative: str, content: str) -> Path:
        target = self.paths.state / relative
        atomic_write(target, content)
        return target

    def write_json_artifact(self, relative: str, payload: Any) -> Path:
        target = self.paths.state / relative
        atomic_write_json(target, payload)
        return target

    def append_discovery(self, text: str) -> None:
        path = self.paths.memory / "discoveries.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(f"- [{now_iso()}] {text}\n")

    def load_facts(self) -> list[dict[str, Any]]:
        """Operator facts, read from the typed memory store (single source).

        `facts.json` is the pre-memory format; MemoryStore migrates it once.
        """
        from ..runtime.memory import memory_store

        return [
            {
                "statement": item.text,
                "source": item.source,
                "created_at": item.created_at,
                "kind": item.kind,
            }
            for item in memory_store(self).all()
            if item.kind == "fact"
        ]

    def add_fact(self, statement: str, source: str = "") -> None:
        from ..runtime.memory import remember

        remember(self, statement, kind="fact", source=source or "operator")

    def load_failures(self) -> list[dict[str, Any]]:
        path = self.paths.memory / "failures.json"
        if not path.is_file():
            return []
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []

    def add_failure(self, record: dict[str, Any]) -> None:
        failures = self.load_failures()
        failures.append(record)
        atomic_write_json(self.paths.memory / "failures.json", failures)

    def agent_log(self, agent: str, name: str, payload: Any) -> Path:
        directory = self.paths.agent_logs / agent
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        if isinstance(payload, str):
            path.write_text(payload, encoding="utf-8")
        else:
            atomic_write_json(path, payload)
        return path

    # ---- checkpoints ----

    def save_checkpoint(self, checkpoint_id: str, payload: dict[str, Any]) -> Path:
        return self.write_json_artifact(f"checkpoints/{checkpoint_id}.json", payload)

    def load_checkpoint(self, checkpoint_id: str) -> dict[str, Any] | None:
        path = self.paths.checkpoints / f"{checkpoint_id}.json"
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def list_checkpoints(self) -> list[str]:
        if not self.paths.checkpoints.is_dir():
            return []
        return sorted(p.stem for p in self.paths.checkpoints.glob("checkpoint-*.json"))
