"""Continuous Objective Mode (#1), runtime server/IPC (#2), and verification
project gates (#5): behavioural tests over the real persisted state."""

from __future__ import annotations

from pathlib import Path

from autonomous_engine.core.workspace import Workspace
from autonomous_engine.runtime.bootstrap import init_project
from autonomous_engine.runtime.maintenance import maintenance_round, scan_backlog
from autonomous_engine.verification.engine import VerificationReport
from autonomous_engine.verification.evidence import EvidenceRecord, EvidenceStore


def _project(tmp_path: Path) -> Workspace:
    root = tmp_path / "proj"
    init_project(root, project_name="proj", objective="Build the thing")
    return Workspace(root)


# ---- #1: continuous objective mode -------------------------------------------


def test_maintenance_scan_finds_todos(tmp_path: Path):
    ws = _project(tmp_path)
    module = ws.paths.root / "src" / "thing.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text("def f():\n    return 1  # TODO: validate inputs\n", encoding="utf-8")
    graph = ws.load_graph()
    findings = scan_backlog(ws, graph=graph)
    assert any(f.kind == "todo_cleanup" and "thing.py" in f.title for f in findings)


def test_maintenance_backlog_dedupes_across_rounds(tmp_path: Path):
    ws = _project(tmp_path)
    module = ws.paths.root / "src" / "m.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text("x = 1  # FIXME: broken edge case\n", encoding="utf-8")
    graph = ws.load_graph()
    first = maintenance_round(ws, graph)
    second = maintenance_round(ws, graph)
    assert first["added"] >= 1
    assert second["added"] == 0, "a still-open finding must not be re-tasked"


def test_maintenance_regression_generator_uses_evidence(tmp_path: Path):
    ws = _project(tmp_path)
    graph = ws.load_graph()
    from autonomous_engine.core.task import Task
    from autonomous_engine.git.manager import GitManager

    git = GitManager(ws.paths.root)
    git.ensure_repo()
    (ws.paths.root / "README.md").write_text("# repo\n", encoding="utf-8")
    git.stage_all()
    git.commit("initial")
    task = Task(id="TASK-EV1", title="verified work", definition_of_done=["file exists: README.md"])
    graph.add_task(task)
    ws.save_graph(graph)
    evidence = EvidenceStore(ws.paths.state)
    report = VerificationReport(task_id=task.id, passed=True)
    record, _ = EvidenceRecord.from_report(
        report, task_id=task.id, round_number=1, commit_sha="a" * 40
    )
    evidence.record(record)
    findings = scan_backlog(ws, graph=graph)
    # HEAD moved since the evidence commit: the regression generator fires
    assert any(f.kind == "regression_check" and "TASK-EV1" in f.subject for f in findings)


# ---- #2: runtime server over local IPC ---------------------------------------


async def test_runtime_server_serves_status_and_replays_events(tmp_path: Path):
    import threading
    import time

    from autonomous_engine.runtime import ipc
    from autonomous_engine.runtime.runtime_server import RuntimeClient, RuntimeServer

    ws = _project(tmp_path)
    server = RuntimeServer(ws.paths.root)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and ipc.read_endpoint(ws.paths.root) is None:
            time.sleep(0.05)
        assert ipc.read_endpoint(ws.paths.root), "server must publish its endpoint"
        client = RuntimeClient(ws.paths.root)
        assert client.connect(), "client must connect over local IPC"
        pong = client.call("hello")
        assert pong["ok"]
        status = client.status()
        assert isinstance(status, dict)
        replay = client.replay_events(limit=10)
        assert any(e["event"] == "project.initialized" for e in replay)
    finally:
        server.stop()
        thread.join(timeout=15)
    assert ipc.read_endpoint(ws.paths.root) is None, "endpoint must be cleaned up"


# ---- #5: project gates over real evidence ------------------------------------


async def test_project_gate_reports_incomplete_requirements(tmp_path: Path):
    from autonomous_engine.verification.project_gates import evaluate_project_gate

    ws = _project(tmp_path)
    graph = ws.load_graph()
    result = evaluate_project_gate(ws, graph)
    assert result.status in ("PASSED", "FAILED", "INSUFFICIENT_EVIDENCE", "BLOCKED")
    # a fresh project has tasks but no evidence: the gate must not say PASSED
    assert result.status != "PASSED", "no-evidence projects must not pass the gate"
