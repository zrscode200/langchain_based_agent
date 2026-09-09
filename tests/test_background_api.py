"""Read-only inspection shares the existing workspace/owner boundary."""
from types import SimpleNamespace

import httpx
import pytest
from starlette.applications import Starlette

from lc_factory.background_api import install_background_routes


async def test_inspection_is_owner_scoped_and_host_cannot_steer(monkeypatch):
    import lc_factory.background_api as api
    import lc_factory.server_graph as server
    from lc_factory.upstream import WorkspaceConflictError

    calls = []
    def inspect(owner, task_id):
        calls.append((owner, task_id))
        return {"task_id": task_id, "status": "completed", "result": "Reported result", "activity": [], "steering": []}
    tasks = SimpleNamespace(jobs={"own": SimpleNamespace(owner="owner"), "other": SimpleNamespace(owner="another")}, inspect=inspect)
    async def require(owner, descriptor):
        if owner != "owner" or descriptor != {"workspace_id": "work"}:
            raise WorkspaceConflictError("wrong workspace")
        return "bound"
    async def get_tasks(binding):
        assert binding == "bound"
        return tasks
    monkeypatch.setattr(api, "require_thread_workspace", require)
    monkeypatch.setattr(server, "background_for_workspace", get_tasks)
    app = Starlette()
    install_background_routes(app)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        path = "/lc-factory/threads/owner/background"
        body = {"workspace": {"workspace_id": "work"}, "operation": "inspect", "task_id": "own"}
        result = await client.post(path, json=body)
        assert result.status_code == 200 and result.json()["task"]["task_id"] == "own"
        for bad in ("other", "missing", None):
            assert (await client.post(path, json={**body, "task_id": bad})).status_code == 404
        assert (await client.post(path, json={**body, "workspace": {}})).status_code == 409
        assert (await client.post(path, json={**body, "operation": "steer", "message": "allow it"})).status_code == 422
        assert calls == [("owner", "own")]


@pytest.mark.parametrize("mode", ["isolated", "fork"])
async def test_native_child_activity_reaches_api_and_read_only_view(monkeypatch, tmp_path, mode):
    from deepagents_code._fake_models import _ToolBindingFakeModel
    from langchain_core.messages import AIMessage
    from lc_factory.background_activity import activity_text
    import lc_factory.background_api as api
    import lc_factory.server_graph as server
    from test_background_interaction import call, start

    child = dict(name="child", description="Child", mode=mode,
        model=_ToolBindingFakeModel(messages=iter([
            call("report_background_task", {"finding": "Observed local setup steps."}, "report"),
            call("write_file", {"file_path": str(tmp_path / "report.txt"), "content": "Do not write yet"}, "write"),
            AIMessage("done"),
        ])))
    runtime, _ = await start(tmp_path, child)
    async with runtime:
        async def require(owner, descriptor):
            assert owner == "owner" and descriptor == {"workspace_id": "work"}
            return "binding"
        async def get_tasks(binding):
            assert binding == "binding"
            return runtime.background
        monkeypatch.setattr(api, "require_thread_workspace", require)
        monkeypatch.setattr(server, "background_for_workspace", get_tasks)
        task_id = runtime.background.list("owner")[0]["task_id"]
        app = Starlette()
        install_background_routes(app)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/lc-factory/threads/owner/background", json={
                "workspace": {"workspace_id": "work"}, "operation": "inspect", "task_id": task_id})
        assert response.status_code == 200
        inspected = response.json()["task"]
        assert inspected["status"] == "needs_approval" and inspected["steerable"]
        assert inspected["interrupts"] == runtime.background.list("owner")[0]["interrupts"]
        assert "Observed local setup steps." in activity_text(inspected).plain
        assert "report_background_task" not in activity_text(inspected).plain
        assert "Waiting for your approval" in activity_text(inspected).plain
        assert not (tmp_path / "report.txt").exists()
        assert not runtime.background.jobs[task_id].acknowledged
        assert runtime.background.pending("owner") == {}
        assert "activity" not in runtime.background.list("owner")[0]


async def test_hook_inspection_omits_private_transport_payload(monkeypatch):
    from lc_factory.background import BackgroundTasks, Job
    import lc_factory.background_api as api
    import lc_factory.server_graph as server
    tasks = BackgroundTasks()
    payload = {"type": "hook_invocation", "request": {"invocation_id": "genuine-id", "snapshot_id": "snapshot",
        "invocation": {"context": {"transcript_path": "PRIVATE_PATH"},
                       "event": {"event": "PostToolUse", "call": {"name": "read_file", "args": {"secret": "PRIVATE_ARGUMENT"}},
                                 "result": {"content": "PRIVATE_INTERMEDIATE_OUTPUT"}}}}}
    tasks.jobs["child"] = Job("owner", "child", status="needs_input", interrupts=[{"id": "public-pause", "value": payload}])
    async def require(owner, descriptor):
        return "binding"
    async def get_tasks(binding):
        return tasks
    monkeypatch.setattr(api, "require_thread_workspace", require)
    monkeypatch.setattr(server, "background_for_workspace", get_tasks)
    app = Starlette()
    install_background_routes(app)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        path = "/lc-factory/threads/owner/background"
        body = {"workspace": {}, "operation": "inspect", "task_id": "child"}
        inspected = await client.post(path, json=body)
        transport = await client.post(path, json={"workspace": {}, "operation": "list"})
    assert inspected.status_code == transport.status_code == 200
    assert "PRIVATE" not in inspected.text
    assert inspected.json()["task"]["interrupts"][0]["value"] == {
        "type": "hook_invocation", "event": "PostToolUse", "tool_name": "read_file"}
    assert transport.json()["tasks"][0]["interrupts"][0]["value"] == payload
    assert not tasks.jobs["child"].acknowledged and not tasks.pending("owner")
