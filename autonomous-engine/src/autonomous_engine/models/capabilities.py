"""Model capability registry and provider health (plan v2 §19–22, §115).

The router must choose models by *advertised capability*, not by name
recognition. A model profile declares what it can do (coding, planning,
tool use, large context, ...), its context window, cost class, and data-use
posture. Provider health is tracked at runtime so a technically capable but
currently rate-limited provider is not preferred over a healthy fallback.

This module is deterministic infrastructure: it records, scores, and ranks.
It never guesses a capability a profile did not declare.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Capability(StrEnum):
    """What a model advertises it can do (§20)."""

    CODING = "coding"
    PLANNING = "planning"
    REASONING = "reasoning"
    TOOL_USE = "tool_use"
    LARGE_CONTEXT = "large_context"
    VISION = "vision"
    BROWSER = "browser"
    STRUCTURED_OUTPUT = "structured_output"
    FAST = "fast"
    LONG_HORIZON = "long_horizon"


class Health(StrEnum):
    """Runtime provider health (§115): capable but unusable is still unusable."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    RATE_LIMITED = "rate_limited"
    UNAVAILABLE = "unavailable"
    MISBEHAVING = "misbehaving"


# Health contributes a multiplicative penalty to selection score; a
# rate-limited primary loses to a healthy fallback.
_HEALTH_PENALTY: dict[Health, float] = {
    Health.HEALTHY: 1.0,
    Health.DEGRADED: 0.6,
    Health.RATE_LIMITED: 0.4,
    Health.UNAVAILABLE: 0.0,
    Health.MISBEHAVING: 0.2,
}

# Roles declare the capabilities they *require*. A candidate lacking a
# required capability is not eligible regardless of its other strengths.
ROLE_REQUIREMENTS: dict[str, tuple[Capability, ...]] = {
    "intent_compiler": (Capability.STRUCTURED_OUTPUT,),
    "planner": (Capability.PLANNING, Capability.STRUCTURED_OUTPUT),
    "architect": (Capability.PLANNING, Capability.LARGE_CONTEXT),
    "coder": (Capability.CODING, Capability.TOOL_USE),
    "tester": (Capability.TOOL_USE,),
    "debugger": (Capability.REASONING, Capability.TOOL_USE),
    "reviewer": (Capability.REASONING,),
    "security": (Capability.REASONING,),
    "director": (Capability.PLANNING, Capability.REASONING),
    "researcher": (Capability.BROWSER,),
    "qa": (Capability.REASONING,),
    "release": (Capability.STRUCTURED_OUTPUT,),
    "product": (Capability.STRUCTURED_OUTPUT,),
}


@dataclass
class ModelProfile:
    """One model's advertised abilities and constraints (§19)."""

    provider: str
    model: str
    capabilities: set[Capability] = field(default_factory=set)
    context_limit: int = 128_000
    max_output: int = 8_192
    supports_tools: bool = True
    supports_streaming: bool = False
    cost_class: str = "standard"  # free | low | standard | premium
    data_use: str = "standard"  # standard | no_training | private_only
    availability: bool = True

    def id(self) -> str:
        return f"{self.provider}/{self.model}"

    def has_all(self, required: set[Capability]) -> bool:
        return required <= self.capabilities

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id(),
            "capabilities": sorted(c.value for c in self.capabilities),
            "context_limit": self.context_limit,
            "max_output": self.max_output,
            "supports_tools": self.supports_tools,
            "supports_streaming": self.supports_streaming,
            "cost_class": self.cost_class,
            "data_use": self.data_use,
            "availability": self.availability,
        }


class CapabilityRegistry:
    """The runtime's knowledge of its model workforce (§19–22, §115)."""

    def __init__(self) -> None:
        self._profiles: dict[str, ModelProfile] = {}
        self._health: dict[str, Health] = {}
        # Rolling behavioural record per model id (§116): recent outcomes.
        self._recent: dict[str, list[bool]] = {}

    # ---- registration ----

    def register(self, profile: ModelProfile) -> None:
        self._profiles[profile.id()] = profile
        self._health.setdefault(profile.id(), Health.HEALTHY)

    def profile(self, provider: str, model: str) -> ModelProfile | None:
        return self._profiles.get(f"{provider}/{model}")

    def profiles(self) -> list[ModelProfile]:
        return list(self._profiles.values())

    # ---- health (§115) ----

    def set_health(self, provider: str, model: str, health: Health) -> None:
        model_id = f"{provider}/{model}"
        if model_id in self._profiles:
            self._health[model_id] = health

    def health(self, provider: str, model: str) -> Health:
        return self._health.get(f"{provider}/{model}", Health.HEALTHY)

    def record_outcome(self, provider: str, model: str, ok: bool, *, window: int = 10) -> None:
        """Feed a real execution outcome into the behavioural record (§116)."""
        model_id = f"{provider}/{model}"
        history = self._recent.setdefault(model_id, [])
        history.append(ok)
        del history[:-window]
        # Persistently failing models degrade; recovery is automatic once they
        # succeed again (reputation is recent performance, not permanent truth).
        if len(history) >= 3:
            rate = sum(history) / len(history)
            if rate < 0.34 and self._health.get(model_id) == Health.HEALTHY:
                self._health[model_id] = Health.DEGRADED
            elif rate >= 0.7 and self._health.get(model_id) == Health.DEGRADED:
                self._health[model_id] = Health.HEALTHY

    # ---- selection (§21) ----

    def eligible(
        self, role: str, *, approx_context_chars: int = 0
    ) -> list[tuple[ModelProfile, float]]:
        """Rank eligible profiles for a role; best first.

        Eligibility: all role-required capabilities present, tools supported
        when the role's loop uses them, model available, health not
        UNAVAILABLE, and the context estimate fits the declared limit.
        Score: capability overlap + context headroom, times health penalty.
        """
        required = set(ROLE_REQUIREMENTS.get(role, ()))
        approx_tokens = approx_context_chars // 4
        ranked: list[tuple[ModelProfile, float]] = []
        for profile in self._profiles.values():
            if not profile.availability:
                continue
            if not profile.has_all(required):
                continue
            if profile.supports_tools is False:
                continue
            health = self.health(profile.provider, profile.model)
            if health == Health.UNAVAILABLE:
                continue
            if approx_tokens > profile.context_limit:
                continue
            # 2 points per extra matching capability beyond requirements.
            overlap = len(profile.capabilities & required)
            extra = len(profile.capabilities) - overlap
            score = overlap * 2.0 + min(extra, 4) * 0.5
            # Context headroom helps for large-context roles.
            if approx_tokens:
                headroom = profile.context_limit / max(1, approx_tokens)
                score += min(headroom, 4.0)
            score *= _HEALTH_PENALTY[health]
            ranked.append((profile, score))
        ranked.sort(key=lambda pair: (-pair[1], pair[0].id()))
        return ranked

    def best(self, role: str, *, approx_context_chars: int = 0) -> ModelProfile | None:
        ranked = self.eligible(role, approx_context_chars=approx_context_chars)
        return ranked[0][0] if ranked else None

    def snapshot(self) -> dict[str, Any]:
        """Registry state for observability (§73)."""
        return {
            profile.id(): {
                **profile.as_dict(),
                "health": self.health(profile.provider, profile.model).value,
                "recent_outcomes": list(self._recent.get(profile.id(), [])),
            }
            for profile in self._profiles.values()
        }


# The global registry instance used by the router.
REGISTRY = CapabilityRegistry()


def register_default_profiles() -> None:
    """Register conservative profiles for the built-in providers.

    The echo provider is a test double: it advertises the smallest honest
    surface. Real deployments replace these with profiles matching the
    actual models configured in model_routes.
    """
    REGISTRY.register(
        ModelProfile(
            provider="echo",
            model="default",
            capabilities={Capability.STRUCTURED_OUTPUT},
            supports_tools=False,
            cost_class="free",
            data_use="private_only",
        )
    )


register_default_profiles()
