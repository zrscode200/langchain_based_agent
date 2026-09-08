"""MCP generation ownership across actual factory reloads and setup failures."""
import asyncio

import pytest
from deepagents_code._fake_models import _ToolBindingFakeModel
from langchain_core.messages import AIMessage
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool

from lc_factory.mcp_resources import MCPToolBundle
from lc_factory.runtime import FactoryRuntime, RuntimeOptions


class Manager:
    def __init__(self, log=None):
        self.closed = False
        self.log = log if log is not None else []

    async def cleanup(self):
        self.closed = True
        self.log.append("resources_closed")


def bundle(name="source", *, log=None):
    manager = Manager(log)

    @tool(name)
    async def source() -> str:
        """Read the generation's bound source."""
        if manager.closed:
            raise RuntimeError("Generation is closed")
        return name

    return MCPToolBundle([source], [], [source], manager)


def args(tmp_path, resources):
    return dict(model=_ToolBindingFakeModel(messages=iter([AIMessage("done")])),
        assistant_id="resource-test", cwd=tmp_path, enable_memory=False, enable_skills=False,
        enable_ask_user=False, interactive=False, auto_approve=True,
        tools=resources.tools, mcp_tools=resources.mcp_tools)


async def test_successful_generations_remain_alive_and_capacity_prevents_discovery(tmp_path):
    old, new = bundle("old_source"), bundle("new_source")
    calls = []

    async def load():
        calls.append("discovery")
        return new

    async with await FactoryRuntime.create(agent_kwargs=args(tmp_path, old),
        initial_resources=old, reload_tools=load, options=RuntimeOptions(reload=True),
        workspace_id="w", max_retained_generations=2) as runtime:
        first = runtime.current
        runtime.request_reload()
        second = await runtime.select()
        assert first is not second
        assert not old.closed and not new.closed
        assert await first.agent.nodes["tools"].bound.tools_by_name["old_source"].ainvoke({}) == "old_source"
        assert await second.agent.nodes["tools"].bound.tools_by_name["new_source"].ainvoke({}) == "new_source"
        runtime.request_reload()
        assert await runtime.select() is second
        assert calls == ["discovery"]
        assert runtime.last_reload_error == "ResourceCapacityError"
        assert "restart" in runtime.last_reload_message
    assert old.closed and new.closed


async def test_invalid_candidate_closes_only_new_resources(tmp_path):
    old, candidate = bundle("old_source"), bundle("new_source")
    candidate.info = [{"status": "error"}]

    async def load():
        return candidate

    async with await FactoryRuntime.create(agent_kwargs=args(tmp_path, old),
        initial_resources=old, reload_tools=load, options=RuntimeOptions(reload=True), workspace_id="w") as runtime:
        first = runtime.current
        runtime.request_reload()
        assert await runtime.select() is first
        assert candidate.closed and not old.closed
        assert len(runtime._resource_bundles) == 1


async def test_empty_mcp_configuration_does_not_exhaust_reload_capacity(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from lc_factory import mcp_reload

    monkeypatch.setattr(mcp_reload, "discover_plugin_mcp_configs", lambda **kw: [])
    async def resolve(**kwargs):
        return [], None, []
    monkeypatch.setattr(mcp_reload, "resolve_and_load_mcp_tools", resolve)
    async def load():
        return await mcp_reload.build_reloadable_tools(SimpleNamespace(
            no_mcp=False, mcp_config_path=None, trust_project_mcp=True), None)
    initial = await load()
    async with await FactoryRuntime.create(agent_kwargs=args(tmp_path, initial),
        initial_resources=initial, reload_tools=load, options=RuntimeOptions(reload=True),
        workspace_id="w", max_retained_generations=2) as runtime:
        for _ in range(3):
            runtime.request_reload()
            await runtime.select()
        assert runtime.generation == 4
        assert not runtime._resource_bundles
        assert runtime.last_reload_error is None


async def test_compile_failure_and_cancellation_close_candidate(tmp_path, monkeypatch):
    from lc_factory import runtime as module
    old, candidate = bundle("old_source"), bundle("new_source")
    ready, release = asyncio.Event(), asyncio.Event()

    async def load():
        return candidate

    async with await FactoryRuntime.create(agent_kwargs=args(tmp_path, old),
        initial_resources=old, reload_tools=load, options=RuntimeOptions(reload=True), workspace_id="w") as runtime:
        def fail(**kwargs):
            raise ValueError("Compilation failed after discovery")
        monkeypatch.setattr(module, "create_factory_agent", fail)
        runtime.request_reload()
        await runtime.select()
        assert candidate.closed and not old.closed
        next_bundle = bundle("cancelled_source")
        async def next_load():
            return next_bundle
        runtime.reload_tools = next_load
        original_to_thread = module.asyncio.to_thread
        async def pause_builder(function, *args, **kwargs):
            if function is fail:
                ready.set()
                await release.wait()
            return await original_to_thread(function, *args, **kwargs)
        monkeypatch.setattr(module.asyncio, "to_thread", pause_builder)
        runtime.request_reload()
        selecting = asyncio.create_task(runtime.select())
        await ready.wait()
        selecting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await selecting
        assert next_bundle.closed and not old.closed


async def test_initial_constructor_failure_closes_resources(tmp_path):
    resources = bundle()
    with pytest.raises(ValueError, match="History requires"):
        await FactoryRuntime.create(agent_kwargs=args(tmp_path, resources), initial_resources=resources,
            options=RuntimeOptions(history=True), workspace_id="w")
    assert resources.closed


async def test_shutdown_cancels_workers_before_closing_resources(tmp_path):
    from lc_factory.background import Job
    log, started = [], asyncio.Event()
    resources = bundle(log=log)
    runtime = await FactoryRuntime.create(agent_kwargs=args(tmp_path, resources),
        initial_resources=resources, options=RuntimeOptions(background=True), workspace_id="w")
    async def worker():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            assert not resources.closed
            log.append("worker_stopped")
    task = asyncio.create_task(worker())
    runtime.background.jobs["j"] = Job("owner", "child", worker=task)
    await started.wait()
    await runtime.close()
    assert log == ["worker_stopped", "resources_closed"]


async def test_server_early_setup_failure_closes_initial_bundle(tmp_path, monkeypatch):
    from lc_factory import server_graph, mcp_reload
    from lc_factory.upstream import ServerConfig
    from deepagents_code.config import ModelResult
    resources = bundle()
    monkeypatch.setenv("LC_FACTORY_CAPABILITIES", "reload")
    monkeypatch.setattr(server_graph, "create_model", lambda *a, **kw:
        ModelResult(model=_ToolBindingFakeModel(), model_name="fixture", provider="fixture"))
    async def load(*a, **kw):
        return resources
    monkeypatch.setattr(mcp_reload, "build_reloadable_tools", load)
    def fail(*a, **kw):
        raise ValueError("Early setup failure")
    monkeypatch.setattr(server_graph, "_criteria_context_tools", fail)
    with pytest.raises(ValueError, match="Early setup failure"):
        await server_graph._make_graphs(config_override=ServerConfig(
            model="fixture:test", cwd=str(tmp_path), no_mcp=True,
            enable_memory=False, enable_skills=False, interactive=False))
    assert resources.closed


async def test_detached_worker_keeps_old_connector_after_reload(tmp_path):
    from langgraph.checkpoint.memory import InMemorySaver
    ready, release = asyncio.Event(), asyncio.Event()
    manager = Manager()

    @tool
    async def slow_source() -> str:
        """Finish reading an earlier generation after a reload."""
        ready.set()
        await release.wait()
        assert not manager.closed
        return "old connector result"

    old = MCPToolBundle([slow_source], [], [slow_source], manager)
    new = bundle("new_source")
    def call(name, kwargs, identifier):
        return AIMessage(content="", tool_calls=[dict(name=name, args=kwargs, id=identifier, type="tool_call")])
    child = _ToolBindingFakeModel(messages=iter([
        call("slow_source", {}, "read"), AIMessage("old connector result"),
    ]))
    kwargs = args(tmp_path, old)
    kwargs.update(model=_ToolBindingFakeModel(messages=iter([
        call("start_background_task", {"description": "Read", "subagent_type": "child"}, "delegate"), AIMessage("parent done"),
    ])), checkpointer=InMemorySaver(), subagents=[dict(name="child", description="Child", model=child)])
    async def load():
        return new
    async with await FactoryRuntime.create(agent_kwargs=kwargs, initial_resources=old,
        reload_tools=load, options=RuntimeOptions(reload=True, background=True), workspace_id="w") as runtime:
        await runtime.ainvoke({"messages": [HumanMessage("Start")]}, {"configurable": {"thread_id": "owner"}})
        await asyncio.wait_for(ready.wait(), 5)
        runtime.request_reload()
        await runtime.select()
        assert runtime.generation == 2 and not old.closed
        release.set()
        results = await asyncio.wait_for(runtime.background.wait("owner"), 5)
        assert "old connector result" in next(iter(results.values()))
        assert not old.closed
    assert old.closed and new.closed
