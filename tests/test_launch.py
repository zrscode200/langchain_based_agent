"""Wave 1.2: the micro-launcher scaffolds a factory-graph server workspace."""

from __future__ import annotations

import ast
import json
from importlib.metadata import version
from pathlib import Path

from lc_factory import launch


def test_scaffold_workspace_targets_factory_graph(tmp_path):
    launch.scaffold_workspace(tmp_path)

    config = json.loads((tmp_path / "langgraph.json").read_text())
    assert config["graphs"] == {"agent": "lc_factory.server_graph:make_graph"}
    assert config["http"] == {
        "app": "lc_factory.offload_api:app",
        "enable_custom_route_auth": True,
    }

    checkpointer = (tmp_path / "checkpointer.py").read_text()
    assert "AsyncSqliteSaver" in checkpointer
    assert "DEEPAGENTS_CODE_SERVER_DB_PATH" in checkpointer

    pyproject = (tmp_path / "pyproject.toml").read_text()
    assert "lc-factory-server-runtime" in pyproject
    assert "lc_factory" in pyproject
    assert 'requires-python = ">=3.12"' in pyproject


def test_offload_adapter_reuses_upstream_app_with_factory_runtime():
    import deepagents_code.offload_api as upstream_offload_api

    from lc_factory import offload_api
    from lc_factory.server_graph import _workspace_runtime

    assert offload_api.app is upstream_offload_api.app
    assert upstream_offload_api.get_server_runtime is _workspace_runtime


async def test_offload_adapter_serves_upstream_operation_routes():
    import httpx

    from lc_factory import offload_api

    transport = httpx.ASGITransport(app=offload_api.app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://factory.test",
    ) as client:
        response = await client.post(
            "/dcode/threads/factory-probe/offload/probe-operation/cancel",
            json={},
        )

    assert response.status_code == 200, response.text
    assert response.json() == {"status": "cancelled"}


async def test_offload_adapter_operation_route_uses_factory_runtime(monkeypatch, tmp_path):
    import httpx
    import deepagents_code.offload_api as upstream_offload_api
    from deepagents_code.offload_middleware import (
        OffloadExecution,
        unchanged_offload_result,
    )
    from deepagents_code.workspace import bind_thread_workspace

    from lc_factory import offload_api, server_graph
    from lc_factory.upstream import ServerConfig, ServerRuntime

    monkeypatch.setenv("DEEPAGENTS_CODE_SERVER_DB_PATH", str(tmp_path / "sessions.db"))
    config = ServerConfig.from_env()
    binding = await bind_thread_workspace(
        "factory-offload",
        str(tmp_path),
        config.to_workspace_payload(),
        config_fingerprint=config.workspace_fingerprint(),
    )
    monkeypatch.setattr(server_graph, "_workspace_runtimes", server_graph.OrderedDict())
    monkeypatch.setattr(server_graph, "_workspace_runtime_locks", {})

    checkpoint = {
        "checkpoint": {"checkpoint_id": "checkpoint-1"},
        "next": [],
        "tasks": [],
        "interrupts": [],
        "values": {
            "messages": [{"type": "human", "content": "please summarize"}],
            "_model_spec": "openai:gpt-5-mini",
            "_model_params": {"temperature": 0},
        },
    }
    calls: dict[str, object] = {}

    class FakeThreads:
        async def get(self, thread_id):
            calls["thread_get"] = thread_id
            return {"status": "idle"}

        async def get_state(self, thread_id):
            calls["thread_state"] = thread_id
            return checkpoint

        async def update_state(self, thread_id, update):
            calls["thread_update"] = (thread_id, update)

    class FakeClient:
        threads = FakeThreads()

    class FakeOffload:
        async def execute(self, state, runtime):
            calls["runtime_store"] = runtime.store
            calls["runtime_model"] = runtime.context["model"]
            calls["state_messages"] = len(state["messages"])
            result = unchanged_offload_result("noop", messages=1, tokens=0)
            return OffloadExecution({}, result)

    async def fake_make_graphs(*, config_override, project_context_override):
        calls["factory_runtime"] = True
        assert config_override.cwd == binding.cwd
        assert str(project_context_override.user_cwd) == binding.cwd
        agent = type("FakeAgent", (), {"store": "factory-store"})()
        return ServerRuntime(agent=agent, backend=object(), offload=FakeOffload())

    monkeypatch.setattr(upstream_offload_api, "_client", FakeClient())
    monkeypatch.setattr(server_graph, "_make_graphs", fake_make_graphs)

    transport = httpx.ASGITransport(app=offload_api.app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://factory.test",
    ) as client:
        response = await client.post(
            "/dcode/threads/factory-offload/offload",
            json={
                "operation_id": "positive-operation",
                "context": {"workspace": binding.to_payload()},
            },
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "complete"
    assert body["result"]["status"] == "noop"
    assert calls == {
        "thread_get": "factory-offload",
        "thread_state": "factory-offload",
        "factory_runtime": True,
        "runtime_store": "factory-store",
        "runtime_model": "openai:gpt-5-mini",
        "state_messages": 1,
    }


def test_generated_workspace_cannot_preempt_the_reservation(tmp_path):
    """D4 precondition, pinned (previously a manual bump-time check).

    The reservation guard in `lc_factory/__init__.py` assumes no upstream
    code runs in the server process before the graph module imports
    `lc_factory`. Two workspace artifacts could break that silently on a pin
    bump: `checkpointer.py` (imported by the langgraph loader — an upstream
    import there could touch the lazy credentials bootstrap and load a
    repository `.env`, ahead of the reservation) and an `env` key in
    `langgraph.json` (langgraph loads that dotenv before importing the graph
    module, outside the reservation's reach entirely).
    """
    launch.scaffold_workspace(tmp_path)

    config = json.loads((tmp_path / "langgraph.json").read_text())
    assert "env" not in config

    tree = ast.parse((tmp_path / "checkpointer.py").read_text())
    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".")[0])
    assert not imported_roots & {"deepagents", "deepagents_code"}


def test_runtime_package_dependency_editable_root():
    # Editable checkout: direct file URI to this project root (src layout:
    # three levels up from launch.py).
    root = Path(launch.__file__).resolve().parent.parent.parent
    dep = launch._runtime_package_dependency()
    assert dep == f"lc_factory @ {root.as_uri()}"


def test_runtime_package_dependency_fallback(tmp_path):
    # A root without pyproject.toml falls back to the installed distribution.
    dep = launch._runtime_package_dependency(package_root=tmp_path)
    assert dep == f"lc_factory=={version('lc_factory')}"


async def test_upstream_launcher_serves_the_factory_graph(monkeypatch, tmp_path):
    """End-to-end seam proof, without spawning a server.

    The rebind test below proves `tui.main` sets the module global; this
    proves upstream's *real* `start_server_and_get_agent` then reads it and
    emits a `langgraph.json` pointing at the factory graph. Without this,
    a future upstream that captured `_scaffold_workspace` earlier (or
    imported it by name) would silently stop using our graph while every
    other test still passed.
    """
    import deepagents_code.client.launch.server as upstream_server
    import deepagents_code.client.remote_client as upstream_remote

    from lc_factory import launch, tui
    from lc_factory.upstream import server_manager_module

    monkeypatch.setattr(
        server_manager_module,
        "_scaffold_workspace",
        server_manager_module._scaffold_workspace,
    )
    monkeypatch.setattr(tui, "cli_main", lambda: None)
    tui.main()

    captured: dict[str, object] = {}

    class FakeServerProcess:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        url = "http://127.0.0.1:0"

        async def start(self):
            return None

        async def wait_for_graph_ready(self, name):
            captured["graph_name"] = name

        def stop(self):
            return None

    monkeypatch.setattr(upstream_server, "ServerProcess", FakeServerProcess)
    class FakeRemoteAgent:
        def __init__(self, **kwargs):
            captured["remote"] = kwargs

        def set_workspace(self, cwd, workspace_config, *, config_fingerprint):
            captured["workspace"] = (cwd, workspace_config, config_fingerprint)

    monkeypatch.setattr(upstream_remote, "RemoteAgent", FakeRemoteAgent)

    await server_manager_module.start_server_and_get_agent(
        assistant_id="lc-factory-seam",
        no_mcp=True,
        cwd=str(tmp_path),
    )

    work_dir = Path(captured["config_dir"])
    config = json.loads((work_dir / "langgraph.json").read_text())
    assert config["graphs"] == {"agent": launch.GRAPH_REF}, (
        "upstream's launcher did not scaffold the factory graph — the "
        "_scaffold_workspace rebind seam no longer holds"
    )
    # The graph name the client waits on must match what we registered.
    assert captured["graph_name"] == "agent"
    assert captured["remote"]["graph_name"] == "agent"
    # And the rescaffold-on-missing-config path carries the factory scaffold.
    assert captured["scaffold"] is launch.scaffold_workspace
    workspace_cwd, workspace_config, workspace_fingerprint = captured["workspace"]
    assert workspace_cwd == str(tmp_path.resolve())
    assert workspace_config["assistant_id"] == "lc-factory-seam"
    assert workspace_config["no_mcp"] is True
    assert workspace_fingerprint


def test_tui_main_rebinds_scaffold_seam(monkeypatch):
    """The TUI entry rebinds the launch seam before upstream cli_main runs.

    This is the port's single point of upstream coupling: `tui.main` must
    rebind `server_manager._scaffold_workspace` (resolved by upstream as a
    module global at call time) so every launch path — TUI startup, restart,
    cwd switch, headless — scaffolds the FACTORY graph.
    """
    from lc_factory import tui
    from lc_factory.upstream import server_manager_module

    # Register the original with monkeypatch so teardown restores it.
    monkeypatch.setattr(
        server_manager_module,
        "_scaffold_workspace",
        server_manager_module._scaffold_workspace,
    )

    seen = {}

    def fake_cli_main():
        seen["scaffold"] = server_manager_module._scaffold_workspace

    monkeypatch.setattr(tui, "cli_main", fake_cli_main)
    tui.main()

    assert seen["scaffold"] is launch.scaffold_workspace
