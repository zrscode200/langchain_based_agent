"""Owned MCP generations retain sessions without crossing transport task scopes."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import AsyncExitStack, asynccontextmanager
from types import SimpleNamespace

import anyio
import pytest

from lc_factory.mcp_resources import MCPToolBundle, OwnedMCPSessionManager
from lc_factory.upstream import MCPSessionManager


@pytest.fixture
def counter_server(tmp_path):
    script = tmp_path / "counter_mcp.py"
    script.write_text('''import os
from mcp.server.fastmcp import FastMCP
server = FastMCP("counter")
calls = 0
@server.tool()
def counter() -> dict:
    """Return process identity, workspace binding, and its persistent call count."""
    global calls
    calls += 1
    return {"pid": os.getpid(), "count": calls, "workspace": os.environ.get("FACTORY_MCP_WORKSPACE", "unset")}
server.run()
''')
    return script


def _connection(script, workspace):
    return {"counter": {"transport": "stdio", "command": sys.executable, "args": [str(script)],
                        "env": {"FACTORY_MCP_WORKSPACE": workspace}}}


async def _counter(session):
    result = await asyncio.wait_for(session.call_tool("counter", {}), 10)
    assert not result.isError
    return result.structuredContent or json.loads(next(item.text for item in result.content if item.type == "text"))


async def test_real_stdio_reuses_pid_and_counter_while_generations_keep_distinct_environments(
    counter_server, monkeypatch, caplog,
):
    monkeypatch.setenv("FACTORY_MCP_WORKSPACE", "process-value")
    old = OwnedMCPSessionManager(connections=_connection(counter_server, "alpha"))
    new = OwnedMCPSessionManager(connections=_connection(counter_server, "beta"))
    try:
        first, second = await asyncio.gather(old.get_session("counter"), old.get_session("counter"))
        assert first is second
        alpha1, alpha2 = await _counter(first), await _counter(second)
        beta = await _counter(await new.get_session("counter"))
        alpha3 = await _counter(await old.get_session("counter"))
        assert alpha1["pid"] == alpha2["pid"] == alpha3["pid"]
        assert [alpha1["count"], alpha2["count"], alpha3["count"]] == [1, 2, 3]
        assert alpha1["workspace"] == alpha2["workspace"] == alpha3["workspace"] == "alpha"
        assert beta["count"] == 1 and beta["workspace"] == "beta"
        assert beta["pid"] != alpha1["pid"]
        assert os.environ["FACTORY_MCP_WORKSPACE"] == "process-value"
    finally:
        await asyncio.gather(old.cleanup(), new.cleanup())
    assert not old._entries and not new._entries
    assert "teardown failed" not in caplog.text


def _tool_value(value):
    if isinstance(value, dict):
        return value
    assert isinstance(value, list), value
    return json.loads(next(item["text"] for item in value if item.get("type") == "text"))


async def test_real_reloadable_bundles_capture_workspace_env_and_keep_old_tools_live(
    counter_server, tmp_path, monkeypatch,
):
    from lc_factory.mcp_reload import build_reloadable_tools
    from lc_factory.upstream import use_environment

    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"mcpServers": {"counter": {
        "command": sys.executable, "args": [str(counter_server)],
        "env": {"FACTORY_MCP_WORKSPACE": "${FACTORY_MCP_WORKSPACE}"},
    }}}))
    config = SimpleNamespace(no_mcp=False, mcp_config_path=str(path), trust_project_mcp=True)
    monkeypatch.setenv("FACTORY_MCP_WORKSPACE", "process-value")
    bundles = []

    async def build(workspace):
        with use_environment({**os.environ, "FACTORY_MCP_WORKSPACE": workspace}):
            bundle = await build_reloadable_tools(config, None)
        bundles.append(bundle)
        return bundle

    try:
        old, new = await asyncio.gather(build("alpha"), build("beta"))
        tools, info, mcp_tools = old
        assert tools is old.tools and info is old.info and mcp_tools is old.mcp_tools
        assert old.manager is not new.manager
        assert info[0].status == "ok" and new.info[0].status == "ok"
        alpha1 = _tool_value(await old.mcp_tools[0].ainvoke({}))
        beta1 = _tool_value(await new.mcp_tools[0].ainvoke({}))
        alpha2 = _tool_value(await old.mcp_tools[0].ainvoke({}))
        assert alpha1["pid"] == alpha2["pid"] != beta1["pid"]
        assert (alpha1["count"], alpha2["count"], beta1["count"]) == (1, 2, 1)
        assert alpha2["workspace"] == "alpha" and beta1["workspace"] == "beta"
        assert os.environ["FACTORY_MCP_WORKSPACE"] == "process-value"
    finally:
        await asyncio.gather(*(bundle.close() for bundle in bundles))
    assert all(bundle.closed and not bundle.manager._entries for bundle in bundles)


def _controlled_entries(monkeypatch, *, initialization=None, closing=None, fail_first=False):
    """Exercise real AnyIO cancel scopes entered and exited by the owner task."""
    observations = {"created": [], "entered": [], "exited": [], "init_entered": asyncio.Event(),
                    "close_entered": asyncio.Event()}

    @asynccontextmanager
    async def scope():
        task = asyncio.current_task()
        observations["entered"].append(task)
        async with anyio.create_task_group():
            try:
                yield
            finally:
                observations["close_entered"].set()
                if closing is not None:
                    await closing.wait()
                assert asyncio.current_task() is task
                observations["exited"].append(asyncio.current_task())

    async def create_entry(self, name):
        observations["created"].append(asyncio.current_task())
        stack = AsyncExitStack()
        await stack.enter_async_context(scope())
        try:
            observations["init_entered"].set()
            if fail_first and len(observations["created"]) == 1:
                raise ValueError("private initialization detail")
            if initialization is not None:
                await initialization.wait()
        except BaseException:
            await stack.aclose()
            raise
        return SimpleNamespace(session=object(), exit_stack=stack)

    monkeypatch.setattr(MCPSessionManager, "_create_entry", create_entry)
    return observations


def _manager():
    return OwnedMCPSessionManager(connections={"server": {"transport": "stdio", "command": "unused", "args": []}})


async def test_anyio_transport_enters_and_exits_same_owner_task(monkeypatch, caplog):
    seen = _controlled_entries(monkeypatch)
    manager = _manager()
    caller = asyncio.current_task()
    first = await manager.get_session("server")
    assert await manager.get_session("server") is first
    await manager.cleanup()
    assert seen["entered"] == seen["exited"] == seen["created"]
    assert seen["created"][0] is not caller
    assert not manager._entries and not manager._owners
    assert "teardown failed" not in caplog.text


async def test_cancelled_waiter_does_not_cancel_shared_initialization(monkeypatch):
    ready = asyncio.Event()
    seen = _controlled_entries(monkeypatch, initialization=ready)
    manager = _manager()
    first = asyncio.create_task(manager.get_session("server"))
    await asyncio.wait_for(seen["init_entered"].wait(), 3)
    second = asyncio.create_task(manager.get_session("server"))
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert not manager._owners["server"].task.done()
    ready.set()
    session = await asyncio.wait_for(second, 3)
    assert await manager.get_session("server") is session
    assert len(seen["created"]) == 1
    await manager.cleanup()
    assert seen["entered"] == seen["exited"]


async def test_failed_initialization_closes_scope_and_later_call_retries(monkeypatch):
    seen = _controlled_entries(monkeypatch, fail_first=True)
    manager = _manager()
    with pytest.raises(RuntimeError, match="MCP initialization failed") as failure:
        await manager.get_session("server")
    assert "private initialization detail" not in str(failure.value)
    session = await manager.get_session("server")
    assert session is await manager.get_session("server")
    assert len(seen["created"]) == 2
    await manager.cleanup()
    assert seen["entered"] == seen["exited"]


async def test_expected_session_prevents_stale_invalidation_of_replacement(monkeypatch):
    seen = _controlled_entries(monkeypatch)
    manager = _manager()
    old = await manager.get_session("server")
    await manager.invalidate("server", expected_session=old)
    new = await manager.get_session("server")
    assert new is not old
    await manager.invalidate("server", expected_session=old)
    assert await manager.get_session("server") is new
    assert len(seen["created"]) == 2 and len(seen["exited"]) == 1
    await manager.cleanup()
    assert len(seen["exited"]) == 2


async def test_concurrent_waiters_after_invalidation_share_one_tracked_new_owner(monkeypatch, caplog):
    release = asyncio.Event()
    seen = _controlled_entries(monkeypatch, closing=release)
    manager = _manager()
    owners = []
    original_own = manager._own

    async def record_owner(name, owner):
        owners.append(owner)
        await original_own(name, owner)

    monkeypatch.setattr(manager, "_own", record_owner)
    waiters, invalidating = [], None
    try:
        old = await manager.get_session("server")
        invalidating = asyncio.create_task(manager.invalidate("server", expected_session=old))
        await asyncio.wait_for(seen["close_entered"].wait(), 3)
        waiters = [asyncio.create_task(manager.get_session("server")) for _ in range(4)]
        # All callers must observe the closing old owner before it finishes.
        await asyncio.sleep(0)
        assert not any(waiter.done() for waiter in waiters)
        release.set()
        sessions = await asyncio.wait_for(asyncio.gather(*waiters), 3)
        await invalidating
        assert all(session is sessions[0] and session is not old for session in sessions)
        assert len(owners) == 2, "A caller waiting for the old close created an untracked competing owner"
        assert manager._owners["server"] is owners[1]
        assert seen["created"] == [owner.task for owner in owners]
        await manager.cleanup()
        assert all(owner.task.done() for owner in owners)
        assert seen["entered"] == seen["exited"]
        assert not manager._owners and not manager._entries
        assert "teardown failed" not in caplog.text
    finally:
        # A regression must not leave the very orphan owners this test detects
        # alive in pytest. Stop recorded owners in creation order so the task
        # that entered each real AnyIO scope also gets first chance to exit it.
        release.set()
        for owner in owners:
            owner.stop.set()
        if owners:
            await asyncio.gather(*(owner.task for owner in owners), return_exceptions=True)
        if waiters:
            await asyncio.gather(*waiters, return_exceptions=True)
        if invalidating is not None:
            await asyncio.gather(invalidating, return_exceptions=True)
        await manager.cleanup()


@pytest.mark.parametrize("before_owner_start", [False, True])
async def test_cleanup_during_initialization_resolves_waiters_and_closes_scopes(monkeypatch, before_owner_start):
    seen = _controlled_entries(monkeypatch, initialization=asyncio.Event())
    manager = _manager()
    waiter = asyncio.create_task(manager.get_session("server"))
    if before_owner_start:
        # get_session schedules its owner behind this test's next loop entry.
        await asyncio.sleep(0)
        assert manager._owners and not seen["created"]
    else:
        await asyncio.wait_for(seen["init_entered"].wait(), 3)
    # timeout() stays in this task; wait_for() would schedule cleanup behind the
    # newly queued owner, accidentally missing the before-first-step race.
    async with asyncio.timeout(3):
        await manager.cleanup()
    with pytest.raises(RuntimeError, match="closed"):
        await asyncio.wait_for(waiter, 3)
    assert seen["entered"] == seen["exited"]
    if before_owner_start:
        assert not seen["created"]
    assert not manager._entries and not manager._owners
    with pytest.raises(RuntimeError, match="cleanup"):
        await manager.get_session("server")


async def test_cancelled_cleanup_caller_does_not_cancel_owner_teardown(monkeypatch):
    release = asyncio.Event()
    seen = _controlled_entries(monkeypatch, closing=release)
    manager = _manager()
    await manager.get_session("server")
    owner = manager._owners["server"].task
    closing = asyncio.create_task(manager.cleanup())
    await asyncio.wait_for(seen["close_entered"].wait(), 3)
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert not owner.done()
    release.set()
    await asyncio.wait_for(manager.cleanup(), 3)
    assert owner.done() and seen["entered"] == seen["exited"]


async def test_invalidation_during_startup_closes_then_allows_clean_retry(monkeypatch):
    release = asyncio.Event()
    seen = _controlled_entries(monkeypatch, initialization=release)
    manager = _manager()
    waiter = asyncio.create_task(manager.get_session("server"))
    await asyncio.wait_for(seen["init_entered"].wait(), 3)
    await manager.invalidate("server")
    with pytest.raises(RuntimeError, match="invalidated"):
        await waiter
    assert seen["entered"] == seen["exited"]
    release.set()
    await manager.get_session("server")
    assert len(seen["created"]) == 2
    await manager.cleanup()
    assert seen["entered"] == seen["exited"]


async def test_shutdown_bounds_an_unresponsive_transport_close(monkeypatch, caplog):
    _controlled_entries(monkeypatch, closing=asyncio.Event())
    manager = _manager()
    manager.close_timeout = 0.01
    await manager.get_session("server")
    owner = manager._owners["server"].task
    async with asyncio.timeout(1):
        await manager.cleanup()
    assert owner.done() and not manager._owners and not manager._entries
    assert "MCP session teardown failed (TimeoutError)" in caplog.text


@pytest.mark.parametrize("initially_configured", [False, True])
async def test_connection_containers_are_copied_and_frozen_before_first_session(initially_configured):
    handle = object()
    supplied = {"server": {"transport": "stdio", "command": "unused", "args": ["old"],
                           "env": {"WORKSPACE": "alpha"}, "auth": handle}}
    expected = {"server": {**supplied["server"], "args": ["old"], "env": {"WORKSPACE": "alpha"}}}
    manager = OwnedMCPSessionManager(connections=supplied if initially_configured else None)
    if not initially_configured:
        manager.configure(supplied)
    manager.configure(expected)
    supplied["server"]["args"].append("mutated")
    supplied["server"]["env"]["WORKSPACE"] = "beta"
    assert manager._connections["server"]["args"] == ["old"]
    assert manager._connections["server"]["env"] == {"WORKSPACE": "alpha"}
    assert manager._connections["server"]["auth"] is handle
    with pytest.raises(RuntimeError, match="new generation"):
        manager.configure(supplied)
    await manager.cleanup()
    with pytest.raises(RuntimeError, match="closed"):
        manager.configure(expected)


async def test_loop_mismatch_and_unknown_server_are_rejected(monkeypatch):
    _controlled_entries(monkeypatch)
    manager = _manager()
    await manager.get_session("server")
    try:
        with pytest.raises(RuntimeError, match="owning event loop"):
            await asyncio.to_thread(lambda: asyncio.run(manager.get_session("server")))
        with pytest.raises(ValueError, match="not configured"):
            await manager.get_session("unknown")
    finally:
        await manager.cleanup()


async def test_bundle_without_owned_resources_keeps_tuple_contract_and_closes():
    tools, info, mcp = [object()], [object()], []
    bundle = MCPToolBundle(tools, info, mcp)
    assert tuple(bundle) == (tools, info, mcp)
    await bundle.close()
    await bundle.close()
    assert bundle.closed


async def test_enabled_mcp_without_tools_closes_unused_manager_and_preserves_discovery_info(monkeypatch):
    from lc_factory import mcp_reload

    captured = []
    info = [SimpleNamespace(name="disabled-server", status="disabled")]
    monkeypatch.setattr(mcp_reload, "discover_plugin_mcp_configs", lambda **kwargs: [])

    async def discover(**kwargs):
        assert kwargs["stateless"] is False
        manager = kwargs["session_manager"]
        manager.configure({})
        captured.append(manager)
        return [], None, info

    monkeypatch.setattr(mcp_reload, "resolve_and_load_mcp_tools", discover)
    config = SimpleNamespace(no_mcp=False, mcp_config_path=None, trust_project_mcp=False)
    bundle = await mcp_reload.build_reloadable_tools(config, None)
    try:
        assert len(captured) == 1 and captured[0]._closed
        assert bundle.manager is None, "An empty generation must not consume owned-resource retention capacity"
        assert bundle.info is info
        assert bundle.mcp_tools == []
        assert bundle.tools == [mcp_reload.fetch_url, mcp_reload.get_current_thread_id]
    finally:
        await bundle.close()
