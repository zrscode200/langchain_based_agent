"""Outcomes reach active/resumed main models without changing user authority."""
import asyncio

import pytest
from pydantic import Field
from deepagents_code._fake_models import _ToolBindingFakeModel
from deepagents_code.auto_mode import _latest_turn_id
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.types import Command

from lc_factory.background import Job
from lc_factory.runtime import FactoryRuntime, RuntimeOptions
from test_background_submission import args, call
from test_subagent_approval import Classifier, session, user

MARKER = "CHILD_OUTCOME_EVIDENCE"


class Recorder(_ToolBindingFakeModel):
    seen: list = Field(default_factory=list)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append(list(messages))
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


def child(release, mode):
    class Gate(AgentMiddleware):
        async def abefore_model(self, state, runtime):
            await release.wait()
    return dict(name="child", description="Child", mode=mode,
                model=_ToolBindingFakeModel(messages=iter([AIMessage(MARKER)])), middleware=[Gate()])


async def finish(runtime, release):
    release.set()
    await asyncio.wait_for(runtime.background.wait("owner"), 5)
    assert next(iter(runtime.background.jobs.values())).status == "completed"


@pytest.mark.parametrize("mode", ["isolated", "fork"])
async def test_completion_reaches_next_model_within_active_run(tmp_path, mode):
    release = asyncio.Event()
    @tool
    async def synchronize_child() -> str:
        """Wait for the local test fixture without returning its result."""
        await finish(runtime, release)
        return "synchronized"
    main = Recorder(messages=iter([
        call("start_background_task", {"description": "Work", "subagent_type": "child"}, "launch"),
        call("synchronize_child", {}, "sync"), AIMessage("Result received"), AIMessage("Next"),
    ]))
    config = {"configurable": {"thread_id": "owner"}}
    async with await FactoryRuntime.create(agent_kwargs=args(tmp_path, main,
        subagents=[child(release, mode)], tools=[synchronize_child]), options=RuntimeOptions(background=True),
        workspace_id="work") as runtime:
        await runtime.ainvoke({"messages": [user()]}, config)
        assert MARKER in str(main.seen[-1])
        assert _latest_turn_id(main.seen[-1]) == "turn-1"
        assert not runtime.background.pending("owner")
        await runtime.ainvoke({"messages": [user("Next", "turn-2")]}, config)
        assert sum(MARKER in str(m.content) for m in main.seen[-1]) == 1


@pytest.mark.parametrize("mode", ["isolated", "fork"])
async def test_approval_resume_delivers_without_new_user_message(tmp_path, mode):
    release = asyncio.Event()
    main = Recorder(messages=iter([
        call("start_background_task", {"description": "Work", "subagent_type": "child"}, "launch"),
        call("write_file", {"file_path": str(tmp_path / "never-written"), "content": "fixture"}, "write"),
        AIMessage("Read outcome after denial"),
    ]))
    config = {"configurable": {"thread_id": "owner"}}
    settings = args(tmp_path, main, subagents=[child(release, mode)])
    settings.update(interactive=True, auto_approve=False)
    async with await FactoryRuntime.create(agent_kwargs=settings, options=RuntimeOptions(background=True), workspace_id="work") as runtime:
        assert (await runtime.ainvoke({"messages": [user()]}, config)).get("__interrupt__")
        assert (await runtime.ainvoke(Command(resume={"decisions": [{"type": "approve"}]}), config)).get("__interrupt__")
        await finish(runtime, release)
        await runtime.ainvoke(Command(resume={"decisions": [{"type": "reject"}]}), config)
        assert MARKER in str(main.seen[-1]) and not runtime.background.pending("owner")
        assert _latest_turn_id(main.seen[-1]) == "turn-1"
        assert not (tmp_path / "never-written").exists()


@pytest.mark.parametrize("status", ["completed", "failed", "timed_out", "cancelled"])
async def test_empty_continuation_delivers_terminal_status_and_preserves_user_identity(tmp_path, status):
    main = Recorder(messages=iter([AIMessage("Idle"), AIMessage("Outcome received")]))
    config = {"configurable": {"thread_id": "owner"}}
    async with await FactoryRuntime.create(agent_kwargs=args(tmp_path, main), options=RuntimeOptions(background=True), workspace_id="work") as runtime:
        await runtime.ainvoke({"messages": [user()]}, config)
        runtime.background.jobs["child"] = Job("owner", "child", status=status, result=MARKER)
        runtime.background.jobs["other"] = Job("other-owner", "child", status=status, result="OTHER_OWNER")
        await runtime.ainvoke({"messages": []}, config)
        assert MARKER in str(main.seen[-1]) and "OTHER_OWNER" not in str(main.seen[-1])
        assert f"status {status}" in str(main.seen[-1])
        assert _latest_turn_id(main.seen[-1]) == "turn-1"
        assert not runtime.background.pending("owner") and runtime.background.pending("other-owner")


@pytest.mark.parametrize("tool_name", ["list_background_tasks", "inspect_background_task"])
async def test_model_inspection_consumes_result_without_extra_notification(tmp_path, tool_name):
    class FinishAtInspection(AgentMiddleware):
        async def awrap_tool_call(self, request, handler):
            if request.tool_call["name"] == tool_name:
                runtime.background.jobs["child"] = Job("owner", "child", status="completed", result=MARKER)
            return await handler(request)
    main = Recorder(messages=iter([
        call(tool_name, {} if tool_name.startswith("list") else {"task_id": "child"}, "inspect"),
        AIMessage("Read child result"), AIMessage("Next"),
    ]))
    config = {"configurable": {"thread_id": "owner"}}
    async with await FactoryRuntime.create(agent_kwargs=args(tmp_path, main, middleware=[FinishAtInspection()]),
        options=RuntimeOptions(background=True), workspace_id="work") as runtime:
        await runtime.ainvoke({"messages": [user()]}, config)
        assert sum(MARKER in str(m.content) for m in main.seen[-1]) == 1
        assert not any(str(m.id).startswith("result-") for m in main.seen[-1])
        assert not runtime.background.pending("owner")
        await runtime.ainvoke({"messages": [user("Next", "turn-2")]}, config)
        assert sum(MARKER in str(m.content) for m in main.seen[-1]) == 1


async def test_background_delivery_does_not_break_auto_managed_temp_tool(tmp_path, monkeypatch):
    from deepagents_code import auto_mode
    allocated = []
    original = auto_mode._allocate_temp_artifact
    def allocate(*args, **kwargs):
        value = original(*args, **kwargs)
        allocated.append(value)
        return value
    monkeypatch.setattr(auto_mode, "_allocate_temp_artifact", allocate)
    main = Recorder(messages=iter([
        call("create_temp_artifact", {"content": "fixture", "suffix": ".txt"}, "temp"), AIMessage("Done"),
    ]))
    store, config, context = session()
    settings = args(tmp_path, main, store=store, auto_mode_enabled=True, auto_classifier_model=Classifier())
    settings.update(interactive=True, auto_approve=False)
    try:
        async with await FactoryRuntime.create(agent_kwargs=settings, options=RuntimeOptions(background=True), workspace_id="work") as runtime:
            runtime.background.jobs["child"] = Job("owner", "child", status="completed", result=MARKER)
            result = await runtime.ainvoke({"messages": [user("Create scratch.")]}, config, context=context)
            message = next(m for m in result["messages"] if isinstance(m, ToolMessage) and m.tool_call_id == "temp")
            assert message.status == "success"
            assert _latest_turn_id(main.seen[0]) == "turn-1"
    finally:
        from pathlib import Path
        for artifact in allocated:
            Path(artifact["file_path"]).unlink(missing_ok=True)
