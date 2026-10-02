"""UI tests: facade, event adapter, state store, and the Textual app.

Integration-first (spec §64/§65): the facade is tested against a *real*
offline run (echo provider), the adapter against real runtime event
vocabularies, and the Textual app through its official test harness
(`run_test`) — no mocked business state.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("textual")

from autonomous_engine.runtime.context_setup import open_context
from autonomous_engine.runtime.orchestrator import Orchestrator
from autonomous_engine.ui.app import EngineTUI
from autonomous_engine.ui.facade import RuntimeFacade
from autonomous_engine.ui.plain import _line
from autonomous_engine.ui.state import EventAdapter, UIState


@pytest.fixture()
def ran_project(project: Path) -> Path:
    """A project that has actually completed an offline run."""
    import asyncio

    def _run() -> None:
        context = open_context(project)
        try:
            orchestrator = Orchestrator(context, use_model_director=False)
            result = asyncio.run(orchestrator.run_loop("Build the UI target"))
            assert result.status == "completed"
        finally:
            context.db.close()

    _run()
    return project


# ---- facade (integration against real runtime data) --------------------------


async def test_facade_reads_real_state_after_a_run(ran_project: Path):
    facade = RuntimeFacade(ran_project)
    try:
        progress = facade.progress()
        assert progress["total"] >= 3
        assert progress["completed"] == progress["total"]

        tasks = facade.tasks()
        assert tasks and all(t["status"] == "COMPLETED" for t in tasks)

        detail = facade.task_detail(tasks[0]["id"])
        assert detail is not None
        assert detail["verification"].get("passed") is True
        assert detail["attempts"] >= 1

        health = facade.verification_health()
        assert health["passed"] >= 3 and health["failed"] == 0

        assert facade.checkpoints(), "completed run must have checkpoints"
        assert facade.objective() == "Build the UI target"
        assert facade.enhance_prompt_enabled() is True  # default config
        events = facade.recent_events(50)
        assert any(e["event"] == "task.completed" for e in events)
    finally:
        facade.close()


async def test_facade_controls_use_the_real_control_channel(ran_project: Path):
    facade = RuntimeFacade(ran_project)
    try:
        facade.pause("ui test")
        signal = facade.control.read()
        assert signal.pause and signal.reason == "ui test"
        facade.control.clear()

        facade.resume()
        assert facade.control.read().resume
        facade.control.clear()

        facade.cancel("ui cancel")
        assert facade.control.read().stop
        facade.control.clear()
    finally:
        facade.close()


async def test_facade_approve_writes_escalation_resolution(ran_project: Path):
    facade = RuntimeFacade(ran_project)
    try:
        facade.workspace.add_escalation(
            {"id": "ESC-UI-1", "task_id": "TASK-1", "kind": "test", "reason": "because", "status": "pending"}
        )
        assert facade.escalations()[0]["id"] == "ESC-UI-1"
        assert facade.approve() is True
        # The orchestrator resolves via the control channel; the request is queued.
        assert facade.control.read().approvals == ["ESC-UI-1"]
        facade.control.clear()
        # Direct resolution path (what the orchestrator does when consuming):
        facade.workspace.resolve_escalation("ESC-UI-1", "approved", "ui test")
        assert facade.escalations() == []
    finally:
        facade.close()


async def test_facade_memory_and_enhance_toggle(ran_project: Path):
    facade = RuntimeFacade(ran_project)
    try:
        assert facade.remember("The UI reads only real state", kind="fact") is True
        assert facade.remember("   ") is False  # no empty memories
        assert any("real state" in m["text"] for m in facade.memory())

        assert facade.set_enhance_prompt(False) is True
        assert facade.enhance_prompt_enabled() is False
        assert facade.set_enhance_prompt(True) is True
    finally:
        facade.close()


def test_facade_rejects_non_project(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        RuntimeFacade(tmp_path)


# ---- event adapter + state store ---------------------------------------------


def test_event_adapter_converts_real_event_shapes():
    adapter = EventAdapter()
    item = adapter.convert(
        {
            "event": "task.completed",
            "timestamp": "2026-09-30T14:02:18Z",
            "task_id": "TASK-042",
            "agent": "coder",
            "summary": "2/2 checks passed",
        }
    )
    assert item is not None
    assert item.kind == "success"
    assert item.time() == "14:02:18"
    assert item.actor == "coder"
    assert item.matches("TASK-042") and item.matches("checks")


def test_event_adapter_deduplicates_and_tolerates_missing_fields():
    adapter = EventAdapter()
    event = {"event": "run.heartbeat", "timestamp": "2026-09-30T14:00:00Z"}
    assert adapter.convert(event) is not None
    assert adapter.convert(dict(event)) is None  # duplicate
    assert adapter.convert({}) is None  # no event name
    # out-of-order late arrival is still ingested (spec §61)
    late = adapter.convert({"event": "task.failed", "timestamp": "2026-09-30T13:00:00Z"})
    assert late is not None and late.kind == "error"


async def test_state_store_polls_incrementally(ran_project: Path):
    facade = RuntimeFacade(ran_project)
    try:
        state = UIState(facade=facade)
        state.load_recent(100)
        initial = len(state.activity)
        assert initial > 0
        # A no-op poll adds nothing (re-read same-second events are deduped).
        assert state.poll_events() == []
        # Filtering narrows the visible feed.
        state.filter_text = "verification"
        assert len(state.visible_activity()) <= initial
    finally:
        facade.close()


# ---- the Textual app (official harness) --------------------------------------


async def test_tui_launches_and_shows_real_state(ran_project: Path):
    app = EngineTUI(ran_project, attached=False)
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        header = app.sub_title or ""
        assert "tasks" in header
        detail = str(app.query_one("#detail").content)
        assert "OVERVIEW" in detail
        # Side panel shows the real completed tasks.
        side = str(app.query_one("#side-panel").content)
        assert "TASK" in side


async def test_tui_slash_commands_and_views(ran_project: Path):
    app = EngineTUI(ran_project, attached=False)
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        input_bar = app.query_one("#input-bar")

        input_bar.focus()  # enter input mode
        input_bar.value = "/status"
        await pilot.press("enter")
        await pilot.pause()
        app.set_focus(None)
        await pilot.pause()
        assert any("tasks" in i.message for i in list(app.state.activity)[-3:])

        input_bar.value = "/tasks"
        input_bar.focus()  # '/'-refocus after the previous blur
        await pilot.press("enter")
        for _ in range(3):
            await pilot.pause()
        app.set_focus(None)
        for _ in range(2):
            await pilot.pause()
        task_lines = [i for i in app.state.activity if i.event == "ui" and "TASK-" in i.message]
        assert task_lines and any("COMPLETED" in i.message for i in task_lines)

        # view switching through keys
        await pilot.press("f")
        await pilot.pause()
        assert "FAILURES" in str(app.query_one("#detail").content)
        await pilot.press("v")
        await pilot.pause()
        assert "VERIFICATION" in str(app.query_one("#detail").content)
        await pilot.press("y")
        await pilot.pause()
        assert "MEMORY" in str(app.query_one("#detail").content)
        await pilot.press("c")
        await pilot.pause()
        assert "CHECKPOINTS" in str(app.query_one("#detail").content)
        await pilot.press("?")
        await pilot.pause()
        assert "HELP" in str(app.query_one("#detail").content)
        await pilot.press("escape")
        await pilot.pause()
        assert "OVERVIEW" in str(app.query_one("#detail").content)


async def test_tui_inspect_command_opens_task_detail(ran_project: Path):
    facade_probe = RuntimeFacade(ran_project)
    task_id = facade_probe.tasks()[0]["id"]
    facade_probe.close()

    app = EngineTUI(ran_project, attached=False)
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        input_bar = app.query_one("#input-bar")
        input_bar.value = f"/inspect {task_id}"
        input_bar.focus()
        await pilot.press("enter")
        await pilot.pause()
        app.set_focus(None)
        await pilot.pause()
        detail_text = str(app.query_one("#detail").content)
        assert task_id in detail_text
        assert "verification" in detail_text.lower()


async def test_tui_pause_and_resume_flow_real_control_channel(ran_project: Path):
    app = EngineTUI(ran_project, attached=False)
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        await pilot.press("p")
        await pilot.pause()
        assert app.facade.control.read().pause
        app.facade.control.clear()
        await pilot.press("p")
        await pilot.pause()
        assert app.facade.control.read().resume
        app.facade.control.clear()


async def test_tui_enhance_toggle_persists_to_config(ran_project: Path):
    app = EngineTUI(ran_project, attached=False)
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        before = app.facade.enhance_prompt_enabled()
        await pilot.press("e")
        await pilot.pause()
        assert app.facade.enhance_prompt_enabled() is (not before)
        # persisted to the real project config
        from autonomous_engine.core.workspace import Workspace

        assert Workspace(ran_project).load_config().enhance_prompt is (not before)


async def test_tui_small_terminal_renders(ran_project: Path):
    """80-col / 24-row terminals stay usable (spec §32)."""
    app = EngineTUI(ran_project, attached=False)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        assert app.query_one("#input-bar") is not None
        assert app.query_one("#activity") is not None


async def test_tui_objective_submission_starts_attached_run(ran_project: Path):
    """Submitting an objective launches the real runtime in the background.

    The TUI is a pure client now (directive #2): with no live daemon the
    explicit in-process fallback keeps this legacy contract working.
    """
    app = EngineTUI(ran_project, attached=True, process_fallback=True)
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        input_bar = app.query_one("#input-bar")
        input_bar.focus()  # enter input mode
        input_bar.value = "Build a tiny greeting module with a test"
        await pilot.press("enter")
        for _ in range(3):
            await pilot.pause()
        assert app.runner.running or app.runner.report() is not None
        # let the runtime work briefly, then cancel cleanly
        await pilot.pause(0.5)
        await app.runner.stop_and_wait()
        assert not app.runner.running


# ---- agent communication view (comms spec §57–§60, §88) ----------------------


async def test_facade_exposes_real_messages_after_a_run(ran_project: Path):
    """The communication view reads REAL persisted messages, not a transcript."""
    facade = RuntimeFacade(ran_project)
    try:
        messages = facade.messages(limit=50)
        assert messages, "a completed run must produce protocol messages"
        types = {m["type"] for m in messages}
        assert {"task.request", "task.completed", "verification.result"} <= types
        # the Director→Architect leg of the spec's conversation
        assert {"architecture.request", "architecture.result"} <= types
        for m in messages:
            assert m["sender"] and m["recipient"]
            assert m["state"] in (
                "COMPLETED", "QUEUED", "DELIVERED", "RECEIVED", "ACKNOWLEDGED",
                "PROCESSING", "FAILED", "EXPIRED", "CANCELLED", "DEAD",
            )
        detail = facade.message_detail(messages[0]["id"])
        assert detail is not None
        assert detail["conversation_id"].startswith("conv-")
        stats = facade.agent_comm_stats()
        names = {s["agent"] for s in stats}
        assert {"orchestrator", "coder"} <= names
        assert any(s["sent"] > 0 for s in stats)
    finally:
        facade.close()


async def test_tui_communication_view_shows_the_real_conversation(ran_project: Path):
    """§88: run the real flow; the TUI shows Director↔Architect↔Coder↔Tester."""
    app = EngineTUI(ran_project, attached=False)
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        await pilot.press("4")
        await pilot.pause()
        side = str(app.query_one("#side-panel").content)
        assert "AGENT COMMUNICATION" in side
        assert "→" in side
        assert "task.request" in side or "task.completed" in side

        # message detail: /inspect msg-... opens the envelope view
        facade = app.facade
        message_id = facade.messages(limit=5)[0]["id"]
        input_bar = app.query_one("#input-bar")
        input_bar.focus()
        input_bar.value = f"/inspect {message_id}"
        await pilot.press("enter")
        await pilot.pause()
        detail = str(app.query_one("#detail").content)
        assert "MESSAGE" in detail
        assert message_id in detail
        assert "correlation" in detail

        # per-agent traffic view (§59)
        await pilot.press("escape")
        await pilot.press("m")
        await pilot.pause()
        traffic = str(app.query_one("#detail").content)
        assert "AGENT TRAFFIC" in traffic
        assert "sent" in traffic


async def test_tui_live_feed_renders_message_events_live(ran_project: Path):
    """§60: message.* runtime events flow into the live activity feed."""
    app = EngineTUI(ran_project, attached=False)
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        events = [i.event for i in app.state.activity]
        assert any(e.startswith("message.") for e in events), (
            "communication transitions must appear in the live feed"
        )


# ---- plain mode --------------------------------------------------------------


def test_plain_line_formatter():
    line = _line(
        {
            "event": "task.completed",
            "timestamp": "2026-09-30T14:02:18Z",
            "task_id": "TASK-042",
            "summary": "2/2 checks passed",
        }
    )
    assert "[14:02:18]" in line
    assert "✓" in line
    assert "TASK-042" in line
    assert "2/2 checks passed" in line

    minimal = _line({"event": "agent.started", "timestamp": "2026-09-30T14:02:20Z", "agent": "coder"})
    assert "agent.started" in minimal
