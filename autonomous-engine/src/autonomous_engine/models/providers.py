"""Provider adapters: echo (offline/testing), OpenAI-compatible, Anthropic.

All providers speak the same `CompletionRequest`/`ModelResponse` contract, so
the orchestration core is provider-agnostic. Credentials are read from the
environment at call time; nothing is hard-coded.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

import httpx

from ..core.security import validate_endpoint_url
from .base import (
    ChatMessage,
    CompletionRequest,
    ModelError,
    ModelResponse,
    ProviderAdapter,
    ToolCall,
    Usage,
)
from .ratelimit import limiter_for
from .streaming import global_observer

# Rough offline cost model for budget accounting (USD / 1M tokens). These are
# intentionally conservative estimates, not billing truth.
_COST_PER_1M = {"default_in": 3.0, "default_out": 15.0}

# Deterministic default responses per schema, so an offline run exercises the
# whole loop honestly: agents that must produce a verdict produce the *weak*
# verdict (review approves only the evidence it is shown; the researcher
# refuses to fabricate answers), and management defaults to continuing.
_ECHO_DEFAULTS: tuple[tuple[str, dict[str, Any]], ...] = (
    (
        "DirectorProposal",
        {
            "action": "continue",
            "rationale": "echo: continue with the deterministic plan",
            "confidence": 0.5,
            "goal_progress": 0.0,
        },
    ),
    (
        "ReviewVerdict",
        {
            "approved": True,
            "findings": [],
            "summary": "echo: no findings recorded",
            "confidence": 0.4,
        },
    ),
    (
        "SecurityReport",
        {"passed": True, "findings": [], "confidence": 0.4},
    ),
    (
        "Diagnosis",
        {
            "root_cause": "echo: verification evidence shows the implementation did not "
            "satisfy the task's checks; retrying with the recorded evidence",
            "candidate_fixes": ["re-implement against the recorded failure evidence"],
            "files_affected": [],
            "tests_required": [],
            "architecture_issue": False,
            "confidence": 0.35,
        },
    ),
    (
        "TestReport",
        {"status": "passed", "commands_run": [], "generated_tests": [], "confidence": 0.3},
    ),
    (
        "Requirements",
        {
            "functional": [],
            "user_stories": [],
            "acceptance_criteria": [],
            "non_functional": [],
            "edge_cases": [],
            "constraints": [],
            "definition_of_done": [],
        },
    ),
    (
        "ResearchAnswer",
        {
            "question": "",
            "answer": "echo: no evidence available offline; treat as unresolved",
            "resolved": False,
            "sources": [],
            "confidence": 0.2,
        },
    ),
    (
        "QACheck",
        {"aligned": True, "gaps": [], "drift": [], "summary": "echo: aligned", "confidence": 0.3},
    ),
    (
        "BoardVerdict",
        {
            "decision": "keep_current",
            "rationale": "echo: no architectural evidence against the current plan",
            "confidence": 0.4,
        },
    ),
    (
        "ReleaseReport",
        {
            "ready": True,
            "version": "",
            "notes": "echo: release notes generated offline from the task graph",
            "checks": [],
            "blocking": [],
            "confidence": 0.3,
        },
    ),
)


def _echo_default(schema_hint: str) -> dict[str, Any] | None:
    hint = (schema_hint or "").lower()
    for marker, payload in _ECHO_DEFAULTS:
        if marker.lower() in hint:
            return dict(payload)
    return None


def estimate_cost(tokens_in: int, tokens_out: int, rate: dict[str, float] | None = None) -> float:
    rate = rate or _COST_PER_1M
    return tokens_in / 1_000_000 * rate["default_in"] + tokens_out / 1_000_000 * rate["default_out"]


def _rough_token_count(text: str) -> int:
    # 4 characters/token is the standard rule of thumb.
    return max(1, len(text) // 4)


class EchoProvider(ProviderAdapter):
    """Offline provider used for tests, dry runs, and CI.

    It never calls the network. It returns a deterministic, schema-shaped
    response so the orchestration loop, persistence, and verification can be
    exercised end-to-end without credentials.
    """

    name = "echo"

    def __init__(self) -> None:
        self.canned: dict[str, Any] = {}
        self.queue: list[dict[str, Any]] = []

    def set_response(self, schema_hint: str, payload: dict[str, Any]) -> None:
        self.canned[schema_hint] = payload

    def enqueue(self, payload: dict[str, Any]) -> None:
        self.queue.append(payload)

    async def complete(self, request: CompletionRequest, model: str) -> ModelResponse:
        started = time.perf_counter()
        payload: dict[str, Any] | None = None
        if self.queue:
            payload = self.queue.pop(0)
        elif request.schema_hint in self.canned:
            payload = self.canned[request.schema_hint]
        else:
            # substring match: tests key canned responses by schema name
            hint = (request.schema_hint or "").lower()
            for key, canned in self.canned.items():
                if key.lower() in hint:
                    payload = canned
                    break
        if payload is None:
            payload = _echo_default(request.schema_hint)
        if payload is None:
            payload = {"echo": True, "schema_hint": request.schema_hint}
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        tokens_in = _rough_token_count(request.system + request.prompt)
        tokens_out = _rough_token_count(text)
        return ModelResponse(
            text=text,
            usage=Usage(
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_usd=estimate_cost(tokens_in, tokens_out),
            ),
            provider=self.name,
            model=model,
            latency_ms=(time.perf_counter() - started) * 1000,
            finish_reason="stop",
        )


class OpenAICompatProvider(ProviderAdapter):
    """Works with OpenAI and any OpenAI-compatible endpoint (Ollama, vLLM,
    LM Studio, OpenRouter, Together, ...).

    Endpoint URLs are validated against the network policy before any request
    is issued; a private/local endpoint requires the explicit
    `AUTO_ALLOW_PRIVATE_ENDPOINTS=true` opt-in.
    """

    name = "openai"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key_env: str = "OPENAI_API_KEY",
        timeout: float = 120.0,
    ):
        self.base_url = base_url or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
        self.api_key_env = api_key_env
        self.timeout = timeout

    def _allow_private(self) -> bool:
        return os.environ.get("AUTO_ALLOW_PRIVATE_ENDPOINTS", "").lower() in {"1", "true", "yes"}

    def _provider_key(self) -> str:
        """Rate-limit identity from the endpoint host (kios→5 RPM, atria→30)."""
        from urllib.parse import urlparse

        host = (urlparse(self.base_url).hostname or "").lower()
        if "kios" in host:
            return "kios"
        if "atria" in host or "aria" in host:
            return "atria"
        return "openai"

    async def complete(self, request: CompletionRequest, model: str) -> ModelResponse:
        endpoint = f"{self.base_url.rstrip('/')}/chat/completions"
        try:
            validate_endpoint_url(endpoint, allow_private=self._allow_private())
        except ValueError as exc:
            raise ModelError(
                f"endpoint rejected by network policy: {exc}", provider=self.name, model=model
            ) from exc

        api_key = os.environ.get(self.api_key_env, "")
        if not api_key and not self.base_url.startswith(("http://localhost", "http://127.0.0.1")):
            raise ModelError(
                f"missing API key: set environment variable {self.api_key_env}",
                provider=self.name,
                model=model,
            )

        messages: list[dict[str, Any]] = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        for message in request.messages:
            messages.append(_openai_message(message))
        messages.append({"role": "user", "content": request.prompt})
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
        }
        if request.tools:
            body["tools"] = [_openai_tool_schema(tool) for tool in request.tools]
            body["tool_choice"] = "auto"
        if request.schema_hint and not request.tools:
            body["response_format"] = {"type": "json_object"}

        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        started = time.perf_counter()
        # Documented rate limits (Kios 5 RPM, Atria 30 RPM) are enforced at
        # the single choke point every model call passes through.
        await limiter_for(self._provider_key()).acquire()
        observer = global_observer()
        call_id = f"{model}-{int(started)}"
        observer.emit_delta(
            agent=str(request.metadata.get("agent", "")),
            model=model,
            provider=self.name,
            delta="",
            call_id=call_id,
        )
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(endpoint, json=body, headers=headers)
        except httpx.TimeoutException as exc:
            raise ModelError(
                f"request timed out: {exc}", provider=self.name, model=model, retriable=True
            ) from exc
        except httpx.HTTPError as exc:
            raise ModelError(
                f"network error: {exc}", provider=self.name, model=model, retriable=True
            ) from exc

        latency = (time.perf_counter() - started) * 1000
        if resp.status_code == 429 or resp.status_code >= 500:
            raise ModelError(
                f"provider returned {resp.status_code}",
                provider=self.name,
                model=model,
                retriable=True,
            )
        if resp.status_code >= 400:
            raise ModelError(
                f"provider returned {resp.status_code}: {resp.text[:300]}",
                provider=self.name,
                model=model,
            )

        try:
            data = resp.json()
            choice = data["choices"][0]
            message = choice["message"]
            text = message.get("content") or ""
            tool_calls = _parse_openai_tool_calls(message.get("tool_calls") or [])
        except (json.JSONDecodeError, KeyError, IndexError) as exc:
            raise ModelError(
                f"malformed provider response: {exc}", provider=self.name, model=model
            ) from exc
        # Stream the produced text as bounded deltas (chunked); the UI renders
        # live model work without ever seeing prompts or credentials.
        for index in range(0, len(text), 240):
            observer.emit_delta(
                agent=str(request.metadata.get("agent", "")),
                model=model,
                provider=self.name,
                delta=text[index : index + 240],
                call_id=call_id,
            )
        observer.emit_delta(
            agent=str(request.metadata.get("agent", "")),
            model=model,
            provider=self.name,
            delta="",
            call_id=call_id,
            final=True,
        )

        usage_raw = data.get("usage", {}) or {}
        tokens_in = int(usage_raw.get("prompt_tokens", _rough_token_count(request.prompt)))
        tokens_out = int(usage_raw.get("completion_tokens", _rough_token_count(text)))
        return ModelResponse(
            text=text,
            usage=Usage(
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_usd=estimate_cost(tokens_in, tokens_out),
            ),
            provider=self.name,
            model=model,
            latency_ms=latency,
            finish_reason=choice.get("finish_reason", ""),
            tool_calls=tool_calls,
        )


class AnthropicProvider(ProviderAdapter):
    """Anthropic Messages API adapter."""

    name = "anthropic"

    def __init__(
        self,
        *,
        api_key_env: str = "ANTHROPIC_API_KEY",
        base_url: str | None = None,
        timeout: float = 120.0,
    ):
        self.api_key_env = api_key_env
        self.base_url = base_url or os.environ.get(
            "ANTHROPIC_BASE_URL", "https://api.anthropic.com"
        )
        self.timeout = timeout

    def _allow_private(self) -> bool:
        return os.environ.get("AUTO_ALLOW_PRIVATE_ENDPOINTS", "").lower() in {"1", "true", "yes"}

    async def complete(self, request: CompletionRequest, model: str) -> ModelResponse:
        endpoint = f"{self.base_url.rstrip('/')}/v1/messages"
        try:
            validate_endpoint_url(endpoint, allow_private=self._allow_private())
        except ValueError as exc:
            raise ModelError(
                f"endpoint rejected by network policy: {exc}", provider=self.name, model=model
            ) from exc

        api_key = os.environ.get(self.api_key_env, "")
        if not api_key:
            raise ModelError(
                f"missing API key: set environment variable {self.api_key_env}",
                provider=self.name,
                model=model,
            )
        messages = _anthropic_conversation(request.messages, request.prompt)
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "messages": messages,
        }
        if request.system:
            body["system"] = request.system
        if request.tools:
            body["tools"] = [_anthropic_tool_schema(tool) for tool in request.tools]
        if request.schema_hint and not request.tools:
            body["system"] = (
                body.get("system", "") + "\n\n" if body.get("system") else ""
            ) + "Respond with a single valid JSON object. Do not include prose or code fences."

        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    endpoint,
                    json=body,
                    headers={
                        "x-api-key": api_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                )
        except httpx.TimeoutException as exc:
            raise ModelError(
                f"request timed out: {exc}", provider=self.name, model=model, retriable=True
            ) from exc
        except httpx.HTTPError as exc:
            raise ModelError(
                f"network error: {exc}", provider=self.name, model=model, retriable=True
            ) from exc

        latency = (time.perf_counter() - started) * 1000
        if resp.status_code == 429 or resp.status_code >= 500:
            raise ModelError(
                f"provider returned {resp.status_code}",
                provider=self.name,
                model=model,
                retriable=True,
            )
        if resp.status_code >= 400:
            raise ModelError(
                f"provider returned {resp.status_code}: {resp.text[:300]}",
                provider=self.name,
                model=model,
            )

        try:
            data = resp.json()
            blocks = data.get("content", [])
            text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
            tool_calls = _parse_anthropic_tool_uses(blocks)
        except (json.JSONDecodeError, AttributeError) as exc:
            raise ModelError(
                f"malformed provider response: {exc}", provider=self.name, model=model
            ) from exc

        usage_raw = data.get("usage", {}) or {}
        tokens_in = int(usage_raw.get("input_tokens", _rough_token_count(request.prompt)))
        tokens_out = int(usage_raw.get("output_tokens", _rough_token_count(text)))
        return ModelResponse(
            text=text,
            usage=Usage(
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_usd=estimate_cost(tokens_in, tokens_out),
            ),
            provider=self.name,
            model=model,
            latency_ms=latency,
            finish_reason=data.get("stop_reason", ""),
            tool_calls=tool_calls,
        )


def _openai_tool_schema(tool: dict[str, Any]) -> dict[str, Any]:
    """Neutral declaration -> OpenAI function schema."""
    return {
        "type": "function",
        "function": {
            "name": tool.get("name", ""),
            "description": tool.get("description", ""),
            "parameters": tool.get("parameters", {"type": "object", "properties": {}}),
        },
    }


def _anthropic_tool_schema(tool: dict[str, Any]) -> dict[str, Any]:
    """Neutral declaration -> Anthropic tool schema."""
    return {
        "name": tool.get("name", ""),
        "description": tool.get("description", ""),
        "input_schema": tool.get("parameters", {"type": "object", "properties": {}}),
    }


def _openai_message(message: ChatMessage) -> dict[str, Any]:
    """Translate one neutral message into the OpenAI chat-completions shape."""
    if message.role == "tool":
        return {
            "role": "tool",
            "tool_call_id": message.tool_call_id or message.name,
            "content": message.content,
        }
    if message.role == "assistant" and message.tool_calls:
        return {
            "role": "assistant",
            "content": message.content or None,
            "tool_calls": [
                {
                    "id": call.id or f"call_{index}",
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": call.raw_arguments
                        or json.dumps(call.arguments, ensure_ascii=False),
                    },
                }
                for index, call in enumerate(message.tool_calls)
            ],
        }
    return {"role": message.role, "content": message.content}


def _parse_openai_tool_calls(raw_calls: list[dict[str, Any]]) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for index, raw in enumerate(raw_calls):
        function = raw.get("function", {}) if isinstance(raw, dict) else {}
        arguments_text = function.get("arguments") or "{}"
        try:
            arguments = json.loads(arguments_text) if isinstance(arguments_text, str) else {}
        except json.JSONDecodeError:
            arguments = {}
        if not isinstance(arguments, dict):
            arguments = {}
        calls.append(
            ToolCall(
                id=str(raw.get("id") or f"call_{index}"),
                name=str(function.get("name") or ""),
                arguments=arguments,
                raw_arguments=arguments_text if isinstance(arguments_text, str) else "",
            )
        )
    return [call for call in calls if call.name]


def _anthropic_message(message: ChatMessage) -> dict[str, Any]:
    """Translate one neutral message into the Anthropic Messages shape."""
    if message.role == "tool":
        return {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": message.tool_call_id or message.name,
                    "content": message.content,
                }
            ],
        }
    if message.role == "assistant" and message.tool_calls:
        blocks: list[dict[str, Any]] = []
        if message.content:
            blocks.append({"type": "text", "text": message.content})
        blocks.extend(
            {
                "type": "tool_use",
                "id": call.id or f"toolu_{index}",
                "name": call.name,
                "input": call.arguments,
            }
            for index, call in enumerate(message.tool_calls)
        )
        return {"role": "assistant", "content": blocks}
    return {"role": message.role, "content": message.content}


def _anthropic_conversation(history: list[ChatMessage], prompt: str) -> list[dict[str, Any]]:
    """Build a role-alternating Anthropic message list.

    The Anthropic API rejects consecutive same-role messages, so: consecutive
    tool results are merged into one user turn, and the caller's prompt
    becomes the FIRST user turn (appending it at the end produced
    user-after-user and a 400 on every tool-loop iteration after the first).
    """
    converted = [_anthropic_message(m) for m in history]
    merged: list[dict[str, Any]] = []
    for message in converted:
        if merged and message["role"] == "user" and merged[-1]["role"] == "user":
            previous = merged[-1]["content"]
            blocks = previous if isinstance(previous, list) else [{"type": "text", "text": previous}]
            blocks.extend(message["content"])
            merged[-1]["content"] = blocks
        else:
            merged.append(message)
    prompt_message: dict[str, Any] = {"role": "user", "content": prompt}
    if merged and merged[0]["role"] == "user":
        first = merged[0]["content"]
        merged[0]["content"] = [{"type": "text", "text": prompt}] + (
            first if isinstance(first, list) else [{"type": "text", "text": first}]
        )
    else:
        merged.insert(0, prompt_message)
    return merged


def _parse_anthropic_tool_uses(blocks: list[dict[str, Any]]) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for index, block in enumerate(blocks):
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        arguments = block.get("input")
        calls.append(
            ToolCall(
                id=str(block.get("id") or f"toolu_{index}"),
                name=str(block.get("name") or ""),
                arguments=arguments if isinstance(arguments, dict) else {},
            )
        )
    return [call for call in calls if call.name]
