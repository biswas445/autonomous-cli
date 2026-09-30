"""Agent-surface tools added from the reference CLI study.

Patterns adopted from the cloned coding CLIs (codex, gemini-cli, kilo/opencode):

- ``write_file`` / ``edit_file`` — every coding CLI gives the model first-class
  write tools behind the permission policy (kilo ``edit``, codex ``apply_patch``).
- ``current_time`` — codex's ``curr_time`` environment tool.
- ``budget_status`` — codex's ``get_context_remaining`` idea, applied to the
  run budget: agents can pace themselves instead of guessing.
- ``save_memory`` / ``recall_memory`` — gemini's ``save_memory`` and kilo's
  ``recall``, so agents participate in the project's typed memory instead of
  only the operator (``auto remember``) doing so.
- ``web_fetch`` — kilo/opencode webfetch, gated by the network policy and the
  SSRF endpoint validator from core.security.
"""

from __future__ import annotations

from pathlib import Path

from autonomous_engine.core.config import PermissionClass
from autonomous_engine.runtime.memory import memory_store
from autonomous_engine.runtime.permissions import ToolBox
from autonomous_engine.runtime.tools import (
    ENVIRONMENT_TOOLS,
    MEMORY_TOOLS,
    NETWORK_TOOLS,
    TOOLS,
    execute_tool,
)


def _toolbox(tmp_path: Path, *, write: bool = True, network: bool = False) -> ToolBox:
    return ToolBox(
        work_root=tmp_path,
        permissions=PermissionClass(
            name="coder",
            read_repo=True,
            write_paths=["src/**", "docs/**"] if write else [],
            run_commands=True,
            allowed_command_globs=["python*", "pytest*"],
            network=network,
        ),
    )


def _sample(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "src" / "app.py").write_text(
        "def greet(name):\n    return f'hi {name}'\n", encoding="utf-8"
    )


# ---- write/edit surface ------------------------------------------------------


def test_all_new_tools_registered():
    for name in (
        "write_file",
        "edit_file",
        "current_time",
        "budget_status",
        "save_memory",
        "recall_memory",
        "web_fetch",
        "web_search",
        "read_files",
    ):
        assert name in TOOLS, f"{name} missing from the tool registry"


def test_write_file_creates_and_tracks(tmp_path):
    tools = _toolbox(tmp_path)
    out = execute_tool(tools, "write_file", {"path": "src/new.py", "content": "x = 1\n"})
    assert "wrote" in out and "src/new.py" in out
    assert (tmp_path / "src" / "new.py").read_text(encoding="utf-8") == "x = 1\n"
    assert tools.written_paths == ["src/new.py"]  # tracked for task artifacts


def test_write_file_denied_outside_policy(tmp_path):
    tools = _toolbox(tmp_path)
    out = execute_tool(tools, "write_file", {"path": "elsewhere/x.py", "content": "x"})
    assert out.startswith("permission denied")


def test_edit_file_replaces_exact_and_flexible(tmp_path):
    _sample(tmp_path)
    tools = _toolbox(tmp_path)

    exact = execute_tool(
        tools,
        "edit_file",
        {"path": "src/app.py", "search": "return f'hi {name}'", "content": "return 'hi'"},
    )
    assert "edited" in exact
    assert "return 'hi'" in (tmp_path / "src" / "app.py").read_text(encoding="utf-8")

    # indentation-flexible search (aider perfect_or_whitespace behaviour)
    flexible = execute_tool(
        tools,
        "edit_file",
        {"path": "src/app.py", "search": "return 'hi'", "content": "return 'oh hi'"},
    )
    assert "edited" in flexible
    assert "return 'oh hi'" in (tmp_path / "src" / "app.py").read_text(encoding="utf-8")


def test_edit_file_miss_is_an_observation(tmp_path):
    _sample(tmp_path)
    tools = _toolbox(tmp_path)
    out = execute_tool(
        tools, "edit_file", {"path": "src/app.py", "search": "not in the file", "content": "x"}
    )
    assert "search block not found" in out  # model-visible, not a crash


def test_edit_file_requires_search(tmp_path):
    tools = _toolbox(tmp_path)
    out = execute_tool(tools, "edit_file", {"path": "src/app.py", "search": "", "content": "x"})
    assert "requires a non-empty" in out


# ---- environment tools -------------------------------------------------------


def test_current_time_returns_utc():
    out = execute_tool(None, "current_time", {})
    assert out.startswith("UTC now: ")
    assert "Z" in out


def test_budget_status_reports_snapshot(tmp_path):
    class _FakeBudget:
        def snapshot(self):
            return {"cost_usd": 1.5, "remaining_usd": 8.5}

    tools = _toolbox(tmp_path)
    tools.budget = _FakeBudget()
    out = execute_tool(tools, "budget_status", {})
    assert "cost_usd: 1.5" in out
    assert "remaining_usd: 8.5" in out


def test_budget_status_without_budget_is_graceful(tmp_path):
    out = execute_tool(_toolbox(tmp_path), "budget_status", {})
    assert "not available" in out


# ---- memory tools ------------------------------------------------------------


def test_save_and_recall_memory_round_trip(tmp_path, project: Path):
    from autonomous_engine.core.workspace import Workspace

    tools = _toolbox(tmp_path)
    tools.workspace = Workspace(project)

    saved = execute_tool(
        tools,
        "save_memory",
        {"text": "The billing API is rate limited to 60 rpm", "kind": "fact", "tags": "billing"},
    )
    assert "saved memory" in saved

    recalled = execute_tool(tools, "recall_memory", {"query": "billing rate limit"})
    assert "billing API" in recalled

    store = memory_store(Workspace(project))
    assert any("rate limited" in i.text for i in store.all())


def test_save_memory_rejects_secrets(tmp_path, project: Path):
    from autonomous_engine.core.workspace import Workspace

    tools = _toolbox(tmp_path)
    tools.workspace = Workspace(project)
    out = execute_tool(
        tools, "save_memory", {"text": "the api key is sk-abcdefghijklmnopqrstuvwx"}
    )
    assert out.startswith("refusing to store a potential secret")
    assert not memory_store(Workspace(project)).all()


def test_save_memory_without_workspace_is_graceful(tmp_path):
    out = execute_tool(_toolbox(tmp_path), "save_memory", {"text": "orphan fact"})
    assert "not available" in out


def test_web_search_requires_network(tmp_path):
    out = execute_tool(_toolbox(tmp_path, network=False), "web_search", {"query": "pytest docs"})
    assert out.startswith("network access is not permitted")


def test_web_search_blocks_and_handles_empty(tmp_path):
    tools = _toolbox(tmp_path, network=True)
    out = execute_tool(tools, "web_search", {"query": ""})
    assert "non-empty query" in out
    loopback = execute_tool(tools, "web_fetch", {"url": "http://127.0.0.1:9/x"})
    assert "blocked by network policy" in loopback


def test_read_files_batches_with_per_file_errors(tmp_path):
    _sample(tmp_path)
    tools = _toolbox(tmp_path)
    out = execute_tool(
        tools,
        "read_files",
        {"paths": ["src/app.py", "missing.py"], "max_chars_each": 500},
    )
    assert "## src/app.py" in out
    assert "def greet" in out
    assert "## missing.py" in out and "not found" in out


def test_read_files_requires_paths(tmp_path):
    out = execute_tool(_toolbox(tmp_path), "read_files", {"paths": []})
    assert "non-empty" in out


def test_builder_tools_include_memory_and_env():
    from autonomous_engine.runtime.tools import BUILDER_TOOLS

    assert "save_memory" in BUILDER_TOOLS and "recall_memory" in BUILDER_TOOLS
    assert "write_file" in BUILDER_TOOLS and "edit_file" in BUILDER_TOOLS
    assert "current_time" in BUILDER_TOOLS
    assert "web_search" not in BUILDER_TOOLS  # network stays researcher-only


def test_memory_tool_groups_exist():
    assert set(MEMORY_TOOLS) == {"save_memory", "recall_memory"}
    assert set(ENVIRONMENT_TOOLS) == {"current_time", "budget_status"}
    assert set(NETWORK_TOOLS) == {"web_fetch", "web_search"}


# ---- network tool ------------------------------------------------------------


def test_web_fetch_denied_without_network_permission(tmp_path):
    out = execute_tool(_toolbox(tmp_path, network=False), "web_fetch", {"url": "https://example.com"})
    assert out.startswith("network access is not permitted")


def test_web_fetch_blocks_loopback_even_with_permission(tmp_path):
    tools = _toolbox(tmp_path, network=True)
    out = execute_tool(tools, "web_fetch", {"url": "http://localhost:8080/admin"})
    assert "blocked by network policy" in out
    out2 = execute_tool(tools, "web_fetch", {"url": "http://169.254.169.254/latest/meta-data"})
    assert "blocked by network policy" in out2


# ---- sandbox wiring ----------------------------------------------------------


def test_toolbox_write_tracking_accumulates(tmp_path):
    tools = _toolbox(tmp_path)
    execute_tool(tools, "write_file", {"path": "src/a.py", "content": "a"})
    execute_tool(tools, "write_file", {"path": "src/a.py", "content": "a2"})
    execute_tool(tools, "write_file", {"path": "src/b.py", "content": "b"})
    assert tools.written_paths == ["src/a.py", "src/b.py"]  # deduplicated, ordered
