"""Behavioral coverage of opt-in runtime generations and actual child graphs."""
import asyncio
from types import SimpleNamespace

import pytest
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from deepagents_code._fake_models import _ToolBindingFakeModel

from lc_factory.runtime import FactoryRuntime, RuntimeOptions
from lc_factory.upstream import get_user_agents_dir


def args(tmp_path, model=None, **kwargs):
    return dict(model=model or _ToolBindingFakeModel(messages=iter([AIMessage("done")])),
                assistant_id="runtime-test", cwd=tmp_path, enable_memory=False,
                enable_skills=False, enable_ask_user=False, interactive=False,
                auto_approve=True, checkpointer=InMemorySaver(), **kwargs)


def call(name, arguments, identifier):
    return AIMessage(content="", tool_calls=[dict(name=name, args=arguments, id=identifier, type="tool_call")])


async def test_reload_changes_complete_generation_and_retains_failed_candidate(tmp_path):
    @tool
    def first() -> str:
        """First generation tool."""
        return "first"

    @tool
    def second() -> str:
        """Second generation tool."""
        return "second"

    fail = False
    async def reload():
        if fail:
            raise ValueError("bad MCP config")
        return [second], [], []

    async with await FactoryRuntime.create(agent_kwargs=args(tmp_path, tools=[first]),
            options=RuntimeOptions(reload=True), workspace_id="w", reload_tools=reload) as runtime:
        old = runtime.current
        runtime.request_reload()
        new = await runtime.select()
        assert new is not old and runtime.generation == 2
        assert "first" in old.agent.nodes["tools"].bound.tools_by_name
        assert "second" in new.agent.nodes["tools"].bound.tools_by_name
        assert "first" not in new.agent.nodes["tools"].bound.tools_by_name
        fail = True
        runtime.request_reload()
        assert await runtime.select() is new
        assert runtime.last_reload_error == "ValueError"
        fail = False
        directory = get_user_agents_dir("runtime-test") / "researcher"
        directory.mkdir(parents=True)
        (directory / "AGENTS.md").write_text("---\ndescription: Research\n---\nResearch carefully")
        runtime.request_reload()
        graph = (await runtime.select()).agent
        assert "researcher" in graph.nodes["tools"].bound.tools_by_name["task"].description
        (directory / "AGENTS.md").write_text("invalid")
        runtime.request_reload()
        assert (await runtime.select()).agent is graph


async def test_reload_requested_during_load_is_not_lost(tmp_path):
    started, finish = asyncio.Event(), asyncio.Event()
    async def load():
        started.set()
        await finish.wait()
        return [], [], []
    async with await FactoryRuntime.create(agent_kwargs=args(tmp_path), options=RuntimeOptions(reload=True),
            workspace_id="w", reload_tools=load) as runtime:
        runtime.request_reload()
        selection = asyncio.create_task(runtime.select())
        await started.wait()
        runtime.request_reload()
        finish.set()
        await selection
        assert runtime.applied_revision == 1 and runtime.revision == 2
        await runtime.select()
        assert runtime.applied_revision == 2


async def test_background_real_fork_result_delivery_and_ownership(tmp_path):
    observed = []
    class Observe(AgentMiddleware):
        async def abefore_model(self, state, runtime):
            observed.append((state.get("_deepagents_forked_context", False), state["messages"]))

    # The model dispatches by state so parent/worker scheduling cannot reorder a script.
    class Model(_ToolBindingFakeModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            if any("TASK:" in str(m.content) or "child request" in str(m.content) for m in messages[-2:]):
                response = AIMessage("child result")
            elif any(isinstance(m, ToolMessage) for m in messages):
                response = AIMessage("parent free")
            else:
                response = call("start_background_task", {"description": "child request", "subagent_type": "general-purpose"}, "delegate")
            from langchain_core.outputs import ChatResult, ChatGeneration
            return ChatResult(generations=[ChatGeneration(message=response)])

    async with await FactoryRuntime.create(agent_kwargs=args(tmp_path, Model(messages=iter([])), middleware=[Observe()]),
            options=RuntimeOptions(background=True), workspace_id="w") as runtime:
        config = {"configurable": {"thread_id": "owner"}}
        result = await runtime.ainvoke({"messages": [HumanMessage("delegate")]}, config)
        assert result["messages"][-1].content == "parent free"
        pending = await asyncio.wait_for(runtime.background.wait("owner"), 5)
        assert len(pending) == 1 and "child result" in next(iter(pending.values()))
        assert runtime.background.list("other") == []
        assert runtime.background.pending("other") == {}
        await runtime.ainvoke({"messages": [HumanMessage("What did you find?")]}, config)
        assert not runtime.background.pending("owner")
        assert any(fork for fork, _ in observed)
        assert len(runtime.background.jobs) == 1  # no recursive background launches


async def test_background_child_approval_blocks_write_and_shutdown_cancels(tmp_path):
    from lc_factory.background import _IN_WORKER
    from langchain_core.outputs import ChatResult, ChatGeneration
    target = tmp_path / "protected.txt"
    class Model(_ToolBindingFakeModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            if _IN_WORKER.get():
                response = call("write_file", {"file_path": str(target), "content": "unsafe"}, "write")
            elif any(isinstance(m, ToolMessage) for m in messages):
                response = AIMessage("parent done")
            else:
                response = call("start_background_task", {"description": "Write the file", "subagent_type": "general-purpose"}, "delegate")
            return ChatResult(generations=[ChatGeneration(message=response)])

    kwargs = args(tmp_path, Model(messages=iter([])))
    kwargs.update(interactive=True, auto_approve=False)
    async with await FactoryRuntime.create(agent_kwargs=kwargs, options=RuntimeOptions(background=True), workspace_id="w") as runtime:
        result = await runtime.ainvoke({"messages": [HumanMessage("delegate")]}, {"configurable": {"thread_id": "owner"}})
        from langgraph.types import Command
        assert result.get("__interrupt__")  # main delegation approval is preserved
        assert not runtime.background.jobs
        await runtime.ainvoke(Command(resume={"decisions": [{"type": "approve"}]}),
                              {"configurable": {"thread_id": "owner"}})
        # Approval to delegate is not approval for the child's protected write.
        pending = await asyncio.wait_for(runtime.background.wait("owner"), 5)
        assert "protected action has not run" in next(iter(pending.values()))
        assert runtime.background.list("owner")[0]["status"] == "needs_approval"
        assert not target.exists()


async def test_background_failed_delivery_retains_pending_results(tmp_path):
    from lc_factory.background import Job
    model = _ToolBindingFakeModel(messages=iter([]))  # exhaustion fails the turn
    async with await FactoryRuntime.create(agent_kwargs=args(tmp_path, model), options=RuntimeOptions(background=True), workspace_id="w") as runtime:
        runtime.background.jobs["job"] = Job("owner", "worker", status="completed", result="valuable data")
        with pytest.raises(Exception):
            await runtime.ainvoke({"messages": [HumanMessage("continue")]}, {"configurable": {"thread_id": "owner"}})
        assert runtime.background.pending("owner") == {"job": "valuable data"}


async def test_background_capacity_cancel_and_close(tmp_path):
    from lc_factory.background import BackgroundTasks, Job
    background = BackgroundTasks(max_running=1)
    started = asyncio.Event()
    stopped = asyncio.Event()
    async def work():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
    worker = asyncio.create_task(work())
    background.jobs["job"] = Job("owner", "worker", worker=worker)
    await started.wait()
    await background.cancel("other")
    assert not worker.done()
    await background.close()
    assert worker.done() and stopped.is_set()
    assert (await background.wait("owner"))["job"] == "Background task cancelled."


async def test_empty_environment_does_not_inherit_host(monkeypatch, tmp_path):
    monkeypatch.setenv("HOST_ONLY_SECRET", "must-not-inherit")
    async with await FactoryRuntime.create(agent_kwargs=args(tmp_path, environ={}),
            options=RuntimeOptions(reload=True, background=True), workspace_id="w") as runtime:
        assert dict(runtime.environ) == {}
        assert dict(runtime.background.environ) == {}
        assert dict(runtime.kwargs["environ"]) == {}


async def test_interrupted_thread_keeps_generation_through_approval(tmp_path):
    from langgraph.types import Command
    from langchain_core.outputs import ChatResult, ChatGeneration
    directory = get_user_agents_dir("runtime-test") / "specialist"
    directory.mkdir(parents=True)
    path = directory / "AGENTS.md"
    path.write_text("---\ndescription: Specialist\n---\nGENERATION_ONE")
    class Model(_ToolBindingFakeModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            system = str(messages[0].content)
            if "GENERATION_ONE" in system:
                response = AIMessage("result one")
            elif "GENERATION_TWO" in system:
                response = AIMessage("result two")
            elif any(isinstance(m, ToolMessage) for m in messages):
                response = AIMessage("parent done")
            else:
                response = call("task", {"description": "Work", "subagent_type": "specialist"}, "delegate")
            return ChatResult(generations=[ChatGeneration(message=response)])
    kwargs = args(tmp_path, Model(messages=iter([])))
    kwargs.update(interactive=True, auto_approve=False)
    async with await FactoryRuntime.create(agent_kwargs=kwargs, options=RuntimeOptions(reload=True), workspace_id="w") as runtime:
        config = {"configurable": {"thread_id": "owner"}}
        result = await runtime.ainvoke({"messages": [HumanMessage("delegate")]}, config)
        assert result.get("__interrupt__")
        old = runtime.current
        path.write_text("---\ndescription: Specialist\n---\nGENERATION_TWO")
        runtime.request_reload()
        assert await runtime.select("other") is not old
        assert await runtime.select("owner") is old
        result = await runtime.ainvoke(Command(resume={"decisions": [{"type": "approve"}]}), config)
        assert any(isinstance(m, ToolMessage) and "result one" in str(m.content) for m in result["messages"])
        assert "owner" not in runtime._thread_generations
        assert await runtime.select("owner") is runtime.current


async def test_server_selection_retains_interrupted_generation(tmp_path, monkeypatch):
    from lc_factory import server_graph
    from lc_factory.upstream import CLIContextSchema
    kwargs = args(tmp_path)
    async with await FactoryRuntime.create(agent_kwargs=kwargs, options=RuntimeOptions(reload=True), workspace_id="w") as runtime:
        anchor = runtime.current
        monkeypatch.setattr(server_graph, "_factory_runtime_owners", {id(anchor.agent): runtime})
        assert await server_graph._select_factory_runtime(anchor, "paused") is anchor
        runtime.request_reload()
        newer = await server_graph._select_factory_runtime(anchor, "other")
        assert newer is not anchor
        assert await server_graph._select_factory_runtime(anchor, "paused") is anchor
        server_graph.factory_checkpoint_committed("paused", {"channel_values": {"_factory_turn_complete": True}}, {"_factory_turn_complete": 2})
        assert await server_graph._select_factory_runtime(anchor, "paused") is newer


async def test_real_stateless_mcp_transports_survive_reconfiguration(tmp_path):
    import json
    import sys
    from lc_factory.mcp_reload import build_reloadable_tools
    script = tmp_path / "mcp_server.py"
    script.write_text('''import sys
from mcp.server.fastmcp import FastMCP
server = FastMCP("generation")
@server.tool()
def generation() -> str:
    """Return this connection's generation."""
    return sys.argv[1]
server.run()
''')
    config_path = tmp_path / "mcp.json"
    config = SimpleNamespace(no_mcp=False, mcp_config_path=str(config_path), trust_project_mcp=True)
    def write(value):
        config_path.write_text(json.dumps({"mcpServers": {"sample": {"command": sys.executable, "args": [str(script), value]}}}))
    write("ONE")
    _, info, old = await build_reloadable_tools(config, None)
    assert info[0].status == "ok"
    assert "ONE" in str(await old[0].ainvoke({}))
    write("TWO")
    _, info, new = await build_reloadable_tools(config, None)
    assert info[0].status == "ok"
    assert "TWO" in str(await new[0].ainvoke({}))
    assert "ONE" in str(await old[0].ainvoke({}))


def test_remote_reload_rejects_partial_invalid_definitions(monkeypatch):
    from lc_factory import mcp_reload
    data = {"async_subagents": {"remote": {"description": "Research", "graph_id": "agent", "headers": {"authorization": "placeholder"}}}}
    sources = SimpleNamespace(user=SimpleNamespace(status=SimpleNamespace(usable=True)),
                              managed=SimpleNamespace(status=SimpleNamespace(usable=True)),
                              merged=lambda: (data, None))
    monkeypatch.setattr("lc_factory.upstream.get_config_sources", lambda: sources)
    snapshot = mcp_reload.load_async_subagent_snapshot()
    data["async_subagents"]["remote"]["headers"]["authorization"] = "changed"
    assert snapshot[0]["headers"]["authorization"] == "placeholder"
    del data["async_subagents"]["remote"]["graph_id"]
    with pytest.raises(ValueError, match="requires graph_id"):
        mcp_reload.load_async_subagent_snapshot()


async def test_background_capacity_rejects_before_dispatch():
    from lc_factory.background import BackgroundTasks, Job
    background = BackgroundTasks(max_running=1)
    worker = asyncio.create_task(asyncio.Event().wait())
    background.jobs["existing"] = Job("owner", "worker", worker=worker)
    async def execute(*args, **kwargs):
        pytest.fail("Capacity must prevent dispatch")
    runtime = SimpleNamespace(state={}, config={"configurable": {"thread_id": "owner"}},
                              tools=[SimpleNamespace(name="task", ainvoke=execute)], tool_call_id="new")
    submit = next(t for t in background.tools if t.name == "start_background_task")
    try:
        result = await submit.coroutine(description="Work", subagent_type="worker", runtime=runtime)
        assert not result["ok"] and "capacity" in result["error"]["message"]
        assert list(background.jobs) == ["existing"]
    finally:
        await background.close()
    assert worker.done()
    assert background.jobs["existing"].status == "cancelled"


async def test_failed_outer_after_agent_keeps_results_and_generation(tmp_path):
    from lc_factory.background import Job
    class FailAfter(AgentMiddleware):
        async def aafter_agent(self, state, runtime):
            raise ValueError("outer after-agent failed")
    async with await FactoryRuntime.create(agent_kwargs=args(tmp_path, middleware={"first": [FailAfter()]}),
            options=RuntimeOptions(background=True, reload=True), workspace_id="w") as runtime:
        runtime.background.jobs["job"] = Job("owner", "worker", status="completed", result="important result")
        old = runtime.current
        with pytest.raises(ValueError, match="outer after-agent failed"):
            await runtime.ainvoke({"messages": [HumanMessage("continue")]}, {"configurable": {"thread_id": "owner"}})
        assert runtime.background.pending("owner") == {"job": "important result"}
        runtime.request_reload()
        await runtime.select("other")
        assert await runtime.select("owner") is old


async def test_verification_loop_does_not_acknowledge_early(tmp_path):
    from lc_factory.background import Job
    from langchain.agents.middleware.types import hook_config
    observations = []
    class VerifyAgain(AgentMiddleware):
        @hook_config(can_jump_to=["model"])
        async def aafter_agent(self, state, runtime):
            observations.append(owner.background.pending("owner"))
            if len(observations) == 1:
                return {"jump_to": "model"}
    model = _ToolBindingFakeModel(messages=iter([AIMessage("first pass"), AIMessage("verified")]))
    async with await FactoryRuntime.create(agent_kwargs=args(tmp_path, model, middleware={"first": [VerifyAgain()]}),
            options=RuntimeOptions(background=True), workspace_id="w") as owner:
        owner.background.jobs["job"] = Job("owner", "worker", status="completed", result="evidence")
        result = await owner.ainvoke({"messages": [HumanMessage("verify")]}, {"configurable": {"thread_id": "owner"}})
        assert result["messages"][-1].content == "verified"
        assert observations == [{"job": "evidence"}, {"job": "evidence"}]
        assert owner.background.pending("owner") == {}
        assert "owner" not in owner._thread_generations


async def test_internal_completion_channels_cannot_be_supplied_as_input(tmp_path):
    from lc_factory.background import Job
    class FailBefore(AgentMiddleware):
        async def abefore_model(self, state, runtime):
            raise ValueError("not completed")
    async with await FactoryRuntime.create(agent_kwargs=args(tmp_path, middleware=[FailBefore()]),
            options=RuntimeOptions(background=True), workspace_id="w") as runtime:
        properties = runtime.current.agent.get_input_jsonschema()["properties"]
        assert not any(key.startswith("_factory_") for key in properties)
        runtime.background.jobs["job"] = Job("owner", "worker", status="completed", result="keep me")
        with pytest.raises(ValueError, match="not completed"):
            await runtime.ainvoke({"messages": [HumanMessage("continue")],
                                  "_factory_turn_complete": True,
                                  "_factory_delivery_complete": ["job"]},
                                 {"configurable": {"thread_id": "owner"}})
        assert runtime.background.pending("owner") == {"job": "keep me"}
        assert "owner" in runtime._thread_generations


async def test_stream_without_saver_acknowledges_only_completed_delivery(tmp_path):
    from lc_factory.background import Job
    kwargs = args(tmp_path)
    kwargs["checkpointer"] = None
    async with await FactoryRuntime.create(agent_kwargs=kwargs, options=RuntimeOptions(background=True), workspace_id="w") as runtime:
        runtime.background.jobs["job"] = Job("owner", "worker", status="completed", result="streamed result")
        async for _ in runtime.astream({"messages": [HumanMessage("continue")]}, {"configurable": {"thread_id": "owner"}}):
            pass
        assert runtime.background.pending("owner") == {}
        assert runtime._completion_candidates == {}
        assert runtime._thread_generations == {}


async def test_cancelled_waiting_turn_does_not_consume_stream_completion(tmp_path):
    from lc_factory.background import Job
    kwargs = args(tmp_path)
    kwargs["checkpointer"] = None
    async with await FactoryRuntime.create(agent_kwargs=kwargs, options=RuntimeOptions(background=True), workspace_id="w") as runtime:
        runtime.background.jobs["job"] = Job("owner", "worker", status="completed", result="streamed result")
        config = {"configurable": {"thread_id": "owner"}}
        stream = runtime.astream({"messages": [HumanMessage("continue")]}, config)
        async for chunk in stream:
            if "RuntimeLifecycleMiddleware.after_agent" in chunk:
                break
        assert "owner" in runtime._completion_candidates
        waiting = asyncio.create_task(runtime.ainvoke({"messages": [HumanMessage("wait")]}, config))
        await asyncio.sleep(0)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert "owner" in runtime._completion_candidates
        async for _ in stream:
            pass
        assert not runtime.background.pending("owner")
        assert runtime._turn_locks == {}


@pytest.mark.parametrize("output", [{"output_keys": "messages"}, {"output_keys": ["messages"]}, {"stream_mode": "updates"}])
@pytest.mark.parametrize("persistent", [True, False])
async def test_invocation_output_selection_preserves_completion(tmp_path, output, persistent):
    from lc_factory.background import Job
    kwargs = args(tmp_path)
    if not persistent:
        kwargs["checkpointer"] = None
    async with await FactoryRuntime.create(agent_kwargs=kwargs, options=RuntimeOptions(background=True), workspace_id="w") as runtime:
        runtime.background.jobs["job"] = Job("owner", "worker", status="completed", result="evidence")
        result = await runtime.ainvoke({"messages": [HumanMessage("continue")]}, {"configurable": {"thread_id": "owner"}}, **output)
        assert result
        assert not runtime.background.pending("owner")
        assert "owner" not in runtime._thread_generations


async def test_server_deletion_releases_generation_only_after_success(tmp_path, monkeypatch):
    from lc_factory import server_graph, server_checkpointer
    monkeypatch.setenv("LC_FACTORY_CAPABILITIES", "reload")
    monkeypatch.setenv("DEEPAGENTS_CODE_SERVER_DB_PATH", str(tmp_path / "server.sqlite"))
    async with await FactoryRuntime.create(agent_kwargs=args(tmp_path), options=RuntimeOptions(reload=True), workspace_id="w") as runtime:
        anchor = await runtime.select("paused")
        monkeypatch.setattr(server_graph, "_factory_runtime_owners", {id(anchor.agent): runtime})
        runtime.request_reload()
        newer = await runtime.select()
        assert newer is not anchor
        async with server_checkpointer.create_checkpointer() as saver:
            original = saver.checkpointer.adelete_thread
            async def fail(_):
                raise OSError("deletion failed")
            monkeypatch.setattr(saver.checkpointer, "adelete_thread", fail)
            with pytest.raises(OSError, match="deletion failed"):
                await saver.adelete_thread("paused")
            assert runtime._thread_generations["paused"] is anchor
            monkeypatch.setattr(saver.checkpointer, "adelete_thread", original)
            await saver.adelete_thread("paused")
            assert "paused" not in runtime._thread_generations
            assert await runtime.select("paused") is newer
