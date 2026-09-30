"""Integration: tools, memory, and budget are wired through the runtime.

Verifies the full chain the reference CLIs taught us to build:
    agent sandbox -> permission-checked tools -> project memory -> next context
"""

from __future__ import annotations

import json
from pathlib import Path

from autonomous_engine.core.config import ModelRoute
from autonomous_engine.core.task import Task
from autonomous_engine.models.base import (
    ModelResponse,
    ProviderAdapter,
    ToolCall,
    register_provider,
)
from autonomous_engine.runtime.context import ContextBuilder
from autonomous_engine.runtime.context_setup import open_context
from autonomous_engine.runtime.memory import memory_store
from autonomous_engine.runtime.permissions import ToolBox
from autonomous_engine.runtime.tools import execute_tool


class ScriptedProvider(ProviderAdapter):
    """Replays a scripted model that writes a file via the tool, then answers."""

    name = "scripted_writer"

    def __init__(self):
        self.script = [
            ModelResponse(
                text="",
                tool_calls=[ToolCall(id="c1", name="write_file", arguments={"path": "src/notes.py", "content": "NOTES = []\n"})],
                provider="scripted_writer",
                model="m",
            ),
            ModelResponse(
                text=json.dumps({"summary": "created notes module via tool", "edits": [], "confidence": 0.9}),
                tool_calls=[],
                provider="scripted_writer",
                model="m",
            ),
        ]
        self.requests = []

    async def complete(self, request, model):
        self.requests.append(request)
        if not self.script:
            from autonomous_engine.models.base import ModelError

            raise ModelError("script exhausted", provider=self.name, model=model)
        return self.script.pop(0)


def _scripted_tools(tmp_path: Path, project: Path) -> ToolBox:
    from autonomous_engine.core.config import PermissionClass
    from autonomous_engine.runtime.budget import BudgetConfig, BudgetManager

    tools = ToolBox(
        work_root=tmp_path,
        permissions=PermissionClass(
            name="coder",
            read_repo=True,
            write_paths=["src/**"],
            run_commands=True,
            allowed_command_globs=["python*", "pytest*"],
        ),
    )
    tools.workspace = open_context(project).workspace
    tools.budget = BudgetManager(config=BudgetConfig())
    return tools


def test_agent_saved_memory_appears_in_next_context(project: Path):
    """The gemini/kilo loop: an agent saves a memory, the next task recalls it."""
    context = open_context(project)
    tools = _scripted_tools(project / "sandbox", project)
    tools.workspace = context.workspace

    # an agent saves a fact through its sandbox tool
    out = execute_tool(
        tools,
        "save_memory",
        {"text": "Invoice ids are prefixed with INV-", "kind": "fact", "tags": "billing"},
    )
    assert "saved memory" in out

    # a *different* task's context recalls it
    built = ContextBuilder(context.workspace).build(
        task=Task(id="TASK-X", title="add billing export"),
        role="coder",
        repo_root=project,
    )
    section = built.get("memory")
    assert "INV-" in section
    context.db.close()


def test_pinned_memory_loads_into_director_context(project: Path):
    """Bootstrap/director sessions (task=None) still get pinned memories."""
    context = open_context(project)
    memory_store(context.workspace).add(
        "preference", "Always ship with rollout flags", pinned=True, source="operator"
    )
    built = ContextBuilder(context.workspace).build(task=None, role="director", repo_root=project)
    assert "rollout flags" in built.get("memory")
    context.db.close()


async def test_tool_loop_writes_reach_task_artifacts(project, tmp_path):
    """A coder's write_file call through the tool loop lands in task artifacts."""
    from autonomous_engine.agents.coder import CoderAgent
    from autonomous_engine.runtime.base import AgentDeps

    provider = ScriptedProvider()
    register_provider(provider, replace=True)

    from autonomous_engine.models.router import ModelRouter

    config = type(open_context(project).config)()
    config.model_routes = [ModelRoute(role="coder", provider="scripted_writer", model="m")]
    router = ModelRouter(config, max_retries=0, retry_backoff_seconds=0.0)

    tools = _scripted_tools(tmp_path, project)
    (tmp_path / "src").mkdir(exist_ok=True)

    coder = CoderAgent(
        AgentDeps(router=router, tools=tools, workspace=open_context(project).workspace, store=None, git=None)
    )
    coder.bind_tools(tools)
    from autonomous_engine.runtime.context import AgentContext

    result = await coder.run(Task(id="TASK-TW", title="create notes module"), AgentContext(goal="g"))

    assert result.ok
    assert "src/notes.py" in result.artifacts  # tool write became an artifact
    assert (tmp_path / "src" / "notes.py").read_text(encoding="utf-8") == "NOTES = []\n"
