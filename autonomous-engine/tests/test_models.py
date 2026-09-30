"""Model abstraction: echo provider behaviour, JSON extraction, routing."""

from __future__ import annotations

import pytest

from autonomous_engine.core.config import ModelRoute, ProjectConfig
from autonomous_engine.models.base import CompletionRequest, ModelError, get_provider
from autonomous_engine.models.providers import AnthropicProvider, OpenAICompatProvider
from autonomous_engine.models.router import ModelRouter, extract_json


def _request(schema_hint: str = "", prompt: str = "hello") -> CompletionRequest:
    return CompletionRequest(system="s", prompt=prompt, schema_hint=schema_hint)


# ---- echo provider ---------------------------------------------------------


async def test_echo_generic_default(echo):
    response = await echo.complete(_request(), "default")
    assert response.provider == "echo"
    assert "echo" in response.text
    assert response.usage.tokens_in > 0


@pytest.mark.parametrize(
    "schema_marker,expected_key",
    [
        ("DirectorProposal JSON ...", "action"),
        ("ReviewVerdict JSON ...", "approved"),
        ("SecurityReport JSON ...", "passed"),
        ("Diagnosis JSON ...", "root_cause"),
        ("ResearchAnswer JSON ...", "resolved"),
        ("ReleaseReport JSON ...", "ready"),
    ],
)
async def test_echo_schema_aware_defaults(echo, schema_marker, expected_key):
    payload = await echo.complete(_request(schema_marker), "default")
    import json

    data = json.loads(payload.text)
    assert expected_key in data


async def test_echo_canned_and_queue(echo):
    echo.set_response("Custom", {"custom": True})
    response = await echo.complete(_request("Custom"), "m")
    assert '"custom": true' in response.text
    echo.enqueue({"next": 1})
    response = await echo.complete(_request(), "m")
    assert '"next": 1' in response.text


# ---- JSON extraction -------------------------------------------------------


def test_extract_json_plain():
    assert extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_fenced():
    text = 'Here you go:\n```json\n{"a": 1}\n```\nDone.'
    assert extract_json(text) == {"a": 1}


def test_extract_json_embedded_in_prose():
    assert extract_json('Sure! {"a": 1} hope that helps') == {"a": 1}


def test_extract_json_rejects_non_objects():
    with pytest.raises(ModelError):
        extract_json("[1, 2, 3]")
    with pytest.raises(ModelError):
        extract_json("")
    with pytest.raises(ModelError):
        extract_json("not json at all")


# ---- router ----------------------------------------------------------------


def _config_with_failing_primary(monkeypatch) -> ProjectConfig:
    config = ProjectConfig()
    config.model_routes = [ModelRoute(role="coder", provider="broken", model="m", fallbacks=[])]
    return config


async def test_router_retries_and_falls_back(monkeypatch):
    """A retriable failure exhausts retries, then the fallback serves."""

    class FlakyProvider:
        name = "flaky"
        calls = 0

        async def complete(self, request, model):
            self.calls += 1
            raise ModelError("boom", provider=self.name, model=model, retriable=True)

    class SolidProvider:
        name = "solid"
        calls = 0

        async def complete(self, request, model):
            self.calls += 1
            from autonomous_engine.models.base import ModelResponse

            return ModelResponse(text='{"ok": true}', provider=self.name, model=model)

    from autonomous_engine.models.base import register_provider

    flaky, solid = FlakyProvider(), SolidProvider()
    register_provider(flaky, replace=True)
    register_provider(solid, replace=True)

    config = ProjectConfig()
    config.model_routes = [
        ModelRoute(role="coder", provider="flaky", model="m", fallbacks=["solid/m2"])
    ]
    router = ModelRouter(config, max_retries=1, retry_backoff_seconds=0.01)
    response = await router.complete(_request(), "coder")
    assert response.provider == "solid"
    assert flaky.calls == 2  # retried once, then moved on
    assert solid.calls == 1
    assert router.stats["coder"]["ok"] == 1


async def test_router_escalates_to_fallback_on_complexity():
    class ProbeProvider:
        name = "probe"
        seen_models: list[str] = []

        async def complete(self, request, model):
            self.seen_models.append(model)
            from autonomous_engine.models.base import ModelResponse

            return ModelResponse(text="{}", provider=self.name, model=model)

    from autonomous_engine.models.base import register_provider

    probe = ProbeProvider()
    register_provider(probe, replace=True)
    config = ProjectConfig()
    config.model_routes = [
        ModelRoute(role="coder", provider="probe", model="small", fallbacks=["probe/large"])
    ]
    router = ModelRouter(config)
    decision = router.route_for("coder", complexity=9)
    assert decision.model == "large"
    response = await router.complete(_request(), "coder", complexity=9)
    assert response.model == "large"


def test_router_unknown_provider_raises_model_error():
    config = ProjectConfig()
    config.model_routes = [ModelRoute(role="coder", provider="nope", model="m")]
    router = ModelRouter(config, max_retries=0)
    import asyncio

    with pytest.raises(ModelError):
        asyncio.run(router.complete(_request(), "coder"))


# ---- provider construction / policy ---------------------------------------


def test_openai_provider_rejects_private_endpoint_by_default(monkeypatch):
    provider = OpenAICompatProvider(base_url="http://127.0.0.1:11434/v1")
    monkeypatch.delenv("AUTO_ALLOW_PRIVATE_ENDPOINTS", raising=False)
    import asyncio

    with pytest.raises(ModelError, match="network policy"):
        asyncio.run(provider.complete(_request(), "m"))


def test_openai_provider_allows_private_endpoint_with_opt_in(monkeypatch):
    monkeypatch.setenv("AUTO_ALLOW_PRIVATE_ENDPOINTS", "true")
    provider = OpenAICompatProvider(base_url="http://127.0.0.1:11434/v1")
    import asyncio

    with pytest.raises(ModelError, match="timed out|network error"):
        # no server is listening; policy passed, transport failed
        asyncio.run(provider.complete(_request("x"), "m"))


def test_anthropic_provider_requires_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    provider = AnthropicProvider()
    import asyncio

    with pytest.raises(ModelError, match="ANTHROPIC_API_KEY"):
        asyncio.run(provider.complete(_request(), "claude-x"))


def test_unknown_provider_lookup():
    with pytest.raises(ModelError):
        get_provider("definitely-not-registered")
