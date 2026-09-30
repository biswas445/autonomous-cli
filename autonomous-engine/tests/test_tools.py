"""Agent tools: registry, sandbox execution, wire formats, and the tool loop."""

from __future__ import annotations

import json
from pathlib import Path

from autonomous_engine.agents.coder import CoderAgent
from autonomous_engine.core.config import ModelRoute, PermissionClass
from autonomous_engine.core.task import Task
from autonomous_engine.models.base import (
    ChatMessage,
    CompletionRequest,
    ModelError,
    ModelResponse,
    ProviderAdapter,
    ToolCall,
    register_provider,
)
from autonomous_engine.models.providers import (
    _anthropic_message,
    _anthropic_tool_schema,
    _openai_message,
    _openai_tool_schema,
    _parse_anthropic_tool_uses,
    _parse_openai_tool_calls,
)
from autonomous_engine.models.router import ModelRouter
from autonomous_engine.runtime.base import AgentDeps
from autonomous_engine.runtime.permissions import ToolBox
from autonomous_engine.runtime.tool_loop import ToolLoop, neutral_tool_schemas
from autonomous_engine.runtime.tools import READ_ONLY_TOOLS, execute_tool


def _toolbox(tmp_path: Path, *, commands: bool = True, globs: list[str] | None = None) -> ToolBox:
    return ToolBox(
        work_root=tmp_path,
        permissions=PermissionClass(
            name="coder",
            read_repo=True,
            write_paths=["src/**"],
            run_commands=commands,
            allowed_command_globs=globs or ["python*", "pytest*"],
        ),
    )


def _sample(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "src" / "app.py").write_text(
        "def greet(name):\n    return f'hi {name}'\n\n\ndef main():\n    print(greet('x'))\n",
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text("# demo\nuses greet()\n", encoding="utf-8")


# ---- registry + sandbox execution -------------------------------------------


def test_read_file_returns_numbered_lines(tmp_path):
    _sample(tmp_path)
    tools = _toolbox(tmp_path)
    output = execute_tool(tools, "read_file", {"path": "src/app.py", "start_line": 1, "end_line": 2})
    assert "1| def greet(name):" in output
    assert "2|     return" in output
    assert "main" not in output


def test_list_dir_and_glob(tmp_path):
    _sample(tmp_path)
    tools = _toolbox(tmp_path)
    assert "src/" in execute_tool(tools, "list_dir", {"path": "."})
    found = execute_tool(tools, "glob", {"pattern": "**/*.py"})
    assert "src/app.py" in found
    assert "README.md" not in found


def test_search_regex_with_glob_filter(tmp_path):
    _sample(tmp_path)
    tools = _toolbox(tmp_path)
    hits = execute_tool(tools, "search", {"pattern": r"def greet", "glob": "*.py"})
    assert "src/app.py:1:" in hits
    assert "README.md" not in hits
    none = execute_tool(tools, "search", {"pattern": "zzz_not_there"})
    assert none == "no matches"


def test_run_command_reports_exit_code_and_output(tmp_path):
    tools = _toolbox(tmp_path)
    output = execute_tool(tools, "run_command", {"command": 'python -c "print(6*7)"'})
    assert "exit code: 0" in output
    assert "42" in output


def test_run_command_policy_denial_is_an_observation_not_a_crash(tmp_path):
    tools = _toolbox(tmp_path, commands=False)
    output = execute_tool(tools, "run_command", {"command": "pytest -q"})
    assert output.startswith("permission denied")


def test_path_traversal_is_denied_as_observation(tmp_path):
    tools = _toolbox(tmp_path)
    output = execute_tool(tools, "read_file", {"path": "../../etc/passwd"})
    assert output.startswith("permission denied")


def test_high_risk_command_denied_as_observation(tmp_path):
    tools = _toolbox(tmp_path, globs=["git*", "python*"])
    output = execute_tool(tools, "run_command", {"command": "git push --force origin main"})
    assert output.startswith("permission denied")
    assert "high-risk" in output


def test_unknown_tool_lists_available():
    output = execute_tool(None, "does_not_exist", {})
    assert "unknown tool" in output and "read_file" in output


def test_missing_file_is_an_observation(tmp_path):
    output = execute_tool(_toolbox(tmp_path), "read_file", {"path": "nope.py"})
    assert output.startswith("not found")


# ---- wire formats ------------------------------------------------------------


def test_neutral_schemas_convert_to_both_providers():
    neutral = neutral_tool_schemas(["read_file"])[0]
    openai = _openai_tool_schema(neutral)
    anthropic = _anthropic_tool_schema(neutral)
    assert openai["type"] == "function"
    assert openai["function"]["name"] == "read_file"
    assert "path" in openai["function"]["parameters"]["required"]
    assert anthropic["name"] == "read_file"
    assert "path" in anthropic["input_schema"]["required"]


def test_openai_message_translation_round_trip():
    assistant = ChatMessage(
        role="assistant",
        tool_calls=[ToolCall(id="c1", name="read_file", arguments={"path": "a.py"})],
    )
    wire = _openai_message(assistant)
    assert wire["tool_calls"][0]["function"]["name"] == "read_file"
    assert json.loads(wire["tool_calls"][0]["function"]["arguments"]) == {"path": "a.py"}
    tool_result = _openai_message(
        ChatMessage(role="tool", tool_call_id="c1", content="1| x = 1")
    )
    assert tool_result == {"role": "tool", "tool_call_id": "c1", "content": "1| x = 1"}


def test_anthropic_message_translation_round_trip():
    assistant = ChatMessage(
        role="assistant",
        content="thinking",
        tool_calls=[ToolCall(id="t1", name="search", arguments={"pattern": "x"})],
    )
    wire = _anthropic_message(assistant)
    assert wire["content"][0] == {"type": "text", "text": "thinking"}
    assert wire["content"][1]["type"] == "tool_use"
    tool_result = _anthropic_message(
        ChatMessage(role="tool", tool_call_id="t1", content="hit")
    )
    assert tool_result["content"][0]["type"] == "tool_result"
    assert tool_result["content"][0]["tool_use_id"] == "t1"


def test_provider_parsers_ignore_malformed_tool_calls():
    assert _parse_openai_tool_calls([{"function": {"name": ""}}]) == []
    assert _parse_openai_tool_calls([{"function": {"name": "f", "arguments": "not json"}}])[0].arguments == {}
    assert _parse_anthropic_tool_uses([{"type": "text", "text": "no tools"}]) == []


# ---- the loop ----------------------------------------------------------------


class ScriptedProvider(ProviderAdapter):
    """A provider that replays a fixed script of responses (offline tool tests)."""

    name = "scripted"

    def __init__(self, script: list[ModelResponse]):
        self.script = list(script)
        self.requests: list[CompletionRequest] = []

    async def complete(self, request: CompletionRequest, model: str) -> ModelResponse:
        self.requests.append(request)
        if not self.script:
            raise ModelError("script exhausted", provider=self.name, model=model)
        return self.script.pop(0)


def _router_for(provider: str) -> ModelRouter:
    from autonomous_engine.core.config import ProjectConfig

    config = ProjectConfig()
    config.model_routes = [ModelRoute(role="coder", provider=provider, model="m")]
    return ModelRouter(config, max_retries=0, retry_backoff_seconds=0.0)


def _response(text: str = "", tool_calls: list[ToolCall] | None = None) -> ModelResponse:
    from autonomous_engine.models.base import Usage

    return ModelResponse(
        text=text,
        tool_calls=tool_calls or [],
        usage=Usage(tokens_in=10, tokens_out=5, cost_usd=0.001),
        provider="scripted",
        model="m",
    )


async def test_loop_executes_tools_then_answers(tmp_path):
    _sample(tmp_path)
    provider = ScriptedProvider(
        [
            _response(
                tool_calls=[ToolCall(id="c1", name="read_file", arguments={"path": "src/app.py"})]
            ),
            _response(text='{"ok": true}'),
        ]
    )
    register_provider(provider, replace=True)
    loop = ToolLoop(_router_for("scripted"))
    result = await loop.run(
        role="coder",
        system="s",
        prompt="implement something",
        tools=_toolbox(tmp_path),
        tool_names=READ_ONLY_TOOLS,
    )
    assert result.text == '{"ok": true}'
    assert result.iterations == 2
    assert result.tool_calls == 1
    assert result.transcript[0]["tool"] == "read_file"
    assert result.transcript[0]["ok"] is True
    assert result.tokens_in == 20  # both calls accounted
    # the second request carried the assistant tool call and the observation
    roles = [m.role for m in provider.requests[1].messages]
    assert roles == ["assistant", "tool"]
    assert "1| def greet" in provider.requests[1].messages[1].content
    # and the first request actually declared the tools
    assert provider.requests[0].tools and provider.requests[0].tools[0]["name"] == "read_file"


async def test_loop_withdraws_tools_on_final_iteration(tmp_path):
    _sample(tmp_path)
    provider = ScriptedProvider(
        [
            _response(tool_calls=[ToolCall(id="c1", name="list_dir", arguments={})]),
            _response(tool_calls=[ToolCall(id="c2", name="list_dir", arguments={})]),
            _response(text='{"done": true}'),
        ]
    )
    register_provider(provider, replace=True)
    loop = ToolLoop(_router_for("scripted"), max_iterations=3)
    result = await loop.run(
        role="coder", system="s", prompt="p", tools=_toolbox(tmp_path), tool_names=READ_ONLY_TOOLS
    )
    assert result.stopped_reason == "max_iterations"
    assert result.text == '{"done": true}'
    # the final request had no tools, forcing an answer
    assert provider.requests[-1].tools == []


async def test_loop_denied_tool_becomes_model_visible(tmp_path):
    provider = ScriptedProvider(
        [
            _response(
                tool_calls=[
                    ToolCall(id="c1", name="read_file", arguments={"path": "../../secret"})
                ]
            ),
            _response(text='{"adapted": true}'),
        ]
    )
    register_provider(provider, replace=True)
    loop = ToolLoop(_router_for("scripted"))
    result = await loop.run(
        role="coder", system="s", prompt="p", tools=_toolbox(tmp_path), tool_names=READ_ONLY_TOOLS
    )
    assert result.transcript[0]["ok"] is False
    assert "permission denied" in provider.requests[1].messages[1].content


async def test_loop_reports_provider_errors():
    provider = ScriptedProvider([])
    register_provider(provider, replace=True)
    loop = ToolLoop(_router_for("scripted"))
    result = await loop.run(
        role="coder", system="s", prompt="p", tools=None, tool_names=READ_ONLY_TOOLS
    )
    assert result.stopped_reason == "error"


# ---- agent integration --------------------------------------------------------


def _coder_deps(tmp_path: Path) -> AgentDeps:
    return AgentDeps(router=None, tools=_toolbox(tmp_path), workspace=None, store=None, git=None)


async def test_coder_explores_with_tools_then_edits(tmp_path):
    """The full agentic path: read a file, then produce an EditPlan."""
    _sample(tmp_path)
    provider = ScriptedProvider(
        [
            _response(
                tool_calls=[ToolCall(id="c1", name="read_file", arguments={"path": "src/app.py"})]
            ),
            _response(
                text=json.dumps(
                    {
                        "summary": "add a docstring",
                        "edits": [
                            {
                                "path": "src/app.py",
                                "action": "replace",
                                "search": "def greet(name):",
                                "content": 'def greet(name):\n    """Say hi."""',
                            }
                        ],
                        "confidence": 0.9,
                    }
                )
            ),
        ]
    )
    register_provider(provider, replace=True)
    coder = CoderAgent(_coder_deps(tmp_path))
    coder.deps.router = _router_for("scripted")
    coder.bind_tools(_toolbox(tmp_path))
    from autonomous_engine.runtime.context import AgentContext

    result = await coder.run(Task(id="TASK-T1", title="docstring"), AgentContext(goal="g"))
    assert result.ok
    assert result.evidence["tool_loop"]["tool_calls"] == 1
    assert "add a docstring" in result.output["summary"]
    body = (tmp_path / "src" / "app.py").read_text(encoding="utf-8")
    assert '"""Say hi."""' in body


async def test_coder_single_shot_when_tools_disabled(tmp_path, project):
    """tools_enabled=false must take the plain ask_model path."""
    from autonomous_engine.core.workspace import Workspace

    ws = Workspace(project)
    config = ws.load_config()
    config.tools_enabled = False
    ws.save_config(config)

    _sample(tmp_path)
    provider = ScriptedProvider(
        [
            _response(
                text=json.dumps(
                    {"summary": "no tools", "edits": [], "confidence": 0.5}
                )
            )
        ]
    )
    register_provider(provider, replace=True)
    coder = CoderAgent(
        AgentDeps(router=_router_for("scripted"), tools=_toolbox(tmp_path), workspace=ws, store=None, git=None)
    )
    coder.bind_tools(_toolbox(tmp_path))
    from autonomous_engine.runtime.context import AgentContext

    await coder.run(Task(id="TASK-T2", title="x"), AgentContext(goal="g"))
    assert provider.requests[0].tools == []  # never offered tools
