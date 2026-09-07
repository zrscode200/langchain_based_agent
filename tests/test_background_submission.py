"""Explicit background submission and additive typed results on real graphs."""
import asyncio
import json

import pytest
from deepagents_code._fake_models import _ToolBindingFakeModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from lc_factory.runtime import FactoryRuntime, RuntimeOptions
from lc_factory.upstream import InterpreterConfig


def call(name, arguments, identifier):
    return AIMessage(content="", tool_calls=[dict(name=name, args=arguments, id=identifier, type="tool_call")])


def args(tmp_path, model, **kwargs):
    return dict(assistant_id="explicit-background", cwd=tmp_path, model=model,
                enable_memory=False, enable_skills=False, enable_ask_user=False,
                interactive=False, auto_approve=True, checkpointer=InMemorySaver(), **kwargs)


async def test_explicit_submission_returns_handle_and_typed_result(tmp_path):
    child = _ToolBindingFakeModel(messages=iter([call("Finding", {"summary": "evidence"}, "result")]))
    model = _ToolBindingFakeModel(messages=iter([
        call("start_background_task", {"description": "Find evidence", "subagent_type": "child"}, "submit"),
        AIMessage("parent done"),
    ]))
    schema = {"title": "Finding", "type": "object", "properties": {"summary": {"type": "string"}},
              "required": ["summary"], "additionalProperties": False}
    async with await FactoryRuntime.create(agent_kwargs=args(tmp_path, model,
        subagents=[dict(name="child", description="Child", model=child, response_format=schema)]),
        options=RuntimeOptions(background=True), workspace_id="work") as runtime:
        result = await runtime.ainvoke({"messages": [HumanMessage("Start")]}, {"configurable": {"thread_id": "owner"}})
        handle = json.loads(next(m.content for m in result["messages"] if isinstance(m, ToolMessage)))
        assert handle["ok"] and handle["status"] == "running"
        await asyncio.wait_for(runtime.background.wait("owner"), 5)
        job = runtime.background.list("owner")[0]
        assert job["task_id"] == handle["task_id"] and job["status"] == "completed"
        assert job["outcome"] == {"ok": True, "value": {"summary": "evidence"}}
        assert json.loads(job["result"]) == {"summary": "evidence"}
        assert not runtime.background.list("other")


@pytest.mark.parametrize("tool_name", ["start_background_task", "task"])
async def test_unknown_child_rejected_before_scheduling(tmp_path, tool_name):
    model = _ToolBindingFakeModel(messages=iter([
        call(tool_name, {"description": "Work", "subagent_type": "missing"}, "submit"), AIMessage("done"),
    ]))
    async with await FactoryRuntime.create(agent_kwargs=args(tmp_path, model),
        options=RuntimeOptions(background=True), workspace_id="work") as runtime:
        result = await runtime.ainvoke({"messages": [HumanMessage("Start")]}, {"configurable": {"thread_id": "owner"}})
        response = next(m for m in result["messages"] if isinstance(m, ToolMessage))
        assert "Unknown local subagent" in str(response.content)
        assert not runtime.background.jobs


async def test_explicit_submission_preserves_parent_and_child_approval(tmp_path):
    target = tmp_path / "protected.txt"
    child = _ToolBindingFakeModel(messages=iter([
        call("write_file", {"file_path": str(target), "content": "protected"}, "write"),
    ]))
    model = _ToolBindingFakeModel(messages=iter([
        call("start_background_task", {"description": "Write a file", "subagent_type": "child"}, "submit"),
        AIMessage("parent done"),
    ]))
    kwargs = args(tmp_path, model, subagents=[dict(name="child", description="Child", model=child)])
    kwargs.update(interactive=True, auto_approve=False)
    async with await FactoryRuntime.create(agent_kwargs=kwargs,
        options=RuntimeOptions(background=True), workspace_id="work") as runtime:
        config = {"configurable": {"thread_id": "owner"}}
        result = await runtime.ainvoke({"messages": [HumanMessage("Start")]}, config)
        assert result.get("__interrupt__") and not runtime.background.jobs
        await runtime.ainvoke(Command(resume={"decisions": [{"type": "approve"}]}), config)
        await asyncio.wait_for(runtime.background.wait("owner"), 5)
        job = runtime.background.list("owner")[0]
        assert job["status"] == "needs_approval" and not job["outcome"]["ok"]
        assert not target.exists()


async def test_settled_dispatch_stays_foreground_alongside_background(tmp_path):
    model = _ToolBindingFakeModel(messages=iter([
        call("task_settled", {"description": "Foreground", "subagentType": "front"}, "front"),
        call("start_background_task", {"description": "Background", "subagent_type": "back"}, "back"),
        AIMessage("parent done"),
    ]))
    children = [dict(name=name, description=name,
        model=_ToolBindingFakeModel(messages=iter([AIMessage(name + " result")]))) for name in ("front", "back")]
    async with await FactoryRuntime.create(agent_kwargs=args(tmp_path, model,
        enable_settled_dispatch=True, subagents=children),
        options=RuntimeOptions(background=True), workspace_id="work") as runtime:
        result = await runtime.ainvoke({"messages": [HumanMessage("Start")]}, {"configurable": {"thread_id": "owner"}})
        front = json.loads(next(m.content for m in result["messages"] if isinstance(m, ToolMessage) and m.tool_call_id == "front"))
        assert front == {"ok": True, "value": "front result"}
        assert len(runtime.background.jobs) == 1
        await asyncio.wait_for(runtime.background.wait("owner"), 5)
        assert runtime.background.list("owner")[0]["name"] == "back"


async def test_oversized_background_result_is_failure_not_truncated_success(tmp_path):
    child = _ToolBindingFakeModel(messages=iter([AIMessage("x" * 65000)]))
    model = _ToolBindingFakeModel(messages=iter([
        call("start_background_task", {"description": "Work", "subagent_type": "child"}, "submit"), AIMessage("done"),
    ]))
    async with await FactoryRuntime.create(agent_kwargs=args(tmp_path, model,
        subagents=[dict(name="child", description="Child", model=child)]),
        options=RuntimeOptions(background=True), workspace_id="work") as runtime:
        await runtime.ainvoke({"messages": [HumanMessage("Start")]}, {"configurable": {"thread_id": "owner"}})
        await asyncio.wait_for(runtime.background.wait("owner"), 5)
        job = runtime.background.list("owner")[0]
        assert job["status"] == "failed" and not job["outcome"]["ok"]
        assert len(job["result"]) <= 64000


@pytest.mark.parametrize("live_mode", ["auto", "yolo"])
async def test_background_child_auto_is_gated_and_live_yolo_is_preserved(tmp_path, live_mode):
    from dataclasses import replace
    from langgraph.store.memory import InMemoryStore
    from deepagents_code.approval_mode import APPROVAL_MODE_NAMESPACE, approval_mode_key, approval_mode_payload

    store = InMemoryStore()
    key = approval_mode_key("owner")
    store.put(APPROVAL_MODE_NAMESPACE, key, approval_mode_payload(mode=live_mode))
    target = tmp_path / "auto-write.txt"
    child = _ToolBindingFakeModel(messages=iter([
        call("write_file", {"file_path": str(target), "content": "approved"}, "write"), AIMessage("child done"),
    ]))
    # Explicitly opting this tool into PTC bypasses parent ToolNode approval;
    # the worker must still apply its own gate using the live approval store.
    model = _ToolBindingFakeModel(messages=iter([
        call("js_eval", {"code": "await tools.startBackgroundTask({description:'Write', subagent_type:'child'})"}, "submit"),
        AIMessage("parent done"),
    ]))
    kwargs = args(tmp_path, model, store=store, auto_mode_enabled=True,
        enable_interpreter=True, interpreter_config=replace(InterpreterConfig.from_resolver(), ptc=["start_background_task"]),
        subagents=[dict(name="child", description="Child", model=child)])
    kwargs.update(interactive=True, auto_approve=False)
    async with await FactoryRuntime.create(agent_kwargs=kwargs,
        options=RuntimeOptions(background=True), workspace_id="work") as runtime:
        await runtime.ainvoke({"messages": [HumanMessage("Start")]},
            {"configurable": {"thread_id": "owner"}},
            context={"approval_mode": "auto", "approval_mode_key": key, "thread_id": "owner"})
        await asyncio.wait_for(runtime.background.wait("owner"), 5)
        job = runtime.background.list("owner")[0]
        assert job["status"] == ("completed" if live_mode == "yolo" else "needs_approval")
        assert target.exists() is (live_mode == "yolo")
