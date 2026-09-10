"""Preserve DeepSeek reasoning at the pinned provider's serialization boundary."""

from __future__ import annotations

from typing import Any

from lc_factory.upstream import AgentMiddleware


def with_deepseek_reasoning(model: Any) -> Any:
    """Adapt a request-local copy; never patch a shared provider or its class.

    The installed ChatDeepSeek captures reasoning_content but delegates outgoing
    history to ChatOpenAI, which discards that non-OpenAI field. DeepSeek requires
    it on assistant history when using thinking with tools, including prior turns.
    Preserve the original converter for tool schemas, content and provider options.
    """
    if getattr(model, "_llm_type", None) != "chat-deepseek":
        return model
    if getattr(model, "_lc_reasoning_roundtrip", False):
        return model
    adapted = model.model_copy()
    original_payload = model._get_request_payload

    def payload(input_: Any, *, stop: Any = None, **kwargs: Any) -> dict:
        messages = model._convert_input(input_).to_messages()
        result = original_payload(messages, stop=stop, **kwargs)
        for message, wire in zip(messages, result["messages"], strict=True):
            if wire.get("role") != "assistant":
                continue
            reasoning = message.additional_kwargs.get("reasoning_content")
            if isinstance(reasoning, str):
                wire["reasoning_content"] = reasoning
            elif "reasoning_content" not in wire:
                # Harness-generated assistant context has no provider reasoning.
                # Represent its absence explicitly; never invent reasoning text.
                wire["reasoning_content"] = ""
        return result

    object.__setattr__(adapted, "_get_request_payload", payload)
    object.__setattr__(adapted, "_lc_reasoning_roundtrip", True)
    return adapted


class DeepSeekReasoningMiddleware(AgentMiddleware):
    """Apply after runtime model selection, for streaming and ordinary calls."""

    def wrap_model_call(self, request, handler):
        return handler(request.override(model=with_deepseek_reasoning(request.model)))

    async def awrap_model_call(self, request, handler):
        return await handler(request.override(model=with_deepseek_reasoning(request.model)))
