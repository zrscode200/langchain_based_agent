"""Settled delegation through real interpreter and approval boundaries."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from xml.etree import ElementTree

import pytest
from deepagents_code._fake_models import _ToolBindingFakeModel
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.runtime import ExecutionInfo
from langgraph.types import Command

from lc_factory.assembly import create_factory_agent
from lc_factory.interpreter_dispatch import (
    MAX_RESULT_CHARS, _ReplayStableDispatchIds, build_settled_dispatch_tool,
)
from lc_factory.upstream import ToolRuntime


def _call(name, args, identifier):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": identifier}])


def _model(*messages):
    return _ToolBindingFakeModel(messages=iter(messages))


def _worker(name="worker", **kwargs):
    return dict(name=name, description=f"Work assigned to {name}.", mode="isolated", **kwargs)


def _options(tmp_path, **kwargs):
    return dict(assistant_id="settled-test", cwd=tmp_path, enable_memory=False,
                enable_skills=False, enable_ask_user=False, enable_shell=False,
                interactive=False, auto_approve=True, enable_settled_dispatch=True,
                enable_interpreter=True, **kwargs)


async def _run(graph, inputs=None, *, asynchronous=True, config=None, context=None):
    inputs = {"messages": [{"role": "user", "content": "Delegate the work."}]} if inputs is None else inputs
    events, result = [], None
    if asynchronous:
        async for mode, chunk in graph.astream(inputs, config, context=context, stream_mode=["values", "custom"]):
            if mode == "values":
                result = chunk
            elif mode == "custom":
                events.append(chunk)
    else:
        for mode, chunk in graph.stream(inputs, config, context=context, stream_mode=["values", "custom"]):
            if mode == "values":
                result = chunk
            elif mode == "custom":
                events.append(chunk)
    return result, events


def _js_value(result, identifier="eval-1"):
    message = next(m for m in result["messages"] if isinstance(m, ToolMessage) and m.tool_call_id == identifier)
    document = ElementTree.fromstring(f"<eval>{message.content}</eval>")
    outcome = document.find("result")
    assert outcome is not None, message.content
    return json.loads(outcome.text)


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_normal_tool_dispatch_works_without_interpreter(tmp_path, asynchronous):
    options = _options(
        tmp_path, model=_model(_call("task_settled", {"description": "Finish work", "subagentType": "worker"}, "delegate"),
                              AIMessage("parent completed")),
        subagents=[_worker(model=_model(AIMessage("worker completed")))],
    )
    options["enable_interpreter"] = False
    graph, _ = create_factory_agent(**options)
    result, _ = await _run(graph, asynchronous=asynchronous)
    assert result["messages"][-1].content == "parent completed"
    output = next(m for m in result["messages"] if isinstance(m, ToolMessage) and m.tool_call_id == "delegate")
    assert json.loads(output.content) == {"ok": True, "value": "worker completed"}


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_parallel_eval_calls_keep_their_own_lifecycle_identity(tmp_path, asynchronous):
    class RoutingModel(_ToolBindingFakeModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            if isinstance(messages[-1], ToolMessage):
                response = AIMessage("parent completed")
            else:
                name = messages[-1].content
                dispatch = {"description": name, "subagentType": name}
                response = _call("js_eval", {
                    "code": f"JSON.stringify(await tools.taskSettled({json.dumps(dispatch)}))",
                }, f"eval-{name}")
            return ChatResult(generations=[ChatGeneration(message=response)])

    graph, _ = create_factory_agent(**_options(
        tmp_path, model=RoutingModel(),
        subagents=[_worker(name, model=_model(AIMessage(f"{name} completed"))) for name in ("alpha", "beta")],
    ))
    # One REPL slot intentionally refuses overlapping evals. Independent graph
    # invocations use distinct slots but share this generation's middleware and
    # dispatch helper, exercising the mutable cross-call scope hazard.
    async def run(name):
        inputs = {"messages": [{"role": "user", "content": name}]}
        if asynchronous:
            return await _run(graph, inputs)
        return await asyncio.to_thread(lambda: asyncio.run(_run(graph, inputs, asynchronous=False)))

    completed = await asyncio.gather(*(run(name) for name in ("alpha", "beta")))
    for name, (result, _) in zip(("alpha", "beta"), completed, strict=True):
        assert _js_value(result, f"eval-{name}") == {"ok": True, "value": f"{name} completed"}
    events = [event for _, events in completed for event in events]
    lifecycle = [e for e in events if isinstance(e, dict) and e.get("type") == "subagent"]
    starts = [e for e in lifecycle if e.get("phase") == "start"]
    assert {e["subagent_type"]: e["eval_id"] for e in starts} == {"alpha": "eval-alpha", "beta": "eval-beta"}
    assert len({e["id"] for e in starts}) == 2
    assert {e["eval_id"] for e in lifecycle if e["phase"] == "phase_complete"} == {"eval-alpha", "eval-beta"}


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_quickjs_mixed_outcomes_remain_readable_and_complete_per_eval(tmp_path, asynchronous):
    class BrokenModel(_ToolBindingFakeModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            raise ValueError("worker failed deliberately")

    code = '''JSON.stringify(await Promise.all([
      tools.taskSettled({description:"Find evidence",subagentType:"good",label:"good work"}),
      tools.taskSettled({description:"Broken work",subagentType:"broken"}),
      tools.taskSettled({description:"Missing work",subagentType:"unknown"})
    ]))'''
    graph, _ = create_factory_agent(**_options(
        tmp_path, model=_model(_call("js_eval", {"code": code}, "eval-1"), AIMessage("parent completed")),
        subagents=[_worker("good", model=_model(AIMessage("verified evidence"))),
                   _worker("broken", model=BrokenModel())],
    ))
    result, events = await _run(graph, asynchronous=asynchronous)
    assert result["messages"][-1].content == "parent completed"
    success, broken, unknown = _js_value(result)
    assert success == {"ok": True, "value": "verified evidence"}
    assert broken["ok"] is False and "worker failed deliberately" in broken["error"]["message"]
    assert unknown["ok"] is False and unknown["error"]["type"] == "LookupError"
    lifecycle = [e for e in events if isinstance(e, dict) and e.get("type") == "subagent"]
    starts = [e for e in lifecycle if e.get("phase") == "start"]
    assert len(starts) == 2
    assert {e["eval_id"] for e in starts} == {"eval-1"}
    assert len({e["id"] for e in starts}) == 2
    by_id = {e["id"]: e["phase"] for e in lifecycle if e.get("phase") in {"complete", "error"}}
    assert by_id[starts[0]["id"]] == ("complete" if starts[0]["subagent_type"] == "good" else "error")
    assert sorted(by_id.values()) == ["complete", "error"]
    assert [e for e in lifecycle if e.get("phase") == "phase_complete"] == [
        {"type": "subagent", "phase": "phase_complete", "eval_id": "eval-1"},
    ]


_SCHEMA = {"title": "ScoredResult", "type": "object", "required": ["score"],
           "properties": {"score": {"type": "integer", "minimum": 1}}, "additionalProperties": False}


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("schema_source", ["requested", "static"])
@pytest.mark.parametrize("valid", [False, True])
async def test_schema_success_requires_valid_values_and_error_has_no_complete_event(
    tmp_path, asynchronous, schema_source, valid,
):
    arguments = {"description": "Score the evidence", "subagentType": "worker"}
    worker_kwargs = {}
    if schema_source == "requested":
        arguments["responseSchema"] = _SCHEMA
    else:
        worker_kwargs["response_format"] = _SCHEMA
    value = {"score": 2 if valid else "two"}
    code = f"JSON.stringify(await tools.taskSettled({json.dumps(arguments)}))"
    graph, _ = create_factory_agent(**_options(
        tmp_path, model=_model(_call("js_eval", {"code": code}, "eval-1"), AIMessage("parent completed")),
        subagents=[_worker(model=_model(_call("ScoredResult", value, "score-1")), **worker_kwargs)],
    ))
    result, events = await _run(graph, asynchronous=asynchronous)
    outcome = _js_value(result)
    assert outcome["ok"] is valid
    if valid:
        # Without a requested schema the SDK's ordinary task result is textual.
        structured = outcome["value"]
        if isinstance(structured, str):
            structured = json.loads(structured)
        assert structured == {"score": 2}
    phases = [e["phase"] for e in events if isinstance(e, dict) and e.get("type") == "subagent"]
    assert phases.count("start") == 1
    assert phases.count("complete") == int(valid)
    assert phases.count("error") == int(not valid)
    assert phases.count("phase_complete") == 1


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_requested_schema_cannot_weaken_declared_child_schema(tmp_path, asynchronous):
    weaker = {"title": "RequestedResult", "type": "object", "properties": {"score": {"type": "string"}}}
    arguments = {"description": "Score the evidence", "subagentType": "worker", "responseSchema": weaker}
    code = f"JSON.stringify(await tools.taskSettled({json.dumps(arguments)}))"
    graph, _ = create_factory_agent(**_options(
        tmp_path, model=_model(_call("js_eval", {"code": code}, "eval-1"), AIMessage("parent completed")),
        subagents=[_worker(response_format=_SCHEMA, model=_model(_call("RequestedResult", {"score": "two"}, "score-1")))],
    ))
    result, events = await _run(graph, asynchronous=asynchronous)
    outcome = _js_value(result)
    assert outcome["ok"] is False
    assert "invalid structured result" in outcome["error"]["message"]
    phases = [e["phase"] for e in events if isinstance(e, dict) and e.get("type") == "subagent"]
    assert phases == ["start", "error", "phase_complete"]


async def test_native_helper_keeps_parent_and_child_approval_boundaries(tmp_path):
    target = tmp_path / "approved-native.txt"
    options = _options(
        tmp_path, model=_model(_call("task_settled", {"description": "Write the file", "subagentType": "writer"}, "delegate"),
                              AIMessage("parent completed")),
        checkpointer=InMemorySaver(),
        subagents=[_worker("writer", model=_model(
            _call("write_file", {"file_path": str(target), "content": "approved"}, "write-1"),
            AIMessage("writer completed")))],
    )
    options.update(enable_interpreter=False, auto_approve=False, interactive=True)
    graph, _ = create_factory_agent(**options)
    config = {"configurable": {"thread_id": "native-approval"}}
    first, events = await _run(graph, config=config)
    assert first.get("__interrupt__") and not target.exists()
    assert not any(e.get("type") == "subagent" for e in events if isinstance(e, dict))
    approval = Command(resume={"decisions": [{"type": "approve"}]})
    second, _ = await _run(graph, approval, config=config)
    assert second.get("__interrupt__") and not target.exists()
    final, _ = await _run(graph, approval, config=config)
    assert not final.get("__interrupt__")
    assert target.read_text() == "approved"
    assert final["messages"][-1].content == "parent completed"


@pytest.mark.parametrize("permitted", [False, True])
async def test_child_policy_controls_helper_exposure_and_recursion_is_refused(tmp_path, permitted):
    observed = []

    class ChildMessages(AgentMiddleware):
        def before_model(self, state, runtime):
            observed.extend(m for m in state["messages"] if isinstance(m, ToolMessage))

    code = '''JSON.stringify({kind:typeof tools.taskSettled,
      outcome:typeof tools.taskSettled === "function"
        ? await tools.taskSettled({description:"Recurse",subagentType:"leaf"}) : null})'''
    child = dict(name="child", description="Inspect child delegation", mode="fork",
                 model=_model(_call("js_eval", {"code": code}, "child-eval"), AIMessage("child completed")),
                 middleware=[ChildMessages()])
    graph, _ = create_factory_agent(**_options(
        tmp_path, model=_model(_call("task", {"description": "Inspect delegation", "subagent_type": "child"}, "delegate"),
                              AIMessage("parent completed")),
        subagents=[child, _worker("leaf", model=_model())],
        subagent_policy={"child": {"tools": ["js_eval", *(["task_settled"] if permitted else [])]}},
    ))
    result, _ = await _run(graph)
    assert result["messages"][-1].content == "parent completed"
    child_result = _js_value({"messages": observed}, "child-eval")
    assert child_result["kind"] == ("function" if permitted else "undefined")
    if permitted:
        assert child_result["outcome"]["ok"] is False
        assert child_result["outcome"]["error"]["type"] == "DelegationError"
    else:
        assert child_result["outcome"] is None


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_js_cannot_catch_approval_interrupt_and_approval_resumes_same_dispatch(tmp_path, asynchronous):
    target = tmp_path / "approved.txt"
    code = '''JSON.stringify(await (async () => {
      try { return await tools.taskSettled({description:"Write the approved file",subagentType:"writer"}); }
      catch (error) { return {caught:true}; }
    })())'''
    options = _options(
        tmp_path, model=_model(_call("js_eval", {"code": code}, "approval-eval"), AIMessage("parent completed")),
        checkpointer=InMemorySaver(),
        subagents=[_worker("writer", model=_model(
            _call("write_file", {"file_path": str(target), "content": "approved"}, "write-1"),
            AIMessage("writer completed")))],
    )
    options.update(auto_approve=False, interactive=True)
    graph, _ = create_factory_agent(**options)
    config = {"configurable": {"thread_id": "approval-thread"}}
    first, first_events = await _run(graph, asynchronous=asynchronous, config=config)
    assert first.get("__interrupt__"), first
    assert not target.exists()
    starts = [e for e in first_events if isinstance(e, dict) and e.get("type") == "subagent" and e.get("phase") == "start"]
    assert len(starts) == 1, first_events
    assert not any(e.get("phase") in {"complete", "phase_complete"}
                   for e in first_events if isinstance(e, dict) and e.get("type") == "subagent")
    final, final_events = await _run(graph, Command(resume={"decisions": [{"type": "approve"}]}),
                                     asynchronous=asynchronous, config=config)
    assert not final.get("__interrupt__")
    assert target.read_text() == "approved"
    assert final["messages"][-1].content == "parent completed"
    assert _js_value(final, "approval-eval") == {"ok": True, "value": "writer completed"}
    resumed_starts = [e for e in final_events if isinstance(e, dict) and e.get("type") == "subagent" and e.get("phase") == "start"]
    assert resumed_starts and resumed_starts[0]["id"] == starts[0]["id"]


@pytest.mark.parametrize("dispatcher", ["tools.taskSettled", "task"])
@pytest.mark.parametrize("live_mode", ["auto", "yolo"])
@pytest.mark.parametrize("child_mode", ["isolated", "fork"])
async def test_live_auto_js_delegation_keeps_child_gate_and_live_yolo_bypasses(
    tmp_path, dispatcher, live_mode, child_mode,
):
    from deepagents_code.approval_mode import (
        APPROVAL_MODE_NAMESPACE, approval_mode_key, approval_mode_payload,
    )
    from langgraph.store.memory import InMemoryStore
    from lc_factory.upstream import CLIContextSchema

    target = tmp_path / "live-approved.txt"
    store = InMemoryStore()
    session = "live-mode-session"
    key = approval_mode_key(session)
    store.put(APPROVAL_MODE_NAMESPACE, key, approval_mode_payload(mode=live_mode))
    # Keeping context at Auto even for the YOLO control proves the actual live
    # store is consulted; a hard-coded Manual child would fail that control.
    context = CLIContextSchema(approval_mode="auto", approval_mode_key=key, thread_id=session)
    code = f'''JSON.stringify(await {dispatcher}({{
      description:"Write the file only when permitted",subagentType:"writer"
    }}))'''
    child = _worker("writer", model=_model(
        _call("write_file", {"file_path": str(target), "content": "approved"}, "live-write"),
        AIMessage("writer completed")))
    child["mode"] = child_mode
    options = _options(
        tmp_path, model=_model(_call("js_eval", {"code": code}, "live-eval"), AIMessage("parent completed")),
        checkpointer=InMemorySaver(), store=store, auto_mode_enabled=True,
        subagents=[child],
    )
    options.update(auto_approve=False, interactive=True)
    graph, _ = create_factory_agent(**options)
    config = {"configurable": {"thread_id": session}}
    first, events = await _run(graph, config=config, context=context)
    starts = [e for e in events if isinstance(e, dict) and e.get("type") == "subagent" and e.get("phase") == "start"]
    assert starts, "The check must reach child execution, not stop at the parent JS tool"
    if live_mode == "auto":
        assert first.get("__interrupt__"), "Live Auto skipped the unclassified child write"
        assert not target.exists()
        final, _ = await _run(graph, Command(resume={"decisions": [{"type": "approve"}]}),
                              config=config, context=context)
    else:
        assert not first.get("__interrupt__"), "Explicit live YOLO was unexpectedly gated"
        final = first
    assert not final.get("__interrupt__")
    assert target.read_text() == "approved"
    assert final["messages"][-1].content == "parent completed"
    expected = {"ok": True, "value": "writer completed"} if dispatcher == "tools.taskSettled" else "writer completed"
    assert _js_value(final, "live-eval") == expected


def _runtime(*, state=None, tools=(), events=None, attempt=1, eval_id="eval-1", **execution):
    return ToolRuntime(
        state=state or {}, context=None, config={"configurable": {}},
        stream_writer=(events.append if events is not None else lambda event: None),
        tool_call_id=eval_id, store=None, tools=list(tools),
        execution_info=ExecutionInfo(thread_id="thread", checkpoint_ns="namespace", checkpoint_id="checkpoint",
            task_id="task-id", node_first_attempt_time=10.0, node_attempt=attempt, **execution),
    )


def test_replay_ids_distinguish_duplicate_siblings_and_parallel_evals():
    ids = _ReplayStableDispatchIds()
    runtime = _runtime()

    def next_id(runtime=runtime, eval_id="eval-1", description="same work"):
        return ids.next_id(runtime=runtime, parent_eval_id=eval_id, description=description,
                           subagent_type="worker", label=None, response_schema=None)

    first, sibling = next_id(), next_id()
    assert first and sibling and first != sibling
    other_eval = next_id(eval_id="eval-2")
    assert other_eval not in {first, sibling}
    replay = _runtime(attempt=2)
    assert next_id(runtime=replay) == first
    assert next_id(runtime=replay) == sibling
    assert next_id(runtime=replay, eval_id="eval-2") == other_eval
    assert next_id(runtime=replay, description="different work") not in {first, sibling, other_eval}
    assert ids.next_id(runtime=replace(runtime, execution_info=None), parent_eval_id="eval-1",
                       description="same work", subagent_type="worker", label=None, response_schema=None) is None


@pytest.mark.parametrize("state, name, expected", [
    ({"_deepagents_forked_context": True}, "worker", "DelegationError"),
    ({"_deepagents_subagent_name": "child"}, "worker", "DelegationError"),
    ({}, "unknown", "LookupError"), ({}, "worker", "LookupError"),
])
async def test_preflight_failures_do_not_dispatch(state, name, expected):
    helper = build_settled_dispatch_tool(["worker"])
    result = await helper.coroutine(description="work", subagentType=name, runtime=_runtime(state=state))
    assert result["ok"] is False and result["error"]["type"] == expected


def _task(callback):
    async def task(description: str, subagent_type: str, runtime: ToolRuntime):
        """Delegate an instrumented test task."""
        return await callback(description, runtime)

    return StructuredTool.from_function(name="task", coroutine=task)


@pytest.mark.parametrize("value", ["plain non-JSON output", '{"score":"wrong"}', "x" * (MAX_RESULT_CHARS + 1)],
                         ids=["non-json", "wrong-type", "oversized"])
async def test_invalid_or_oversized_values_have_error_lifecycle(value):
    async def result(description, runtime):
        return value

    events = []
    helper = build_settled_dispatch_tool(["worker"])
    outcome = await helper.coroutine(description="work", subagentType="worker",
        responseSchema=None if len(value) > MAX_RESULT_CHARS else _SCHEMA,
        runtime=_runtime(tools=[_task(result)], events=events))
    assert outcome["ok"] is False
    assert [event["phase"] for event in events] == ["start", "error"]


async def test_ordinary_dispatch_exception_is_bounded_in_outcome_and_lifecycle_event():
    async def fail(description, runtime):
        raise RuntimeError("E" * 100000)

    events = []
    helper = build_settled_dispatch_tool(["worker"])
    # This uses the real QuickJS private dispatcher, whose exception path emits
    # its event before the factory catches the same error for the outcome.
    outcome = await helper.coroutine(description="work", subagentType="worker",
        runtime=_runtime(tools=[_task(fail)], events=events))
    assert outcome == {"ok": False, "error": {"type": "RuntimeError", "message": "E" * 2048}}
    assert [event["phase"] for event in events] == ["start", "error"]
    assert events[1]["error"] == "E" * 2048
    assert events[1]["id"] == events[0]["id"]


async def test_concurrency_limit_cancellation_and_per_loop_reuse():
    running = peak = 0
    release, entered = asyncio.Event(), asyncio.Event()

    async def result(description, runtime):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        entered.set()
        try:
            await release.wait()
            return description
        finally:
            running -= 1

    task = _task(result)
    helper = build_settled_dispatch_tool(["worker"], max_concurrent=1)

    async def dispatch(name):
        return await helper.coroutine(description=name, subagentType="worker", runtime=_runtime(tools=[task]))

    first = asyncio.create_task(dispatch("first"))
    await asyncio.wait_for(entered.wait(), 3)
    second = asyncio.create_task(dispatch("second"))
    await asyncio.sleep(0)
    assert peak == running == 1
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()
    assert await asyncio.wait_for(second, 3) == {"ok": True, "value": "second"}
    assert peak == 1 and running == 0
    # A synchronous interpreter uses another loop. Reusing this generation's
    # helper there must not retain a semaphore tied to the preceding loop.
    assert await asyncio.to_thread(lambda: asyncio.run(dispatch("other-loop"))) == {
        "ok": True, "value": "other-loop",
    }
