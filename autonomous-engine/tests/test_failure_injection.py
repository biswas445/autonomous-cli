"""Failure injection: the runtime survives a hostile/broken model (v2 §102).

A long-running runtime that only works when the model behaves is not
complete. These tests inject malformed output, provider crashes, and
rate-limit storms into a real orchestration run and verify:

  * no state corruption (graph stays loadable, no false completion);
  * agents fall back deterministically instead of crashing;
  * budget/stop accounting stays truthful.
"""

from __future__ import annotations

import json

from autonomous_engine.models.base import (
    ModelError,
    ModelResponse,
    ProviderAdapter,
    Usage,
    get_provider,
    register_provider,
)
from autonomous_engine.runtime.context_setup import open_context
from autonomous_engine.runtime.orchestrator import Orchestrator


class ChaosProvider(ProviderAdapter):
    """A provider that misbehaves in configurable ways before delegating.

    Behaviours are consumed in order, then remaining calls pass through to a
    delegate (the echo provider). This models real failures: garbage output,
    HTTP 429 storms, and hard crashes.
    """

    name = "chaos"

    def __init__(self, behaviours: list[str]):
        self.behaviours = list(behaviours)
        self.delegate = get_provider("echo")
        self.calls = 0

    async def complete(self, request, model):
        self.calls += 1
        if self.behaviours:
            behaviour = self.behaviours.pop(0)
            if behaviour == "garbage":
                return ModelResponse(
                    text="this is not json at all {{{",
                    usage=Usage(tokens_in=10, tokens_out=10),
                    provider=self.name,
                    model=model,
                )
            if behaviour == "not_an_object":
                return ModelResponse(
                    text=json.dumps(["a", "list", "not", "an", "object"]),
                    usage=Usage(tokens_in=10, tokens_out=10),
                    provider=self.name,
                    model=model,
                )
            if behaviour == "rate_limited":
                raise ModelError(
                    "provider returned 429", provider=self.name, model=model, retriable=True
                )
            if behaviour == "hard_crash":
                raise ModelError("segmentation fault in provider", provider=self.name, model=model)
        return await self.delegate.complete(request, model)


async def test_garbage_model_output_falls_back_deterministically(project, echo):
    """Malformed JSON from the intent compiler must not kill the bootstrap."""
    chaos = ChaosProvider(["garbage", "garbage"])
    register_provider(chaos, replace=True)

    context = open_context(project)
    context.config.model_routes = [
        __import__(
            "autonomous_engine.core.config", fromlist=["ModelRoute"]
        ).ModelRoute(role=r, provider="chaos", model="m")
        for r in (
            "intent_compiler",
            "product",
            "researcher",
            "architect",
            "planner",
            "coder",
            "tester",
            "debugger",
            "reviewer",
            "security",
            "director",
            "release",
            "qa",
        )
    ]
    from autonomous_engine.models.router import ModelRouter

    orchestrator = Orchestrator(
        context, router=ModelRouter(context.config, max_retries=0, retry_backoff_seconds=0.0),
        use_model_director=False,
    )
    result = await orchestrator.run_loop("Build the chaos target")

    # The run still finishes: deterministic fallbacks replaced the garbage.
    assert result.status == "completed"
    # ...and the fallback plan is real work, not an empty graph.
    assert len(orchestrator.graph.tasks) >= 3
    assert not orchestrator.workspace.pending_escalations()
    context.db.close()


async def test_rate_limit_storm_degrades_without_false_completion(project, echo):
    """A 429 storm on every call must produce an honest stop, not fake success."""
    chaos = ChaosProvider(["rate_limited"] * 5 + ["garbage"] * 20)
    register_provider(chaos, replace=True)

    context = open_context(project)
    context.config.model_routes = [
        __import__(
            "autonomous_engine.core.config", fromlist=["ModelRoute"]
        ).ModelRoute(role=r, provider="chaos", model="m")
        for r in (
            "intent_compiler",
            "product",
            "researcher",
            "architect",
            "planner",
            "coder",
            "tester",
            "debugger",
            "reviewer",
            "security",
            "director",
            "release",
            "qa",
        )
    ]
    from autonomous_engine.models.router import ModelRouter

    orchestrator = Orchestrator(
        context, router=ModelRouter(context.config, max_retries=0, retry_backoff_seconds=0.0),
        use_model_director=False,
    )
    result = await orchestrator.run_loop("Build the storm target")

    # The run must NOT claim PROJECT_COMPLETE with no verified work: it stops
    # honestly (human escalation, budget, no-progress) or completes only via
    # real fallback evidence. The graph must never claim completion it lacks.
    completed = orchestrator.graph.completed_tasks()
    for task in completed:
        assert task.verification.get("passed"), f"false completion on {task.id}"
    assert result.status in (
        "completed",
        "budget_exceeded",
        "no_progress",
        "repeated_failure",
        "human_approval_required",
        "unrecoverable_failure",
    )
    if result.status == "completed":
        assert completed, "a chaos run may only complete with real verified tasks"
    context.db.close()


async def test_state_stays_loadable_after_chaos(project, echo):
    """After garbage output, a fresh orchestrator loads a consistent graph."""
    chaos = ChaosProvider(["garbage", "not_an_object", "hard_crash"])
    register_provider(chaos, replace=True)

    context = open_context(project)
    from autonomous_engine.core.config import ModelRoute
    from autonomous_engine.models.router import ModelRouter

    context.config.model_routes = [
        ModelRoute(role=r, provider="chaos", model="m")
        for r in (
            "intent_compiler",
            "product",
            "researcher",
            "architect",
            "planner",
            "coder",
            "tester",
            "debugger",
            "reviewer",
            "security",
            "director",
            "release",
            "qa",
        )
    ]
    orchestrator = Orchestrator(
        context, router=ModelRouter(context.config, max_retries=0, retry_backoff_seconds=0.0),
        use_model_director=False,
    )
    await orchestrator.run_loop("Build the durable target")

    # Restart: the persisted graph must load and be structurally sound.
    fresh = Orchestrator(context, use_model_director=False)
    fresh._load_state()
    assert fresh.graph.detect_cycles() == []
    for task in fresh.graph.all():
        assert task.status in {s.value for s in (
            __import__("autonomous_engine.core.state_machine", fromlist=["TaskState"]).TaskState
        )}
    context.db.close()
