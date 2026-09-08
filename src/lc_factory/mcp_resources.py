"""Generation-owned MCP resources with same-task transport scope lifetimes."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from lc_factory.upstream import MCPSessionManager, _connections_signature

logger = logging.getLogger(__name__)


def _copy_containers(value):
    # OAuth providers and other host transport handles remain shared within
    # their generation; copying their locks/sessions is neither safe nor useful.
    if isinstance(value, dict):
        return {key: _copy_containers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_copy_containers(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_copy_containers(item) for item in value)
    return value


@dataclass
class _SessionOwner:
    ready: asyncio.Future
    stop: asyncio.Event
    task: asyncio.Task | None = None
    session: object = None


class OwnedMCPSessionManager(MCPSessionManager):
    """Only a server's long-lived owner task enters/exits its AnyIO scopes."""
    def __init__(self, *, connections=None, close_timeout=5):
        super().__init__(connections=_copy_containers(connections or {}))
        self._signature = _connections_signature(self._connections) if connections is not None else None
        self._owners = {}
        self._loop = None
        self.close_timeout = close_timeout

    def configure(self, connections):
        if self._closed:
            raise RuntimeError("Cannot configure a closed MCP session manager")
        signature = _connections_signature(connections)
        if self._signature is not None and signature != self._signature:
            raise RuntimeError("Cannot reconfigure an owned MCP generation; build a new generation")
        if self._signature is None:
            self._connections = _copy_containers(connections)
            self._signature = signature

    async def get_session(self, server_name):
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            raise RuntimeError("An MCP generation must run on its owning event loop")
        self._loop = loop
        if self._closed:
            raise RuntimeError("Cannot create an MCP session after cleanup")
        if server_name not in self._connections:
            raise ValueError("MCP server is not configured in this generation")
        while True:
            if self._closed:
                raise RuntimeError("Cannot create an MCP session after cleanup")
            owner = self._owners.get(server_name)
            if owner is None or not owner.stop.is_set():
                break
            await asyncio.shield(asyncio.gather(owner.task, return_exceptions=True))
            if self._owners.get(server_name) is owner:
                self._owners.pop(server_name, None)
            # Another waiter may already have installed a replacement. Read
            # it again before creating an owner so all callers share its task.
        if owner is None or owner.task.done():
            owner = _SessionOwner(loop.create_future(), asyncio.Event())
            # A caller may cancel while initialization is in flight. Consume
            # otherwise-unobserved errors without altering later await behavior.
            owner.ready.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
            self._owners[server_name] = owner
            owner.task = asyncio.create_task(self._own(server_name, owner), name=f"mcp-owner-{server_name}")
        return await asyncio.shield(owner.ready)

    async def _own(self, name, owner):
        try:
            owner.session = await super().get_session(name)
            owner.ready.set_result(owner.session)
            await owner.stop.wait()
        except BaseException as exc:
            if not owner.ready.done():
                owner.ready.set_exception(RuntimeError(f"MCP initialization failed ({type(exc).__name__})"))
            elif not isinstance(exc, asyncio.CancelledError):
                logger.warning("MCP session owner ended (%s)", type(exc).__name__)
        finally:
            try:
                # asyncio.timeout stays inside this task; wait_for would move
                # AnyIO scope teardown to a different task and corrupt it.
                async with asyncio.timeout(self.close_timeout):
                    await super().invalidate(name, expected_session=owner.session)
            except BaseException as exc:
                logger.warning("MCP session teardown failed (%s)", type(exc).__name__)

    async def invalidate(self, server_name, *, expected_session=None):
        owner = self._owners.get(server_name)
        if owner is None or (expected_session is not None and owner.session is not expected_session):
            return
        owner.stop.set()
        if not owner.ready.done():
            owner.ready.set_exception(RuntimeError("MCP initialization invalidated"))
            owner.task.cancel()
        await asyncio.shield(asyncio.gather(owner.task, return_exceptions=True))
        if self._owners.get(server_name) is owner:
            self._owners.pop(server_name, None)

    async def cleanup(self):
        self._closed = True
        owners = list(self._owners.values())
        for owner in owners:
            owner.stop.set()
            if not owner.ready.done():
                owner.ready.set_exception(RuntimeError("MCP initialization closed"))
                owner.task.cancel()
        if owners:
            await asyncio.shield(asyncio.gather(*(owner.task for owner in owners), return_exceptions=True))
        self._owners.clear()


@dataclass
class MCPToolBundle:
    tools: list
    info: object
    mcp_tools: list
    manager: OwnedMCPSessionManager | None = None
    closed: bool = False

    def __iter__(self):
        return iter((self.tools, self.info, self.mcp_tools))

    async def close(self):
        if self.closed:
            return
        if self.manager is not None:
            await self.manager.cleanup()
        self.closed = True
