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
    def inspect(owner, task_id, *, transcript_page):
        assert transcript_page == -1
        calls.append((owner, task_id))
        return {"task_id": task_id, "status": "completed", "result": "Reported result", "activity": [], "steering": []}
    tasks = SimpleNamespace(jobs={"own": SimpleNamespace(owner="owner"), "other": SimpleNamespace(owner="another")}, host_inspect=inspect)
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


async def test_structured_host_routes_preserve_owner_boundary_and_do_not_consume_results(monkeypatch):
    from langchain_core.messages import AIMessage
    from lc_factory.background import BackgroundTasks, Job
    import lc_factory.background_api as api
    import lc_factory.server_graph as server
    tasks = BackgroundTasks()
    own, other = Job('owner', 'child', status='completed'), Job('other', 'other', status='completed')
    tasks.jobs.update(own=own, other=other)
    own.transcript.capture([AIMessage('Retained result', id='answer')])
    async def require(owner, descriptor): return 'bound'
    async def get_tasks(binding): return tasks
    monkeypatch.setattr(api, 'require_thread_workspace', require)
    monkeypatch.setattr(server, 'background_for_workspace', get_tasks)
    app = Starlette(); install_background_routes(app)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            path = '/lc-factory/threads/owner/background'
            body = {'workspace': {}, 'operation': 'conversation', 'task_id': 'own'}
            response = await client.post(path, json=body)
            assert response.status_code == 200
            records = response.json()['task']['conversation_records']
            message = records['messages'][0]
            full = {**body, 'operation': 'message', 'message_id': message['id'], 'revision': message['_transcript']['revision']}
            assert (await client.post(path, json=full)).status_code == 200
            for operation in ('message', 'conversation'):
                assert (await client.post(path, json={**full, 'operation': operation, 'task_id': 'other'})).status_code == 404
            assert (await client.post(path, json={**body, 'after': True})).status_code == 422
            assert (await client.post(path, json={**full, 'revision': 999})).status_code == 422
        assert not own.acknowledged
        assert 'conversation_records' not in tasks.list('owner')[0]
    finally:
        own.transcript.close(); other.transcript.close()


async def test_host_list_adds_timing_queue_and_latest_without_widening_model_projection(monkeypatch):
    from lc_factory.background import BackgroundTasks, Job
    import lc_factory.background_api as api
    import lc_factory.server_graph as server
    tasks = BackgroundTasks(max_running=1)
    running = Job("owner", "child", status="running", queued_at=99.0, started_at=100.0, updated_at=100.0)
    running.record("finding", text="Observed setup steps. " * 40)
    running.record("tool", tool_name="report_background_task", status="completed")
    first, second = Job("owner", "child", status="queued"), Job("owner", "child", status="queued")
    done = Job("owner", "child", status="completed", result="Done", started_at=101.0, updated_at=130.0, finished_at=130.0)
    tasks.jobs.update(running=running, first=first, foreign=Job("another", "child", status="queued"), second=second, done=done)
    tasks._queue.extend(["first", "foreign", "second", "stale"])
    async def require(owner, descriptor):
        return "bound"
    async def get_tasks(binding):
        return tasks
    monkeypatch.setattr(api, "require_thread_workspace", require)
    monkeypatch.setattr(server, "background_for_workspace", get_tasks)
    app = Starlette()
    install_background_routes(app)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        listed = (await client.post("/lc-factory/threads/owner/background", json={"workspace": {}, "operation": "list"})).json()
        for operation in ("inspect", "conversation"):
            body = {"workspace": {}, "operation": operation, "task_id": "running"}
            response = await client.post("/lc-factory/threads/owner/background", json=body)
            assert response.status_code == 200
            detail = response.json()["task"]
            assert (detail["started_at"], detail["updated_at"]) == (100.0, 100.0)
            body["task_id"] = "second"
            assert (await client.post("/lc-factory/threads/owner/background", json=body)).json()["task"]["queue_position"] == 3
            body["task_id"] = "foreign"
            assert (await client.post("/lc-factory/threads/owner/background", json=body)).status_code == 404
    rows = {row["task_id"]: row for row in listed["tasks"]}
    assert set(rows) == {"running", "first", "second", "done"}
    latest = rows["running"]["latest"]
    assert latest["kind"] == "finding" and latest["text"].startswith("Observed") and len(latest["text"]) <= 240
    assert (rows["running"]["queued_at"], rows["running"]["started_at"]) == (99.0, 100.0)
    assert "queue_position" not in rows["running"]
    assert rows["first"]["queue_position"] == 1 and rows["second"]["queue_position"] == 3
    assert rows["done"]["finished_at"] == 130.0 and rows["done"]["acknowledged"] is False and rows["done"]["latest"] is None
    assert listed["capacity"] == {"running": 0, "max_running": 1, "queued": 3, "retained": 5, "max_jobs": 128}
    assert listed["pending_results"] == ["done"] and not done.acknowledged
    host_only = {"latest", "queued_at", "started_at", "updated_at", "finished_at", "queue_position", "acknowledged"}
    for row in tasks.list("owner", inspection=True) + tasks.list("owner") + [tasks.inspect("owner", "running")]:
        assert not host_only & set(row)
    with pytest.raises(ValueError):
        tasks.host_inspect("owner", "foreign")


async def test_job_timestamps_follow_queue_launch_and_completion():
    import asyncio
    from langchain_core.messages import AIMessage
    from lc_factory.background import BackgroundTasks, Job
    tasks = BackgroundTasks(max_running=1)
    async def ainvoke(value, config):
        await asyncio.sleep(0.01)
        return {"result": AIMessage("finished")}
    job = Job("owner", "child", graph=SimpleNamespace(ainvoke=ainvoke))
    tasks.jobs["job"] = job
    tasks._enqueue("job", job, {})
    assert job.status == "running" and job.queued_at is not None and job.started_at is not None
    assert job.started_at >= job.queued_at and tasks.capacity()["running"] == 1
    await job.worker
    assert job.status == "completed" and job.finished_at is not None and job.finished_at >= job.started_at
    assert job.updated_at == job.finished_at
    row = tasks.host_list("owner")[0]
    assert row["finished_at"] == job.finished_at and row["acknowledged"] is False
    assert tasks.capacity() == {"running": 0, "max_running": 1, "queued": 0, "retained": 1, "max_jobs": 128}
