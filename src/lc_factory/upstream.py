"""Import boundary: every upstream symbol lc_factory touches, in one place.

All lc_factory code MUST import upstream (``deepagents-code`` / ``deepagents``
/ ``langchain*``) symbols through this module — never directly. On a pin bump,
upstream breakage surfaces here first, and this file doubles as the divergence
inventory. Private (underscore) upstream names carry no semver protection;
they are the exact surface the parity suite guards.

Verified against deepagents-code==0.1.52 / deepagents==0.7.1.
(The port was authored against 0.1.47 / monorepo commit 8da0ccb13; the
0.1.48 bump required no changes here. The 0.1.52 bump added the
cost-tracking, server-hooks, and auto-classifier config symbols.)

Layout mirrors the consumers: assembly (the ported ``create_cli_agent``),
server graph, and launcher. Type-only names live in the TYPE_CHECKING block
at the bottom.
"""

from typing import TYPE_CHECKING

# --- SDK (deepagents) ---
from deepagents import FsToolName, create_deep_agent
from deepagents.backends import CompositeBackend, LocalShellBackend
from deepagents.backends.filesystem import FilesystemBackend
from deepagents.middleware import FilesystemMiddleware, MemoryMiddleware
from deepagents.middleware.subagents import (
    GENERAL_PURPOSE_SUBAGENT,
    SubAgent as RuntimeSubAgent,
)

# --- langchain core ---
# `AgentMiddleware` is a RUNTIME export, not type-only: the injection seam's
# public API is defined in terms of it, and `_testing_middleware` subclasses it
# at runtime — which the boundary rule requires it do through this module.
from langchain.agents.middleware.types import AgentMiddleware
from langchain_core._api import suppress_langchain_beta_warning

# NOTE: `cli_main` deliberately lives in `lc_factory.upstream_cli`, not here —
# importing it eagerly would drag the entire CLI stack into the server
# subprocess. See that module's docstring.
from deepagents_code._cli_context import CLIContextSchema
from deepagents_code._glm_5p2_profile import (
    _ensure_glm_5p2_profile_registered,
    _GlmTerminalStallRecovery,
)
from deepagents_code._repository_bounds import REPOSITORY_TOOL_CALL_LIMIT
from deepagents_code._server_config import ServerConfig
from deepagents_code._startup_error import STARTUP_ERROR_MARKER

# --- v0 assembly module: reference implementation + its private helpers ---
from deepagents_code.agent import (
    AsyncApprovalHITLMiddleware,
    ShellAllowListMiddleware,
    _add_interrupt_on,
    _apply_inherited_pythonpath,
    _create_rubric_grader_tools,
    _get_harness_tool_descriptions,
    _inject_fs_tools_into_subagents,
    _MEMORY_READONLY_SYSTEM_PROMPT,
    _normalize_rubric_grader_context_tools,
    _resolve_ptc_option,
    _rubric_grader_read_file_prefix,
    _rubric_grader_repository_tool_names,
    _rubric_grader_system_prompt,
    _sanitize_agent_message_name,
    create_cli_agent,
    get_system_prompt,
    load_async_subagents,
)
from deepagents_code.ask_user import AskUserMiddleware
from deepagents_code.auto_mode import (
    AutoModeHITLMiddleware,
    HeadlessMCPGuardMiddleware,
    gated_mcp_tool_names,
)

# --- client machinery reused by the micro-launcher and TUI entry ---
from deepagents_code.client.launch import server_manager as server_manager_module
from deepagents_code.client.launch.server import (
    ServerProcess,
    generate_langgraph_json,
)
from deepagents_code.client.launch.server_manager import (
    _write_checkpointer,
    start_server_and_get_agent,
)
from deepagents_code.client.remote_client import RemoteAgent

# --- config / settings ---
from deepagents_code.config import (
    _ShellAllowAll,
    config,
    configure_langsmith_secret_redaction,
    create_model,
    get_langsmith_project_name,
    is_memory_auto_save_enabled,
    resolve_auto_classifier_model,
    restore_user_tracing_api_keys,
    restore_user_tracing_env,
    settings,
)
from deepagents_code.config_manifest import (
    resolve_auto_classifier_timeout,
    resolve_recursion_limit,
)
from deepagents_code.configurable_model import ConfigurableModelMiddleware
from deepagents_code.cost_tracking import CostTrackingMiddleware

# --- verification pipeline (goals -> criteria -> rubric) ---
from deepagents_code.goal_rubric import (
    GoalCriteriaMiddleware,
    _ContextToolCallBudgetMiddleware,
    _create_goal_criteria_agent,
    _CriteriaContextBudgetMiddleware,
    _rubric_interrupt_on,
    _WebSearchBudgetMiddleware,
    create_goal_criteria_fallback_agent,
)
from deepagents_code.goal_tools import GoalToolsMiddleware
from deepagents_code.hooks.server_middleware import ServerHooksMiddleware
from deepagents_code.integrations.sandbox_factory import (
    create_sandbox,
    get_default_working_dir,
)
from deepagents_code.local_context import (
    LocalContextMiddleware,
    _AsyncExecutableBackend,
    _ExecutableBackend,
)
from deepagents_code.memory_guard import ManagedMemoryGuardMiddleware

# --- offload / compaction ---
from deepagents_code.offload import (
    _FALLBACK_ARTIFACTS_ROOT,
    CONVERSATION_HISTORY_DIRNAME,
    _artifacts_root,
    _offload_fallback_root,
)
from deepagents_code.offload_middleware import _create_cli_compaction_middleware

# --- plugins / skills ---
from deepagents_code.plugins import discover_plugins
from deepagents_code.plugins.adapters.skills import plugin_skill_sources
from deepagents_code.plugins.adapters.skills_middleware import PluginSkillsMiddleware
from deepagents_code.project_utils import (
    ProjectContext,
    get_server_project_context,
)
from deepagents_code.reliable_rubric import ReliableRubricMiddleware
from deepagents_code.resume_state import ResumeStateMiddleware

# --- server graph internals reused by lc_factory.server_graph ---
from deepagents_code.server_graph import (
    _build_graph_factory,
    _build_tools,
    _criteria_context_tools,
)
from deepagents_code.subagents import list_subagents


def import_code_interpreter():  # noqa: ANN201
    """Lazily import the optional QuickJS interpreter middleware pieces.

    Kept lazy to mirror upstream (interpreter import cost is paid only when
    ``enable_interpreter`` is set) while still routing through the boundary.

    Returns:
        Tuple of ``(CodeInterpreterMiddleware, PTCOption)``.
    """
    from langchain_quickjs import CodeInterpreterMiddleware, PTCOption

    return CodeInterpreterMiddleware, PTCOption


TYPE_ONLY_IMPORTS: tuple[tuple[str, str], ...] = (
    ("deepagents.backends.protocol", "BackendProtocol"),
    ("deepagents.backends.sandbox", "SandboxBackendProtocol"),
    ("deepagents.middleware.async_subagents", "AsyncSubAgent"),
    ("deepagents.middleware.subagents", "CompiledSubAgent"),
    ("deepagents.middleware.subagents", "SubAgent"),
    ("langchain.agents.middleware", "InterruptOnConfig"),
    ("langchain.tools", "BaseTool"),
    ("langchain_core.language_models", "BaseChatModel"),
    ("langgraph.checkpoint.base", "BaseCheckpointSaver"),
    ("langgraph.pregel", "Pregel"),
    ("deepagents_code.mcp_tools", "MCPServerInfo"),
    ("deepagents_code.plugins.adapters.skills", "CodeSkillSource"),
)
"""Upstream names used only in annotations, mirrored from the block below.

These are never evaluated at runtime (every lc_factory module uses
`from __future__ import annotations`), so an upstream rename would fail
silently. `tests/test_boundary.py` resolves this list explicitly to keep
them inside the pin-bump tripwire. Keep it in sync with the block below.
"""

if TYPE_CHECKING:
    # Type-only names (annotation/type-checking use; never evaluated at
    # runtime by lc_factory modules, which all use
    # `from __future__ import annotations`). Mirrored in TYPE_ONLY_IMPORTS.
    from deepagents.backends.protocol import BackendProtocol
    from deepagents.backends.sandbox import SandboxBackendProtocol
    from deepagents.middleware.async_subagents import AsyncSubAgent
    from deepagents.middleware.subagents import CompiledSubAgent, SubAgent
    from langchain.agents.middleware import InterruptOnConfig
    from langchain.tools import BaseTool
    from langchain_core.language_models import BaseChatModel
    from langgraph.checkpoint.base import BaseCheckpointSaver
    from langgraph.pregel import Pregel
    from deepagents_code.mcp_tools import MCPServerInfo
    from deepagents_code.plugins.adapters.skills import CodeSkillSource

# Runtime surface only: TYPE_CHECKING-only re-exports (BaseTool, Pregel, ...)
# are intentionally NOT in __all__ — they exist solely for type checkers, and
# the boundary-integrity test getattr-sweeps this list.
__all__ = [
    "AgentMiddleware",
    "AskUserMiddleware",
    "AsyncApprovalHITLMiddleware",
    "AutoModeHITLMiddleware",
    "CLIContextSchema",
    "CompositeBackend",
    "ConfigurableModelMiddleware",
    "CONVERSATION_HISTORY_DIRNAME",
    "CostTrackingMiddleware",
    "FilesystemBackend",
    "FilesystemMiddleware",
    "FsToolName",
    "GENERAL_PURPOSE_SUBAGENT",
    "GoalCriteriaMiddleware",
    "GoalToolsMiddleware",
    "HeadlessMCPGuardMiddleware",
    "LocalContextMiddleware",
    "LocalShellBackend",
    "ManagedMemoryGuardMiddleware",
    "MemoryMiddleware",
    "PluginSkillsMiddleware",
    "ProjectContext",
    "REPOSITORY_TOOL_CALL_LIMIT",
    "ReliableRubricMiddleware",
    "RemoteAgent",
    "ResumeStateMiddleware",
    "RuntimeSubAgent",
    "ServerConfig",
    "ServerHooksMiddleware",
    "ServerProcess",
    "ShellAllowListMiddleware",
    "STARTUP_ERROR_MARKER",
    "_FALLBACK_ARTIFACTS_ROOT",
    "_MEMORY_READONLY_SYSTEM_PROMPT",
    "_AsyncExecutableBackend",
    "_ContextToolCallBudgetMiddleware",
    "_CriteriaContextBudgetMiddleware",
    "_ExecutableBackend",
    "_GlmTerminalStallRecovery",
    "_ShellAllowAll",
    "_WebSearchBudgetMiddleware",
    "_add_interrupt_on",
    "_apply_inherited_pythonpath",
    "_artifacts_root",
    "_build_graph_factory",
    "_build_tools",
    "_create_cli_compaction_middleware",
    "_create_goal_criteria_agent",
    "_create_rubric_grader_tools",
    "_criteria_context_tools",
    "_ensure_glm_5p2_profile_registered",
    "_get_harness_tool_descriptions",
    "_inject_fs_tools_into_subagents",
    "_normalize_rubric_grader_context_tools",
    "_offload_fallback_root",
    "_resolve_ptc_option",
    "_rubric_grader_read_file_prefix",
    "_rubric_grader_repository_tool_names",
    "_rubric_grader_system_prompt",
    "_rubric_interrupt_on",
    "_sanitize_agent_message_name",
    "_write_checkpointer",
    "config",
    "configure_langsmith_secret_redaction",
    "create_cli_agent",
    "create_deep_agent",
    "create_goal_criteria_fallback_agent",
    "create_model",
    "create_sandbox",
    "discover_plugins",
    "gated_mcp_tool_names",
    "generate_langgraph_json",
    "get_default_working_dir",
    "get_langsmith_project_name",
    "get_server_project_context",
    "get_system_prompt",
    "import_code_interpreter",
    "is_memory_auto_save_enabled",
    "list_subagents",
    "load_async_subagents",
    "plugin_skill_sources",
    "resolve_auto_classifier_model",
    "resolve_auto_classifier_timeout",
    "resolve_recursion_limit",
    "restore_user_tracing_api_keys",
    "restore_user_tracing_env",
    "server_manager_module",
    "settings",
    "start_server_and_get_agent",
    "suppress_langchain_beta_warning",
]
