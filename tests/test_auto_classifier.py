"""DeepSeek classifier wire compatibility and real Auto approval enforcement."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import json
from types import SimpleNamespace

import httpx
import pytest
from deepagents_code._fake_models import _ToolBindingFakeModel
from deepagents_code.approval_mode import (
    APPROVAL_MODE_NAMESPACE, approval_mode_key, approval_mode_payload,
)
from deepagents_code.auto_mode import (
    AutoDecisionBatch,
    INHERIT_CLASSIFIER_MODEL,
    _ClassifierDeadlineExceededError,
)
from deepagents_code.model_retry import MODEL_RETRIES_ATTR
from langchain.agents.middleware.types import ModelRequest
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.runtime import Runtime
from langgraph.store.memory import InMemoryStore
from langgraph.types import Command
from pydantic import SecretStr

from lc_factory.auto_classifier import AutoModeHITLMiddleware
from lc_factory.runtime import FactoryRuntime, RuntimeOptions


CALL = {"name": "start_background_task", "args": {
    "description": "Inspect tools", "subagent_type": "child",
}, "id": "submit", "type": "tool_call"}


def verdict(decision="allow", identifier="submit"):
    return {"tool_call_id": identifier, "decision": decision,
            "category": "other_policy", "reason": "Synthetic review."}


def completion(message):
    return {"id": "mock", "object": "chat.completion", "created": 0,
            "model": "deepseek-v4-flash", "choices": [
                {"index": 0, "finish_reason": "stop", "message": message},
            ]}


def structured_message(decisions):
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": "verdict", "type": "function", "function": {
            "name": "AutoDecisionBatch", "arguments": json.dumps({"decisions": decisions}),
        }},
    ]}


@asynccontextmanager
async def deepseek_model(handler):
    # Provider extras remain optional. Use the installed SDK's JSON serializer
    # with an in-memory transport, dummy credentials and no environment defaults.
    ChatDeepSeek = pytest.importorskip("langchain_deepseek").ChatDeepSeek
    openai = pytest.importorskip("openai")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False) as client:
        sdk = openai.AsyncOpenAI(api_key="synthetic", base_url="https://fixture.invalid/v1",
                                 http_client=client, max_retries=0)
        factories = {name: None for name, info in ChatDeepSeek.model_fields.items()
                     if info.default_factory is not None}
        model = ChatDeepSeek.model_construct(**{
            **factories, "model_name": "deepseek-v4-flash", "model_kwargs": {},
            "api_key": SecretStr("synthetic"), "openai_api_key": SecretStr("synthetic"),
            "api_base": "https://fixture.invalid/v1", "openai_api_base": "https://fixture.invalid/v1",
            "client": SimpleNamespace(), "async_client": sdk.chat.completions,
            "root_client": SimpleNamespace(), "root_async_client": sdk,
            "streaming": False, "cache": False, "max_retries": 0,
            "profile": {"tool_calling": True, "structured_output": True, "max_input_tokens": 8000},
            "extra_body": {"thinking": {"type": "enabled"}},
        })
        object.__setattr__(model, MODEL_RETRIES_ATTR, 0)
        yield model


def request(model, settings=None, context=None):
    messages = [HumanMessage("Launch a child to inspect tools.")]
    return ModelRequest(model=model, messages=messages, tools=[],
                        state={"messages": messages}, runtime=Runtime(context=context or {}),
                        model_settings=settings or {})


async def review(middleware, model_request):
    return await middleware._review_batch(model_request, [CALL], [CALL], {}, {})


class CaptureCallback(BaseCallbackHandler):
    def __init__(self):
        self.seen = []

    def on_chat_model_start(self, serialized, messages, *, run_id, tags=None, metadata=None, **kwargs):
        self.seen.append((tags, metadata))


@pytest.mark.parametrize("body_source", ["model", "model_kwargs", "invocation", "cleared"])
async def test_wire_choice_preserves_settings_parser_and_callbacks(tmp_path, body_source):
    payloads = []

    def handler(req):
        payloads.append(json.loads(req.content))
        return httpx.Response(200, json=completion(structured_message([verdict()])))

    async with deepseek_model(handler) as model:
        body = {"thinking": {"type": "enabled"}, "tool_choice": "required", "fixture": body_source}
        settings = {"tool_choice": {"type": "function", "function": {"name": "wrong"}}, "temperature": 0.2}
        if body_source == "model_kwargs":
            model.model_kwargs["extra_body"] = body
        elif body_source == "invocation":
            settings["extra_body"] = body
        else:
            model.extra_body = body
        if body_source == "cleared":
            settings["extra_body"] = None
        before = deepcopy((model.extra_body, model.model_kwargs, settings))
        middleware = AutoModeHITLMiddleware({}, worktree_root=tmp_path)
        view, label = await middleware._classifier_model(request(model))
        callback = CaptureCallback()
        result = await view.with_structured_output(AutoDecisionBatch).ainvoke(
            [HumanMessage("Synthetic review")],
            config={"callbacks": [callback], "tags": ["fixture"], "metadata": {"fixture": "retained"}},
            **settings,
        )
        assert label is None and getattr(view, MODEL_RETRIES_ATTR) == 0
        assert isinstance(result, AutoDecisionBatch) and result.decisions[0].decision == "allow"
        payload = payloads[-1]
        assert payload["tool_choice"] == "auto"
        assert payload["tools"][0]["function"]["name"] == "AutoDecisionBatch"
        assert payload["temperature"] == 0.2
        if body_source == "cleared":
            assert "thinking" not in payload and "fixture" not in payload
        else:
            assert payload["thinking"] == {"type": "enabled"} and payload["fixture"] == body_source
        assert callback.seen and "fixture" in callback.seen[-1][0]
        assert callback.seen[-1][1]["fixture"] == "retained"
        assert (model.extra_body, model.model_kwargs, settings) == before


@pytest.mark.parametrize("selection", ["inherited", "object", "configured", "runtime", "clear"])
async def test_classifier_selection_cache_and_settings(tmp_path, selection):
    payloads = []

    def handler(req):
        payloads.append(json.loads(req.content))
        return httpx.Response(200, json=completion(structured_message([verdict()])))

    async with deepseek_model(handler) as model:
        model.temperature = 0.4
        spec = "deepseek:deepseek-v4-flash"
        configured = model if selection == "object" else spec if selection in {"configured", "clear"} else None
        middleware = AutoModeHITLMiddleware({}, worktree_root=tmp_path, classifier_model=configured)
        middleware._classifier_model_cache[spec] = model
        context = ({"classifier_model": spec} if selection == "runtime" else
                   {"classifier_model": INHERIT_CLASSIFIER_MODEL} if selection == "clear" else {})
        inherited = selection in {"inherited", "clear"}
        primary = model if inherited else _ToolBindingFakeModel()
        req = request(primary, {"temperature": 0.7, "extra_body": {"thinking": {"type": "enabled"}, "fixture": "main"}}, context)
        for _ in range(2):
            result = await review(middleware, req)
            assert result.decisions[0].decision == "allow"
        assert middleware._classifier_model_cache[spec] is model
        assert all(p["tool_choice"] == "auto" and p["thinking"] == {"type": "enabled"} for p in payloads)
        assert all(p["temperature"] == (0.7 if inherited else 0.4) for p in payloads)
        assert all(("fixture" in p) == inherited for p in payloads)


async def test_other_providers_are_returned_unchanged(tmp_path):
    primary, classifier = _ToolBindingFakeModel(), _ToolBindingFakeModel()
    middleware = AutoModeHITLMiddleware({}, worktree_root=tmp_path)
    selected, label = await middleware._classifier_model(request(primary))
    assert selected is primary and label is None
    middleware = AutoModeHITLMiddleware({}, worktree_root=tmp_path, classifier_model=classifier)
    selected, _ = await middleware._classifier_model(request(primary))
    assert selected is classifier


async def test_classifier_and_primary_calls_do_not_share_overrides(tmp_path):
    payloads = []
    both_entered = asyncio.Event()

    async def handler(req):
        payload = json.loads(req.content)
        payloads.append(payload)
        if len(payloads) == 2:
            both_entered.set()
        await asyncio.wait_for(both_entered.wait(), 3)
        return httpx.Response(200, json=completion(structured_message([verdict()])))

    async with deepseek_model(handler) as model:
        model.extra_body["tool_choice"] = "required"
        middleware = AutoModeHITLMiddleware({}, worktree_root=tmp_path)
        await asyncio.gather(review(middleware, request(model)), model.ainvoke([HumanMessage("Primary conversation")]))
        assert {p["tool_choice"] for p in payloads} == {"auto", "required"}
        assert model.extra_body["tool_choice"] == "required"
        assert all(p["thinking"] == {"type": "enabled"} for p in payloads)


async def test_classifier_retries_and_timeout_remain_native(tmp_path, monkeypatch):
    from deepagents_code import model_retry

    monkeypatch.setattr(model_retry, "_retry_delay_seconds", lambda *args: 0)
    payloads = []
    block = False

    async def handler(req):
        payloads.append(json.loads(req.content))
        if block:
            await asyncio.Event().wait()
        if len(payloads) == 1:
            return httpx.Response(429, json={"error": {"message": "Synthetic rate limit", "type": "rate_limit_error"}})
        return httpx.Response(200, json=completion(structured_message([verdict()])))

    async with deepseek_model(handler) as model:
        object.__setattr__(model, MODEL_RETRIES_ATTR, 1)
        middleware = AutoModeHITLMiddleware({}, worktree_root=tmp_path, classifier_timeout_seconds=0.3)
        assert (await review(middleware, request(model))).decisions[0].decision == "allow"
        assert len(payloads) == 2 and all(p["tool_choice"] == "auto" for p in payloads)
        block = True
        with pytest.raises(_ClassifierDeadlineExceededError):
            await review(middleware, request(model))


@pytest.mark.parametrize("outcome", ["allow", "deny", "missing", "malformed", "empty", "wrong_id", "duplicate", "provider_error"])
@pytest.mark.parametrize("inherited", [False, True])
async def test_real_auto_gate_only_starts_child_after_valid_allow(tmp_path, outcome, inherited):
    payloads = []
    main_turn = 0

    def handler(req):
        nonlocal main_turn
        payload = json.loads(req.content)
        payloads.append(payload)
        classifier = [t["function"]["name"] for t in payload.get("tools", [])] == ["AutoDecisionBatch"]
        if not classifier:
            main_turn += 1
            message = ({"role": "assistant", "content": None, "tool_calls": [{
                "id": CALL["id"], "type": "function", "function": {
                    "name": CALL["name"], "arguments": json.dumps(CALL["args"]),
                },
            }]} if main_turn == 1 else {"role": "assistant", "content": "Parent done"})
            return httpx.Response(200, json=completion(message))
        # Match the provider's actual failure: forced choice plus thinking.
        if payload.get("tool_choice") != "auto" or outcome == "provider_error":
            return httpx.Response(400, json={"error": {"message": "Thinking mode does not support this tool_choice", "type": "invalid_request_error"}})
        choices = {"allow": [verdict()], "deny": [verdict("deny")], "malformed": [{"decision": "allow"}],
                   "empty": [], "wrong_id": [verdict(identifier="other")], "duplicate": [verdict(), verdict()]}
        message = ({"role": "assistant", "content": "No verdict"} if outcome == "missing"
                   else structured_message(choices[outcome]))
        return httpx.Response(200, json=completion(message))

    async with deepseek_model(handler) as model:
        primary = model if inherited else _ToolBindingFakeModel(messages=iter([
            AIMessage(content="", tool_calls=[CALL]), AIMessage("Parent done"),
        ]))
        child = _ToolBindingFakeModel(messages=iter([AIMessage("Child evidence")]))
        store = InMemoryStore()
        key = approval_mode_key("owner")
        store.put(APPROVAL_MODE_NAMESPACE, key, approval_mode_payload(mode="auto"))
        kwargs = dict(assistant_id="classifier-fixture", cwd=tmp_path, model=primary,
                      auto_mode_enabled=True, auto_approve=False, interactive=True,
                      enable_memory=False, enable_skills=False, enable_ask_user=False,
                      store=store, checkpointer=InMemorySaver(),
                      subagents=[dict(name="child", description="Child", model=child)])
        if not inherited:
            kwargs["auto_classifier_model"] = model
        async with await FactoryRuntime.create(agent_kwargs=kwargs, options=RuntimeOptions(background=True), workspace_id="work") as runtime:
            result = await runtime.ainvoke({"messages": [HumanMessage("Launch a child to inspect tools.")]},
                {"configurable": {"thread_id": "owner"}},
                context={"approval_mode": "auto", "approval_mode_key": key, "thread_id": "owner"})
            tool_result = next(m for m in result["messages"] if isinstance(m, ToolMessage) and m.tool_call_id == "submit")
            if outcome == "allow":
                assert json.loads(tool_result.content)["ok"]
                await asyncio.wait_for(runtime.background.wait("owner"), 5)
                assert runtime.background.list("owner")[0]["status"] == "completed"
            else:
                assert tool_result.status == "error" and "Auto denied" in tool_result.content
                assert not runtime.background.jobs
            classifier_payloads = [p for p in payloads if [t["function"]["name"] for t in p.get("tools", [])] == ["AutoDecisionBatch"]]
            assert len(classifier_payloads) == 1
            assert classifier_payloads[0]["tool_choice"] == "auto"
            assert classifier_payloads[0]["thinking"] == {"type": "enabled"}


async def test_repeated_missing_verdicts_require_human_approval(tmp_path):
    payloads = []

    def handler(req):
        payloads.append(json.loads(req.content))
        return httpx.Response(200, json=completion({"role": "assistant", "content": "No verdict"}))

    async with deepseek_model(handler) as classifier:
        messages = []
        for i in range(3):
            messages.extend([AIMessage(content="", tool_calls=[{**CALL, "id": f"submit-{i}"}]), AIMessage("Parent done")])
        primary = _ToolBindingFakeModel(messages=iter(messages))
        child = _ToolBindingFakeModel(messages=iter([AIMessage("Child evidence")]))
        store = InMemoryStore()
        key = approval_mode_key("owner")
        store.put(APPROVAL_MODE_NAMESPACE, key, approval_mode_payload(mode="auto"))
        kwargs = dict(assistant_id="classifier-fallback", cwd=tmp_path, model=primary,
                      auto_classifier_model=classifier, auto_mode_enabled=True,
                      auto_approve=False, interactive=True, enable_memory=False,
                      enable_skills=False, enable_ask_user=False, store=store,
                      checkpointer=InMemorySaver(), subagents=[dict(name="child", description="Child", model=child)])
        config = {"configurable": {"thread_id": "owner"}}
        context = {"approval_mode": "auto", "approval_mode_key": key, "thread_id": "owner"}
        async with await FactoryRuntime.create(agent_kwargs=kwargs, options=RuntimeOptions(background=True), workspace_id="work") as runtime:
            for i in range(2):
                result = await runtime.ainvoke({"messages": [HumanMessage("Launch a child.")]}, config, context=context)
                denied = next(m for m in result["messages"] if isinstance(m, ToolMessage) and m.tool_call_id == f"submit-{i}")
                assert denied.status == "error" and not runtime.background.jobs
            result = await runtime.ainvoke({"messages": [HumanMessage("Launch a child.")]}, config, context=context)
            assert result.get("__interrupt__") and not runtime.background.jobs
            assert len(payloads) == 2
            await runtime.ainvoke(Command(resume={"decisions": [{"type": "approve"}]}), config, context=context)
            await asyncio.wait_for(runtime.background.wait("owner"), 5)
            assert runtime.background.list("owner")[0]["status"] == "completed"
