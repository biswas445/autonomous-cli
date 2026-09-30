"""Provider-neutral model interfaces."""

from __future__ import annotations

import abc
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, Field


class ModelError(Exception):
    """Raised when a provider call fails after retries."""

    def __init__(
        self, message: str, *, provider: str = "", model: str = "", retriable: bool = False
    ):
        self.provider = provider
        self.model = model
        self.retriable = retriable
        super().__init__(message)


class Usage(BaseModel):
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0


class ToolCall(BaseModel):
    """One tool invocation requested by a model."""

    id: str = ""
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    raw_arguments: str = ""


class ChatMessage(BaseModel):
    """A conversation turn, provider-neutral.

    Roles: system | user | assistant | tool. Assistant turns may carry tool
    calls; tool turns carry the result of exactly one call (`tool_call_id`).
    Providers translate these into their own wire format.
    """

    role: str
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str = ""
    name: str = ""


class CompletionRequest(BaseModel):
    """One structured model call.

    `system` carries the agent role contract; `prompt` carries the
    reconstructed context. `schema_hint` names the expected JSON shape so
    providers can steer structured output. `tools` declares callable tools
    (provider-native schemas are derived by the caller from
    `runtime/tools.py`); `messages` carries a tool-loop conversation when the
    caller needs multi-turn exchanges.
    """

    system: str = ""
    prompt: str = ""
    schema_hint: str = ""
    max_tokens: int = 4096
    temperature: float = 0.2
    tools: list[dict[str, Any]] = Field(default_factory=list)
    messages: list[ChatMessage] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ModelResponse(BaseModel):
    text: str
    usage: Usage = Field(default_factory=Usage)
    provider: str = ""
    model: str = ""
    latency_ms: float = 0.0
    finish_reason: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)


# Async callable used by providers that need streaming or custom transport.
AsyncSender = Callable[[CompletionRequest], Awaitable[ModelResponse]]


class ProviderAdapter(abc.ABC):
    """A named model provider.

    Implementations must be safe to construct repeatedly and must raise
    ModelError (with retriable=True for transient failures) instead of
    leaking vendor exceptions.
    """

    name: str = "abstract"

    @abc.abstractmethod
    async def complete(self, request: CompletionRequest, model: str) -> ModelResponse: ...


class _Registry:
    def __init__(self) -> None:
        self._providers: dict[str, ProviderAdapter] = {}

    def register(self, adapter: ProviderAdapter, *, replace: bool = False) -> None:
        if adapter.name in self._providers and not replace:
            raise ValueError(f"provider already registered: {adapter.name}")
        self._providers[adapter.name] = adapter

    def get(self, name: str) -> ProviderAdapter:
        if name not in self._providers:
            raise ModelError(
                f"unknown provider: {name!r}. Registered: {sorted(self._providers)}",
                provider=name,
            )
        return self._providers[name]

    def names(self) -> list[str]:
        return sorted(self._providers)


_registry = _Registry()


def register_provider(adapter: ProviderAdapter, *, replace: bool = False) -> None:
    _registry.register(adapter, replace=replace)


def get_provider(name: str) -> ProviderAdapter:
    return _registry.get(name)
