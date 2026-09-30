"""Project memory: typed store, recall, episodes, consolidation, index."""

from __future__ import annotations

import json
from pathlib import Path

from autonomous_engine.core.task import Task
from autonomous_engine.core.workspace import Workspace
from autonomous_engine.runtime.context import ContextBuilder
from autonomous_engine.runtime.context_setup import open_context
from autonomous_engine.runtime.memory import (
    MemoryStore,
    memory_context_section,
    memory_store,
    remember,
)


def _store(project: Path) -> MemoryStore:
    return MemoryStore(project / ".agents")


# ---- typed store ------------------------------------------------------------


def test_add_dedupe_and_kinds(project):
    store = _store(project)
    item = store.add("fact", "PostgreSQL is the production database", source="operator")
    assert item.kind == "fact"
    initial_confidence = item.confidence
    duplicate = store.add(
        "fact", "PostgreSQL is the production database.", source="operator"  # trailing dot
    )
    assert duplicate.id == item.id  # merged, not duplicated
    assert duplicate.confidence > initial_confidence
    assert len(store.all()) == 1
    store.add("preference", "Always run ruff before committing", source="operator", pinned=True)
    store.add("lesson", "Library X breaks under Node 24", source="debugger")
    kinds = {i.kind for i in store.all()}
    assert kinds == {"fact", "preference", "lesson"}


def test_persisted_across_instances(project):
    _store(project).add("decision", "Authentication uses server-side sessions", source="architect")
    reopened = _store(project)
    assert any("server-side sessions" in i.text for i in reopened.all())


def test_prune_keeps_pinned_and_preferences(project):
    store = _store(project)
    store.add("preference", "Never edit generated files", pinned=True, source="operator")
    store.add("preference", "Prefer dataclasses", source="operator")
    for index in range(12):
        store.add("incident", f"transient incident number {index}", confidence=0.2, source="x")
    removed = store.prune(max_items=5)
    assert removed == 9  # 14 items - 5 kept
    remaining = store.all()
    assert len(remaining) == 5
    assert any(i.pinned for i in remaining)
    assert any(i.kind == "preference" for i in remaining)


def test_recall_ranks_relevance_and_respects_budget(project):
    store = _store(project)
    store.add("fact", "Invoices are stored as immutable records", tags=["billing"], source="op")
    store.add("lesson", "Stripe webhooks retry out of order", tags=["billing"], source="dbg")
    store.add("fact", "The marketing site is a static export", tags=["frontend"], source="op")
    recalled = store.recall("billing invoice webhook", limit=5)
    texts = [i.text for i in recalled]
    assert any("Invoices" in t for t in texts)
    assert any("Stripe" in t for t in texts)
    assert not any("marketing" in t for t in texts)

    tight = store.recall("billing invoice", char_budget=60)
    assert len(tight) < len(recalled)


def test_pinned_items_always_recall(project):
    store = _store(project)
    store.add("preference", "Money is always integer cents", pinned=True, source="operator")
    store.add("fact", "unrelated fact about the logo", source="op")
    recalled = store.recall("completely different topic")
    assert [i.text for i in recalled] == ["Money is always integer cents"]


# ---- episodes and consolidation ---------------------------------------------


def test_episodes_append_and_filter(project):
    store = _store(project)
    store.record_episode({"kind": "task", "task_id": "TASK-1", "outcome": "completed"})
    store.record_episode({"kind": "run", "stop_reason": "PROJECT_COMPLETE"})
    store.record_episode({"kind": "task", "task_id": "TASK-2", "outcome": "failed"})
    assert len(store.recent_episodes(10)) == 3
    assert [e["task_id"] for e in store.recent_episodes(5, kind="task")] == ["TASK-1", "TASK-2"]


def test_consolidate_rolls_failure_lessons_into_memory(project):
    memory_dir = project / ".agents" / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / "failures.json").write_text(
        json.dumps(
            [
                {
                    "summary": "websocket timeout in tests",
                    "root_cause": "server never closes the socket",
                    "lesson": "always close websockets in test teardown",
                },
                {"summary": "no lesson", "root_cause": "", "lesson": ""},
            ]
        ),
        encoding="utf-8",
    )
    store = _store(project)
    summary = store.consolidate()
    assert summary["lessons_added"] == 1
    lessons = [i for i in store.all() if i.kind == "lesson"]
    assert lessons and "test teardown" in lessons[0].text
    # idempotent: consolidating twice does not duplicate
    assert store.consolidate()["lessons_added"] == 0


# ---- index -------------------------------------------------------------------


def test_memory_index_is_written_and_bounded(project):
    store = _store(project)
    store.add("fact", "The API is versioned in the URL path", source="op")
    store.record_episode({"kind": "task", "task_id": "TASK-9", "outcome": "completed"})
    index = store.index_path.read_text(encoding="utf-8")
    assert "# Project Memory" in index
    assert "The API is versioned" in index
    assert "Recent episodes" in index


# ---- workspace + context integration ----------------------------------------


def test_facts_legacy_format_migrates(project):
    memory_dir = project / ".agents" / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / "facts.json").write_text(
        json.dumps([{"statement": "SQLite is the datastore", "source": "operator"}]),
        encoding="utf-8",
    )
    ws = Workspace(project)
    facts = ws.load_facts()
    assert any("SQLite is the datastore" in f["statement"] for f in facts)


def test_workspace_add_fact_routes_to_memory(project):
    ws = Workspace(project)
    ws.add_fact("The scheduler runs on Fridays", source="operator")
    items = memory_store(ws).all()
    assert any(i.text == "The scheduler runs on Fridays" and i.kind == "fact" for i in items)


def test_memory_section_in_task_context(project):
    context = open_context(project)
    remember(context.workspace, "Invoices must be idempotent", kind="fact", tags=["billing"])
    remember(context.workspace, "Never use floats for money", kind="preference", pinned=True)
    built = ContextBuilder(context.workspace).build(
        task=Task(id="T", title="add billing invoice endpoint"),
        role="coder",
        repo_root=project,
    )
    section = built.get("memory")
    assert "PROJECT MEMORY" in section
    assert "idempotent" in section
    assert "Never use floats" in section  # pinned, despite no keyword overlap
    context.db.close()


async def test_episodes_recorded_by_a_real_run(project):
    from autonomous_engine.runtime.orchestrator import Orchestrator

    context = open_context(project)
    orchestrator = Orchestrator(context, use_model_director=False)
    result = await orchestrator.run_loop("Build the memory target")
    assert result.status == "completed"

    store = memory_store(context.workspace)
    task_episodes = store.recent_episodes(50, kind="task")
    run_episodes = store.recent_episodes(5, kind="run")
    assert task_episodes and all(e["outcome"] == "completed" for e in task_episodes)
    assert run_episodes and run_episodes[-1]["stop_reason"] == "PROJECT_COMPLETE"
    # a second run recalls the first run's episodes in context
    section = memory_context_section(context.workspace, Task(id="T2", title="follow-up work"))
    assert "Recent task episodes" in section
    context.db.close()


async def test_run_ends_with_a_clean_tree(project):
    """The memory snapshot commit must leave nothing uncommitted after a run."""
    from autonomous_engine.runtime.orchestrator import Orchestrator

    context = open_context(project)
    orchestrator = Orchestrator(context, use_model_director=False)
    await orchestrator.run_loop("Build the clean-memory target")
    assert not orchestrator.git.is_dirty(), orchestrator.git.status_porcelain()
    context.db.close()


async def test_decisions_become_memories_at_release(project):
    from autonomous_engine.runtime.orchestrator import Orchestrator

    context = open_context(project)
    orchestrator = Orchestrator(context, use_model_director=False)
    result = await orchestrator.run_loop("Build the decision-memory target")
    assert result.status == "completed"
    # the offline run records a release-board decision or none; if any decision
    # exists it must be mirrored into memory as kind=decision
    store = memory_store(context.workspace)
    decisions = [i for i in store.all() if i.kind == "decision"]
    recorded = [d for d in context.store.list_decisions() if d.status == "accepted" and d.body.strip()]
    assert len(decisions) == len(recorded[-10:])
    context.db.close()


# ---- CLI ---------------------------------------------------------------------


def test_cli_memory_add_query_and_prune(project: Path):
    from typer.testing import CliRunner

    from autonomous_engine.cli.app import app

    runner = CliRunner()
    added = runner.invoke(
        app,
        [
            "memory",
            "--path",
            str(project),
            "--add",
            "Deploys happen only from main",
            "--kind",
            "preference",
            "--pinned",
            "--tags",
            "deploy",
        ],
    )
    assert added.exit_code == 0, added.output

    queried = runner.invoke(
        app, ["memory", "--path", str(project), "--query", "deployment policy", "--json"]
    )
    assert queried.exit_code == 0
    payload = json.loads(queried.output)
    assert any("Deploys happen only from main" in i["text"] for i in payload["items"])

    listed = runner.invoke(app, ["memory", "--path", str(project), "--json"])
    assert json.loads(listed.output)["items"]

    remembered = runner.invoke(
        app, ["remember", "Payments use idempotency keys", "--path", str(project), "--kind", "fact"]
    )
    assert remembered.exit_code == 0
    assert "remembered" in remembered.output
