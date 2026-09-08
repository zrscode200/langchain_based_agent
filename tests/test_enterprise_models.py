"""Offline enterprise gateway contracts against the current host retry boundary."""

from __future__ import annotations

import importlib
import json
import os
from copy import deepcopy
from pathlib import Path

import pytest
import requests
from langchain_core.exceptions import ModelError
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from pydantic import BaseModel, SecretStr
from urllib3.exceptions import ProtocolError, ReadTimeoutError


_REQUESTS_SESSION_REQUEST = requests.sessions.Session.request


@pytest.fixture
def adapters(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "enterprise" / "src"))

    def refuse_network(*args, **kwargs):
        pytest.fail("Enterprise model test attempted an unmocked HTTP request")

    monkeypatch.setattr(requests.sessions.Session, "request", refuse_network)
    return importlib.import_module("lc_factory_enterprise.merck_models")


@pytest.fixture(params=["MerckChatModel", "MerckAnthropicChatModel"])
def model_class(adapters, request):
    return getattr(adapters, request.param)


def _response(data=None, *, status=200, events=None, text=None):
    response = requests.Response()
    response.status_code = status
    response.url = "https://gateway.invalid/test"
    response._content = (text if text is not None else json.dumps(data or {})).encode()
    response._content_consumed = True
    response.headers["Content-Type"] = "application/json" if events is None else "text/event-stream"
    response.closed_count = 0

    def close():
        response.closed_count += 1

    response.close = close
    if events is not None:
        def lines(**kwargs):
            for event in events:
                if isinstance(event, BaseException):
                    raise event
                yield ("data: " + json.dumps(event, ensure_ascii=False)).encode("utf-8")
                yield b""

        response.iter_lines = lines
    return response


def _post_sequence(monkeypatch, adapters, *responses):
    queued = iter(responses)
    calls = []

    def post(url, **kwargs):
        calls.append((url, deepcopy(kwargs)))
        try:
            result = next(queued)
        except StopIteration:
            pytest.fail("Adapter made an unexpected extra HTTP request")
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(adapters.requests, "post", post)
    return calls


def _success(model_class, text="answer"):
    if model_class.__name__ == "MerckChatModel":
        return {"choices": [{"message": {"content": text}}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}}
    return {"content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": 2, "output_tokens": 3}, "stop_reason": "end_turn"}


def _instance(model_class, **kwargs):
    return model_class(model="display-alias", api_key="test-key", base_url="https://gateway.invalid/root", **kwargs)


def test_scoped_credentials_are_captured_without_process_fallback(model_class, monkeypatch):
    from deepagents_code.config import use_environment

    monkeypatch.setenv("GPT_API_Key", "process-key")
    monkeypatch.setenv("DEEPAGENTS_CODE_GPT_API_Key", "process-scoped-key")
    with use_environment({"GPT_API_Key": "workspace-key", "DEEPAGENTS_CODE_GPT_API_Key": "workspace-scoped-key"}):
        first = model_class(model="alias")
    with use_environment({"GPT_API_Key": "other-key"}):
        second = model_class(model="alias")
    assert first._headers["X-Merck-APIKey"] == "workspace-scoped-key"
    assert second._headers["X-Merck-APIKey"] == "other-key"
    assert "workspace-scoped-key" not in repr(first)
    assert os.environ["GPT_API_Key"] == "process-key"
    with use_environment({}), pytest.raises(ValueError, match="API key"):
        model_class(model="alias")


@pytest.mark.parametrize("explicit", [None, "", "   "])
def test_explicit_empty_credentials_do_not_fall_back(model_class, explicit):
    from deepagents_code.config import use_environment

    with use_environment({"GPT_API_Key": "valid-workspace-key"}), pytest.raises(ValueError, match="API key"):
        model_class(model="alias", api_key=explicit)
    with use_environment({}), pytest.raises(ValueError, match="API key"):
        model_class(model="alias", api_key=SecretStr(""))


def test_empty_scoped_key_does_not_fall_back_to_lower_priority_key(model_class):
    from deepagents_code.config import use_environment

    with use_environment({"GPT_API_Key": "lower-key", "DEEPAGENTS_CODE_GPT_API_Key": ""}):
        with pytest.raises(ValueError, match="API key"):
            model_class(model="alias")


@pytest.mark.parametrize("root_key", ["base_url", "api_root"])
def test_url_routing_uses_api_model_while_body_keeps_alias(
    adapters, model_class, monkeypatch, root_key,
):
    response = _response(_success(model_class))
    calls = _post_sequence(monkeypatch, adapters, response)
    model = model_class(model="display-alias", api_model="wire-id", api_key=SecretStr("explicit-key"),
                        **{root_key: "https://gateway.invalid/root/"})
    result = model.invoke([HumanMessage("Work")])
    assert result.text == "answer"
    assert len(calls) == 1 and response.closed_count == 1
    url, request = calls[0]
    assert url.startswith("https://gateway.invalid/root/wire-id")
    assert ("/chat/completions?" in url) is (model_class.__name__ == "MerckChatModel")
    assert request["json"]["model"] == "display-alias"
    assert request["headers"]["X-Merck-APIKey"] == "explicit-key"
    assert "base_url" not in request["json"] and "api_root" not in request["json"]
    assert model.max_retries == 0
    assert result.usage_metadata == {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5}


def test_conflicting_roots_are_rejected_but_trailing_slashes_are_equivalent(model_class):
    with pytest.raises(ValueError, match="base_url|api_root"):
        model_class(model="alias", api_key="test-key", base_url="https://first.invalid", api_root="https://second.invalid")
    model = model_class(model="alias", api_key="test-key", base_url="https://same.invalid/", api_root="https://same.invalid")
    assert model._url.startswith("https://same.invalid/alias")


class AnswerSchema(BaseModel):
    """A scored test result."""

    score: int


def test_tool_schema_conversion_and_explicit_none_choice(adapters, model_class, monkeypatch):
    response = _response(_success(model_class))
    calls = _post_sequence(monkeypatch, adapters, response)
    model = _instance(model_class)
    bound = model.bind_tools([AnswerSchema], tool_choice="none")
    bound.invoke([HumanMessage("Do not call a tool.")])
    payload = calls[0][1]["json"]
    if model_class.__name__ == "MerckChatModel":
        assert payload["tool_choice"] == "none"
        tool = payload["tools"][0]["function"]
        assert tool["name"] == "AnswerSchema"
        assert tool["parameters"]["properties"]["score"]["type"] == "integer"
    else:
        assert payload["tool_choice"] == {"type": "none"}
        tool = payload["tools"][0]
        assert tool["name"] == "AnswerSchema"
        assert tool["input_schema"]["properties"]["score"]["type"] == "integer"
    assert model.bound_tools == []


def test_structured_tool_results_parse_into_langchain_calls(adapters, model_class, monkeypatch):
    if model_class.__name__ == "MerckChatModel":
        body = {"choices": [{"message": {"content": "", "tool_calls": [{
            "id": "score-1", "type": "function", "function": {"name": "AnswerSchema", "arguments": '{"score":3}'},
        }]}}]}
    else:
        body = {"content": [{"type": "tool_use", "id": "score-1", "name": "AnswerSchema", "input": {"score": 3}}]}
    _post_sequence(monkeypatch, adapters, _response(body))
    message = _instance(model_class).bind_tools([AnswerSchema]).invoke([HumanMessage("Score")])
    assert message.tool_calls == [{"name": "AnswerSchema", "args": {"score": 3}, "id": "score-1", "type": "tool_call"}]


def test_langchain_structured_output_parser_uses_gateway_tool_conversion(adapters, model_class, monkeypatch):
    if model_class.__name__ == "MerckChatModel":
        body = {"choices": [{"message": {"content": "", "tool_calls": [{
            "id": "score-1", "type": "function", "function": {"name": "AnswerSchema", "arguments": '{"score":3}'},
        }]}}]}
    else:
        body = {"content": [{"type": "tool_use", "id": "score-1", "name": "AnswerSchema", "input": {"score": 3}}]}
    calls = _post_sequence(monkeypatch, adapters, _response(body))
    result = _instance(model_class).with_structured_output(AnswerSchema).invoke([HumanMessage("Score")])
    assert isinstance(result, AnswerSchema) and result.score == 3
    assert "ls_structured_output_format" not in calls[0][1]["json"]
    expected_choice = "required" if model_class.__name__ == "MerckChatModel" else {"type": "any"}
    assert calls[0][1]["json"]["tool_choice"] == expected_choice


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_compiled_tool_strategy_uses_provider_valid_required_choice(
    adapters, model_class, monkeypatch, asynchronous,
):
    from langchain.agents import create_agent
    from langchain.agents.structured_output import ToolStrategy

    if model_class.__name__ == "MerckChatModel":
        body = {"choices": [{"message": {"content": "", "tool_calls": [{
            "id": "score-1", "type": "function", "function": {"name": "AnswerSchema", "arguments": '{"score":3}'},
        }]}}]}
        expected_choice = "required"
    else:
        body = {"content": [{"type": "tool_use", "id": "score-1", "name": "AnswerSchema", "input": {"score": 3}}]}
        expected_choice = {"type": "any"}
    response = _response(body)
    calls = _post_sequence(monkeypatch, adapters, response)
    model_options = {} if model_class.__name__ == "MerckChatModel" else {"thinking_effort": "high"}
    model = _instance(model_class, **model_options)
    graph = create_agent(model=model, tools=[], response_format=ToolStrategy(AnswerSchema))
    inputs = {"messages": [HumanMessage("Score the result.")]}
    result = await graph.ainvoke(inputs) if asynchronous else graph.invoke(inputs)

    assert result["structured_response"] == AnswerSchema(score=3)
    assert len(calls) == 1 and response.closed_count == 1
    assert calls[0][1]["json"]["tool_choice"] == expected_choice
    assert "response_format" not in calls[0][1]["json"]
    if model_class.__name__ == "MerckAnthropicChatModel":
        assert "thinking" not in calls[0][1]["json"]
        assert "effort" not in calls[0][1]["json"].get("output_config", {})
        assert model.thinking_effort == "high"


def test_gpt_named_tool_choice_uses_function_choice_object(adapters, monkeypatch):
    response = _response(_success(adapters.MerckChatModel))
    calls = _post_sequence(monkeypatch, adapters, response)
    _instance(adapters.MerckChatModel).bind_tools(
        [AnswerSchema], tool_choice="AnswerSchema",
    ).invoke([HumanMessage("Call AnswerSchema.")])

    assert calls[0][1]["json"]["tool_choice"] == {
        "type": "function", "function": {"name": "AnswerSchema"},
    }
    assert len(calls) == 1 and response.closed_count == 1


@pytest.mark.parametrize("source", ["thinking_effort", "model_kwargs", "invoke_kwargs"])
@pytest.mark.parametrize("forced_choice", ["any", "AnswerSchema"])
def test_claude_forced_tools_disable_effective_thinking_without_mutation(
    adapters, monkeypatch, source, forced_choice,
):
    preserved_format = {"type": "json_schema", "schema": {"type": "object"}}
    output_config = {"effort": "high", "format": preserved_format}
    if source == "thinking_effort":
        model_options = {"thinking_effort": "high", "model_kwargs": {"output_config": output_config}}
        invoke_options = {}
        expected_thinking = {"type": "adaptive", "display": "summarized"}
    else:
        expected_thinking = (
            {"type": "adaptive", "display": "summarized"} if source == "model_kwargs"
            else {"type": "enabled", "budget_tokens": 1024}
        )
        options = {"thinking": expected_thinking, "output_config": output_config}
        model_options = {"model_kwargs": options} if source == "model_kwargs" else {}
        invoke_options = options if source == "invoke_kwargs" else {}
    original_options = deepcopy((model_options, invoke_options))
    model = _instance(adapters.MerckAnthropicChatModel, **model_options)
    original_model_kwargs = deepcopy(model.model_kwargs)
    responses = [_response(_success(adapters.MerckAnthropicChatModel)) for _ in range(3)]
    calls = _post_sequence(monkeypatch, adapters, *responses)

    for choice in (forced_choice, "auto", "none"):
        model.bind_tools([AnswerSchema], tool_choice=choice).invoke([HumanMessage("Work")], **invoke_options)

    forced_payload = calls[0][1]["json"]
    assert forced_payload["tool_choice"] == (
        {"type": "any"} if forced_choice == "any" else {"type": "tool", "name": "AnswerSchema"}
    )
    assert "thinking" not in forced_payload
    assert forced_payload["output_config"] == {"format": preserved_format}
    for call, choice in zip(calls[1:], ("auto", "none"), strict=True):
        payload = call[1]["json"]
        assert payload["tool_choice"] == {"type": choice}
        assert payload["thinking"] == expected_thinking
        assert payload["output_config"] == output_config
    assert (model_options, invoke_options) == original_options
    assert model.model_kwargs == original_model_kwargs
    assert model.tool_choice is None and model.bound_tools == []
    assert len(calls) == 3 and all(response.closed_count == 1 for response in responses)


def test_legacy_adapter_names_and_og_shim_resolve_to_same_classes(adapters):
    shim = importlib.import_module("lc_factory.merck_models")
    assert adapters.MerckChatOpenAI is adapters.MerckChatModel
    assert adapters.MerckChatAnthropic is adapters.MerckAnthropicChatModel
    for name in shim.__all__:
        assert getattr(shim, name) is getattr(adapters, name)


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_gpt_stream_api_keeps_its_single_blocking_response_fallback(adapters, monkeypatch, asynchronous):
    response = _response(_success(adapters.MerckChatModel))
    calls = _post_sequence(monkeypatch, adapters, response)
    model = _instance(adapters.MerckChatModel)
    if asynchronous:
        chunks = [chunk async for chunk in model.astream([HumanMessage("Work")])]
    else:
        chunks = list(model.stream([HumanMessage("Work")]))
    assert len(chunks) == 1 and chunks[0].text == "answer"
    assert len(calls) == 1 and calls[0][1]["stream"] is True
    assert not calls[0][1]["json"].get("stream", False)
    assert response.closed_count == 1


@pytest.mark.parametrize("status, retryable", [(400, False), (401, False), (408, True), (429, True), (500, True), (529, True)])
def test_http_errors_have_authoritative_retryability_and_one_adapter_attempt(
    adapters, model_class, monkeypatch, status, retryable,
):
    from deepagents_code.model_retry import _is_retryable_model_error

    response = _response(status=status, text="gateway failure")
    calls = _post_sequence(monkeypatch, adapters, response)
    with pytest.raises(ModelError) as error:
        _instance(model_class).invoke([HumanMessage("Work")])
    assert isinstance(error.value, requests.HTTPError)
    assert error.value.is_retryable is retryable
    assert _is_retryable_model_error(error.value) is retryable
    assert len(calls) == 1 and response.closed_count == 1


@pytest.mark.parametrize("failure, retryable", [
    (requests.ConnectionError("not connected"), True),
    (requests.ReadTimeout("request acceptance unknown"), False),
])
def test_transport_errors_override_broad_host_retry_heuristics(
    adapters, model_class, monkeypatch, failure, retryable,
):
    from deepagents_code.model_retry import _is_retryable_model_error

    calls = _post_sequence(monkeypatch, adapters, failure)
    with pytest.raises(ModelError) as error:
        _instance(model_class).invoke([HumanMessage("Work")])
    assert error.value.is_retryable is retryable
    assert _is_retryable_model_error(error.value) is retryable
    assert len(calls) == 1


def test_nonstream_response_closes_even_if_json_is_invalid(adapters, model_class, monkeypatch):
    response = _response(text="not-json")
    _post_sequence(monkeypatch, adapters, response)
    with pytest.raises(ModelError) as error:
        _instance(model_class).invoke([HumanMessage("Work")])
    assert error.value.is_retryable is False
    assert isinstance(error.value.__cause__, requests.exceptions.JSONDecodeError)
    assert response.closed_count == 1


def test_anthropic_native_reasoning_and_tool_replay_filtering(adapters, monkeypatch):
    body = {"content": [
        {"type": "thinking", "thinking": "consider evidence", "signature": "opaque-signature"},
        {"type": "redacted_thinking", "data": "opaque-data"},
        {"type": "text", "text": "answer"},
        {"type": "tool_use", "id": "score-1", "name": "AnswerSchema", "input": {"score": 3}},
    ], "usage": {"input_tokens": 2, "output_tokens": 3}}
    second = _response(_success(adapters.MerckAnthropicChatModel))
    calls = _post_sequence(monkeypatch, adapters, _response(body), second)
    model = _instance(adapters.MerckAnthropicChatModel)
    message = model.invoke([HumanMessage("Work")])
    blocks = message.content_blocks
    assert any(b["type"] == "reasoning" and "consider evidence" in b["reasoning"] for b in blocks)
    assert message.text == "answer"
    assert "opaque-data" not in json.dumps(message.content)
    model.invoke([SystemMessage("Follow the task"), HumanMessage("Work"), message,
                  ToolMessage(content="tool evidence", tool_call_id="score-1")])
    payload = calls[1][1]["json"]
    assistant = next(m for m in payload["messages"] if m["role"] == "assistant")
    assert {block["type"] for block in assistant["content"]} == {"text", "tool_use"}
    assert len([b for b in assistant["content"] if b["type"] == "tool_use"]) == 1
    assert payload["system"] == "Follow the task"
    assert payload["messages"][-1]["content"][0]["type"] == "tool_result"


@pytest.mark.parametrize("legacy_blocks", [False, True])
def test_claude_reasoning_is_filtered_when_history_switches_to_gpt(adapters, monkeypatch, legacy_blocks):
    body = {"content": [
        {"type": "thinking", "thinking": "private deliberation", "signature": "opaque-signature"},
        {"type": "redacted_thinking", "data": "opaque-data"},
        {"type": "text", "text": "visible answer"},
        {"type": "tool_use", "id": "score-1", "name": "AnswerSchema", "input": {"score": 3}},
    ]}
    claude_response = _response(body)
    gpt_response = _response(_success(adapters.MerckChatModel))
    calls = _post_sequence(monkeypatch, adapters, claude_response, gpt_response)
    history = _instance(adapters.MerckAnthropicChatModel).invoke([HumanMessage("Work")])
    assert any(block["type"] == "reasoning" for block in history.content_blocks)
    if legacy_blocks:
        # A restored conversation can contain old provider-native blocks in
        # addition to the current parser's standard reasoning content blocks.
        history.content.extend([
            {"type": "thinking", "thinking": "legacy deliberation", "signature": "legacy-signature"},
            {"type": "redacted_thinking", "data": "legacy-data"},
        ])
    original_history = history.model_dump()
    _instance(adapters.MerckChatModel).invoke([
        HumanMessage("Work"), history, ToolMessage(content="tool evidence", tool_call_id="score-1"),
    ])

    payload = calls[1][1]["json"]
    assistant = next(message for message in payload["messages"] if message["role"] == "assistant")
    assert assistant["content"] == [{"type": "text", "text": "visible answer"}]
    assert len(assistant["tool_calls"]) == 1
    tool_call = assistant["tool_calls"][0]
    assert tool_call["id"] == "score-1" and tool_call["function"]["name"] == "AnswerSchema"
    assert json.loads(tool_call["function"]["arguments"]) == {"score": 3}
    assert payload["messages"][-1] == {"role": "tool", "content": "tool evidence", "tool_call_id": "score-1"}
    assert history.model_dump() == original_history
    assert len(calls) == 2 and claude_response.closed_count == gpt_response.closed_count == 1


def _stream_events():
    return [
        {"type": "message_start", "message": {"usage": {"input_tokens": 4}}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "consider "}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "evidence"}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "17 × 23"}},
        {"type": "content_block_start", "index": 2, "content_block": {"type": "tool_use", "name": "AnswerSchema", "id": "score-1"}},
        {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": '{"score":'}},
        {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": '3}'}},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 5}},
        {"type": "message_stop"},
    ]


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_anthropic_sse_preserves_utf8_reasoning_tool_chunks_and_closes(adapters, monkeypatch, asynchronous):
    response = _response(events=_stream_events())
    calls = _post_sequence(monkeypatch, adapters, response)
    model = _instance(adapters.MerckAnthropicChatModel)
    if asynchronous:
        chunks = [chunk async for chunk in model.astream([HumanMessage("Work")])]
    else:
        chunks = list(model.stream([HumanMessage("Work")]))
    combined = chunks[0]
    for chunk in chunks[1:]:
        combined += chunk
    reasoning = "".join(block.get("reasoning", "") for block in combined.content_blocks if block["type"] == "reasoning")
    assert reasoning == "consider evidence"
    assert combined.text == "17 × 23"
    assert combined.tool_calls == [{"name": "AnswerSchema", "args": {"score": 3}, "id": "score-1", "type": "tool_call"}]
    assert combined.usage_metadata == {"input_tokens": 4, "output_tokens": 5, "total_tokens": 9}
    assert calls[0][1]["stream"] is True and calls[0][1]["json"]["stream"] is True
    assert len(calls) == 1 and response.closed_count == 1


def test_closing_anthropic_stream_early_closes_accepted_response(adapters, monkeypatch):
    response = _response(events=_stream_events())
    calls = _post_sequence(monkeypatch, adapters, response)
    stream = _instance(adapters.MerckAnthropicChatModel).stream([HumanMessage("Work")])
    next(stream)
    stream.close()
    assert response.closed_count == 1 and len(calls) == 1


@pytest.mark.parametrize("phase", ["before-output", "after-output"])
@pytest.mark.parametrize("failure", ["eof", "connection", "read-timeout", "error-event"])
def test_accepted_stream_failure_is_never_reopened_or_host_retried(adapters, monkeypatch, phase, failure):
    from deepagents_code.config import MODEL_RETRIES_ATTR
    from deepagents_code.model_retry import retry_model_call, _is_retryable_model_error

    events = [] if phase == "before-output" else [_stream_events()[3]]
    if failure == "connection":
        events.append(requests.ConnectionError("stream transport failed"))
    elif failure == "read-timeout":
        events.append(requests.ReadTimeout("stream stalled"))
    elif failure == "error-event":
        events.append({"type": "error", "error": {"type": "overloaded_error", "message": "try again"}})
    response = _response(events=events)
    calls = _post_sequence(monkeypatch, adapters, response)
    model = _instance(adapters.MerckAnthropicChatModel)
    object.__setattr__(model, MODEL_RETRIES_ATTR, 3)
    with pytest.raises(ModelError) as error:
        retry_model_call(model, lambda: list(model.stream([HumanMessage("Work")])))
    assert error.value.is_retryable is False and _is_retryable_model_error(error.value) is False
    assert len(calls) == 1 and response.closed_count == 1


def _host_config(home, class_name):
    from deepagents_code.configuration.service import invalidate_config_sources
    from deepagents_code.model_config import clear_caches

    path = home / ".deepagents" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'''[models.providers.enterprise]
models = ["display-alias"]
class_path = "lc_factory_enterprise.merck_models:{class_name}"
api_key_env = "ENTERPRISE_TEST_KEY"
base_url = "https://gateway.invalid/root"
[models.providers.enterprise.params]
api_model = "wire-id"
max_retries = 9
[retries.enterprise]
max_retries = 2
param = "max_retries"
''')
    clear_caches()
    invalidate_config_sources()


def test_actual_host_creation_owns_retry_budget_and_request_count(
    adapters, model_class, monkeypatch, isolated_environment,
):
    from deepagents_code import model_retry
    from deepagents_code.config import MODEL_RETRIES_ATTR, create_model, use_environment

    _host_config(isolated_environment, model_class.__name__)
    first, second, success = _response(status=429), _response(status=503), _response(_success(model_class))
    calls = _post_sequence(monkeypatch, adapters, first, second, success)
    monkeypatch.setattr(model_retry.time, "sleep", lambda seconds: None)
    with use_environment({"ENTERPRISE_TEST_KEY": "workspace-host-key"}):
        result = create_model("enterprise:display-alias")
    assert result.model.max_retries == 0
    assert result.model_retries == getattr(result.model, MODEL_RETRIES_ATTR) == 2
    response = model_retry.retry_model_call(result.model, lambda: result.model.invoke([HumanMessage("Work")]))
    assert response.text == "answer"
    assert len(calls) == 3
    assert all(call[1]["headers"]["X-Merck-APIKey"] == "workspace-host-key" for call in calls)
    assert all(item.closed_count == 1 for item in (first, second, success))


@pytest.mark.parametrize("failure", ["read-timeout", "protocol-error"])
def test_real_requests_accepted_body_failure_is_closed_without_host_replay(
    adapters, model_class, monkeypatch, isolated_environment, failure,
):
    """Exercise Requests' real body-error wrapping, below Session.send.

    With transport stream=False, Session.send consumes the body before the
    adapter sees its accepted response. In particular, urllib3 ReadTimeoutError
    becomes requests.ConnectionError and can incorrectly trigger host retries.
    """
    from deepagents_code import model_retry
    from deepagents_code.config import MODEL_RETRIES_ATTR, create_model, use_environment

    class InterruptedBody:
        def __init__(self):
            self.read_count = 0
            self.close_count = 0
            self.release_count = 0

        def stream(self, *args, **kwargs):
            self.read_count += 1
            if failure == "read-timeout":
                raise ReadTimeoutError(None, "https://gateway.invalid/test", "accepted body stalled")
            raise ProtocolError("accepted body interrupted")
            yield b""  # Keep the failure at iteration time, as in urllib3.

        def close(self):
            self.close_count += 1

        def release_conn(self):
            self.release_count += 1

    calls = []

    def send(adapter, request, **kwargs):
        response = requests.Response()
        response.status_code = 200
        response.url = request.url
        response.request = request
        response.headers["Content-Type"] = "application/json"
        response.raw = InterruptedBody()
        calls.append((request, kwargs, response))
        return response

    # Restore only Session.request; every HTTPAdapter.send remains intercepted,
    # so this uses real request preparation/body consumption without networking.
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    monkeypatch.setattr(requests.sessions.Session, "request", _REQUESTS_SESSION_REQUEST)
    monkeypatch.setattr(model_retry.time, "sleep", lambda seconds: None)
    _host_config(isolated_environment, model_class.__name__)
    with use_environment({"ENTERPRISE_TEST_KEY": "workspace-host-key"}):
        result = create_model("enterprise:display-alias")
    assert result.model.max_retries == 0
    assert result.model_retries == getattr(result.model, MODEL_RETRIES_ATTR) == 2

    with pytest.raises(ModelError) as error:
        model_retry.retry_model_call(result.model, lambda: result.model.invoke([HumanMessage("Work")]))

    assert error.value.is_retryable is False
    assert model_retry._is_retryable_model_error(error.value) is False
    expected_cause = requests.ConnectionError if failure == "read-timeout" else requests.exceptions.ChunkedEncodingError
    assert isinstance(error.value.__cause__, expected_cause)
    assert len(calls) == 1
    request, transport, response = calls[0]
    assert transport["stream"] is True
    assert not json.loads(request.body).get("stream", False)
    assert request.headers["X-Merck-APIKey"] == "workspace-host-key"
    assert response.raw.read_count == 1
    assert response.raw.close_count == response.raw.release_count == 1
