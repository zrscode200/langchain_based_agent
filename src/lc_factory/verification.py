"""Owned verification selection over the pinned criteria/grader implementations."""
from __future__ import annotations

from dataclasses import replace

from lc_factory._env import VERIFICATION_MODEL_ENV
from lc_factory.upstream import CLIContextSchema, ModelConfig, create_model, get_config_sources


def configured_verification_model(environ, *, cli_max_retries=None):
    """Resolve a trusted configured model without changing main-model runtime state.

    Caller must enter the captured workspace environment before calling. Config
    comes from the upstream user/managed snapshots, never a workspace TOML file.
    An explicitly named provider/model pair must be declared: otherwise custom
    providers can silently construct an unconfigured model with default params.
    """
    ref = environ.get(VERIFICATION_MODEL_ENV, "").strip()
    if not ref:
        sources = get_config_sources()
        if not sources.user.status.usable or not sources.managed.status.usable:
            raise ValueError("Cannot read verification settings from invalid configuration")
        data, _ = sources.merged()
        table = data.get("lc_factory", {})
        if not isinstance(table, dict):
            raise ValueError("[lc_factory] must be a table")
        ref = table.get("verification_model")
        if ref is None:
            return None
        if not isinstance(ref, str) or not ref.strip():
            raise ValueError("[lc_factory].verification_model must be a non-empty model reference")
        ref = ref.strip()
    model_config = ModelConfig.load()
    model_config.require_model_allowed(ref, context="verification_model")
    provider, separator, name = ref.partition(":")
    if separator:
        declared = (model_config.providers or {}).get(provider, {}).get("models", [])
        if name not in declared:
            raise ValueError(f"verification_model {ref!r} must name a configured provider/model pair")
    # create_model tags the model with its own retry budget and provenance.
    # Do not apply_to_runtime_state(): the main model owns that state.
    return create_model(ref, cli_max_retries=cli_max_retries).model


class FixedVerificationAgent:
    """Keep explicit criteria models fixed while retaining approval/hook context.

    Upstream criteria graphs inherit runtime main-model selection from context.
    Merely changing their construction model would be overwritten on /model.
    GoalCriteriaMiddleware only needs invoke/ainvoke on its private graphs.
    """
    def __init__(self, agent):
        self.agent = agent

    @staticmethod
    def _context(context):
        context = CLIContextSchema.from_payload(context)
        if context is None:
            return None
        return replace(context, model=None, model_params={}, profile_overrides={},
                       model_context_limit=None, summarization_model=None)

    def invoke(self, input, config=None, *, context=None, **kwargs):
        return self.agent.invoke(input, config, context=self._context(context), **kwargs)

    async def ainvoke(self, input, config=None, *, context=None, **kwargs):
        return await self.agent.ainvoke(input, config, context=self._context(context), **kwargs)
