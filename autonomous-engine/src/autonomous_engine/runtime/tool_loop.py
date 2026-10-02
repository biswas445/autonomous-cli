"""The agentic tool loop: call a model, run its tools, feed back observations.

This is the pattern every capable coding CLI implements — the model does not
answer in one shot; it reads, searches, runs something, looks at the result,
and only then concludes. The loop here is deliberately small and bounded:

    1. send (system, prompt, tools, transcript)
    2. if the model requested tool calls -> execute each through the agent's
       permission-checked ToolBox, append the observations, goto 1
    3. otherwise the model's text is the answer

Guarantees that make it safe to run unattended:

* **Bounded.** `max_iterations` caps the exchange; on the final iteration the
  tools are withdrawn so the model must answer with what it has.
* **Sandboxed.** Every tool call goes through the same ToolBox as everything
  else; permission failures are returned *to the model* as observations
  (so it can adapt) and recorded in the transcript.
* **Observable.** The transcript — tool, arguments, result size, ok/denied —
  is returned for evidence and the activity log; nothing is hidden.
* **Provider-neutral.** The loop speaks `ChatMessage`/`ToolCall`; providers
  translate to their wire format (OpenAI tool_calls, Anthropic tool_use).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..models.base import ChatMessage, CompletionRequest, ModelError, ToolCall
from .tools import READ_ONLY_TOOLS, execute_tool, tool_specs

# Neutral tool declaration form passed through CompletionRequest.tools; each
# provider converts it to its own schema shape.
NeutralTool = dict[str, Any]


@dataclass
class ToolLoopResult:
    text: str = ""
    transcript: list[dict[str, Any]] = field(default_factory=list)
    iterations: int = 0
    tool_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    stopped_reason: str = "answered"  # answered | max_iterations | error

    def evidence(self) -> dict[str, Any]:
        return {
            "iterations": self.iterations,
            "tool_calls": self.tool_calls,
            "transcript": self.transcript[-20:],
            "stopped_reason": self.stopped_reason,
        }


def neutral_tool_schemas(names: tuple[str, ...] | list[str]) -> list[NeutralTool]:
    return [
        {
            "name": spec.name,
            "description": spec.description,
            "parameters": spec.parameters,
        }
        for spec in tool_specs(names)
    ]


class ToolLoop:
    """Runs one bounded tool-calling conversation for an agent."""

    def __init__(self, router: Any, *, max_iterations: int = 6, on_event: Any = None):
        self.router = router
        self.max_iterations = max(1, max_iterations)
        # Optional live-activity sink (agent._activity): real tool traffic as
        # it happens, for the TUI/IPC observers. Never breaks the loop.
        self.on_event = on_event

    def _emit(self, event: str, **fields: Any) -> None:
        if self.on_event is None:
            return
        import contextlib

        with contextlib.suppress(Exception):
            self.on_event(event, **fields)

    async def run(
        self,
        *,
        role: str,
        system: str,
        prompt: str,
        tools: Any,
        tool_names: tuple[str, ...] | list[str] = READ_ONLY_TOOLS,
        schema_hint: str = "",
        max_tokens: int = 4096,
        temperature: float = 0.2,
        complexity: int = 5,
        security_sensitivity: str = "low",
    ) -> ToolLoopResult:
        result = ToolLoopResult()
        schemas = neutral_tool_schemas(tool_names)
        messages: list[ChatMessage] = []

        for iteration in range(1, self.max_iterations + 1):
            result.iterations = iteration
            final_iteration = iteration == self.max_iterations
            request = CompletionRequest(
                system=system,
                prompt=prompt,
                schema_hint=schema_hint,
                max_tokens=max_tokens,
                temperature=temperature,
                tools=[] if final_iteration or not schemas else schemas,
                messages=list(messages),
            )
            try:
                self._emit("model.call", role=role, iteration=iteration)
                response = await self.router.complete(
                    request,
                    role,
                    complexity=complexity,
                    security_sensitivity=security_sensitivity,
                )
            except ModelError as exc:
                result.stopped_reason = "error"
                result.text = getattr(exc, "message", str(exc))
                return result

            result.tokens_in += response.usage.tokens_in
            result.tokens_out += response.usage.tokens_out
            result.cost_usd += response.usage.cost_usd
            self._emit(
                "model.response",
                role=role,
                iteration=iteration,
                chars=len(response.text),
                tokens_in=response.usage.tokens_in,
                tokens_out=response.usage.tokens_out,
                cost_usd=response.usage.cost_usd,
                preview=response.text[:400],
            )

            if not response.tool_calls or final_iteration:
                # No tools requested (offline echo, or the model answered) —
                # or the budget is spent: take the text as the answer.
                result.text = response.text
                if final_iteration:
                    # The answer was forced by the iteration cap, whether or
                    # not the model also tried to call tools: the loop ran out
                    # of room, and downstream evidence should know it.
                    result.stopped_reason = "max_iterations"
                # Live test finding: a schema_hinted agent whose final answer
                # is prose (no JSON object) fails extract_json downstream and
                # wastes the whole loop. One schema-forced retry turns the
                # prose answer into the expected JSON.
                if schema_hint and result.stopped_reason != "error":
                    from ..models.router import extract_json

                    try:
                        extract_json(result.text)
                    except ModelError:
                        retry_request = CompletionRequest(
                            system=system,
                            prompt=(
                                f"{prompt}\n\nYour previous answer was not valid JSON. "
                                "Convert it to a single valid JSON object now, with "
                                "no prose, no code fences. Original instructions:\n"
                                + (f"Schema: {schema_hint}" if schema_hint else "")
                            ),
                            schema_hint=schema_hint,
                            max_tokens=max_tokens,
                            temperature=temperature,
                            tools=[],
                            messages=list(messages),
                        )
                        try:
                            self._emit("model.call", role=role, iteration=iteration, retry=True)
                            retry_response = await self.router.complete(
                                retry_request,
                                role,
                                complexity=complexity,
                                security_sensitivity=security_sensitivity,
                            )
                            from ..models.router import extract_json as _extract

                            _extract(retry_response.text)  # only adopt valid JSON
                            result.tokens_in += retry_response.usage.tokens_in
                            result.tokens_out += retry_response.usage.tokens_out
                            result.cost_usd += retry_response.usage.cost_usd
                            result.text = retry_response.text
                            self._emit(
                                "model.response",
                                role=role,
                                iteration=iteration,
                                retry=True,
                                chars=len(retry_response.text),
                                tokens_in=retry_response.usage.tokens_in,
                                tokens_out=retry_response.usage.tokens_out,
                                cost_usd=retry_response.usage.cost_usd,
                                preview=retry_response.text[:400],
                            )
                        except ModelError:
                            pass  # keep the prose answer; the caller fails honestly
                return result

            messages.append(
                ChatMessage(
                    role="assistant",
                    content=response.text or "",
                    tool_calls=response.tool_calls,
                )
            )
            for call in response.tool_calls:
                self._emit(
                    "tool.call",
                    role=role,
                    iteration=iteration,
                    tool=call.name,
                    arguments={
                        k: str(v)[:80] for k, v in (call.arguments or {}).items()
                    },
                )
                observation = execute_tool(tools, call.name, call.arguments)
                result.tool_calls += 1
                entry = _transcript_entry(call, observation)
                result.transcript.append(entry)
                self._emit(
                    "tool.observation",
                    role=role,
                    tool=call.name,
                    chars=entry["chars"],
                    ok=entry["ok"],
                    preview=entry["preview"],
                )
                # Tool output is untrusted data (v2 §104): a repository file
                # may contain text that looks like instructions. The marker
                # lets the model separate observed content from policy.
                marked = (
                    f"<<<UNTRUSTED_DATA tool:{call.name}>>>\n"
                    f"{observation}\n"
                    "<<<END UNTRUSTED_DATA>>>"
                )
                messages.append(
                    ChatMessage(
                        role="tool",
                        content=marked,
                        tool_call_id=call.id or call.name,
                        name=call.name,
                    )
                )

        result.stopped_reason = "max_iterations"
        return result


_DENIED_PREFIXES = (
    "permission denied",
    "not permitted",
    "unknown tool",
    "tool error",
    "blocked by network policy",
    "high-risk command refused",
    "not found",
    "invalid arguments for",
    "invalid base revision",
    "refusing to store",
)


def _transcript_entry(call: ToolCall, observation: str) -> dict[str, Any]:
    # Denial/refusal observations must be recorded as failures: incident
    # auditors rely on this transcript, so sniff every denial shape
    # execute_tool and the permission layer can emit.
    head = observation[:160].lower()
    denied = head.startswith(_DENIED_PREFIXES) or " is not permitted" in head
    return {
        "tool": call.name,
        "arguments": {k: str(v)[:80] for k, v in (call.arguments or {}).items()},
        "chars": len(observation),
        "ok": not denied,
        "preview": observation[:160].replace("\n", " ⏎ "),
    }
