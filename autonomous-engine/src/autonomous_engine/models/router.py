"""Model Router: role -> provider/model selection with fallbacks (§18, §19).

Selection layers, innermost wins:
    1. configured model_routes (explicit operator intent)
    2. capability registry eligibility + provider health (v2 §19–22, §115)
    3. static fallbacks

The registry feeds on real execution outcomes: a model that keeps failing
degrades until it succeeds again. Routing never invents a capability a
profile did not declare.
"""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic import BaseModel

from ..core.config import ProjectConfig
from .base import (
    CompletionRequest,
    ModelError,
    ModelResponse,
    get_provider,
    register_provider,
)
from .capabilities import REGISTRY, Health


class RouterDecision(BaseModel):
    provider: str
    model: str
    role: str
    reason: str = ""


class ModelRouter:
    """Routes completion requests to providers based on the project's
    configured model routes, with deterministic fallback and retry.

    Selection inputs (task type/complexity, historical success, cost) enter
    through `route_for()`; the basic router uses static configuration, and
    richer routing logic can be layered later without changing callers.
    """

    def __init__(
        self,
        config: ProjectConfig,
        *,
        max_retries: int = 2,
        retry_backoff_seconds: float = 1.5,
    ):
        self.config = config
        self.max_retries = max_retries
        self.retry_backoff_seconds = retry_backoff_seconds
        # per-role success/failure counters, updated by the orchestrator
        self.stats: dict[str, dict[str, int]] = {}

    def route_for(
        self, role: str, *, complexity: int = 5, security_sensitivity: str = "low"
    ) -> RouterDecision:
        route = self.config.route_for(role)
        reason_bits: list[str] = []
        # Escalation heuristics (§19): high complexity or high security
        # sensitivity prefers the first fallback when one is configured; a
        # primary with a historically poor success rate is demoted too (§59).
        stats = self.stats.get(role, {"ok": 0, "failed": 0})
        total = stats["ok"] + stats["failed"]
        poor_history = total >= 3 and stats["ok"] / total < 0.5 and bool(route.fallbacks)
        if poor_history:
            primary = route.fallbacks[0]
            provider, _, model = primary.partition("/")
            reason_bits.append(f"escalated to fallback (historical success {stats['ok']}/{total})")
        elif (complexity >= 8 or security_sensitivity == "high") and route.fallbacks:
            primary = route.fallbacks[0]
            provider, _, model = primary.partition("/")
            reason_bits.append("escalated to fallback (complexity/security)")
        elif REGISTRY.profile(route.provider, route.model) is not None:
            # A registered profile must still be eligible (v2 §21, §115): an
            # unavailable or persistently failing model yields to a healthy
            # fallback instead of poisoning every call this cycle.
            health = REGISTRY.health(route.provider, route.model)
            if health == Health.UNAVAILABLE and route.fallbacks:
                provider, _, model = route.fallbacks[0].partition("/")
                reason_bits.append(f"primary unavailable ({health.value}); using fallback")
            else:
                provider, model = route.provider, route.model
                if health != Health.HEALTHY:
                    reason_bits.append(f"primary health: {health.value}")
        else:
            provider, model = route.provider, route.model
            if route.fallbacks:
                reason_bits.append(f"fallbacks available: {route.fallbacks}")
        reason_bits.append(f"success stats: {stats}")
        return RouterDecision(
            provider=provider, model=model, role=role, reason="; ".join(reason_bits)
        )

    def record_outcome(self, role: str, ok: bool) -> None:
        stats = self.stats.setdefault(role, {"ok": 0, "failed": 0})
        stats["ok" if ok else "failed"] += 1

    async def complete(
        self,
        request: CompletionRequest,
        role: str,
        *,
        complexity: int = 5,
        security_sensitivity: str = "low",
    ) -> ModelResponse:
        """Execute a completion with retry + provider/model fallback chain."""
        decision = self.route_for(
            role, complexity=complexity, security_sensitivity=security_sensitivity
        )
        chain: list[tuple[str, str]] = [(decision.provider, decision.model)]
        for fb in self.config.route_for(role).fallbacks:
            provider, _, model = fb.partition("/")
            if (provider, model) not in chain:
                chain.append((provider, model))

        last_error: ModelError | None = None
        for provider_name, model in chain:
            for attempt in range(self.max_retries + 1):
                try:
                    provider = get_provider(provider_name)
                    response = await provider.complete(request, model)
                    self.record_outcome(role, ok=True)
                    REGISTRY.record_outcome(provider_name, model, ok=True)
                    return response
                except ModelError as exc:
                    last_error = exc
                    if not exc.retriable or attempt >= self.max_retries:
                        break
                    await asyncio.sleep(self.retry_backoff_seconds * (attempt + 1))
                except Exception as exc:  # vendor SDK leak -> wrap
                    last_error = ModelError(str(exc), provider=provider_name, model=model)
                    break
            # move to next fallback
        self.record_outcome(role, ok=False)
        if last_error is not None:
            REGISTRY.record_outcome(last_error.provider or decision.provider, model, ok=False)
        raise last_error or ModelError("all providers failed", role=role)

    async def complete_json(
        self,
        request: CompletionRequest,
        role: str,
        *,
        complexity: int = 5,
        security_sensitivity: str = "low",
    ) -> dict[str, Any]:
        """Completion expected to return a JSON object; tolerates code fences."""
        response = await self.complete(
            request, role, complexity=complexity, security_sensitivity=security_sensitivity
        )
        return extract_json(response.text)


def extract_json(text: str) -> dict[str, Any]:
    """Best-effort JSON extraction from a model response.

    Handles plain JSON, ```json fences, and a JSON object embedded in prose
    (before or after surrounding text).
    """
    import json
    import re

    text = text.strip()
    if not text:
        raise ModelError("empty model response; expected JSON")

    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
        raise ModelError(f"expected JSON object, got {type(obj).__name__}")
    except json.JSONDecodeError:
        pass

    # Scan for the first balanced { ... } block, honouring strings so braces
    # inside string values do not break the scan.
    depth = 0
    start: int | None = None
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                candidate = text[start : index + 1]
                try:
                    obj = json.loads(candidate)
                except json.JSONDecodeError:
                    start = None  # not JSON; keep scanning
                    continue
                if isinstance(obj, dict):
                    return obj
                start = None
    raise ModelError("model response is not valid JSON: no JSON object found")


def ensure_default_providers() -> None:
    """Register built-in providers once (idempotent)."""
    import contextlib

    from .providers import AnthropicProvider, EchoProvider, OpenAICompatProvider

    for adapter in (EchoProvider(), OpenAICompatProvider(), AnthropicProvider()):
        with contextlib.suppress(ValueError):  # already registered
            register_provider(adapter)


ensure_default_providers()
