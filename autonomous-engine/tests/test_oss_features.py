"""Tests for the capabilities ported from popular open-source coding CLIs:

* repo map            — aider's repomap, dependency-free (AST + regex + git recency)
* instructions memory — Claude Code / Gemini CLI hierarchy (global -> project)
* search/replace      — aider's editblock format with flexible matching
* dirty-repo pre-flight — aider's git discipline
"""

from __future__ import annotations

import json
from pathlib import Path

from autonomous_engine.agents.coder import CoderAgent, ProposedEdit
from autonomous_engine.core.config import PermissionClass
from autonomous_engine.core.task import Task
from autonomous_engine.runtime.context import ContextBuilder
from autonomous_engine.runtime.context_setup import open_context
from autonomous_engine.runtime.instructions import (
    ensure_project_instructions,
    instructions_context_section,
    load_instructions,
)
from autonomous_engine.runtime.orchestrator import Orchestrator
from autonomous_engine.runtime.permissions import ToolBox
from autonomous_engine.runtime.repo_map import RepoMap, keywords_for_task

# ---- repo map (aider) -------------------------------------------------------


def _sample_repo(root: Path) -> None:
    src = root / "src"
    src.mkdir(parents=True)
    (src / "auth.py").write_text(
        "import hashlib\n\n\nclass Authenticator:\n    def login(self, user):\n"
        "        return hashlib.sha256(user.encode()).hexdigest()\n",
        encoding="utf-8",
    )
    (src / "billing.py").write_text(
        "def charge_invoice(amount):\n    return amount\n\n\ndef refund(invoice_id):\n"
        "    return invoice_id\n",
        encoding="utf-8",
    )
    (root / "app.js").write_text(
        "export function renderDashboard() {}\nexport class Widget {}\n",
        encoding="utf-8",
    )


def test_repo_map_extracts_python_symbols_via_ast(tmp_path):
    _sample_repo(tmp_path)
    mapping = RepoMap(tmp_path).render()
    assert "src/auth.py" in mapping
    assert "Authenticator" in mapping
    assert "charge_invoice" in mapping
    assert "imports: hashlib" in mapping


def test_repo_map_extracts_js_symbols_via_regex(tmp_path):
    _sample_repo(tmp_path)
    mapping = RepoMap(tmp_path).render()
    assert "app.js" in mapping
    assert "renderDashboard" in mapping
    assert "Widget" in mapping


def test_repo_map_ranks_task_relevant_files_first(tmp_path):
    _sample_repo(tmp_path)
    keywords = keywords_for_task("implement refund for an invoice")
    ranked = RepoMap(tmp_path).ranked_files(keywords, include_recency=False)
    assert ranked[0].path == "src/billing.py"
    assert "refund" in ranked[0].defs


def test_repo_map_respects_budget(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    for index in range(30):
        (src / f"mod_{index:02d}.py").write_text(
            f"def func_{index}():\n    return {index}\n", encoding="utf-8"
        )
    mapping = RepoMap(tmp_path, max_chars=600, max_files=40).render()
    assert len(mapping) <= 700
    assert "truncated" in mapping


def test_repo_map_handles_gitless_directories(tmp_path):
    _sample_repo(tmp_path)  # tmp_path is not a git repo
    mapping = RepoMap(tmp_path).render()
    assert mapping  # recency simply contributes nothing


def test_context_includes_repo_map_section(project):
    context = open_context(project)
    _sample_repo(project)
    task = Task(id="TASK-M1", title="extend the Authenticator login")
    built = ContextBuilder(context.workspace).build(task=task, role="coder", repo_root=project)
    assert "REPOSITORY MAP" in built.get("repo_map")
    assert "Authenticator" in built.get("repo_map")
    context.db.close()


# ---- instructions memory (Claude Code / Gemini CLI) -------------------------


def test_instructions_template_created_once_and_skipped_when_untouched(project):
    from autonomous_engine.core.workspace import Workspace

    ws = Workspace(project)
    path = ensure_project_instructions(ws)
    assert path.is_file()
    untouched = path.read_text(encoding="utf-8")
    ensure_project_instructions(ws)
    assert path.read_text(encoding="utf-8") == untouched
    # untouched template carries no information
    assert instructions_context_section(ws) == ""


def test_instructions_hierarchy_global_then_project(project, tmp_path, monkeypatch):
    from autonomous_engine.core.workspace import Workspace

    global_file = tmp_path / "global-instructions.md"
    global_file.write_text("Always answer tersely.\n", encoding="utf-8")
    monkeypatch.setenv("AUTO_INSTRUCTIONS_FILE", str(global_file))

    ws = Workspace(project)
    ensure_project_instructions(ws)
    (project / ".agents" / "instructions.md").write_text("Use uv, never pip.\n", encoding="utf-8")
    scopes = load_instructions(ws)
    assert scopes["global"] == "Always answer tersely."
    assert scopes["project"] == "Use uv, never pip."

    section = instructions_context_section(ws)
    assert section.index("User-global") < section.index("Project instructions")
    assert section.index("tersely") < section.index("never pip")


def test_context_includes_instructions_section(project, tmp_path, monkeypatch):
    global_file = tmp_path / "gi.md"
    global_file.write_text("No network calls in tests.\n", encoding="utf-8")
    monkeypatch.setenv("AUTO_INSTRUCTIONS_FILE", str(global_file))
    context = open_context(project)
    (project / ".agents" / "instructions.md").write_text("Prefer dataclasses.\n")
    built = ContextBuilder(context.workspace).build(
        task=Task(id="T", title="x"), role="coder", repo_root=project
    )
    assert "AGENT INSTRUCTIONS" in built.get("instructions")
    assert "dataclasses" in built.get("instructions")
    context.db.close()


# ---- search/replace edits (aider editblock) ---------------------------------


def _coder(tmp_path: Path) -> CoderAgent:
    tools = ToolBox(
        work_root=tmp_path,
        permissions=PermissionClass(
            name="coder", read_repo=True, write_paths=["src/**"], run_commands=False
        ),
    )
    agent = CoderAgent.__new__(CoderAgent)
    agent._tools = tools
    return agent


def test_replace_edit_exact_match(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text(
        "VALUE = 1\n\n\ndef main():\n    return VALUE\n", encoding="utf-8"
    )
    coder = _coder(tmp_path)
    edit = ProposedEdit(
        path="src/app.py",
        action="replace",
        search="def main():\n    return VALUE",
        content="def main():\n    return VALUE + 1",
    )
    applied, rejected, errors = coder._apply([edit])
    assert applied and not rejected and not errors
    assert "return VALUE + 1" in (tmp_path / "src" / "app.py").read_text(encoding="utf-8")


def test_replace_edit_whitespace_flexible_match(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("def main():\n        return 41\n", encoding="utf-8")
    coder = _coder(tmp_path)
    edit = ProposedEdit(
        path="src/app.py",
        action="replace",
        search="def main():\n    return 41",
        content="def main():\n    return 42",
    )
    applied, rejected, errors = coder._apply([edit])
    assert applied and not rejected and not errors, (rejected, errors)
    body = (tmp_path / "src" / "app.py").read_text(encoding="utf-8")
    assert "return 42" in body
    assert "def main():" in body  # indentation preserved on the context lines


def test_replace_edit_no_match_is_rejected_honestly(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    coder = _coder(tmp_path)
    edit = ProposedEdit(
        path="src/app.py",
        action="replace",
        search="def missing_function():\n    pass",
        content="x",
    )
    applied, rejected, errors = coder._apply([edit])
    assert not applied
    assert any("not found" in r["reason"] for r in rejected)


def test_replace_edit_missing_file_rejected(tmp_path):
    coder = _coder(tmp_path)
    edit = ProposedEdit(path="src/ghost.py", action="replace", search="a", content="b")
    applied, rejected, errors = coder._apply([edit])
    assert not applied
    assert any("does not exist" in r["reason"] for r in rejected)


def test_replace_without_search_downgrades_to_write(tmp_path):
    coder = _coder(tmp_path)
    plan = coder._to_plan(
        {"edits": [{"path": "src/new.py", "action": "replace", "content": "x = 1"}]}
    )
    assert plan.edits[0].action == "write"


def test_replace_edit_respects_write_policy(tmp_path):
    (tmp_path / "blocked.md").write_text("hello\n", encoding="utf-8")
    coder = _coder(tmp_path)  # policy only allows src/**
    edit = ProposedEdit(path="blocked.md", action="replace", search="hello", content="goodbye")
    applied, rejected, errors = coder._apply([edit])
    assert not applied
    assert rejected and "permission denied" in rejected[0]["reason"]


# ---- dirty-repo pre-flight (aider git discipline) ---------------------------


async def test_preflight_commits_uncommitted_work(project):
    context = open_context(project)
    (project / "loose.txt").write_text("leftover from a crash", encoding="utf-8")
    orchestrator = Orchestrator(context, use_model_director=False)
    result = await orchestrator.run_loop("Build the preflight target")
    assert result.status == "completed"
    events = orchestrator.workspace.events.read_all()
    pre_runs = [e for e in events if e["event"] == "git.pre_run_commit"]
    assert pre_runs, "dirty repo at start must produce a pre-run commit"
    # the leftover file is inside the pre-run commit, not the task checkpoints
    commits = orchestrator.git.log(20)
    assert any("pre-run" in line for line in commits)
    context.db.close()


async def test_preflight_skips_clean_repo(project):
    """A fresh workspace is dirty (nothing committed yet) — run twice and the
    second run must find a clean tree and skip the pre-run commit."""
    context = open_context(project)
    first = Orchestrator(context, use_model_director=False)
    await first.run_loop("Build the clean target")
    pre_first = sum(
        1 for e in first.workspace.events.read_all() if e["event"] == "git.pre_run_commit"
    )
    second = Orchestrator(context, use_model_director=False)
    await second.run_loop()
    pre_second = sum(
        1 for e in second.workspace.events.read_all() if e["event"] == "git.pre_run_commit"
    )
    assert pre_second == pre_first, "clean repo must not trigger another pre-run commit"
    context.db.close()


# ---- CLI: remember / instructions -------------------------------------------


def test_cli_remember_and_instructions(project: Path):
    from typer.testing import CliRunner

    from autonomous_engine.cli.app import app

    runner = CliRunner()
    result = runner.invoke(
        app, ["remember", "PostgreSQL is the production database", "--path", str(project)]
    )
    assert result.exit_code == 0
    store = json.loads((project / ".agents" / "memory" / "memory.json").read_text("utf-8"))
    facts = [i for i in store["items"] if i["kind"] == "fact"]
    assert any("PostgreSQL" in f["text"] for f in facts)

    (project / ".agents" / "instructions.md").write_text("Keep modules small.\n")
    shown = runner.invoke(app, ["instructions", "--path", str(project)])
    assert shown.exit_code == 0
    assert "Keep modules small." in shown.output
    assert "user-global" in shown.output


async def test_live_activity_events_flow_from_model_and_tools(project, echo):
    """Directive #12: model calls and tool executions emit real activity
    events that observers (TUI/IPC) can watch — no simulated traffic."""
    from autonomous_engine.runtime.context_setup import open_context
    from autonomous_engine.runtime.orchestrator import Orchestrator

    context = open_context(project)
    orchestrator = Orchestrator(context, use_model_director=False)
    result = await orchestrator.run_loop("Build the observable target")
    assert result.status == "completed"

    events = [e["event"] for e in context.workspace.events.read_all()]
    assert "agent.started" in events
    assert "agent.finished" in events
    # the echo provider answers without tools, so model.response must exist
    assert "model.response" in events, "model output must be observable"
    response = [e for e in context.workspace.events.read_all() if e["event"] == "model.response"][0]
    assert response["chars"] > 0
    assert response["agent"]
    context.db.close()


async def test_tool_calls_emit_live_observability_events(project, echo):
    """Tool executions stream tool.call/tool.observation events live.

    A scripted router drives the real ToolLoop and the real sandbox: first it
    requests a `list_dir` tool call, then it answers — exactly the traffic a
    real model produces, observed through the event log.
    """
    from autonomous_engine.core.task import Task
    from autonomous_engine.models.base import ModelResponse, ToolCall, Usage
    from autonomous_engine.runtime.context import ContextBuilder
    from autonomous_engine.runtime.context_setup import open_context
    from autonomous_engine.runtime.orchestrator import Orchestrator

    class ScriptedRouter:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, request, role, **kwargs) -> ModelResponse:
            self.calls += 1
            if self.calls == 1:
                return ModelResponse(
                    text="",
                    usage=Usage(tokens_in=10, tokens_out=5, cost_usd=0.0),
                    latency_ms=1.0,
                    tool_calls=[
                        ToolCall(id="t1", name="list_dir", arguments={"path": "."})
                    ],
                )
            return ModelResponse(
                text='{"answer": "listed", "sources": []}',
                usage=Usage(tokens_in=20, tokens_out=10, cost_usd=0.0),
                latency_ms=2.0,
            )

    context = open_context(project)
    orchestrator = Orchestrator(context, use_model_director=False)
    scripted = ScriptedRouter()
    orchestrator.deps.router = scripted  # agents read the router via deps
    task = Task(id="TASK-OBS", title="look around", role="research")
    orchestrator.graph.add_task(task)
    researcher = orchestrator.agents["researcher"]
    researcher.bind_tools(
        orchestrator.tools_for("researcher", approved=False)
    )
    builder = ContextBuilder(context.workspace)
    builder.build(task=task, role="researcher", repo_root=orchestrator.repo_root)
    payload, usage = await researcher.ask_model_with_tools(
        system=researcher.SYSTEM,
        prompt='Inspect the repository: list files, then answer as {"answer": "...", "sources": []}',
        schema_hint="JSON with answer and sources",
        tool_names=("list_dir", "search"),
    )
    assert payload.get("answer") == "listed"
    assert usage["tool_loop"]["tool_calls"] == 1
    events = [e["event"] for e in context.workspace.events.read_all()]
    assert "model.call" in events
    assert "model.response" in events
    assert "tool.call" in events, "tool execution must be observable live"
    assert "tool.observation" in events
    observation = [e for e in context.workspace.events.read_all() if e["event"] == "tool.observation"][0]
    assert observation["ok"] is True
    context.db.close()


async def test_schema_hinted_prose_answer_gets_json_retry(project):
    """Live test: a schema_hinted agent whose final tool-loop answer is prose
    failed extract_json downstream, wasting the whole loop. The loop now
    retries once with a schema-forced prompt and adopts only valid JSON."""
    from autonomous_engine.models.base import ModelResponse, Usage
    from autonomous_engine.runtime.context_setup import open_context
    from autonomous_engine.runtime.orchestrator import Orchestrator
    from autonomous_engine.runtime.tool_loop import ToolLoop

    class ProseThenJsonRouter:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, request, role, **kwargs) -> ModelResponse:
            self.calls += 1
            # every call answers prose first (the model ignoring JSON),
            # except when the retry prompt arrives
            if "not valid JSON" in request.prompt:
                return ModelResponse(
                    text='{"edits": []}',
                    usage=Usage(tokens_in=50, tokens_out=10, cost_usd=0.0),
                    latency_ms=1.0,
                )
            return ModelResponse(
                text="I looked at the repository and it seems fine.",
                usage=Usage(tokens_in=50, tokens_out=10, cost_usd=0.0),
                latency_ms=1.0,
            )

    context = open_context(project)
    orchestrator = Orchestrator(context, use_model_director=False)
    router = ProseThenJsonRouter()
    loop = ToolLoop(router, max_iterations=2, on_event=orchestrator.emit)
    result = await loop.run(
        role="coder",
        system="system prompt",
        prompt="implement the thing",
        tools=orchestrator.orchestrator_tools,
        tool_names=(),
        schema_hint="JSON with edits[]",
    )
    assert router.calls == 2, "one schema-forced retry must happen"
    assert '"edits"' in result.text, "the valid JSON retry must be adopted"
    retry_events = [e for e in context.workspace.events.read_all() if e.get("retry")]
    assert retry_events, "the retry must be observable"
    context.db.close()
