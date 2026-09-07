"""Single import boundary for the pinned deepagents runtime.

Every project source module imports ``deepagents-code``, ``deepagents``, and
LangChain runtime symbols through this module. A pin bump therefore fails at
one explicit boundary before copied assembly or launch code can drift silently.

Verified against deepagents-code 0.1.66 / deepagents 0.7.13 from commit
``6c89fe2197a2dfe4f3851cda38565bcadba6066b``, plus langchain-quickjs 0.3.7.
The factory's owned assembly and server runtime port this unreleased baseline;
private names remain intentionally guarded by tests and the bump ledger.
"""

from typing import TYPE_CHECKING

# --- SDK (deepagents) ---
from deepagents import FsToolName, create_deep_agent
from deepagents.middleware.filesystem import FilesystemPermission
from deepagents.backends import CompositeBackend, LocalShellBackend
from deepagents.backends.filesystem import FilesystemBackend
from deepagents.middleware import FilesystemMiddleware, MemoryMiddleware
from deepagents.middleware.subagents import (
    GENERAL_PURPOSE_SUBAGENT,
    SubAgent as RuntimeSubAgent,
)

# --- LangChain runtime ---
from langchain.agents.middleware import ToolErrorMiddleware
from langchain.agents.structured_output import ToolStrategy
from langchain.agents.middleware.types import AgentMiddleware, AgentState, OmitFromSchema
from langchain_core._api import suppress_langchain_beta_warning
from langgraph_sdk.runtime import ServerRuntime as LangGraphServerRuntime

# --- Optional owned runtime / Talon adaptations ---
from langchain.tools import ToolRuntime
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage, SystemMessage, convert_to_messages, MessageLikeRepresentation
from langchain_core.tools import tool, BaseTool
from langchain_core.runnables import RunnableConfig
from langchain_core.runnables.utils import ConfigurableFieldSpec
from langgraph.checkpoint.base import BaseCheckpointSaver, ChannelVersions, Checkpoint, CheckpointMetadata, CheckpointTuple, DeltaChannelHistory
from langgraph.checkpoint.serde.types import _DeltaSnapshot
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.errors import GraphInterrupt
from langgraph.types import Command
from langgraph.runtime import Runtime
from deepagents_code.subagents import _parse_subagent_file

from deepagents_code.tools import create_web_search_tool, fetch_url, get_current_thread_id
from deepagents_code.plugins.adapters.mcp import discover_plugin_mcp_configs
from deepagents_code.mcp_tools import resolve_and_load_mcp_tools
from deepagents_code.configuration.service import get_config_sources

# NOTE: `cli_main` deliberately lives in `lc_factory.upstream_cli`, not here.
# Eagerly importing it would pull the full TUI into the server subprocess.
from deepagents_code._cli_context import CLIContextSchema, INHERIT_CLASSIFIER_MODEL
from deepagents_code._env_vars import EXPERIMENTAL, FORKED_SUBAGENTS, is_env_truthy
from deepagents_code._glm_5p2_profile import (
    _ensure_glm_5p2_profile_registered,
    _GlmTerminalStallRecovery,
)
from deepagents_code._paths import (
    ensure_agent_dir,
    get_project_agent_md_path,
    get_project_agents_dir,
    get_user_agent_md_path,
    get_user_agents_dir,
)
from deepagents_code._repository_bounds import REPOSITORY_TOOL_CALL_LIMIT
from deepagents_code._server_config import ServerConfig
from deepagents_code._startup_error import (
    STARTUP_ERROR_MARKER,
    emit_startup_failure,
)

# --- released agent assembly and helpers ---
from deepagents_code.agent import (
    AsyncApprovalHITLMiddleware,
    ShellAllowListMiddleware,
    _add_interrupt_on,
    _apply_inherited_pythonpath,
    _create_rubric_grader_tools,
    _format_task_error,
    _get_harness_tool_descriptions,
    _has_resolvable_model_provider,
    _inject_fs_tools_into_subagents,
    _MEMORY_READONLY_SYSTEM_PROMPT,
    _normalize_rubric_grader_context_tools,
    _resolve_ptc_option,
    _resolve_retry_owned_model,
    _resolve_shell_allow_list,
    _rubric_grader_read_file_prefix,
    _rubric_grader_repository_tool_names,
    _rubric_grader_system_prompt,
    _sanitize_agent_message_name,
    create_cli_agent,
    get_skill_sources,
    get_system_prompt,
    load_async_subagents,
)
from deepagents_code.ask_user import AskUserMiddleware
from deepagents_code.auto_mode import (
    AutoModeHITLMiddleware,
    HeadlessMCPGuardMiddleware,
    gated_mcp_tool_names,
)

# --- client machinery reused by the launcher/TUI seam ---
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

# --- config, runtime state, and model policy ---
from deepagents_code.config import (
    Credentials,
    DEFAULT_MODEL_RETRIES,
    _ShellAllowAll,
    _preview_dotenv_environ,
    apply_inherited_user_tracing,
    configure_langsmith_secret_redaction,
    create_model,
    credentials,
    get_langsmith_project_name,
    is_memory_auto_save_enabled,
    resolve_auto_classifier_model,
    resolve_auto_classifier_model_for_provider,
    restore_user_tracing_api_keys,
    restore_user_tracing_env,
    runtime_state,
    use_environment,
)
from deepagents_code.config_manifest import (
    resolve_auto_classifier_timeout,
    resolve_recursion_limit,
)
from deepagents_code.configurable_model import ConfigurableModelMiddleware
from deepagents_code.configuration.interpreter import InterpreterConfig
from deepagents_code.configuration.resolver import get_config_resolver
from deepagents_code.cost_tracking import CostTrackingMiddleware
from deepagents_code.model_config import ModelConfig
from deepagents_code.model_retry import CodeModelRetryMiddleware

# --- extensions ---
from deepagents_code.extensions import ExtensionMode, load_extensions
from deepagents_code.extensions.hosting import (
    ExtensionRuntimeMiddleware,
    bind_runtime_host_policy,
    validate_backend_route,
)
from deepagents_code.extensions.runtime import (
    bind_server_extensions,
    shutdown_server_extensions,
)

# --- verification pipeline (goals -> criteria -> rubric) ---
from deepagents_code.goal_rubric import (
    GoalCriteriaMiddleware,
    RubricGraderState,
    _ContextToolCallBudgetMiddleware,
    _create_goal_criteria_agent,
    _CriteriaContextBudgetMiddleware,
    _rubric_interrupt_on,
    _rubric_grader_messages,
    _rubric_grader_state,
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
from deepagents_code.reliable_rubric import ReliableRubricMiddleware
from deepagents_code.resume_state import ResumeStateMiddleware

# --- offload / compaction ---
from deepagents_code.offload import (
    _FALLBACK_ARTIFACTS_ROOT,
    CONVERSATION_HISTORY_DIRNAME,
    _artifacts_root,
    _offload_fallback_root,
)
from deepagents_code.offload_middleware import (
    OffloadOperation,
    _create_cli_compaction_middleware,
    attach_offload_operation,
    offload_operation_from,
)

# --- plugins, skills, and project context ---
from deepagents_code.plugins.adapters.skills_middleware import PluginSkillsMiddleware
from deepagents_code.project_utils import ProjectContext, get_server_project_context
from deepagents_code.subagents import list_subagents
from deepagents_code.workspace import (
    WorkspaceConflictError,
    require_thread_workspace,
    get_thread_workspace,
    resolve_workspace,
)

# --- server graph internals reused by lc_factory.server_graph ---
from deepagents_code.server_graph import (
    ServerRuntime,
    _build_graph_factory,
    _build_runtime_factory,
    _build_tools,
    _criteria_context_tools,
)


def import_code_interpreter():  # noqa: ANN201
    """Lazily import the optional QuickJS interpreter middleware pieces."""
    from langchain_quickjs import CodeInterpreterMiddleware, PTCOption

    return CodeInterpreterMiddleware, PTCOption


def import_offload_api():  # noqa: ANN201
    """Lazily return upstream's HTTP app module for the factory adapter."""
    from deepagents_code import offload_api

    return offload_api


def import_tool_calling_test_model():  # noqa: ANN201
    """Load the deterministic model only for subprocess integration fixtures."""
    from deepagents_code._testing_models import ToolCallingIntegrationChatModel

    return ToolCallingIntegrationChatModel


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
    ("langgraph.store.base", "BaseStore"),
    ("deepagents_code.extensions.registry", "ExtensionRegistry"),
    ("deepagents_code.mcp_tools", "MCPServerInfo"),
    ("deepagents_code.workspace", "WorkspaceBinding"),
    ("deepagents_code.config", "CredentialsSnapshot"),
    ("deepagents_code.config", "ModelResult"),
)
"""Annotation-only imports explicitly resolved by the boundary test."""

if TYPE_CHECKING:
    from deepagents.backends.protocol import BackendProtocol
    from deepagents.backends.sandbox import SandboxBackendProtocol
    from deepagents.middleware.async_subagents import AsyncSubAgent
    from deepagents.middleware.subagents import CompiledSubAgent, SubAgent
    from langchain.agents.middleware import InterruptOnConfig
    from langchain.tools import BaseTool
    from langchain_core.language_models import BaseChatModel
    from langgraph.checkpoint.base import BaseCheckpointSaver
    from langgraph.pregel import Pregel
    from langgraph.store.base import BaseStore

    from deepagents_code.extensions.registry import ExtensionRegistry
    from deepagents_code.mcp_tools import MCPServerInfo
    from deepagents_code.workspace import WorkspaceBinding
    from deepagents_code.config import CredentialsSnapshot, ModelResult


__all__ = [
    "ToolStrategy",
    "OmitFromSchema",
    "get_config_sources",
    "create_web_search_tool", "fetch_url", "get_current_thread_id",
    "discover_plugin_mcp_configs", "resolve_and_load_mcp_tools",
    "get_thread_workspace",
    "AgentState",
    "Runtime",
    'ToolRuntime',
    'ToolCallRequest',
    'AIMessage',
    'BaseMessage',
    'HumanMessage',
    'ToolMessage',
    'SystemMessage',
    'convert_to_messages',
    'MessageLikeRepresentation',
    'tool',
    'BaseTool',
    'RunnableConfig',
    'ConfigurableFieldSpec',
    'BaseCheckpointSaver',
    'ChannelVersions',
    'Checkpoint',
    'CheckpointMetadata',
    'CheckpointTuple',
    'DeltaChannelHistory',
    '_DeltaSnapshot',
    'AsyncSqliteSaver',
    'GraphInterrupt',
    'Command',
    '_parse_subagent_file',

    "AgentMiddleware",
    "AskUserMiddleware",
    "AsyncApprovalHITLMiddleware",
    "AutoModeHITLMiddleware",
    "CLIContextSchema",
    "CONVERSATION_HISTORY_DIRNAME",
    "CodeModelRetryMiddleware",
    "CompositeBackend",
    "ConfigurableModelMiddleware",
    "CostTrackingMiddleware",
    "Credentials",
    "DEFAULT_MODEL_RETRIES",
    "EXPERIMENTAL",
    "FORKED_SUBAGENTS",
    "ExtensionMode",
    "ExtensionRuntimeMiddleware",
    "FilesystemBackend",
    "FilesystemMiddleware",
    "FsToolName",
    "FilesystemPermission",
    "GENERAL_PURPOSE_SUBAGENT",
    "GoalCriteriaMiddleware",
    "GoalToolsMiddleware",
    "HeadlessMCPGuardMiddleware",
    "INHERIT_CLASSIFIER_MODEL",
    "InterpreterConfig",
    "LangGraphServerRuntime",
    "LocalContextMiddleware",
    "LocalShellBackend",
    "ManagedMemoryGuardMiddleware",
    "MemoryMiddleware",
    "ModelConfig",
    "OffloadOperation",
    "PluginSkillsMiddleware",
    "ProjectContext",
    "REPOSITORY_TOOL_CALL_LIMIT",
    "ReliableRubricMiddleware",
    "RemoteAgent",
    "ResumeStateMiddleware",
    "RubricGraderState",
    "RuntimeSubAgent",
    "STARTUP_ERROR_MARKER",
    "ServerConfig",
    "ServerHooksMiddleware",
    "ServerProcess",
    "ServerRuntime",
    "ShellAllowListMiddleware",
    "ToolErrorMiddleware",
    "WorkspaceConflictError",
    "_AsyncExecutableBackend",
    "_ContextToolCallBudgetMiddleware",
    "_CriteriaContextBudgetMiddleware",
    "_ExecutableBackend",
    "_FALLBACK_ARTIFACTS_ROOT",
    "_GlmTerminalStallRecovery",
    "_MEMORY_READONLY_SYSTEM_PROMPT",
    "_ShellAllowAll",
    "_WebSearchBudgetMiddleware",
    "_add_interrupt_on",
    "_apply_inherited_pythonpath",
    "_artifacts_root",
    "_build_graph_factory",
    "_build_runtime_factory",
    "_build_tools",
    "_create_cli_compaction_middleware",
    "_create_goal_criteria_agent",
    "_create_rubric_grader_tools",
    "_criteria_context_tools",
    "_ensure_glm_5p2_profile_registered",
    "_format_task_error",
    "_get_harness_tool_descriptions",
    "_has_resolvable_model_provider",
    "_inject_fs_tools_into_subagents",
    "_normalize_rubric_grader_context_tools",
    "_offload_fallback_root",
    "_preview_dotenv_environ",
    "_resolve_ptc_option",
    "_resolve_retry_owned_model",
    "_resolve_shell_allow_list",
    "_rubric_grader_read_file_prefix",
    "_rubric_grader_messages",
    "_rubric_grader_state",
    "_rubric_grader_repository_tool_names",
    "_rubric_grader_system_prompt",
    "_rubric_interrupt_on",
    "_sanitize_agent_message_name",
    "_write_checkpointer",
    "attach_offload_operation",
    "apply_inherited_user_tracing",
    "bind_runtime_host_policy",
    "bind_server_extensions",
    "configure_langsmith_secret_redaction",
    "create_cli_agent",
    "create_deep_agent",
    "create_goal_criteria_fallback_agent",
    "create_model",
    "create_sandbox",
    "credentials",
    "emit_startup_failure",
    "ensure_agent_dir",
    "gated_mcp_tool_names",
    "generate_langgraph_json",
    "get_config_resolver",
    "get_default_working_dir",
    "get_langsmith_project_name",
    "get_project_agent_md_path",
    "get_project_agents_dir",
    "get_server_project_context",
    "get_skill_sources",
    "get_system_prompt",
    "get_user_agent_md_path",
    "get_user_agents_dir",
    "import_code_interpreter",
    "import_offload_api",
    "import_tool_calling_test_model",
    "is_env_truthy",
    "is_memory_auto_save_enabled",
    "list_subagents",
    "load_async_subagents",
    "load_extensions",
    "offload_operation_from",
    "require_thread_workspace",
    "resolve_auto_classifier_model",
    "resolve_auto_classifier_model_for_provider",
    "resolve_auto_classifier_timeout",
    "resolve_recursion_limit",
    "resolve_workspace",
    "restore_user_tracing_api_keys",
    "restore_user_tracing_env",
    "runtime_state",
    "server_manager_module",
    "shutdown_server_extensions",
    "start_server_and_get_agent",
    "suppress_langchain_beta_warning",
    "validate_backend_route",
    "use_environment",
]
