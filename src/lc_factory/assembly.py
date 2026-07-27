"""Factory assembly: an owned recomposition of upstream's ``create_cli_agent``.

``create_factory_agent`` is a line-faithful port of
``deepagents_code.agent.create_cli_agent`` (``agent.py:2155-2989`` at monorepo
``8da0ccb13``, authored against deepagents-code==0.1.47 and unchanged
through 0.1.48), with every upstream import routed
through :mod:`lc_factory.upstream`. Wave 1.2 rule: byte-equivalent semantics
to v0 — zero behavioral deltas beyond the import indirection. The middleware
injection seam and the other ratified deltas land in later iterations.

Pin-bump discipline: diff upstream's ``agent.py`` between tags, re-apply
mechanical changes here, and let the parity suite guard the composed
middleware stack.
"""

from __future__ import annotations

import logging
import os
import warnings
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from lc_factory.upstream import (
    CONVERSATION_HISTORY_DIRNAME,
    REPOSITORY_TOOL_CALL_LIMIT,
    AsyncApprovalHITLMiddleware,
    CLIContextSchema,
    CompositeBackend,
    ConfigurableModelMiddleware,
    FilesystemBackend,
    FilesystemMiddleware,
    LocalContextMiddleware,
    LocalShellBackend,
    MemoryMiddleware,
    PluginSkillsMiddleware,
    ReliableRubricMiddleware,
    ShellAllowListMiddleware,
    _FALLBACK_ARTIFACTS_ROOT,
    _MEMORY_READONLY_SYSTEM_PROMPT,
    _add_interrupt_on,
    _apply_inherited_pythonpath,
    _artifacts_root,
    _AsyncExecutableBackend,
    _create_cli_compaction_middleware,
    _create_rubric_grader_tools,
    _ensure_glm_5p2_profile_registered,
    _ExecutableBackend,
    _get_harness_tool_descriptions,
    _GlmTerminalStallRecovery,
    _inject_fs_tools_into_subagents,
    _normalize_rubric_grader_context_tools,
    _offload_fallback_root,
    _resolve_ptc_option,
    _rubric_grader_read_file_prefix,
    _rubric_grader_repository_tool_names,
    _rubric_grader_system_prompt,
    _sanitize_agent_message_name,
    _ShellAllowAll,
    config,
    create_deep_agent,
    get_default_working_dir,
    get_langsmith_project_name,
    get_system_prompt,
    list_subagents,
    restore_user_tracing_api_keys,
    restore_user_tracing_env,
    settings,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from lc_factory.upstream import (
        AgentMiddleware,
        AsyncSubAgent,
        BackendProtocol,
        BaseChatModel,
        BaseCheckpointSaver,
        BaseTool,
        CodeSkillSource,
        CompiledSubAgent,
        FsToolName,
        InterruptOnConfig,
        MCPServerInfo,
        Pregel,
        ProjectContext,
        SandboxBackendProtocol,
        SubAgent,
    )

logger = logging.getLogger(__name__)


def create_factory_agent(
    model: str | BaseChatModel,
    assistant_id: str,
    *,
    tools: Sequence[BaseTool | Callable | dict[str, Any]] | None = None,
    mcp_tools: Sequence[BaseTool] | None = None,
    sandbox: SandboxBackendProtocol | None = None,
    sandbox_type: str | None = None,
    system_prompt: str | None = None,
    interactive: bool = True,
    auto_approve: bool = False,
    auto_mode_enabled: bool = False,
    interrupt_shell_only: bool = False,
    shell_allow_list: list[str] | None = None,
    fs_tools: list[FsToolName] | None = None,
    enable_ask_user: bool = True,
    enable_memory: bool = True,
    memory_auto_save: bool = True,
    enable_skills: bool = True,
    enable_shell: bool = True,
    enable_interpreter: bool = False,
    rubric_model: str | BaseChatModel | None = None,
    rubric_max_iterations: int | None = None,
    recursion_limit: int | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
    mcp_server_info: list[MCPServerInfo] | None = None,
    cwd: str | Path | None = None,
    project_context: ProjectContext | None = None,
    async_subagents: list[AsyncSubAgent] | None = None,
    goal_criteria_tools: Sequence[BaseTool | Callable[..., Any]] | None = None,
    rubric_grader_tools: Sequence[BaseTool | Callable[..., Any]] | None = None,
) -> tuple[Pregel[Any, Any, Any, Any], CompositeBackend]:
    """Create a CLI-configured agent with flexible options.

    This is the main entry point for creating a Deep Agents Code agent, usable
    both internally and from external code (e.g., benchmarking frameworks).

    Args:
        model: LLM model to use (e.g., `'provider:model'`)
        assistant_id: Agent identifier for memory/state storage
        tools: Additional tools to provide to agent.
        mcp_tools: Exact MCP tools within `tools`, used to extend approval policy
            from their protocol annotations.
        sandbox: Optional sandbox backend for remote execution
            (e.g., `ModalSandbox`).

            If `None`, uses local filesystem + shell.
        sandbox_type: Type of sandbox provider
            (`'agentcore'`, `'daytona'`, `'langsmith'`, `'modal'`, `'runloop'`).
            Used for system prompt generation.
        system_prompt: Override the default system prompt.

            If `None`, a system prompt is auto-generated with dynamic context
            interpolated in (model identity, working directory, sandbox vs.
            local execution mode, skills path, and interactive-vs-headless
            guidance).

            !!! warning

                Passing a value here replaces that auto-generated prompt
                entirely — none of the dynamic context above is added, and
                `sandbox_type` and `interactive` no longer influence the
                prompt. Only pass an explicit prompt when you intend to take
                full ownership of the system prompt's content.
        interactive: When `False`, the auto-generated system prompt is
            tailored for headless non-interactive execution, and every stack
            gains terminal-stall recovery middleware (a runtime no-op unless the
            resolved model is Fireworks GLM-5.2). Only the system-prompt
            tailoring is ignored when `system_prompt` is provided explicitly;
            the recovery wiring still applies.
        auto_approve: If `True`, no tools trigger human-in-the-loop
            interrupts — all calls (shell execution, file writes/edits,
            web search, URL fetch) run automatically.

            If `False`, tools pause for user confirmation via the approval menu.
            See `_add_interrupt_on` for the full list of gated tools.
        auto_mode_enabled: Install classifier-backed Auto for the local Textual
            runtime. Callers must leave this disabled for headless, remote, and
            sandbox-backed graphs.
        interrupt_shell_only: If `True`, all HITL interrupts are disabled;
            shell commands are validated inline by `ShellAllowListMiddleware`
            against the configured allow-list instead.

            Used in non-interactive mode with a restrictive shell allow-list
            to avoid splitting traces into multiple LangSmith runs.

            Has no effect when `auto_approve` is `True` (interrupts are already
            disabled) or when `shell_allow_list` is `SHELL_ALLOW_ALL`.
        shell_allow_list: Explicit restrictive shell allow-list forwarded from
            the CLI process. When provided (and `interrupt_shell_only` is
            `True`), used directly instead of reading `settings.shell_allow_list`
            (which may not be set in the server subprocess environment).
        fs_tools: Allowlist of filesystem tools to expose to the agent, from
            `--allow-fs-tools`. `None` (default; also what `--allow-fs-tools
            all` parses to) leaves `FilesystemMiddleware` at its SDK default
            (all tools). An explicit list (which must include `"read_file"`)
            installs a `FilesystemMiddleware` restricted to those tool names,
            replacing the SDK's default for the main agent and every synchronous
            subagent (including `general-purpose`) as well as the nested
            goal-criteria agent, so delegation cannot bypass the restriction.
            Async subagents are unaffected (they run on their own remote
            backend, not the local filesystem).
        enable_ask_user: Enable `AskUserMiddleware` so the agent can ask
            clarifying questions.

            Non-interactive callers without a resume loop must explicitly pass
            `enable_ask_user=False`.
        enable_memory: Enable `MemoryMiddleware` for persistent memory
        memory_auto_save: When `True` (default), the memory prompt tells the
            agent to proactively persist learnings to the `AGENTS.md` sources.

            When `False`, memory is still loaded into context but the read-only
            prompt is used instead, so the agent does not auto-save; explicit
            saves (e.g. the `remember` skill) still work.

            No effect when
            `enable_memory` is `False`.
        enable_skills: Enable `SkillsMiddleware` for custom agent skills
        enable_shell: Enable shell execution via `LocalShellBackend`
            (only in local mode). When enabled, the `execute` tool is available.
        enable_interpreter: Wire `CodeInterpreterMiddleware` from
            `langchain-quickjs` into the main agent.

            Local-mode only — passing a non-`None` `sandbox` while
            `enable_interpreter=True` raises `ValueError`. Subagents do not
            receive the interpreter in v1.

            PTC (`tools.*` host bridge) calls bypass `interrupt_on`/HITL
            approval, so `settings.interpreter_ptc` is the only effective
            control over which host tools can be invoked from inside the
            REPL. `js_eval` itself is intentionally not gated by HITL —
            per-call approval would be unusably noisy and would not block
            PTC fan-out anyway. The `"safe"` preset is therefore restricted
            to tools that are already non-HITL outside the REPL (read-only
            file inspection); exposing HITL-gated tools — network fetch,
            subagent dispatch, shell, file writes — requires an explicit
            list or `interpreter_ptc="all"` with
            `interpreter_ptc_acknowledge_unsafe=True`.

            Requires the core `langchain-quickjs` dependency.
        rubric_model: Grader model for `RubricMiddleware`.

            A `'provider:model'` string or `BaseChatModel`.

            When `None`, the main `model` is reused.
        rubric_max_iterations: Explicit grader iterations per rubric attempt
            before the agent terminates with `'max_iterations_reached'`; `None`
            uses the SDK default.
        recursion_limit: Explicit LangGraph `recursion_limit` (graph step budget)
            for the main agent. When `None`, it is resolved from the
            `DEEPAGENTS_CODE_RECURSION_LIMIT` env var, `[runtime].recursion_limit`
            in `config.toml`, then the default via `resolve_recursion_limit`.
        checkpointer: Optional checkpointer for session persistence.
            When `None`, the graph is compiled without a checkpointer.
        mcp_server_info: MCP server metadata to surface in the system prompt.
        cwd: Override the working directory for the agent's filesystem backend
            and system prompt.
        project_context: Explicit project path context for project-sensitive
            behavior such as project `AGENTS.md` files, skills, subagents, and
            MCP trust.
        async_subagents: Remote LangGraph deployments to expose as async subagent tools.

            Loaded from `[async_subagents]` in `config.toml` or passed directly.
        goal_criteria_tools: External read-only context tools available to server-side
            goal criteria generation. `None` disables goal criteria requests.
        rubric_grader_tools: External read-only context tools available to rubric
            grading for verifying work completed in MCP-backed or web-accessible
            systems.

    Returns:
        2-tuple of `(agent_graph, backend)`

            - `agent_graph`: Configured LangGraph Pregel instance ready
                for execution
            - `composite_backend`: `CompositeBackend` for file operations

    Raises:
        ValueError: When `enable_interpreter=True` is paired with a
            non-`None` `sandbox`, when `settings.interpreter_ptc` contains
            unknown tool names, or when `interpreter_ptc="all"` is used
            without `auto_approve` or `interpreter_ptc_acknowledge_unsafe`.
    """
    tools = tools or []
    mcp_tools = tuple(mcp_tools or ())
    if auto_mode_enabled and (not interactive or sandbox is not None):
        logger.warning(
            "Classifier-backed Auto is unavailable outside the local interactive "
            "runtime; using Manual HITL"
        )
        auto_mode_enabled = False
    effective_cwd = (
        Path(cwd)
        if cwd is not None
        else (project_context.user_cwd if project_context is not None else None)
    )

    # Setup agent directory for persistent memory (if enabled)
    if enable_memory or enable_skills:
        agent_dir = settings.ensure_agent_dir(assistant_id)
        agent_md = agent_dir / "AGENTS.md"
        if not agent_md.exists():
            # Create empty file for user customizations
            # Base instructions are loaded fresh from get_system_prompt()
            agent_md.touch()

    # Skills directories (if enabled)
    skills_dir = None
    user_agent_skills_dir = None
    project_skills_dir = None
    project_agent_skills_dir = None
    if enable_skills:
        skills_dir = settings.ensure_user_skills_dir(assistant_id)
        user_agent_skills_dir = settings.get_user_agent_skills_dir()
        project_skills_dir = (
            project_context.project_skills_dir()
            if project_context is not None
            else settings.get_project_skills_dir()
        )
        project_agent_skills_dir = (
            project_context.project_agent_skills_dir()
            if project_context is not None
            else settings.get_project_agent_skills_dir()
        )

    # Load custom subagents from filesystem
    custom_subagents: list[SubAgent | CompiledSubAgent] = []
    restrictive_shell_allow_list: list[str] | None = None
    if interrupt_shell_only and not auto_approve:
        # Prefer the explicitly forwarded allow-list (set by the CLI process
        # and passed through ServerConfig).  Fall back to settings only for
        # direct callers (e.g. benchmarking frameworks) that don't go through
        # the server subprocess path.
        if shell_allow_list:
            restrictive_shell_allow_list = list(shell_allow_list)
        elif settings.shell_allow_list and not isinstance(
            settings.shell_allow_list, _ShellAllowAll
        ):
            restrictive_shell_allow_list = list(settings.shell_allow_list)
        else:
            logger.warning(
                "interrupt_shell_only=True but no restrictive shell allow-list "
                "available; falling back to standard HITL interrupts"
            )

    hitl_active = not auto_approve and restrictive_shell_allow_list is None
    resolved_interrupt_on = (
        _add_interrupt_on(
            mcp_tools=mcp_tools,
            auto_mode_enabled=auto_mode_enabled,
        )
        if hitl_active
        else None
    )

    user_agents_dir = settings.get_user_agents_dir(assistant_id)
    project_agents_dir = (
        project_context.project_agents_dir()
        if project_context is not None
        else settings.get_project_agents_dir()
    )

    def _subagent_cli_middleware(
        *,
        has_explicit_model: bool,
    ) -> list[AgentMiddleware[Any, Any]]:
        middleware: list[AgentMiddleware[Any, Any]] = []
        if resolved_interrupt_on is not None:
            middleware.append(AsyncApprovalHITLMiddleware(resolved_interrupt_on))
        if not has_explicit_model:
            middleware.append(ConfigurableModelMiddleware(persist_model_state=False))
        # Interactive turns may legitimately be tool-free, so terminal-stall
        # recovery is installed only on headless stacks. The middleware itself
        # activates only for the measured Fireworks GLM-5.2 endpoint.
        if not interactive:
            middleware.append(_GlmTerminalStallRecovery())
        if restrictive_shell_allow_list is not None:
            middleware.append(ShellAllowListMiddleware(restrictive_shell_allow_list))
        # Subagents share the on-disk filesystem backend and can edit the user
        # AGENTS.md, so they get the same managed onboarding-name block guard as
        # the main agent. Gated on memory because the block only exists when
        # memory is enabled.
        if enable_memory:
            from lc_factory.upstream import ManagedMemoryGuardMiddleware

            middleware.append(
                ManagedMemoryGuardMiddleware(
                    [settings.get_user_agent_md_path(assistant_id)]
                )
            )
        return middleware

    for subagent_meta in list_subagents(
        user_agents_dir=user_agents_dir,
        project_agents_dir=project_agents_dir,
    ):
        # Treat a falsy spec (`None` or `""`) as "no explicit model" so an empty
        # `model:` in subagent frontmatter inherits the runtime model rather than
        # being forwarded verbatim to `resolve_model("")`.
        model_spec = subagent_meta["model"]
        has_explicit_model = bool(model_spec)
        subagent: SubAgent = {
            "name": subagent_meta["name"],
            "description": subagent_meta["description"],
            "system_prompt": subagent_meta["system_prompt"],
        }
        if model_spec:
            subagent["model"] = model_spec
        subagent_middleware = _subagent_cli_middleware(
            has_explicit_model=has_explicit_model,
        )
        if subagent_middleware:
            subagent["middleware"] = subagent_middleware
        if resolved_interrupt_on is not None:
            # The async-aware stock-compatible middleware above owns approval
            # routing. A declarative subagent with no `interrupt_on` inherits
            # the parent's top-level map (`spec.get("interrupt_on", ...)` in
            # deepagents graph assembly), which would wrap its tools in a second
            # synchronous stock HITL. An explicit empty (falsy) map opts out.
            subagent["interrupt_on"] = {}
        custom_subagents.append(subagent)

    from lc_factory.upstream import (
        GENERAL_PURPOSE_SUBAGENT,
        RuntimeSubAgent,
    )

    if not any(
        subagent["name"] == GENERAL_PURPOSE_SUBAGENT["name"]
        for subagent in custom_subagents
    ):
        general_purpose_subagent: RuntimeSubAgent = {
            "name": GENERAL_PURPOSE_SUBAGENT["name"],
            "description": GENERAL_PURPOSE_SUBAGENT["description"],
            "system_prompt": GENERAL_PURPOSE_SUBAGENT["system_prompt"],
            "middleware": _subagent_cli_middleware(has_explicit_model=False),
        }
        if resolved_interrupt_on is not None:
            general_purpose_subagent["interrupt_on"] = {}
        custom_subagents.append(general_purpose_subagent)

    # Build middleware stack based on enabled features
    agent_middleware: list[AgentMiddleware[Any, Any]] = [
        ConfigurableModelMiddleware(),
    ]
    if not interactive:
        agent_middleware.append(_GlmTerminalStallRecovery())

    if not interactive and mcp_tools:
        from lc_factory.upstream import (
            HeadlessMCPGuardMiddleware,
            gated_mcp_tool_names,
        )

        if gated_names := gated_mcp_tool_names(mcp_tools):
            agent_middleware.append(HeadlessMCPGuardMiddleware(gated_names))

    # Resume state: declares private checkpoint channels used on resume.
    # `ResumeStateMiddleware.after_model` writes `_context_tokens`; model metadata
    # is written by `ConfigurableModelMiddleware` from the actual completed model
    # request. The CLI reads them back from `state_values` on thread resume.
    # Goal tools: exposes the read-only `get_goal`/`get_rubric` tools and the
    # constrained `update_goal` tool, and maintains goal-state notices.
    from lc_factory.upstream import GoalToolsMiddleware
    from lc_factory.upstream import ResumeStateMiddleware

    agent_middleware.extend([ResumeStateMiddleware(), GoalToolsMiddleware()])

    # Add ask_user middleware (must be early so its tool is available)
    trusted_ask_user_tool: BaseTool | None = None
    if enable_ask_user:
        from lc_factory.upstream import AskUserMiddleware

        ask_user_middleware = AskUserMiddleware()
        agent_middleware.append(ask_user_middleware)
        trusted_ask_user_tool = ask_user_middleware.tools[0]

    # Add memory middleware
    if enable_memory:
        memory_sources = [str(settings.get_user_agent_md_path(assistant_id))]
        project_agent_md_paths = (
            project_context.project_agent_md_paths()
            if project_context is not None
            else settings.get_project_agent_md_path()
        )
        memory_sources.extend(str(p) for p in project_agent_md_paths)

        # Loading memory stays on either way; a read-only prompt drops the
        # "proactively persist learnings" guidance when auto-save is disabled.
        if memory_auto_save:
            memory_middleware = MemoryMiddleware(
                backend=FilesystemBackend(virtual_mode=False),
                sources=memory_sources,
            )
        else:
            memory_middleware = MemoryMiddleware(
                backend=FilesystemBackend(virtual_mode=False),
                sources=memory_sources,
                system_prompt=_MEMORY_READONLY_SYSTEM_PROMPT,
            )
        agent_middleware.append(memory_middleware)

        # Protect the machine-managed onboarding-name block in the user
        # AGENTS.md from being rewritten by agent file edits. The block's
        # markers are HTML comments stripped before injection, so the model
        # can't see the boundary and would otherwise clobber it.
        from lc_factory.upstream import ManagedMemoryGuardMiddleware

        agent_middleware.append(
            ManagedMemoryGuardMiddleware(
                [settings.get_user_agent_md_path(assistant_id)]
            )
        )

    # Add skills middleware
    if enable_skills:
        # Lowest to highest precedence:
        # built-in -> plugins -> user .deepagents -> user .agents
        # -> project .deepagents -> project .agents
        # -> user .claude (experimental) -> project .claude (experimental)
        # Plugin skills are namespaced as `{plugin_id}:{skill_name}` to avoid
        # collisions between plugins and user/project skills.
        sources: list[CodeSkillSource] = [
            (str(settings.get_built_in_skills_dir()), "Built-in"),
        ]
        try:
            from lc_factory.upstream import discover_plugins
            from lc_factory.upstream import plugin_skill_sources

            plugin_result = discover_plugins()
            if plugin_result.warnings:
                logger.warning("Plugin discovery warnings: %s", plugin_result.warnings)
            sources.extend(plugin_skill_sources(plugin_result.plugins))
        except Exception:
            logger.warning("Could not discover plugin skills", exc_info=True)
        sources.extend(
            [
                (str(skills_dir), "User Deepagents"),
                (str(user_agent_skills_dir), "User Agents"),
            ]
        )
        if project_skills_dir:
            sources.append((str(project_skills_dir), "Project Deepagents"))
        if project_agent_skills_dir:
            sources.append((str(project_agent_skills_dir), "Project Agents"))

        # Experimental: Claude Code skill directories
        user_claude_skills_dir = settings.get_user_claude_skills_dir()
        if user_claude_skills_dir.exists():
            sources.append((str(user_claude_skills_dir), "User Claude"))
        project_claude_skills_dir = settings.get_project_claude_skills_dir()
        if project_claude_skills_dir:
            sources.append((str(project_claude_skills_dir), "Project Claude"))

        # `PluginSkillsMiddleware` namespaces plugin skills before dedup while
        # behaving like the SDK middleware when no plugin namespaces are
        # present, so it is safe to use for all skill sources.
        agent_middleware.append(
            PluginSkillsMiddleware(
                backend=FilesystemBackend(virtual_mode=False),
                sources=sources,
            )
        )

    # CONDITIONAL SETUP: Local vs Remote Sandbox
    if sandbox is None:
        # ========== LOCAL MODE ==========
        root_dir = effective_cwd if effective_cwd is not None else Path.cwd()
        if enable_shell:
            # Create environment for shell commands.
            # Restore the user's original LANGSMITH_PROJECT so their code traces
            # separately. When they had none, drop the agent's override (the
            # `deepagents-code` default applied at bootstrap) entirely so shell
            # commands don't inherit it.
            shell_env = os.environ.copy()
            if settings.user_langchain_project is not None:
                shell_env["LANGSMITH_PROJECT"] = settings.user_langchain_project
            else:
                shell_env.pop("LANGSMITH_PROJECT", None)
            restore_user_tracing_env(shell_env)
            restore_user_tracing_api_keys(shell_env)
            # Re-apply a launch-time PYTHONPATH that was stripped from the server
            # interpreter but relayed for approval-gated `execute` commands.
            _apply_inherited_pythonpath(shell_env)

            # Use LocalShellBackend for filesystem + shell execution.
            # The SDK's FilesystemMiddleware exposes per-command timeout
            # on the execute tool natively.
            # `inherit_env=False`: `shell_env` is already a complete, curated
            # copy of `os.environ`. Inheriting again would re-copy `os.environ`
            # and resurrect the popped carrier var, leaking it into `execute`.
            # `restore_user_tracing_api_keys` above depends on this too: flipping
            # to `inherit_env=True` would re-copy the agent's overridden
            # `LANGSMITH_API_KEY` and undo the restore, leaking it into `execute`.
            backend = LocalShellBackend(
                root_dir=root_dir,
                virtual_mode=False,
                inherit_env=False,
                env=shell_env,
            )
        else:
            # No shell access - use plain FilesystemBackend
            backend = FilesystemBackend(root_dir=root_dir, virtual_mode=False)
    else:
        # ========== REMOTE SANDBOX MODE ==========
        backend = sandbox  # Remote sandbox (ModalSandbox, etc.)
        # Note: Shell middleware not used in sandbox mode
        # File operations and execute tool are provided by the sandbox backend

    if enable_interpreter:
        if sandbox is not None:
            msg = (
                "enable_interpreter=True is not supported with a remote "
                "sandbox in this release. Disable the sandbox or unset "
                "enable_interpreter."
            )
            raise ValueError(msg)
        # Lazy interpreter import, routed through the boundary (upstream
        # keeps this lazy for CLI startup perf; preserved here).
        from lc_factory.upstream import (
            import_code_interpreter,
            suppress_langchain_beta_warning,
        )

        CodeInterpreterMiddleware, PTCOption = import_code_interpreter()

        ptc_names = _resolve_ptc_option(
            settings.interpreter_ptc,
            tools=tools,
            acknowledge_unsafe=settings.interpreter_ptc_acknowledge_unsafe,
            auto_approve=auto_approve,
        )
        ptc_option: PTCOption | None = (
            cast("PTCOption", list(ptc_names)) if ptc_names is not None else None
        )
        # `CodeInterpreterMiddleware` is decorated `@beta()`, which emits a
        # `LangChainBetaWarning` on every instantiation. We intentionally use it
        # and the warning is not actionable for users, so suppress it.
        with suppress_langchain_beta_warning():
            agent_middleware.append(
                CodeInterpreterMiddleware(
                    tool_name="js_eval",
                    timeout=settings.interpreter_timeout_seconds,
                    memory_limit=settings.interpreter_memory_limit_mb * 1024 * 1024,
                    max_ptc_calls=settings.interpreter_max_ptc_calls,
                    max_result_chars=settings.interpreter_max_result_chars,
                    ptc=ptc_option,
                )
            )

    # Local context middleware (git info, directory tree, etc.).
    if isinstance(backend, (_ExecutableBackend, _AsyncExecutableBackend)):
        agent_middleware.append(
            LocalContextMiddleware(
                backend=backend,
                mcp_server_info=mcp_server_info,
                tracing_project=get_langsmith_project_name(),
                user_tracing_project=settings.user_langchain_project,
            )
        )

    # Add shell allow-list middleware when interrupt_shell_only is active.
    if restrictive_shell_allow_list is not None:
        agent_middleware.append(ShellAllowListMiddleware(restrictive_shell_allow_list))

    # Get or use custom system prompt
    if system_prompt is None:
        system_prompt = get_system_prompt(
            assistant_id=assistant_id,
            sandbox_type=sandbox_type,
            interactive=interactive,
            cwd=effective_cwd,
            fs_tools=fs_tools,
        )

    interrupt_on: dict[str, bool | InterruptOnConfig] | None
    auto_mode_config: tuple[Path, list[str]] | None = None
    if resolved_interrupt_on is None:
        interrupt_on = {}
    else:
        interrupt_on = resolved_interrupt_on  # ty: ignore[invalid-assignment]  # InterruptOnConfig is compatible at runtime
        if auto_mode_enabled:
            configured_allow_list = shell_allow_list or settings.shell_allow_list
            narrow_allow_list = (
                configured_allow_list if isinstance(configured_allow_list, list) else []
            )
            trusted_root = (
                project_context.project_root
                if project_context is not None
                and project_context.project_root is not None
                else effective_cwd or Path.cwd()
            )
            auto_mode_config = (Path(trusted_root), narrow_allow_list)

    # Set up composite backend with routing.
    if sandbox is None:
        # Local mode normally lets large results fall through to the default
        # backend at the real, hardened `artifacts_root`, so filesystem tools and
        # `execute` receive the same host path. If that predictable directory is
        # unusable, `_artifacts_root` supplies a stable virtual root plus private
        # temporary storage, and `large_tool_results` is routed there explicitly.
        # Conversation history always has a dedicated route to persistent storage.
        # The fallback alias remains installed even after the predictable directory
        # recovers, so archive paths saved during fallback stay resolvable.
        artifacts_storage = _artifacts_root()
        artifacts_root = artifacts_storage.root
        conversation_history_backend = FilesystemBackend(
            root_dir=_offload_fallback_root() / CONVERSATION_HISTORY_DIRNAME,
            virtual_mode=True,
        )
        fallback_history_root = (
            f"{_FALLBACK_ARTIFACTS_ROOT}/{CONVERSATION_HISTORY_DIRNAME}/"
        )
        artifact_routes: dict[str, BackendProtocol] = {
            f"{artifacts_root}/{CONVERSATION_HISTORY_DIRNAME}/": (
                conversation_history_backend
            ),
            fallback_history_root: conversation_history_backend,
        }
        if artifacts_storage.large_results_dir is not None:
            artifact_routes[f"{artifacts_root}/large_tool_results/"] = (
                FilesystemBackend(
                    root_dir=artifacts_storage.large_results_dir,
                    virtual_mode=True,
                )
            )
        composite_backend = CompositeBackend(
            default=backend,
            routes=artifact_routes,
            artifacts_root=artifacts_root,
        )
    else:
        # Sandbox mode: No special routing needed
        composite_backend = CompositeBackend(
            default=backend,
            routes={},
        )

    compaction_middleware = _create_cli_compaction_middleware(model, composite_backend)
    if auto_mode_config is not None and resolved_interrupt_on is not None:
        from lc_factory.upstream import AutoModeHITLMiddleware

        trusted_root, narrow_allow_list = auto_mode_config
        agent_middleware.append(
            AutoModeHITLMiddleware(
                resolved_interrupt_on,
                worktree_root=trusted_root,
                shell_allow_list=narrow_allow_list,
                trusted_ask_user_tool=trusted_ask_user_tool,
                trusted_compaction_tool=compaction_middleware.tools[0],
            )
        )

    if fs_tools is not None:
        # `fs_tools` is an explicit allowlist here (`--allow-fs-tools all` and an
        # omitted flag both arrive as `None`, leaving the SDK default in place).
        main_tool_descriptions = _get_harness_tool_descriptions(model)
        # Overrides the SDK's default `FilesystemMiddleware` (matched by
        # `.name` in `create_deep_agent`'s custom-middleware merge) for the
        # main agent. Preserve the SDK harness's model-specific tool metadata
        # on the replacement.
        #
        # NOTE: this replacement only carries `backend`/`tools`/descriptions.
        # The SDK also builds its default with `_permissions`; dcode passes no
        # filesystem `permissions` to `create_deep_agent` today, so there is
        # nothing to preserve. If dcode ever adopts filesystem permissions,
        # they must be threaded through here (and into
        # `_inject_fs_tools_into_subagents`) or `--allow-fs-tools` would
        # silently strip them.
        agent_middleware.append(
            FilesystemMiddleware(
                backend=composite_backend,
                tools=fs_tools,
                custom_tool_descriptions=main_tool_descriptions,
            )
        )
        # dcode always supplies its own `general-purpose` spec, so the SDK's
        # auto-created-GP middleware inheritance path never fires; the
        # restriction must be injected into each subagent's own `middleware`
        # list, or delegating via `task` could bypass `--allow-fs-tools`.
        _inject_fs_tools_into_subagents(
            custom_subagents,
            fs_tools=fs_tools,
            backend=composite_backend,
            main_tool_descriptions=main_tool_descriptions,
        )

    if goal_criteria_tools is not None:
        from lc_factory.upstream import (
            GoalCriteriaMiddleware,
            _create_goal_criteria_agent,
            create_goal_criteria_fallback_agent,
        )

        if sandbox is not None:
            if sandbox_type is not None:
                criteria_backend = sandbox
                criteria_root = get_default_working_dir(sandbox_type)
            else:
                criteria_backend = None
                criteria_root = "/"
        elif project_context is not None and project_context.project_root is not None:
            criteria_backend = FilesystemBackend(
                root_dir=project_context.project_root,
                virtual_mode=True,
            )
            criteria_root = "/"
        else:
            criteria_backend = None
            criteria_root = "/"
        criteria_agent = _create_goal_criteria_agent(
            model=model,
            repository_backend=criteria_backend,
            repository_root=criteria_root,
            context_tools=goal_criteria_tools,
            auto_mode_enabled=auto_mode_enabled,
            fs_tools=fs_tools,
        )
        criteria_fallback_agent = create_goal_criteria_fallback_agent(model=model)
        agent_middleware.append(
            GoalCriteriaMiddleware(criteria_agent, criteria_fallback_agent)
        )

    agent_middleware.append(compaction_middleware)

    grader_context_tools = _normalize_rubric_grader_context_tools(
        rubric_grader_tools or ()
    )

    # Give the rubric grader read-only inspection of the working directory so it
    # can verify criteria against the actual files rather than the transcript,
    # which is truncated for extremely long efforts. Local grading gets a
    # dedicated virtual backend rooted at the working directory so files found by
    # `glob` and `grep` receive the backend's canonical containment checks too.
    # Without a recognized sandbox type there is no trusted working-directory
    # root, so repository inspection stays disabled rather than exposing `/`.
    if sandbox is not None and sandbox_type is not None:
        grader_repository_backend: BackendProtocol | None = backend
        grader_repository_root = get_default_working_dir(sandbox_type)
    elif sandbox is None:
        grader_repository_backend = FilesystemBackend(
            root_dir=root_dir,
            virtual_mode=True,
        )
        grader_repository_root = "/"
    else:
        grader_repository_backend = None
        grader_repository_root = None

    grader_repository_tool_names = _rubric_grader_repository_tool_names(fs_tools)
    grader_tools = _create_rubric_grader_tools(
        composite_backend,
        repository_backend=grader_repository_backend,
        repository_root=grader_repository_root,
        context_tools=grader_context_tools,
        fs_tools=fs_tools,
    )
    from lc_factory.upstream import (
        _ContextToolCallBudgetMiddleware,
        _CriteriaContextBudgetMiddleware,
        _rubric_interrupt_on,
        _WebSearchBudgetMiddleware,
    )

    grader_middleware: list[AgentMiddleware[Any, Any]] = [
        _ContextToolCallBudgetMiddleware(
            # `read_file` is bounded separately by the grader's in-tool
            # working-directory counter, which excludes offloaded-result reads.
            # Excluding `read_file` here keeps reading offloaded tool results
            # (the grader's primary evidence source) from consuming this shared
            # context-call budget.
            {
                grader_tool.name
                for grader_tool in grader_tools
                if grader_tool.name != "read_file"
            },
            limit=REPOSITORY_TOOL_CALL_LIMIT,
        ),
        _WebSearchBudgetMiddleware(),
        _CriteriaContextBudgetMiddleware(label="Rubric grader context"),
    ]
    if grader_context_tools and hitl_active:
        grader_middleware.append(
            AsyncApprovalHITLMiddleware(
                interrupt_on=_rubric_interrupt_on(
                    grader_context_tools,
                    auto_mode_enabled=auto_mode_enabled,
                )
            )
        )

    # Rubric-driven self-evaluation. The middleware is a no-op until a
    # `rubric` is supplied on invocation state, so installing it is safe.
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="The middleware `RubricMiddleware` is in beta",
            category=Warning,
        )
        rubric_kwargs: dict[str, Any] = {
            "model": rubric_model if rubric_model is not None else model,
            "system_prompt": _rubric_grader_system_prompt(
                _rubric_grader_read_file_prefix(composite_backend),
                grader_repository_root,
                [context_tool.name for context_tool in grader_context_tools],
                repository_tool_names=grader_repository_tool_names,
            ),
            "tools": grader_tools,
            "grader_middleware": grader_middleware,
            "grader_context_schema": CLIContextSchema,
        }
        if rubric_max_iterations is not None:
            rubric_kwargs["max_iterations"] = rubric_max_iterations
        agent_middleware.append(ReliableRubricMiddleware(**rubric_kwargs))

    # Create the agent
    all_subagents: list[SubAgent | CompiledSubAgent | AsyncSubAgent] = [
        *custom_subagents,
        *(async_subagents or []),
    ]
    _ensure_glm_5p2_profile_registered()
    from lc_factory.upstream import resolve_recursion_limit

    effective_recursion_limit = (
        recursion_limit if recursion_limit is not None else resolve_recursion_limit()
    )
    agent = create_deep_agent(
        model=model,
        system_prompt=system_prompt,
        tools=tools,
        backend=composite_backend,
        middleware=agent_middleware,
        interrupt_on=interrupt_on,
        context_schema=CLIContextSchema,
        checkpointer=checkpointer,
        subagents=all_subagents or None,
        name=_sanitize_agent_message_name(assistant_id),
    ).with_config({**config, "recursion_limit": effective_recursion_limit})
    return agent, composite_backend
