"""Exercise the web adapter without starting a server or calling a model."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from starlette.applications import Starlette

from lc_factory import web_api


@pytest.fixture
def setup_web(monkeypatch):
    binding = SimpleNamespace(cwd="/project", project_root="/project", resource_key="web-test")
    validate = AsyncMock(return_value=binding)
    monkeypatch.setattr(web_api, "require_thread_workspace", validate)
    app = Starlette()
    web_api.install_web_routes(app)
    web_api.install_web_routes(app)
    return app, binding, validate


async def test_catalog_uses_current_graph_and_binding(setup_web, monkeypatch):
    app, binding, validate = setup_web
    assert len(app.routes) == 1
    tool = SimpleNamespace(name="read_file", description="Read a file")
    runtime = SimpleNamespace(agent=SimpleNamespace(nodes={"tools": SimpleNamespace(bound=SimpleNamespace(tools_by_name={"read_file": tool}))}))
    from lc_factory import server_graph
    selected = AsyncMock(return_value=runtime)
    monkeypatch.setattr(server_graph, "_workspace_runtime", selected)
    monkeypatch.setattr(web_api, "discover", lambda b: ([{"name": "review", "path": "/project/skills/review/SKILL.md", "description": "Review code"}], []))
    monkeypatch.setattr(web_api.ServerConfig, "from_env", lambda: SimpleNamespace(model="test:model", enable_ask_user=True, enable_shell=True))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/lc-factory/threads/t/web", json={"workspace": {"generation": 3}, "operation": "catalog"})
    assert response.status_code == 200
    assert response.json()["tools"] == [{"name": "read_file", "description": "Read a file"}]
    assert response.json()["model"] == "test:model"
    validate.assert_awaited_once_with("t", {"generation": 3})
    selected.assert_awaited_once_with(binding)


def test_skill_invocation_requires_catalog_membership_and_nonempty_roots(monkeypatch, tmp_path):
    skill = tmp_path / "SKILL.md"
    skill.write_text("---\nname: review\ndescription: Review\n---\nRead carefully.")
    row = {"name": "review", "description": "Review", "path": str(skill)}
    monkeypatch.setattr(web_api, "discover", lambda b: ([row], [tmp_path]))
    result = web_api.skill_message(None, str(skill), "Review this change")
    assert result["additional_kwargs"]["__skill"]["name"] == "review"
    assert "Read carefully." in result["content"]
    with pytest.raises(ValueError):
        web_api.skill_message(None, "/etc/passwd", "Read")
    monkeypatch.setattr(web_api, "discover", lambda b: ([row], []))
    with pytest.raises(ValueError):
        web_api.skill_message(None, str(skill), "Read")


async def test_catalog_rejects_bad_operations_and_stale_binding(setup_web):
    app, _, validate = setup_web
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post("/lc-factory/threads/t/web", json={"operation": "read_anywhere"})).status_code == 422
        validate.side_effect = ValueError("Missing workspace")
        response = await client.post("/lc-factory/threads/t/web", json={"operation": "catalog"})
        assert response.status_code == 422


async def test_backend_launcher_restores_scaffold_and_stops_child(monkeypatch, tmp_path):
    from lc_factory import web_backend
    from contextlib import nullcontext
    from unittest.mock import Mock
    server = SimpleNamespace(url="http://127.0.0.1:2024", stop=Mock())
    starter = AsyncMock(return_value=(None, server, None))
    monkeypatch.setattr(web_backend, "start_server_and_get_agent", starter)
    monkeypatch.setattr(web_backend, "client_runtime_environment", nullcontext)
    class Done:
        async def wait(self): pass
        def set(self): pass
    monkeypatch.setattr(web_backend.asyncio, "Event", Done)
    monkeypatch.setattr(web_backend.asyncio, "get_running_loop", lambda: SimpleNamespace(add_signal_handler=lambda *args: None))
    original = web_backend.server_manager_module._scaffold_workspace
    await web_backend.serve(SimpleNamespace(cwd=str(tmp_path), port=2024, model=None))
    server.stop.assert_called_once()
    assert web_backend.server_manager_module._scaffold_workspace is original
    assert starter.call_args.kwargs["auto_approve"] is False


async def test_backend_port_collision_stops_only_its_fallback(monkeypatch, tmp_path):
    from lc_factory import web_backend
    from contextlib import nullcontext
    from unittest.mock import Mock
    server = SimpleNamespace(url="http://127.0.0.1:2025", stop=Mock())
    monkeypatch.setattr(web_backend, "start_server_and_get_agent", AsyncMock(return_value=(None, server, None)))
    monkeypatch.setattr(web_backend, "client_runtime_environment", nullcontext)
    original = web_backend.server_manager_module._scaffold_workspace
    with pytest.raises(RuntimeError, match="2024 is occupied"):
        await web_backend.serve(SimpleNamespace(cwd=str(tmp_path), port=2024, model=None))
    server.stop.assert_called_once()
    assert web_backend.server_manager_module._scaffold_workspace is original


async def test_catalog_observes_pinned_generation_without_pinning_idle_thread(monkeypatch):
    from lc_factory import server_graph
    from lc_factory.runtime import FactoryRuntime, RuntimeOptions
    owner = FactoryRuntime(agent_kwargs={"environ": {}}, options=RuntimeOptions(reload=True), workspace_id="w")
    first = SimpleNamespace(agent=object())
    second = SimpleNamespace(agent=object())
    owner.current = first
    async def rebuild():
        owner.current = second
    monkeypatch.setattr(owner, "_build", rebuild)
    async def select_current(binding):
        return await owner.select()
    monkeypatch.setattr(server_graph, "_workspace_runtime", select_current)
    monkeypatch.setitem(server_graph._workspace_runtimes, "web-test", first)
    monkeypatch.setitem(server_graph._factory_runtime_owners, id(first.agent), owner)
    binding = SimpleNamespace(resource_key="web-test")
    assert await owner.select("paused") is first
    assert await web_api.catalog_runtime(binding, "idle") is first
    assert "idle" not in owner._thread_generations
    owner.request_reload()
    assert await owner.select("idle") is second
    assert await web_api.catalog_runtime(binding, "paused") is first
