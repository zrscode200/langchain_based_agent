"""Actual skill reads, saved evidence and bounded child projections."""
from types import SimpleNamespace

import pytest
from deepagents_code._fake_models import _ToolBindingFakeModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from lc_factory.assembly import create_factory_agent
from lc_factory.skill_activity import SKILL_ACTIVITY, SkillActivityMiddleware, skill_activities, skill_for_call
from lc_factory.background_transcript import projected_message, preview_message, message_text


def catalog_skill(tmp_path):
    path = tmp_path / ".agents/skills/review/SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\nname: review\ndescription: Inspect changes\n---\nRead carefully.\n")
    return path, {"name": "review", "description": "Inspect changes", "source": "project", "path": str(path)}


def call(path, **args):
    return {"id": "skill-read", "name": "read_file", "args": {"file_path": str(path), **args}}


async def test_real_graph_stream_and_checkpoint_preserve_skill_read(tmp_path):
    path, _ = catalog_skill(tmp_path)
    c = call(path, limit=1000)
    graph, _ = create_factory_agent(
        _ToolBindingFakeModel(messages=iter([AIMessage("", tool_calls=[c]), AIMessage("Done")])),
        "skill-activity", cwd=tmp_path, skill_policy={"mode": "project"},
        enable_memory=False, enable_ask_user=False, enable_shell=False, checkpointer=InMemorySaver(),
    )
    config = {"configurable": {"thread_id": "skill-test"}}
    updates = [u async for u in graph.astream({"messages": [HumanMessage("Review this")]}, config, stream_mode="updates")]
    recorded = [m for u in updates for node in u.values() if isinstance(node, dict)
                for m in node.get("messages", []) if hasattr(m, "additional_kwargs")]
    assert any(skill_activities(m.additional_kwargs).get(c["id"], {}).get("status") == "loading" for m in recorded)
    state = await graph.aget_state(config)
    messages = state.values["messages"]
    ai = next(m for m in messages if isinstance(m, AIMessage) and m.tool_calls)
    tool = next(m for m in messages if isinstance(m, ToolMessage))
    assert ai.tool_calls == [{**c, "type": "tool_call"}]
    assert skill_activities(ai.additional_kwargs)[c["id"]]["status"] == "loading"
    assert skill_activities(tool.additional_kwargs)[c["id"]]["status"] == "loaded"
    assert "Read carefully." in tool.content
    assert skill_activities(projected_message(tool)["additional_kwargs"])[c["id"]]["name"] == "review"
    assert "Loaded skill: review" in message_text(tool)


@pytest.mark.parametrize(("content", "status", "args", "expected"), [
    ("Error: permission denied", "error", {}, "failed"),
    ("System reminder: File exists but has empty contents", "success", {}, "empty"),
    ("System reminder: no lines were read", "success", {"limit": 0}, "empty"),
    ("1  first\n2  second", "success", {"limit": 2}, "loaded"),
    ("1  first\n2  second\n\n[Read 2 lines (lines 1-2 of 6 total). 4 lines remaining from offset 2.]", "success", {"limit": "2"}, "partial"),
    ("6  later", "success", {"offset": 5}, "partial"),
    ("1  first\n\n[Output was truncated due to size limits.", "success", {}, "partial"),
    ("1  all instructions", "success", {}, "loaded"),
    ("1  ", "success", {}, "empty"),
    ("@@ lines 1-2 of 2 @@\nFirst\nSecond", "success", {}, "loaded"),
    ("@@ lines 1-2 of 6 | next offset 2 @@\nFirst\nSecond", "success", {}, "partial"),
    ("@@ lines 5-6 of 6 @@\nLater\nLast", "success", {}, "partial"),
    ("@@ lines 1-1 @@\nExtent unknown", "success", {}, "partial"),
    ("@@ lines 2-2 of 5 | next offset 2 @@\n ", "success", {}, "empty"),
    ("[Requested offset -1 is before the start of the file; read from line 1 instead.]\n@@ lines 1-1 of 1 @@\nAll", "success", {"offset": -1}, "loaded"),
    ("[Output was truncated due to size limits. Use a smaller window.]\n@@ lines 1-2 of 2 | truncated due to size @@\nFirst\nSecond", "success", {}, "partial"),
    ("@@ lines 1-1 of 1 | truncated mid-line | 2 of 50 chars @@\nSo", "success", {}, "partial"),
    ("@@ lines 1-2 of 2 @@\n@@ lines 9-9 of 99 | next offset 9 @@\nActual instructions", "success", {}, "loaded"),
    ("@@ lines 1-1 of 1 @@\n[Output was truncated due to size limits. This is literal source.]", "success", {}, "loaded"),
    ("@@ lines 0-1 of 2 @@\nInvalid range", "success", {}, "unavailable"),
    ("Unknown tool response", "success", {}, "unavailable"),
])
async def test_read_outcomes_do_not_claim_completed_work(tmp_path, content, status, args, expected):
    path, row = catalog_skill(tmp_path)
    c = call(path, **args)
    request = SimpleNamespace(tool_call=c, state={"skills_metadata": [row]})
    result = ToolMessage(content, tool_call_id=c["id"], status=status, additional_kwargs={"keep": True})
    async def handler(r):
        assert r is request
        return result
    observed = await SkillActivityMiddleware().awrap_tool_call(request, handler)
    assert observed.content == result.content and observed.status == result.status
    assert observed.additional_kwargs["keep"] is True
    assert SKILL_ACTIVITY not in result.additional_kwargs
    assert skill_activities(observed.additional_kwargs)[c["id"]]["status"] == expected


def test_catalog_identity_no_filename_or_prose_guessing(tmp_path):
    path, row = catalog_skill(tmp_path)
    assert skill_for_call(call(path), [row])["name"] == "review"
    assert skill_for_call(call(path.parent / "guide.md"), [row]) is None
    assert skill_for_call(call(path), []) is None
    assert skill_for_call(call("review/SKILL.md"), [row]) is None
    assert skill_for_call(call(str(path.parent) + "/link/../SKILL.md"), [row]) is None
    assert skill_for_call({**call(path), "name": "execute"}, [row]) is None
    assert skill_for_call({**call(path), "args": "{partial"}, [row]) is None


def test_sync_command_result_preserves_other_messages_and_metadata(tmp_path):
    path, row = catalog_skill(tmp_path)
    c = call(path)
    other = ToolMessage("Other output", tool_call_id="other")
    target = ToolMessage("1  Instructions", tool_call_id=c["id"])
    original = Command(update={"messages": [other, target], "keep": 42})
    request = SimpleNamespace(tool_call=c, state={"skills_metadata": [row]})
    observed = SkillActivityMiddleware().wrap_tool_call(request, lambda _: original)
    assert observed.update["keep"] == 42
    assert observed.update["messages"][0] is other
    assert SKILL_ACTIVITY not in target.additional_kwargs
    assert skill_activities(observed.update["messages"][1].additional_kwargs)[c["id"]]["status"] == "loaded"


def test_metadata_is_bounded_and_child_preview_keeps_no_private_fields(tmp_path):
    path, row = catalog_skill(tmp_path)
    value = {**row, "origin": "agent", "status": "loaded", "private": "SECRET"}
    metadata = {SKILL_ACTIVITY: {"one": value, "bad": {**value, "status": {}}}, "private": "SECRET"}
    projected, _ = preview_message(projected_message(ToolMessage("1  Instructions", tool_call_id="one", additional_kwargs=metadata)))
    assert "SECRET" not in str(projected)
    assert list(projected["additional_kwargs"][SKILL_ACTIVITY]) == ["one"]


def test_after_model_idempotent_and_catalog_scoped(tmp_path):
    path, row = catalog_skill(tmp_path)
    c = call(path)
    message = AIMessage("", id="ai", tool_calls=[c], additional_kwargs={"reasoning_content": "Public"})
    middleware = SkillActivityMiddleware()
    updated = middleware.after_model({"messages": [message], "skills_metadata": [row]}, None)["messages"][0]
    assert updated.id == message.id
    assert updated.additional_kwargs["reasoning_content"] == "Public"
    assert middleware.after_model({"messages": [updated], "skills_metadata": [row]}, None) is None
    assert middleware.after_model({"messages": [message], "skills_metadata": []}, None) is None


@pytest.mark.parametrize(("args", "expected"), [
    ({"offset": "4", "limit": 1000}, "partial"),
    ({"limit": "2"}, "partial"),
    ({"limit": 5}, "loaded"),
    ({"offset": -1, "limit": 5}, "loaded"),
    ({"limit": 0}, "empty"),
])
async def test_real_file_tool_reports_actual_window(tmp_path, args, expected):
    path, _ = catalog_skill(tmp_path)
    c = call(path, **args)
    graph, _ = create_factory_agent(
        _ToolBindingFakeModel(messages=iter([AIMessage("", tool_calls=[c]), AIMessage("Done")])),
        "skill-window", cwd=tmp_path, skill_policy={"mode": "project"},
        enable_memory=False, enable_ask_user=False, enable_shell=False)
    result = await graph.ainvoke({"messages": [HumanMessage("Read skill")]})
    tool = next(m for m in result["messages"] if isinstance(m, ToolMessage))
    assert skill_activities(tool.additional_kwargs)[c["id"]]["status"] == expected


@pytest.mark.parametrize("body", ["Long line " * 500, "First line\n" + "Another long line\n" * 500])
def test_actual_sdk_size_truncation_reports_partial(tmp_path, body):
    from deepagents.backends.protocol import ReadResult
    from deepagents.middleware.filesystem import _truncate_paginated_read

    path, row = catalog_skill(tmp_path)
    count = len(body.splitlines())
    output = _truncate_paginated_read(body.rstrip("\n"), str(path),
        ReadResult(start_line=1, end_line=count, total_lines=count), 250)
    c = call(path)
    request = SimpleNamespace(tool_call=c, state={"skills_metadata": [row]})
    result = SkillActivityMiddleware().wrap_tool_call(request,
        lambda _: ToolMessage(output, tool_call_id=c["id"]))
    assert "truncated" in output
    assert skill_activities(result.additional_kwargs)[c["id"]]["status"] == "partial"


async def test_loading_is_checkpointed_before_native_approval_and_denial(tmp_path, monkeypatch):
    import lc_factory.assembly as assembly
    # Exercise the existing factory/native gate with an explicit read policy.
    original = assembly._add_interrupt_on
    monkeypatch.setattr(assembly, "_add_interrupt_on", lambda **kwargs:
                        {**original(**kwargs), "read_file": True})
    path, _ = catalog_skill(tmp_path)
    c = call(path)
    graph, _ = create_factory_agent(
        _ToolBindingFakeModel(messages=iter([AIMessage("", tool_calls=[c]), AIMessage("Stopped")])),
        "skill-approval", cwd=tmp_path, skill_policy={"mode": "project"},
        enable_memory=False, enable_ask_user=False, enable_shell=False,
        auto_approve=False, checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "approval"}}
    paused = await graph.ainvoke({"messages": [HumanMessage("Read skill")]}, config)
    assert paused.get("__interrupt__")
    state = await graph.aget_state(config)
    assert any(skill_activities(m.additional_kwargs).get(c["id"], {}).get("status") == "loading"
               for m in state.values["messages"])
    result = await graph.ainvoke(Command(resume={"decisions": [{"type": "reject"}]}), config)
    tool = next(m for m in result["messages"] if isinstance(m, ToolMessage))
    assert tool.status == "error" and "Read carefully." not in tool.content


@pytest.mark.parametrize("mode", ["isolated", "fork"])
async def test_child_loading_is_visible_while_read_is_pending(tmp_path, mode):
    import asyncio
    from lc_factory.runtime import FactoryRuntime, RuntimeOptions
    from lc_factory.upstream import AgentMiddleware

    path, _ = catalog_skill(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()

    class Gate(AgentMiddleware):
        async def awrap_tool_call(self, request, handler):
            if request.tool_call["name"] == "read_file":
                entered.set()
                await release.wait()
            return await handler(request)

    child = {"name": "reader", "description": "Read instructions", "mode": mode,
             "model": _ToolBindingFakeModel(messages=iter([
                 AIMessage("", tool_calls=[call(path)]), AIMessage("Child done")])),
             "middleware": [Gate()]}
    model = _ToolBindingFakeModel(messages=iter([
        AIMessage("", tool_calls=[{"id": "launch", "name": "start_background_task",
                                  "args": {"description": "Read instructions", "subagent_type": "reader"}}]),
        AIMessage("Parent continues")]))
    settings = dict(model=model, assistant_id="child-skill", cwd=tmp_path,
                    skill_policy={"mode": "project"}, enable_memory=False,
                    enable_ask_user=False, enable_shell=False, auto_approve=True,
                    subagents=[child], checkpointer=InMemorySaver())
    async with await FactoryRuntime.create(agent_kwargs=settings,
            options=RuntimeOptions(background=True), workspace_id="workspace") as runtime:
        config = {"configurable": {"thread_id": "parent"}}
        parent = await runtime.ainvoke({"messages": [HumanMessage("Delegate reading")]}, config)
        try:
            await asyncio.wait_for(entered.wait(), 5)
            job = next(iter(runtime.background.jobs.values()))
            pending = job.transcript.structured()["messages"]
            assert any(skill_activities(m.get("additional_kwargs", {})).get("skill-read", {}).get("status") == "loading"
                       for m in pending)
            assert not any(skill_activities(m.additional_kwargs) for m in parent["messages"])
        finally:
            release.set()
        await asyncio.wait_for(runtime.background.wait("parent"), 5)
        completed = job.transcript.structured()["messages"]
        assert any(skill_activities(m.get("additional_kwargs", {})).get("skill-read", {}).get("status") == "loaded"
                   for m in completed)
