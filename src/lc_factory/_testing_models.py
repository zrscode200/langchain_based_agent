"""Deterministic model fixtures importable by the real server subprocess.

The upstream fixture scans every prior message for its dispatch markers. A
fork inherits the parent's delegation prompt, so that marker wins over the
child's new task and requests recursive delegation instead of writing a file.
This adapter scopes fixture dispatch to the current human turn. The agent
still receives and checkpoints its complete inherited history.

Not part of the public factory API; production code does not import this module.
"""

from __future__ import annotations

from typing import Any

from lc_factory.upstream import import_tool_calling_test_model


class ForkAwareIntegrationChatModel(import_tool_calling_test_model()):
    """Use upstream tool responses for the latest task and its tool results."""

    def _generate(
        self,
        messages: list[Any],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> Any:
        # Keep subsequent tool results: upstream uses them to reply "done"
        # after its one write rather than issuing the same tool call again.
        start = next(
            (i for i in range(len(messages) - 1, -1, -1) if messages[i].type == "human"),
            0,
        )
        return super()._generate(
            messages[start:], stop=stop, run_manager=run_manager, **kwargs
        )


class SettledIntegrationChatModel(ForkAwareIntegrationChatModel):
    """Exercise the opt-in native settled tool through the real server transport."""
    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        result = super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        for generation in result.generations:
            message = generation.message
            if any(call["name"] == "task" for call in getattr(message, "tool_calls", [])):
                generation.message = message.model_copy(update={"tool_calls": [
                    {**call, "name": "task_settled", "args": {
                        "description": call["args"]["description"],
                        "subagentType": call["args"]["subagent_type"],
                    }} if call["name"] == "task" else call for call in message.tool_calls
                ]})
        return result
