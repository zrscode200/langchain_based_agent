"""Classifier-only compatibility for DeepSeek's thinking-mode tool selection."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from lc_factory.upstream import AutoModeHITLMiddleware as _AutoModeHITLMiddleware


class _StructuredClassifierView:
    def __init__(self, runnable: Any, model: Any) -> None:
        self._runnable = runnable
        self._model = model

    async def ainvoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        # DeepSeek thinking rejects forced/required tool selection. Override at
        # invocation, after both the structured-output binding and inherited
        # primary-model settings. Keep the native tool schema and strict parser:
        # an absent or malformed verdict still fails upstream's validation.
        settings = {**kwargs, "tool_choice": "auto"}
        default_body = self._model.model_kwargs.get(
            "extra_body", self._model.extra_body
        )
        extra_body = kwargs.get("extra_body", default_body)
        if isinstance(extra_body, Mapping) and "tool_choice" in extra_body:
            # The SDK merges extra_body last, over its ordinary parameters.
            # Copy only when needed; never mutate cached/shared model settings.
            settings["extra_body"] = {**extra_body, "tool_choice": "auto"}
        return await self._runnable.ainvoke(input, config=config, **settings)


class _DeepSeekClassifierView:
    def __init__(self, model: Any) -> None:
        self._model = model

    def __getattr__(self, name: str) -> Any:
        # In particular, retain the upstream auxiliary-call retry metadata.
        return getattr(self._model, name)

    def with_structured_output(self, *args: Any, **kwargs: Any) -> Any:
        return _StructuredClassifierView(
            self._model.with_structured_output(*args, **kwargs), self._model
        )


class AutoModeHITLMiddleware(_AutoModeHITLMiddleware):
    """Retain upstream Auto policy with a native DeepSeek invocation adapter.

    Adapt the selected classifier after upstream resolves its configured or
    inherited model. The ephemeral view preserves cache identity and leaves
    primary chat, thinking, other providers, retries, deadlines and HITL intact.
    No optional provider import or global model patch is needed.
    """

    async def _classifier_model(self, request: Any) -> tuple[Any, str | None]:
        model, spec = await super()._classifier_model(request)
        if getattr(model, "_llm_type", None) == "chat-deepseek":
            return _DeepSeekClassifierView(model), spec
        return model, spec
