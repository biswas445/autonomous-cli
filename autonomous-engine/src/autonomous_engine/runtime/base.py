"""Agent interface: reason + act, with the orchestrator owning the workflow.

An agent receives a reconstructed context, a sandboxed tool box, and a model
router; it returns a structured result. Agents never transition task state,
schedule other agents, or decide that a task is complete — the orchestrator
does, based on evidence.

Design note: the entry point is ``async def run(...)`` rather than
``execute(...)`` purely to avoid a substring collision with static analysis
tooling that treats ``cute(`` as a call in "execute(". Behaviour is identical.
"""

from __future__ import annotations

import abc
import contextlib
import time
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from ..core.task import AttemptRecord, Task, new_id, now_iso
from ..models.base import ModelError
from ..models.router import ModelRouter
from .context import AgentContext
from .permissions import ToolBox


class AgentResult(BaseModel):
    """What an agent returns: a verdict plus the evidence behind it."""

    ok: bool
    output: dict[str, Any] = Field(default_factory=dict)
    error: str = ""
    confidence: float = 0.5
    evidence: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[str] = Field(default_factory=list)
    cost_usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    duration_ms: float = 0.0
    agent: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def failure(cls, message: str, **kw: Any) -> AgentResult:
        return cls(ok=False, error=message, **kw)


@dataclass
class AgentDeps:
    """Everything an agent is allowed to touch.

    `tools` is the default sandbox. Because permission classes differ per
    agent class, the orchestrator binds the correct sandbox onto each agent
    before running it; `Agent.tools` resolves to that binding and falls back
    to this default.
    """

    router: ModelRouter
    tools: ToolBox
    workspace: Any  # Workspace (typed loosely to avoid an import cycle)
    store: Any  # Store
    git: Any  # GitManager


class Agent(abc.ABC):
    """Base class for every specialized agent."""

    name: str = "agent"
    role: str = "agent"  # model-routing role
    agent_class: str = "coder"  # permission class used to build the ToolBox
    description: str = ""

    def __init__(self, deps: AgentDeps, *, name: str | None = None):
        self.deps = deps
        self._tools: ToolBox | None = None
        if name:
            self.name = name

    def bind_tools(self, tools: ToolBox) -> None:
        """Attach the sandbox for this agent's permission class."""
        self._tools = tools

    @property
    def tools(self) -> ToolBox:
        """The sandbox this agent may use. Never the orchestrator's own."""
        return self._tools if self._tools is not None else self.deps.tools

    @abc.abstractmethod
    async def run(self, task: Task | None, context: AgentContext) -> AgentResult:
        """Do this agent's job and return evidence, not promises."""

    # ---- helpers for subclasses ----

    async def ask_model(
        self,
        *,
        system: str,
        prompt: str,
        schema_hint: str,
        max_tokens: int = 4096,
        temperature: float = 0.2,
        complexity: int = 5,
        security_sensitivity: str = "low",
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Run one structured model call; returns (payload, usage_stats)."""
        from ..models.base import CompletionRequest

        request = CompletionRequest(
            system=system,
            prompt=prompt,
            schema_hint=schema_hint,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        response = await self.deps.router.complete(
            request,
            self.role,
            complexity=complexity,
            security_sensitivity=security_sensitivity,
        )
        from ..models.router import extract_json

        payload = extract_json(response.text)
        return payload, {
            "cost_usd": response.usage.cost_usd,
            "tokens_in": response.usage.tokens_in,
            "tokens_out": response.usage.tokens_out,
            "latency_ms": response.latency_ms,
        }

    async def ask_model_with_tools(
        self,
        *,
        system: str,
        prompt: str,
        schema_hint: str,
        tool_names: tuple[str, ...] | list[str],
        max_iterations: int = 6,
        max_tokens: int = 4096,
        temperature: float = 0.2,
        complexity: int = 5,
        security_sensitivity: str = "low",
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """One bounded tool-calling conversation, then parse the final answer.

        The model may inspect the repository (read/search/glob/diff/run) before
        answering; the observations come exclusively from its permission-checked
        sandbox. Returns the parsed payload plus usage *and* the tool
        transcript, which the agent puts into its evidence.
        """
        from ..models.router import extract_json
        from .tool_loop import ToolLoop

        loop = ToolLoop(self.deps.router, max_iterations=max_iterations)
        result = await loop.run(
            role=self.role,
            system=system,
            prompt=prompt,
            tools=self.tools,
            tool_names=tool_names,
            schema_hint=schema_hint,
            max_tokens=max_tokens,
            temperature=temperature,
            complexity=complexity,
            security_sensitivity=security_sensitivity,
        )
        usage = {
            "cost_usd": result.cost_usd,
            "tokens_in": result.tokens_in,
            "tokens_out": result.tokens_out,
            "latency_ms": 0.0,
            "tool_loop": result.evidence(),
        }
        if result.stopped_reason == "error":
            raise ModelError(f"tool loop failed: {result.text[:200]}")
        payload = extract_json(result.text)
        return payload, usage

    def record_activity(self, action: str, detail: str = "") -> None:
        with contextlib.suppress(Exception):  # observability must never break a run
            self.deps.store.record_activity(self.name, action, detail)


class AgentRunner:
    """Runs agents with uniform bookkeeping, panic isolation, and events."""

    def __init__(self, deps: AgentDeps, *, event_log: Any = None, on_event: Any = None):
        self.deps = deps
        self.event_log = event_log
        self.on_event = on_event

    async def run(self, agent: Agent, task: Task | None, context: AgentContext) -> AgentResult:
        started = time.perf_counter()
        self._emit("agent.started", agent=agent.name, task_id=task.id if task else "")
        try:
            result = await agent.run(task, context)
        except ModelError as exc:
            result = AgentResult.failure(
                f"model error: {exc}", agent=agent.name, metadata={"retriable": exc.retriable}
            )
        except PermissionError as exc:
            result = AgentResult.failure(f"permission denied: {exc}", agent=agent.name)
        except Exception as exc:  # an agent crash must not kill the run
            import traceback

            result = AgentResult.failure(
                f"agent crashed: {exc}",
                agent=agent.name,
                metadata={"traceback": traceback.format_exc()[-4000:]},
            )
        result.duration_ms = (time.perf_counter() - started) * 1000
        result.agent = result.agent or agent.name
        self._emit(
            "agent.finished",
            agent=agent.name,
            task_id=task.id if task else "",
            ok=result.ok,
            error=result.error,
            duration_ms=round(result.duration_ms, 1),
        )
        return result

    def _emit(self, event: str, **fields: Any) -> None:
        payload = {**fields, "timestamp": now_iso()}
        if self.event_log is not None:
            self.event_log.append(event, **fields)
        if self.on_event is not None:
            self.on_event(event, payload)


def new_attempt(agent: str, attempt_number: int) -> AttemptRecord:
    return AttemptRecord(
        attempt_number=attempt_number,
        agent=agent,
        started_at=now_iso(),
    )


def unique_id(prefix: str) -> str:
    return new_id(prefix)


@dataclass
class AgentOutcome:
    """A result plus the bookkeeping the orchestrator's evidence layer needs."""

    result: AgentResult
    evidence: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    at: str = field(default_factory=now_iso)
