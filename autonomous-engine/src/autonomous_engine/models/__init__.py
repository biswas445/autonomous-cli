"""Model abstraction: providers, routing, structured completions.

The orchestrator never talks to a vendor SDK directly; it talks to
`ModelClient`. Providers are pluggable so OpenAI/Anthropic/local models can
be swapped without rewriting the orchestration core (plan.md §18, §49).
"""

from .base import (
    CompletionRequest,
    ModelError,
    ModelResponse,
    ProviderAdapter,
    Usage,
    get_provider,
    register_provider,
)
from .providers import AnthropicProvider, EchoProvider, OpenAICompatProvider
from .router import ModelRouter, extract_json

__all__ = [
    "CompletionRequest",
    "ModelError",
    "ModelResponse",
    "ProviderAdapter",
    "Usage",
    "register_provider",
    "get_provider",
    "ModelRouter",
    "extract_json",
    "EchoProvider",
    "OpenAICompatProvider",
    "AnthropicProvider",
]
