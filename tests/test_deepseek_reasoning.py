"""Real SDK serialization with synthetic credentials and in-memory HTTP only."""

import json
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from lc_factory.deepseek_reasoning import DeepSeekReasoningMiddleware, with_deepseek_reasoning
from test_auto_classifier import completion, deepseek_model, request


TOOL = {"type": "function", "function": {"name": "inspect", "description": "Inspect",
        "parameters": {"type": "object", "properties": {}}}}


@pytest.mark.parametrize("streaming", [False, True])
async def test_reasoning_survives_real_sdk_tool_binding_and_followup(streaming):
    payloads = []

    def handler(req):
        body = json.loads(req.content)
        payloads.append(body)
        if body.get("stream"):
            chunks = [{"role": "assistant", "reasoning_content": "new thought"}, {"content": "done"}]
            events = [json.dumps({"id": "chunk", "object": "chat.completion.chunk", "created": 0,
                       "model": "deepseek-v4-flash", "choices": [{"index": 0, "delta": d, "finish_reason": None}]}) for d in chunks]
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  text="".join("data: " + e + "\n\n" for e in events) + "data: [DONE]\n\n")
        return httpx.Response(200, json=completion({"role": "assistant", "content": "done", "reasoning_content": "new thought"}))

    history = [HumanMessage("inspect"), AIMessage("", additional_kwargs={"reasoning_content": "original thought", "private": "do not send"},
        tool_calls=[{"name": "inspect", "id": "call", "args": {}}]), ToolMessage("result", tool_call_id="call"),
        AIMessage("completed", additional_kwargs={"reasoning_content": "final thought"}),
        AIMessage("background result"), HumanMessage("follow up")]
    before = deepcopy(history)
    async with deepseek_model(handler) as original:
        adapted = with_deepseek_reasoning(original)
        assert adapted is not original
        assert adapted.async_client is original.async_client
        assert with_deepseek_reasoning(adapted) is adapted
        bound = adapted.bind_tools([TOOL])
        if streaming:
            output = None
            async for chunk in bound.astream(history):
                output = chunk if output is None else output + chunk
        else:
            output = await bound.ainvoke(history)
        assert output.additional_kwargs["reasoning_content"] == "new thought"
        await bound.ainvoke([*history, output, HumanMessage("again")])
        assert "reasoning_content" not in original._get_request_payload(history)["messages"][1]
    assert history == before
    for payload in payloads:
        assert payload["thinking"] == {"type": "enabled"}
        assistants = [m for m in payload["messages"] if m["role"] == "assistant"]
        assert [m["reasoning_content"] for m in assistants[:3]] == ["original thought", "final thought", ""]
        assert assistants[0]["tool_calls"][0]["id"] == "call"
        assert all("private" not in m for m in assistants)
    assert payloads[-1]["messages"][-2]["reasoning_content"] == "new thought"


async def test_compiled_graph_passes_reasoning_back_after_a_tool():
    payloads = []

    def inspect() -> str:
        """Return a synthetic observation."""
        return "observed"

    def handler(req):
        payloads.append(json.loads(req.content))
        if len(payloads) == 1:
            return httpx.Response(200, json=completion({"role": "assistant", "content": "", "reasoning_content": "inspect first",
                "tool_calls": [{"id": "call", "type": "function", "function": {"name": "inspect", "arguments": "{}"}}]}))
        assert payloads[-1]["messages"][-2]["reasoning_content"] == "inspect first"
        return httpx.Response(200, json=completion({"role": "assistant", "content": "done", "reasoning_content": "finished"}))

    async with deepseek_model(handler) as model:
        graph = create_agent(model, tools=[inspect], middleware=[DeepSeekReasoningMiddleware()])
        state = await graph.ainvoke({"messages": [HumanMessage("inspect")]})
    assert len(payloads) == 2
    assert state["messages"][-1].content == "done"


async def test_runtime_model_selection_and_sync_async_wrappers():
    async with deepseek_model(lambda req: httpx.Response(500)) as model:
        middleware = DeepSeekReasoningMiddleware()
        incoming = request(model)
        seen = []

        def handler(adapted):
            seen.append(adapted)
            return "response"

        async def async_handler(adapted):
            return handler(adapted)

        assert middleware.wrap_model_call(incoming, handler) == "response"
        assert await middleware.awrap_model_call(incoming, async_handler) == "response"
        assert all(r.model is not model and r.messages is incoming.messages for r in seen)
        assert incoming.model is model
        other = SimpleNamespace(_llm_type="another-provider")
        assert with_deepseek_reasoning(other) is other


@pytest.mark.parametrize("reasoning", ["", None])
async def test_empty_reasoning_and_nonassistant_fields(reasoning):
    async with deepseek_model(lambda req: httpx.Response(500)) as model:
        messages = [HumanMessage("hi", additional_kwargs={"reasoning_content": "private user metadata"}),
                    AIMessage("answer", additional_kwargs={"reasoning_content": reasoning})]
        wire = with_deepseek_reasoning(model)._get_request_payload(messages)["messages"]
        assert "reasoning_content" not in wire[0]
        assert wire[1]["reasoning_content"] == ""
