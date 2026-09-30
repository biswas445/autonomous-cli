"""Adaptive-intelligence behaviours: reputation routing, heartbeat, drift."""

from __future__ import annotations

from autonomous_engine.agents.director import DirectorProposal
from autonomous_engine.core.config import ModelRoute, ProjectConfig
from autonomous_engine.models.base import CompletionRequest, ModelResponse, register_provider
from autonomous_engine.models.router import ModelRouter
from autonomous_engine.runtime.context_setup import open_context
from autonomous_engine.runtime.orchestrator import Orchestrator


def _request() -> CompletionRequest:
    return CompletionRequest(system="s", prompt="p", schema_hint="x")


class _StubProvider:
    def __init__(self, name: str):
        self.name = name

    async def complete(self, request, model):
        return ModelResponse(text="{}", provider=self.name, model=model)


def test_router_demotes_primary_with_poor_history():
    """Agent reputation (§59): a primary failing most calls is demoted."""
    register_provider(_StubProvider("rep-a"), replace=True)
    register_provider(_StubProvider("rep-b"), replace=True)
    config = ProjectConfig()
    config.model_routes = [
        ModelRoute(role="coder", provider="rep-a", model="a", fallbacks=["rep-b/b"])
    ]
    router = ModelRouter(config)
    for _ in range(3):
        router.record_outcome("coder", ok=False)
    router.record_outcome("coder", ok=True)
    decision = router.route_for("coder")
    assert decision.provider == "rep-b"
    assert "historical success" in decision.reason
    # a healthy history keeps the primary
    router2 = ModelRouter(config)
    router2.record_outcome("coder", ok=True)
    assert router2.route_for("coder").provider == "rep-a"


def test_goal_drift_detection_emits_warning(project):
    """A significant drop in goal-progress self-assessment is surfaced (§55)."""
    context = open_context(project)
    orchestrator = Orchestrator(context, use_model_director=False)
    orchestrator._track_goal_drift(DirectorProposal(goal_progress=0.9))
    assert orchestrator._last_goal_progress == 0.9
    orchestrator._track_goal_drift(DirectorProposal(goal_progress=0.4))
    events = [e["event"] for e in orchestrator.workspace.events.read_all()]
    assert "goal.drift_detected" in events
    context.db.close()


def test_goal_drift_ignores_noise(project):
    context = open_context(project)
    orchestrator = Orchestrator(context, use_model_director=False)
    orchestrator._track_goal_drift(DirectorProposal(goal_progress=0.5))
    orchestrator._track_goal_drift(DirectorProposal(goal_progress=0.4))
    events = [e["event"] for e in orchestrator.workspace.events.read_all()]
    assert "goal.drift_detected" not in events
    context.db.close()


def test_heartbeat_emits_after_interval(project):
    context = open_context(project)
    orchestrator = Orchestrator(context, use_model_director=False)
    orchestrator.HEARTBEAT_SECONDS = 0.0  # force immediate
    orchestrator._heartbeat()
    events = [e["event"] for e in orchestrator.workspace.events.read_all()]
    assert "run.heartbeat" in events
    context.db.close()


def test_heartbeat_silent_before_interval(project):
    context = open_context(project)
    orchestrator = Orchestrator(context, use_model_director=False)
    orchestrator._heartbeat()
    events = [e["event"] for e in orchestrator.workspace.events.read_all()]
    assert "run.heartbeat" not in events
    context.db.close()
