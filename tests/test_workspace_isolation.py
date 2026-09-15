"""Workspace configuration and sandbox ownership cross the constructor seam."""
from __future__ import annotations

import asyncio
import os
from collections import OrderedDict
from types import SimpleNamespace

import pytest

from lc_factory import server_graph
from lc_factory.upstream import ProjectContext, ServerConfig, WorkspaceConflictError, resolve_workspace


@pytest.fixture
def runtime_cache(monkeypatch):
    monkeypatch.setattr(server_graph, "_workspace_runtimes", OrderedDict())
    monkeypatch.setattr(server_graph, "_workspace_runtime_lock", asyncio.Lock())
    monkeypatch.setattr(server_graph, "_sandbox_workspace_id", None)


async def test_concurrent_workspace_snapshots_do_not_change_process_environment(
    monkeypatch, tmp_path,
):
    from deepagents_code.config import active_environment

    for name in ("FACTORY_WORKSPACE_PROBE", "TAVILY_API_KEY", "OPENAI_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    # Project dotenv must never gain the right to choose executable middleware.
    monkeypatch.setenv("LC_FACTORY_MIDDLEWARE", "")
    arrived = []
    ready = asyncio.Event()

    async def inspect_environment(**kwargs):
        snapshot = kwargs["workspace_env"]
        identity = snapshot["FACTORY_WORKSPACE_PROBE"]
        arrived.append(identity)
        if len(arrived) == 2:
            ready.set()
        await asyncio.wait_for(ready.wait(), timeout=5)
        assert active_environment()["FACTORY_WORKSPACE_PROBE"] == identity
        assert active_environment()["OPENAI_BASE_URL"] == f"https://{identity}.invalid/v1"
        assert kwargs["workspace_credentials"].tavily_api_key == f"test-{identity}"
        assert snapshot["LC_FACTORY_MIDDLEWARE"] == ""
        assert server_graph._factory_middleware() is None
        with pytest.raises(TypeError):
            snapshot["FACTORY_WORKSPACE_PROBE"] = "changed"
        return snapshot

    monkeypatch.setattr(server_graph, "_make_graphs_in_environment", inspect_environment)
    projects = []
    for identity in ("alpha", "beta"):
        project = tmp_path / identity
        project.mkdir()
        (project / ".git").mkdir()
        (project / ".env").write_text(
            f"FACTORY_WORKSPACE_PROBE={identity}\n"
            f"OPENAI_BASE_URL=https://{identity}.invalid/v1\n"
            f"TAVILY_API_KEY=test-{identity}\n"
            "LC_FACTORY_MIDDLEWARE=untrusted_project:execute\n"
        )
        projects.append(project)
    before = dict(os.environ)
    snapshots = await asyncio.gather(*(
        server_graph._make_graphs(
            config_override=ServerConfig(cwd=str(project)),
            project_context_override=ProjectContext(user_cwd=project, project_root=project),
        ) for project in projects
    ))
    assert [snapshot["FACTORY_WORKSPACE_PROBE"] for snapshot in snapshots] == ["alpha", "beta"]
    assert dict(os.environ) == before
    assert active_environment() is os.environ


@pytest.mark.parametrize("failed_first_build", [False, True])
async def test_sandbox_claim_survives_failed_build_and_refuses_other_workspace(
    monkeypatch, tmp_path, runtime_cache, failed_first_build,
):
    config = ServerConfig(sandbox_type="modal")
    monkeypatch.setattr(ServerConfig, "from_env", classmethod(lambda cls: config))
    attempts = []

    async def build(**kwargs):
        attempts.append(kwargs["config_override"].cwd)
        if failed_first_build and len(attempts) == 1:
            raise SystemExit(1)
        return object()

    monkeypatch.setattr(server_graph, "_make_graphs", build)
    bindings = []
    for name in ("first", "second"):
        project = tmp_path / name
        project.mkdir()
        bindings.append(resolve_workspace(
            str(project), config.to_workspace_payload(),
            config_fingerprint=config.workspace_fingerprint(),
        ))
    if failed_first_build:
        with pytest.raises(SystemExit):
            await server_graph._workspace_runtime(bindings[0])
    else:
        await server_graph._workspace_runtime(bindings[0])
    with pytest.raises(WorkspaceConflictError, match="process-wide"):
        await server_graph._workspace_runtime(bindings[1])
    assert attempts == [bindings[0].cwd]
    runtime = await server_graph._workspace_runtime(bindings[0])
    assert await server_graph._workspace_runtime(bindings[0]) is runtime
    assert len(attempts) == (2 if failed_first_build else 1)


async def test_default_and_workspace_paths_share_runtime(monkeypatch, tmp_path, runtime_cache):
    config = ServerConfig(cwd=str(tmp_path))
    monkeypatch.setattr(ServerConfig, "from_env", classmethod(lambda cls: config))
    calls = []
    runtime = object()

    async def startup():
        calls.append("startup")
        return runtime

    async def duplicate(**kwargs):
        pytest.fail("The launch runtime must be reused for its workspace")

    monkeypatch.setattr(server_graph, "_get_runtime", startup)
    monkeypatch.setattr(server_graph, "_make_graphs", duplicate)
    binding = resolve_workspace(
        str(tmp_path), config.to_workspace_payload(),
        config_fingerprint=config.workspace_fingerprint(),
    )
    assert await server_graph.get_server_runtime() is runtime
    assert await server_graph._workspace_runtime(binding) is runtime
    assert await server_graph.get_server_runtime() is runtime
    assert calls == ["startup"]


async def test_default_binding_failure_emits_startup_marker(monkeypatch, capsys, runtime_cache):
    async def fail(config):
        raise ValueError("invalid workspace probe")

    monkeypatch.setattr(server_graph, "_default_workspace_binding", fail)
    with pytest.raises(SystemExit):
        await server_graph.get_server_runtime()
    assert server_graph._STARTUP_ERROR_MARKER in capsys.readouterr().err


@pytest.mark.parametrize("failure", ["sandbox_conflict", "startup_exit"])
async def test_workspace_http_refusal_precedes_thread_creation(
    monkeypatch, tmp_path, runtime_cache, failure,
):
    import httpx
    from deepagents_code import offload_api as upstream_api
    from lc_factory import offload_api

    monkeypatch.setenv("DEEPAGENTS_CODE_SERVER_DB_PATH", str(tmp_path / "sessions.db"))
    config = ServerConfig(sandbox_type="modal")
    monkeypatch.setattr(ServerConfig, "from_env", classmethod(lambda cls: config))
    created = []

    class Threads:
        async def create(self, **kwargs):
            created.append(kwargs["thread_id"])

        async def update(self, *args, **kwargs):
            pass

    monkeypatch.setattr(upstream_api, "_client", SimpleNamespace(threads=Threads()))

    async def build(**kwargs):
        if failure == "startup_exit":
            raise SystemExit(1)
        return SimpleNamespace(agent=object(), backend=object(), offload=object(), mcp_server_info=None)

    monkeypatch.setattr(server_graph, "_make_graphs", build)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=offload_api.app), base_url="http://factory.test",
    ) as client:
        if failure == "sandbox_conflict":
            first = await client.post("/dcode/threads/first/workspace", json={"cwd": str(tmp_path)})
            assert first.status_code == 200, first.text
        second_dir = tmp_path / "second"
        second_dir.mkdir()
        response = await client.post("/dcode/threads/second/workspace", json={"cwd": str(second_dir)})
    assert response.status_code == (409 if failure == "sandbox_conflict" else 503), response.text
    assert created == (["first"] if failure == "sandbox_conflict" else [])


def test_factory_shell_and_lazy_model_keep_workspace_snapshot(monkeypatch, tmp_path):
    from types import MappingProxyType
    from deepagents_code._fake_models import _ToolBindingFakeModel
    from deepagents_code.config import Credentials, active_environment
    from deepagents_code import configurable_model
    from lc_factory import assembly

    captured = []
    original = assembly.ConfigurableModelMiddleware

    def capture(*args, **kwargs):
        instance = original(*args, **kwargs)
        captured.append(instance)
        return instance

    monkeypatch.setattr(assembly, "ConfigurableModelMiddleware", capture)
    runtimes = []
    for name in ("alpha", "beta"):
        environment = MappingProxyType({"PATH": "/usr/bin:/bin", "FACTORY_WORKSPACE_PROBE": name})
        _, backend = assembly.create_factory_agent(
            model=_ToolBindingFakeModel(), assistant_id=f"snapshot-{name}", cwd=tmp_path,
            environ=environment,
            credentials_snapshot=Credentials.snapshot_from_environment(
                start_path=tmp_path, environ=environment,
            ),
            enable_interpreter=False, enable_memory=False, enable_skills=False,
        )
        runtimes.append((backend, [mw for mw in captured if mw._environ is environment]))
    # A later process-level mutation must not affect an already-built runtime.
    monkeypatch.setenv("FACTORY_WORKSPACE_PROBE", "process-mutated")
    observed = []

    def resolve(request, **kwargs):
        observed.append(active_environment()["FACTORY_WORKSPACE_PROBE"])
        return SimpleNamespace(request=request)

    monkeypatch.setattr(configurable_model, "_apply_overrides", resolve)
    for expected, (backend, middleware) in zip(("alpha", "beta"), runtimes):
        assert middleware
        for mw in middleware:
            # Skip checkpoint bookkeeping; exercise the real model wrapper's
            # environment scope after construction and after another workspace.
            mw._persist_model_state = False
            mw.wrap_model_call(object(), lambda request: object())
            assert observed[-1] == expected
        execution = backend.execute('printf "%s" "$FACTORY_WORKSPACE_PROBE"')
        assert execution.exit_code == 0
        assert execution.output == expected
    assert os.environ["FACTORY_WORKSPACE_PROBE"] == "process-mutated"
