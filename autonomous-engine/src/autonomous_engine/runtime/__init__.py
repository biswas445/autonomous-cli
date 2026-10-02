"""Runtime layer: the deterministic engine the agents live inside.

Agents never touch the filesystem or shell directly. They call tools through
a ToolBox that enforces the agent's PermissionClass, and the orchestrator —
not the agent — owns the workflow (plan.md §67).

Heavy modules (orchestrator, agents) are imported lazily: `verification.engine`
imports `runtime.permissions`, and `agents.tester` imports
`verification.engine` — eagerly importing `orchestrator` here made
`import autonomous_engine.verification` a circular-import lottery that only
passed when the entry module happened to be imported first.
"""

from .base import Agent, AgentDeps, AgentResult, AgentRunner
from .budget import BudgetManager
from .constitution import build_constitution, ensure_constitution
from .context import AgentContext, ContextBuilder
from .context_setup import RuntimeContext, open_context
from .control import ControlChannel, ControlSignal
from .locks import LockManager, resource_keys
from .permissions import CommandResult, PermissionDenied, ToolBox
from .stop import STOP_DESCRIPTIONS, StopEngine, StopReason, StopSignal


def __getattr__(name: str):  # PEP 562 lazy re-exports
    if name in ("CycleReport", "Orchestrator", "RunResult", "NoObjective"):
        from . import orchestrator as _orchestrator

        return getattr(_orchestrator, name)
    if name in ("init_project", "project_summary", "set_objective"):
        from . import bootstrap as _bootstrap

        return getattr(_bootstrap, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "Agent",
    "AgentContext",
    "AgentDeps",
    "AgentResult",
    "AgentRunner",
    "BudgetManager",
    "CommandResult",
    "ContextBuilder",
    "ControlChannel",
    "ControlSignal",
    "CycleReport",
    "LockManager",
    "Orchestrator",
    "PermissionDenied",
    "RunResult",
    "RuntimeContext",
    "STOP_DESCRIPTIONS",
    "StopEngine",
    "StopReason",
    "StopSignal",
    "ToolBox",
    "build_constitution",
    "ensure_constitution",
    "init_project",
    "open_context",
    "project_summary",
    "resource_keys",
    "set_objective",
]
