"""Micro-launcher: boot ``langgraph dev`` serving the factory graph.

Port of the workspace-scaffolding and server-startup flow from
``deepagents_code.client.launch.server_manager`` (monorepo ``8da0ccb13``),
with two changes: the generated ``langgraph.json`` references
``lc_factory.server_graph:make_graph``, and the generated runtime pyproject
depends on ``lc_factory`` (which transitively pins ``deepagents-code``).
``ServerProcess``, checkpointer generation, the ServerConfig env bridge, and
``RemoteAgent`` are reused from upstream through the boundary, so the
sessions DB layout and client protocol stay TUI-compatible.
"""

from __future__ import annotations

import logging
import tempfile
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import TYPE_CHECKING, Any

from lc_factory.upstream import (
    _EPHEMERAL_PORT,
    RemoteAgent,
    ServerConfig,
    ServerProcess,
    _capture_project_context,
    _preflight_validate_mcp_config,
    _set_or_clear_server_env,
    _write_checkpointer,
    emit_preserved_log_notices,
    generate_langgraph_json,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from lc_factory.upstream import FsToolName

logger = logging.getLogger(__name__)
_DISTRIBUTION_NAME = "lc_factory"
GRAPH_REF = "lc_factory.server_graph:make_graph"


def _apply_server_config(config: ServerConfig) -> None:
    """Write a ``ServerConfig`` to ``DEEPAGENTS_CODE_SERVER_*`` env vars.

    Args:
        config: Fully resolved server configuration.
    """
    for suffix, value in config.to_env().items():
        _set_or_clear_server_env(suffix, value)


def scaffold_workspace(work_dir: Path) -> None:
    """Prepare the server working directory for the FACTORY graph.

    Port of upstream ``_scaffold_workspace``; the graph reference points at
    the installed ``lc_factory`` package instead of ``deepagents_code``.
    Also the rebind target for the TUI launch seam (see ``lc_factory.tui``).

    Args:
        work_dir: Temporary directory that will become the server's cwd.
    """
    _write_checkpointer(work_dir)
    _write_pyproject(work_dir)

    # `graph_ref` is a dotted import of the installed `lc_factory` package;
    # `checkpointer_path` stays cwd-relative because checkpointer.py is
    # generated fresh into work_dir and is not an importable package module.
    generate_langgraph_json(
        work_dir,
        graph_ref=GRAPH_REF,
        checkpointer_path="./checkpointer.py:create_checkpointer",
    )


def _write_pyproject(work_dir: Path) -> None:
    """Write a minimal pyproject.toml for the server working directory.

    Args:
        work_dir: Server working directory.
    """
    content = f"""[project]
name = "lc-factory-server-runtime"
version = "0.0.1"
requires-python = ">=3.11"
dependencies = [
    "{_runtime_package_dependency()}",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
"""
    (work_dir / "pyproject.toml").write_text(content)


def _default_package_project_root() -> Path | None:
    """Return the project root that contains the ``lc_factory`` package.

    src layout: in editable checkouts the package resolves to
    ``<root>/src/lc_factory`` (root is two levels up); for installed wheels it
    resolves to ``site-packages/lc_factory`` (no pyproject above it, so the
    caller falls back to the installed distribution version).

    Returns:
        First ancestor directory holding a ``pyproject.toml``, or ``None``.
    """
    import lc_factory

    package_init = getattr(lc_factory, "__file__", None)
    if package_init is None:
        return None
    package_dir = Path(package_init).resolve().parent
    for candidate in (package_dir.parent, package_dir.parent.parent):
        if (candidate / "pyproject.toml").is_file():
            return candidate
    return None


def _runtime_package_dependency(package_root: Path | None = None) -> str:
    """Return the dependency spec for ``lc_factory`` in the server runtime.

    Args:
        package_root: Optional package project root for tests.

    Returns:
        Requirement string for the generated runtime ``pyproject.toml``.
    """
    root = package_root or _default_package_project_root()
    if root is not None and (root / "pyproject.toml").is_file():
        return f"{_DISTRIBUTION_NAME} @ {root.as_uri()}"

    try:
        installed_version = version(_DISTRIBUTION_NAME)
    except PackageNotFoundError:
        return _DISTRIBUTION_NAME
    return f"{_DISTRIBUTION_NAME}=={installed_version}"


async def start_factory_server_and_get_agent(
    *,
    assistant_id: str,
    model_name: str | None = None,
    model_params: dict[str, Any] | None = None,
    profile_overrides: dict[str, Any] | None = None,
    auto_approve: bool = False,
    interrupt_shell_only: bool = False,
    shell_allow_list: list[str] | None = None,
    sandbox_type: str = "none",
    sandbox_id: str | None = None,
    sandbox_snapshot_name: str | None = None,
    sandbox_setup: str | None = None,
    enable_shell: bool = True,
    enable_ask_user: bool = False,
    enable_interpreter: bool | None = None,
    interpreter_ptc: str | list[str] | None = None,
    interpreter_ptc_acknowledge_unsafe: bool = False,
    allow_fs_tools: list[FsToolName] | None = None,
    rubric_model: str | None = None,
    rubric_max_iterations: int | None = None,
    recursion_limit: int | None = None,
    mcp_config_path: str | None = None,
    no_mcp: bool = False,
    trust_project_mcp: bool | None = None,
    interactive: bool = True,
    host: str = "127.0.0.1",
    port: int = _EPHEMERAL_PORT,
) -> tuple[RemoteAgent, ServerProcess]:
    """Start a LangGraph server on the factory graph and return a client.

    Port of upstream ``start_server_and_get_agent`` with the factory scaffold
    (the dropped third tuple slot was upstream's always-``None`` MCP session
    manager placeholder).

    Args:
        assistant_id: Agent identifier.
        model_name: Model spec string (``provider:model``).
        model_params: Extra model kwargs.
        profile_overrides: Model profile metadata overrides.
        auto_approve: Auto-approve all tools.
        interrupt_shell_only: Validate shell via middleware instead of HITL.
        shell_allow_list: Restrictive shell allow-list.
        sandbox_type: Sandbox type (``"none"`` for local).
        sandbox_id: Existing sandbox ID to reuse.
        sandbox_snapshot_name: Snapshot or blueprint name.
        sandbox_setup: Path to sandbox setup script.
        enable_shell: Enable shell execution tools.
        enable_ask_user: Enable the ask_user tool.
        enable_interpreter: Enable ``js_eval``; ``None`` = sandbox-aware default.
        interpreter_ptc: PTC allowlist override.
        interpreter_ptc_acknowledge_unsafe: Acknowledge ``ptc="all"`` risk.
        allow_fs_tools: Filesystem tool allowlist (``None`` = SDK default).
        rubric_model: Grader model spec; ``None`` reuses the main model.
        rubric_max_iterations: Grader iterations; ``None`` = SDK default.
        recursion_limit: Main-agent recursion limit; ``None`` = resolved.
        mcp_config_path: Path to MCP config.
        no_mcp: Disable MCP.
        trust_project_mcp: Trust project MCP servers.
        interactive: Whether the agent is interactive.
        host: Server host.
        port: Server port (0 = free ephemeral port).

    Returns:
        Tuple of ``(remote_agent, server_process)``.
    """
    project_context = _capture_project_context()

    _preflight_validate_mcp_config(
        mcp_config_path=mcp_config_path,
        no_mcp=no_mcp,
    )

    config = ServerConfig.from_cli_args(
        project_context=project_context,
        model_name=model_name,
        model_params=model_params,
        profile_overrides=profile_overrides,
        assistant_id=assistant_id,
        auto_approve=auto_approve,
        interrupt_shell_only=interrupt_shell_only,
        shell_allow_list=shell_allow_list,
        sandbox_type=sandbox_type,
        sandbox_id=sandbox_id,
        sandbox_snapshot_name=sandbox_snapshot_name,
        sandbox_setup=sandbox_setup,
        enable_shell=enable_shell,
        enable_ask_user=enable_ask_user,
        enable_interpreter=enable_interpreter,
        interpreter_ptc=interpreter_ptc,
        interpreter_ptc_acknowledge_unsafe=interpreter_ptc_acknowledge_unsafe,
        allow_fs_tools=allow_fs_tools,
        rubric_model=rubric_model,
        rubric_max_iterations=rubric_max_iterations,
        recursion_limit=recursion_limit,
        mcp_config_path=mcp_config_path,
        no_mcp=no_mcp,
        trust_project_mcp=trust_project_mcp,
        interactive=interactive,
    )
    _apply_server_config(config)

    work_dir = Path(tempfile.mkdtemp(prefix="lc_factory_server_"))
    scaffold_workspace(work_dir)

    server = ServerProcess(
        host=host,
        port=port,
        config_dir=work_dir,
        owns_config_dir=True,
        scaffold=scaffold_workspace,
    )
    started = False
    try:
        await server.start()
        await server.wait_for_graph_ready("agent")
        agent = RemoteAgent(
            url=server.url,
            graph_name="agent",
        )
        started = True
        return agent, server
    finally:
        if not started:
            # Mirror upstream: `finally` (not `except Exception`) so
            # cancellation also reaps the subprocess instead of orphaning it.
            try:
                server.stop()
            except Exception:
                logger.exception(
                    "Error stopping server during startup cleanup",
                )


@asynccontextmanager
async def factory_server_session(
    **kwargs: Any,
) -> AsyncIterator[tuple[RemoteAgent, ServerProcess]]:
    """Async context manager wrapping startup and guaranteed cleanup.

    Args:
        **kwargs: Forwarded to ``start_factory_server_and_get_agent``.

    Yields:
        Tuple of ``(remote_agent, server_process)``.
    """
    server_proc: ServerProcess | None = None
    try:
        agent, server_proc = await start_factory_server_and_get_agent(**kwargs)
        yield agent, server_proc
    finally:
        if server_proc is not None:
            server_proc.stop()
        # Drain unconditionally: a failed startup may have queued a
        # debug-preserved log path before `server_proc` was assigned.
        emit_preserved_log_notices()
