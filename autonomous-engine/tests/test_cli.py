"""CLI tests: every command is a thin, tested wrapper over the core."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from autonomous_engine.cli.app import app

runner = CliRunner()


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    result = runner.invoke(
        app, ["init", str(tmp_path / "cli-proj"), "--objective", "Build a CLI test target"]
    )
    assert result.exit_code == 0, result.output
    return tmp_path / "cli-proj"


def test_init_creates_workspace(project: Path):
    state = project / ".agents"
    assert (state / "config.json").is_file()
    assert (state / "project.json").is_file()
    assert (state / "constitution.md").is_file()
    assert (state / "planning" / "task_graph.json").is_file()
    assert (state / "requirements").is_dir()
    assert (state / "checkpoints").is_dir()
    assert (project / ".gitignore").is_file()


def test_init_is_idempotent(project: Path):
    result = runner.invoke(app, ["init", str(project)])
    assert result.exit_code == 0
    # constitution is written once, never silently rewritten
    before = (project / ".agents" / "constitution.md").read_text(encoding="utf-8")
    runner.invoke(app, ["init", str(project)])
    after = (project / ".agents" / "constitution.md").read_text(encoding="utf-8")
    assert before == after


def test_status_json(project: Path):
    result = runner.invoke(app, ["status", "--path", str(project), "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["objective"] == "Build a CLI test target"
    assert payload["progress"]["total"] == 0


def test_tasks_empty(project: Path):
    result = runner.invoke(app, ["tasks", "--path", str(project)])
    assert result.exit_code == 0
    assert "no tasks yet" in result.output


def test_config_set_and_show(project: Path):
    result = runner.invoke(
        app, ["config", "--path", str(project), "--set", "budget.max_token_budget=500"]
    )
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["budget"]["max_token_budget"] == 500.0
    # the change persisted
    again = runner.invoke(app, ["config", "--path", str(project)])
    assert json.loads(again.output)["budget"]["max_token_budget"] == 500.0


def test_config_rejects_unknown_key(project: Path):
    result = runner.invoke(app, ["config", "--path", str(project), "--set", "not.a.key=1"])
    assert result.exit_code == 1
    assert "unknown configuration key" in result.output


def test_constitution_command(project: Path):
    result = runner.invoke(app, ["constitution", "--path", str(project)])
    assert result.exit_code == 0
    assert "Project Constitution" in result.output


def test_offline_run_and_inspect(project: Path):
    """`auto run --provider echo --no-director` completes; inspect shows evidence."""
    result = runner.invoke(
        app,
        [
            "run",
            "--path",
            str(project),
            "--provider",
            "echo",
            "--no-director",
            "--max-cycles",
            "25",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "PROJECT_COMPLETE" in result.output

    tasks = runner.invoke(app, ["tasks", "--path", str(project), "--all"])
    assert "COMPLETED" in tasks.output

    events = runner.invoke(app, ["events", "--path", str(project), "-n", "10"])
    assert events.exit_code == 0

    checkpoints = runner.invoke(app, ["checkpoints", "--path", str(project)])
    assert "checkpoint-" in checkpoints.output


def test_offline_run_positional_objective(project: Path):
    result = runner.invoke(
        app,
        [
            "run",
            "Build a widget forge",
            "--path",
            str(project),
            "--provider",
            "echo",
            "--no-director",
            "--max-cycles",
            "25",
        ],
    )
    assert result.exit_code == 0, result.output


def test_supervised_run_mode_persists(project: Path):
    """Supervised mode is persisted so a resumed run stays supervised.

    High-risk gating itself is covered by the orchestrator e2e tests; the
    offline fallback plan contains no high-risk tasks, so this run completes.
    """
    result = runner.invoke(
        app,
        [
            "run",
            "--path",
            str(project),
            "--provider",
            "echo",
            "--no-director",
            "--supervised",
            "--max-cycles",
            "25",
        ],
    )
    assert result.exit_code == 0, result.output
    config = json.loads((project / ".agents" / "config.json").read_text(encoding="utf-8"))
    assert config["run_mode"] == "supervised"


def test_pause_and_resume_control(project: Path):
    result = runner.invoke(app, ["pause", "--path", str(project), "--reason", "testing"])
    assert result.exit_code == 0
    control = project / ".agents" / "execution" / "control.json"
    assert control.is_file()
    payload = json.loads(control.read_text(encoding="utf-8"))
    assert payload["pause"] is True
    result = runner.invoke(app, ["resume", "--path", str(project)])
    assert result.exit_code == 0


def test_reset_clears_graph(project: Path):
    runner.invoke(
        app,
        [
            "run",
            "--path",
            str(project),
            "--provider",
            "echo",
            "--no-director",
            "--max-cycles",
            "25",
        ],
    )
    result = runner.invoke(app, ["reset", "--path", str(project)])
    assert result.exit_code == 0
    graph = json.loads(
        (project / ".agents" / "planning" / "task_graph.json").read_text(encoding="utf-8")
    )
    assert graph["tasks"] == {}


def test_help_lists_plan_commands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in (
        "init",
        "run",
        "pause",
        "resume",
        "approve",
        "reject",
        "status",
        "inspect",
        "events",
        "checkpoints",
        "rollback",
    ):
        assert command in result.output


def test_run_without_objective_fails_fast(tmp_path: Path):
    """Planning without an objective would produce a meaningless project."""
    project = tmp_path / "no-objective"
    runner.invoke(app, ["init", str(project)])
    result = runner.invoke(
        app,
        ["run", "--path", str(project), "--provider", "echo", "--no-director"],
    )
    assert result.exit_code == 1
    assert "no objective recorded" in result.output
    # ... and providing one positionally works
    result = runner.invoke(
        app,
        [
            "run",
            "Build something real",
            "--path",
            str(project),
            "--provider",
            "echo",
            "--no-director",
            "--max-cycles",
            "25",
        ],
    )
    assert result.exit_code == 0, result.output


def test_config_rejects_invalid_run_mode(project: Path):
    result = runner.invoke(app, ["config", "--path", str(project), "--set", "run_mode=bogus"])
    assert result.exit_code == 1
    assert "must be" in result.output and "supervised" in result.output
