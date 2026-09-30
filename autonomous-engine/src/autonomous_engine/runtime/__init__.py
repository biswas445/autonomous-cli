"""Runtime layer: the deterministic engine the agents live inside.

Agents never touch the filesystem or shell directly. They call tools through
a ToolBox that enforces the agent's PermissionClass, and the orchestrator —
not the agent — owns the workflow (plan.md §67).
"""

from .base import Agent, AgentDeps, AgentResult, AgentRunner
from .bootstrap import init_project, project_summary, set_objective
from .budget import BudgetManager
from .constitution import build_constitution, ensure_constitution
from .context import AgentContext, ContextBuilder
from .context_setup import RuntimeContext, open_context
from .control import ControlChannel, ControlSignal
from .locks import LockManager, resource_keys
from .orchestrator import CycleReport, Orchestrator, RunResult
from .permissions import CommandResult, PermissionDenied, ToolBox
from .stop import STOP_DESCRIPTIONS, StopEngine, StopReason, StopSignal

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
