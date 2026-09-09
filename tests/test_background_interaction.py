"""Paused workers resume their own checkpoints, never the parent run."""
import asyncio
from types import SimpleNamespace

import httpx
import pytest
from deepagents_code._fake_models import _ToolBindingFakeModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from starlette.applications import Starlette

from lc_factory.background import BackgroundTasks, Job
from lc_factory.background_api import install_background_routes
from lc_factory.background_ui import approval_responses
from lc_factory.runtime import FactoryRuntime, RuntimeOptions


def call(name, arguments, identifier):
    return AIMessage(content="", tool_calls=[dict(name=name, args=arguments, id=identifier, type="tool_call")])


async def start(tmp_path, child, *, reload=False):
    parent = _ToolBindingFakeModel(messages=iter([
        call("start_background_task", {"description": "Do bounded work", "subagent_type": "child"}, "start"),
        AIMessage("Parent remains available"), AIMessage("Another parent turn"),
    ]))
    runtime = await FactoryRuntime.create(agent_kwargs=dict(
        assistant_id="resume-test", cwd=tmp_path, model=parent, subagents=[child],
        enable_memory=False, enable_skills=False, enable_ask_user=False,
        interactive=True, auto_approve=False, checkpointer=InMemorySaver(),
    ), options=RuntimeOptions(background=True, reload=reload), workspace_id="work")
    config = {"configurable": {"thread_id": "owner"}}
    await runtime.ainvoke({"messages": [HumanMessage("start")]}, config)
    await runtime.ainvoke(Command(resume={"decisions": [{"type": "approve"}]}), config)
    await asyncio.wait_for(runtime.background.wait("owner"), 5)
    return runtime, config


@pytest.mark.parametrize("mode", ["isolated", "fork"])
@pytest.mark.parametrize("reload", [False, True])
async def test_two_child_approvals_no_replay_and_main_keeps_running(tmp_path, mode, reload):
    calls = []

    @tool
    def marker() -> str:
        """Record a completed harmless step."""
        calls.append("before")
        return "recorded"

    one, two = tmp_path / "one", tmp_path / "two"
    child = dict(name="child", description="Child", mode=mode, tools=[marker],
        model=_ToolBindingFakeModel(messages=iter([
            call("marker", {}, "mark"),
            call("write_file", {"file_path": str(one), "content": "one"}, "one"),
            call("write_file", {"file_path": str(two), "content": "two"}, "two"), AIMessage("done"),
        ])))
    runtime, config = await start(tmp_path, child, reload=reload)
    async with runtime:
        tasks = runtime.background
        first = tasks.list("owner")[0]
        assert first["status"] == "needs_approval" and calls == ["before"]
        assert not one.exists() and not tasks.pending("owner")
        previous = runtime.current
        if reload:
            runtime.request_reload()
        await runtime.ainvoke({"messages": [HumanMessage("continue talking")]}, config)
        if reload:
            assert runtime.current is not previous and runtime.background is tasks
        assert tasks.list("owner")[0]["interrupts"] == first["interrupts"]
        responses = approval_responses(first, "approve")
        tasks.max_running = 0
        with pytest.raises(ValueError, match="capacity"):
            tasks.resume("owner", first["task_id"], responses)
        assert tasks.list("owner")[0]["interrupts"] == first["interrupts"]
        tasks.max_running = 4
        with pytest.raises(ValueError):
            tasks.resume("other", first["task_id"], responses)
        with pytest.raises(ValueError):
            tasks.resume("owner", first["task_id"], {"wrong-id": {"decisions": [{"type": "approve"}]}})
        tasks.resume("owner", first["task_id"], responses)
        with pytest.raises(ValueError):
            tasks.resume("owner", first["task_id"], responses)
        await asyncio.wait_for(tasks.wait("owner"), 5)
        second = tasks.list("owner")[0]
        assert second["status"] == "needs_approval"
        assert one.read_text() == "one" and not two.exists() and calls == ["before"]
        assert second["interrupts"][0]["id"] != first["interrupts"][0]["id"]
        with pytest.raises(ValueError):
            tasks.resume("owner", first["task_id"], responses)
        tasks.resume("owner", second["task_id"], approval_responses(second, "reject"))
        await asyncio.wait_for(tasks.wait("owner"), 5)
        assert tasks.list("owner")[0]["status"] == "completed"
        assert not two.exists() and calls == ["before"]


async def test_cancelling_before_resume_starts_releases_checkpoint(tmp_path):
    target = tmp_path / "cancelled"
    child = dict(name="child", description="Child", model=_ToolBindingFakeModel(messages=iter([
        call("write_file", {"file_path": str(target), "content": "no"}, "write"), AIMessage("done"),
    ])))
    runtime, _ = await start(tmp_path, child)
    async with runtime:
        tasks = runtime.background
        job = tasks.list("owner")[0]
        tasks.resume("owner", job["task_id"], approval_responses(job, "approve"))
        await tasks.cancel("owner", task_id=job["task_id"])
        assert tasks.jobs[job["task_id"]].graph is None
        assert tasks.jobs[job["task_id"]].config == {}
        assert tasks.list("owner")[0]["status"] == "cancelled"
        assert not target.exists()


async def test_cancel_waiting_child_revokes_resume(tmp_path):
    target = tmp_path / "must-not-exist"
    child = dict(name="child", description="Child", model=_ToolBindingFakeModel(messages=iter([
        call("write_file", {"file_path": str(target), "content": "no"}, "write"),
    ])))
    runtime, _ = await start(tmp_path, child)
    async with runtime:
        job = runtime.background.list("owner")[0]
        await runtime.background.cancel("owner", task_id=job["task_id"])
        with pytest.raises(ValueError):
            runtime.background.resume("owner", job["task_id"], approval_responses(job, "approve"))
        assert runtime.background.list("owner")[0]["status"] == "cancelled"
        assert not target.exists()


async def test_api_checks_workspace_owner_and_does_not_consume_results(monkeypatch):
    import lc_factory.background_api as api
    import lc_factory.server_graph as server
    from lc_factory.upstream import WorkspaceConflictError
    tasks = BackgroundTasks()
    tasks.jobs["own"] = Job("owner", "worker", status="completed", result="evidence")
    tasks.jobs["other"] = Job("another", "worker", status="running")

    async def require(owner, descriptor):
        if owner != "owner" or descriptor != {"workspace_id": "work"}:
            raise WorkspaceConflictError("wrong")
        return "binding"

    async def get_tasks(binding):
        assert binding == "binding"
        return tasks

    monkeypatch.setattr(api, "require_thread_workspace", require)
    monkeypatch.setattr(server, "background_for_workspace", get_tasks)
    app = Starlette()
    install_background_routes(app)
    install_background_routes(app)
    assert len(app.routes) == 1
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        path = "/lc-factory/threads/owner/background"
        assert (await client.post(path, json={"workspace": {}})).status_code == 409
        body = {"workspace": {"workspace_id": "work"}}
        response = await client.post(path, json=body)
        assert [j["task_id"] for j in response.json()["tasks"]] == ["own"]
        assert tasks.pending("owner") == {"own": "evidence"}
        assert (await client.post(path, json={**body, "operation": "cancel", "task_id": "other"})).status_code == 404
        assert (await client.post(path, json={**body, "operation": "resume", "task_id": "own", "responses": {}})).status_code == 422


async def test_runtime_lookup_uses_initial_generation_identity(monkeypatch):
    import lc_factory.server_graph as server
    base, selected, tasks = SimpleNamespace(agent=object()), SimpleNamespace(agent=object()), object()
    binding = SimpleNamespace(resource_key="workspace")
    monkeypatch.setattr(server, "_workspace_runtimes", {"workspace": base})
    monkeypatch.setattr(server, "_factory_runtime_owners", {id(base.agent): SimpleNamespace(background=tasks, closed=False)})
    async def select(_):
        return selected
    monkeypatch.setattr(server, "_workspace_runtime", select)
    assert await server.background_for_workspace(binding) is tasks
