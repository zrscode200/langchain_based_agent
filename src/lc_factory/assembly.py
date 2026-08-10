"""Factory assembly: an owned recomposition of upstream's ``create_cli_agent``.

``create_factory_agent`` is a line-faithful port of
``deepagents_code.agent.create_cli_agent`` (``agent.py:2155-2989`` at monorepo
``8da0ccb13``, authored against deepagents-code==0.1.47, unchanged through
0.1.48, re-applied for 0.1.52's composition changes: cost tracking,
server-owned Hooks v2, the Auto classifier configuration, and the HITL
restructure, then unchanged again through 0.1.54 — upstream's ``agent.py``
is byte-identical between 0.1.52 and 0.1.54), with every upstream import
routed through :mod:`lc_factory.upstream`.

**One deliberate behavioral delta**: the middleware injection seam
(``middleware=``, Group 2), which is inert unless used — the default
composition stays byte-identical to v0, and ``tests/test_parity.py`` proves it
unmodified. Every splice and resolve point is marked ``# SEAM`` — grep that
marker rather than trusting a count, since Group 3 added targets; see
``UPGRADING.md``
for the full divergence inventory. Everything else here is line-faithful, and
the remaining ratified deltas land in later iterations.

Pin-bump discipline: diff upstream's ``agent.py`` between tags, re-apply
mechanical changes here, and let the parity suite guard the composed
middleware stack.
"""

from __future__ import annotations

import logging
import os
import warnings
from collections import Counter
from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

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

FactoryPhase = Literal["first", "before_verification", "last"]
"""Where caller-supplied middleware is spliced into the factory stack.

Each phase anchors to middleware the assembly builds *unconditionally*, so a
phase boundary does not move when configuration turns other middleware on or
off. See ``_PHASE_ORDER`` for the guarantees.
"""

_PHASE_ORDER: tuple[FactoryPhase, ...] = ("first", "before_verification", "last")
"""Phases in stack order.

- ``first`` — ahead of every factory middleware, with exceptions the seam
  does not control: a factory middleware whose name matches an SDK-default
  slot is hoisted to that slot by the SDK's name-based merge. Two standing
  instances: the compaction middleware (named ``SummarizationMiddleware``
  since 0.1.52, so it always rides the SDK core's summarization slot), and
  the factory's own ``FilesystemMiddleware`` when ``fs_tools`` is set.
- ``before_verification`` — after everything that installs tools, context,
  memory, skills and approval policy (since 0.1.52 that includes the main
  agent's HITL approval middleware and server-owned hooks, which upstream
  moved into the stack); ahead of the goal-criteria -> rubric verification
  tail.
- ``last`` — after every factory middleware, immediately ahead of the SDK's own
  tail (harness profile, prompt caching), which is not addressable. Since
  0.1.52 the approval gate is NOT part of that tail: upstream installs HITL
  inside the factory stack, ahead of the verification tail, so ``last``
  middleware sits after the approval gate in list order rather than before it.
"""

_DEFAULT_PHASE: FactoryPhase = "before_verification"
"""Phase used when ``middleware`` is given as a bare sequence."""

SubagentPhase = Literal["first", "last"]
"""Where caller-supplied middleware is spliced into each *subagent* stack.

Deliberately a smaller vocabulary than `FactoryPhase`: subagent stacks have no
goal-criteria/rubric verification tail, so `'before_verification'` would name a
boundary that does not exist. The two phases here are the edges of the factory's
own subagent block, which is never empty — `CostTrackingMiddleware(nested=True)`
and `ServerHooksMiddleware` are appended unconditionally — so neither boundary
moves with configuration.
"""

_SUBAGENT_PHASE_ORDER: tuple[SubagentPhase, ...] = ("first", "last")
"""Subagent phases in stack order.

- ``first`` — ahead of every factory subagent middleware, including the
  approval gate. Outermost of the factory block.
- ``last`` — after every factory subagent middleware, still ahead of the SDK's
  own subagent tail (harness-profile extras, prompt caching), which is not
  addressable from here.

Both sit *inside* the block the SDK splices ahead of its tail: `graph.py`
captures `_subagent_core_names` before appending profile extras, so the whole
factory block — injections included — lands ahead of them.
"""

GraderPhase = Literal["first", "last"]
"""Where caller-supplied middleware is spliced into the rubric grader stack.

Same two-phase shape as `SubagentPhase`, for the same reason — the grader has
no verification tail of its own — but a distinct type because the stacks are
distinct and a caller addressing the wrong one should get a clear error rather
than a silent no-op.
"""

_GRADER_PHASE_ORDER: tuple[GraderPhase, ...] = ("first", "last")
"""Grader phases in stack order.

The factory's grader block is never empty — three budget middlewares
(`_ContextToolCallBudgetMiddleware`, `_WebSearchBudgetMiddleware`,
`_CriteriaContextBudgetMiddleware`) are unconditional — so both boundaries are
well defined regardless of configuration.
"""

_DEFAULT_GRADER_PHASE: GraderPhase = "last"
"""Phase used when ``rubric_grader_middleware`` is given as a bare sequence.

``last`` puts the caller's middleware *inside* the budget middlewares, which is
the direction that keeps grader cost bounded: earlier is outermost, so budgets
at the front still wrap — and therefore still count — whatever the injection
does. ``first`` places it outside them, which is legitimate but has to be asked
for.
"""

_DEFAULT_SUBAGENT_PHASE: SubagentPhase = "last"
"""Phase used when ``subagent_middleware`` is given as a bare sequence.

``last`` rather than ``first``, mirroring the main agent's
``before_verification`` default: the caller's middleware lands *after* every
middleware that installs approval policy, hooks, and the memory guard. A caller
who wants to sit outside the approval gate has to ask for ``first`` explicitly,
which is the safer direction for a default on a delegated stack.
"""

_SDK_RESERVED_MIDDLEWARE_NAMES = frozenset(
    {
        # Core, ahead of the factory block.
        "FilesystemMiddleware",
        "SubAgentMiddleware",
        "SummarizationMiddleware",
        "PatchToolCallsMiddleware",
        "AsyncSubAgentMiddleware",
        "SkillsMiddleware",
        # Tail, behind the factory block. Reachable for replacement just the
        # same: the SDK's merge compares against its FULLY assembled stack.
        "AnthropicPromptCachingMiddleware",
        "BedrockPromptCachingMiddleware",
        "FireworksPromptCachingMiddleware",
        "MemoryMiddleware",
        "HumanInTheLoopMiddleware",
        # Harness-profile `extra_middleware`. Model-dependent at runtime, so
        # this is the union across every built-in profile — the guard stays
        # model-independent on purpose (see below).
        "TodoListMiddleware",
        "ChatNVIDIAMessageCompatibilityMiddleware",
        "EntityResolutionGuardMiddleware",
        "FinalAnswerGuardMiddleware",
        "FollowupDisciplineMiddleware",
        "ModelRateLimitRetryMiddleware",
        "NemotronPolicyNudgeMiddleware",
        "NemotronProgressBudgetMiddleware",
        "NemotronReasoningTagCleanupMiddleware",
        "NemotronTextToolCallParser",
        "NemotronToolCallShim",
        "ReadFileContinuationNoticeMiddleware",
        "ToolRetryMiddleware",
    }
)
"""Names the deepagents SDK may own in the stack it assembles around ours.

`create_deep_agent` merges custom middleware **by name** against its *fully
assembled* stack (`graph.py:217`, called at `:883` — after the tail is appended
at `:859-876`). A collision is *replaced in place at the SDK's position*
instead of being inserted at the requested phase, silently.

Both halves matter, and the tail is the dangerous one. Measured at this pin:
injecting langchain's stock `HumanInTheLoopMiddleware` — the most natural thing
a caller might do — raised nothing and replaced the factory's approval gate,
taking the gated-tool set from eleven entries down to the caller's own. `execute`,
`write_file`, `edit_file`, `delete` and `task` all became unattended. Injecting
a `FilesystemMiddleware` likewise displaces the SDK's own, which backs every
filesystem tool and enforces `permissions`.

`tests/test_seam.py` re-derives all three groups — core, tail, and the
harness-profile union — and asserts this constant still covers them, so an
upstream release that adds middleware fails there rather than opening a hole.
Deriving in the test rather than at runtime keeps the private
`_HARNESS_PROFILES` registry out of `src/`, and keeps profile middleware from
being *constructed* on every composition just to read its name.

**Deliberately model-independent.** A name is rejected when *any* registered
profile could own it, not only the profile for the caller's model — so
injecting e.g. `ToolRetryMiddleware` is refused even on a model whose profile
does not install one. Over-rejection is the safe direction: the error is
explicit and the caller can override `.name`, whereas under-rejection is
silent replacement. This matches the stance already taken for
`MemoryMiddleware`, which the factory never lets the SDK install either.

**Known residual:** profiles registered by third parties after import
(`register_harness_profile` is public API) are not covered.
"""


def _checked_middleware(
    items: Sequence[AgentMiddleware[Any, Any]], phase: str
) -> list[Any]:
    """Validate and materialize one phase's middleware sequence.

    Shared by the main-agent and subagent normalizers so both documented
    surfaces agree on what input is legal.

    Args:
        items: Caller-supplied sequence for this phase.
        phase: Phase name, used only in error messages.

    Returns:
        The materialized list.

    Raises:
        ValueError: For an unordered collection, a non-iterable, or an entry
            that is not usable as middleware.
    """
    # Order IS the seam's contract, and set iteration order varies with
    # PYTHONHASHSEED between runs. Accepting one would turn a documented
    # position into a coin flip that nothing downstream notices, and
    # `{MyMiddleware()}` is the natural typo for a phase mapping.
    if isinstance(items, AbstractSet):
        msg = (
            f"Middleware for phase {phase!r} was given as an unordered "
            f"{type(items).__name__}; composition order is part of the "
            f"seam's contract. Use a list or tuple."
        )
        raise ValueError(msg)
    # Materialize BEFORE inspecting: a generator, `map`, or any one-shot
    # iterable would otherwise be consumed by the check and compose as
    # empty — a silent drop, which is the exact failure the phase-key check
    # below exists to prevent. Convert the non-iterable TypeError to the
    # documented ValueError so the transport's env-var-attribution funnel
    # (which catches ValueError only) still names the knob at fault.
    try:
        items = list(items)
    except TypeError as exc:
        msg = (
            f"Middleware for phase {phase!r} is not iterable: got "
            f"{type(items).__name__}. Pass a sequence of middleware — a "
            f"bare middleware instance is the usual cause."
        )
        raise ValueError(msg) from exc
    # Shape is validated here rather than at the composition site so a bad
    # entry fails before any setup work, and with a message that says what
    # was wrong instead of an `AttributeError` on `.name` much later.
    for item in items:
        if not isinstance(getattr(item, "name", None), str):
            msg = (
                f"Injected middleware for phase {phase!r} must be "
                f"AgentMiddleware instances with a string `.name`; got "
                f"{item!r}."
            )
            raise ValueError(msg)
    return items


def _normalize_for_phases(
    middleware: Sequence[AgentMiddleware[Any, Any]]
    | Mapping[str, Sequence[AgentMiddleware[Any, Any]]]
    | None,
    *,
    phase_order: tuple[str, ...],
    default_phase: str,
    argument: str,
) -> dict[Any, list[AgentMiddleware[Any, Any]]]:
    """Resolve caller middleware into one list per phase of a given vocabulary.

    Args:
        middleware: A bare sequence (assigned to ``default_phase``) or a mapping
            keyed by phase. ``None`` yields empty lists.
        phase_order: The valid phases, in stack order.
        default_phase: Phase used for a bare sequence.
        argument: Parameter name, used in the unknown-phase error so a caller
            addressing the wrong target sees which knob they got wrong.

    Returns:
        Mapping of every phase in ``phase_order`` to its middleware list.

    Raises:
        ValueError: When a mapping contains an unknown phase key, or when an
            entry is not usable as middleware.
    """
    resolved: dict[Any, list[AgentMiddleware[Any, Any]]] = {
        phase: [] for phase in phase_order
    }
    if middleware is None:
        return resolved

    if isinstance(middleware, Mapping):
        # Fail fast on shape: a typo'd phase key would otherwise be silently
        # dropped, and the caller's middleware would never be composed at all.
        if unknown := sorted(str(key) for key in middleware if key not in resolved):
            msg = (
                f"Unknown middleware phase(s) {unknown} for {argument!r}. "
                f"Valid phases: {list(phase_order)}."
            )
            raise ValueError(msg)
        for phase, items in middleware.items():
            resolved[phase].extend(_checked_middleware(items, phase))
    else:
        resolved[default_phase].extend(
            _checked_middleware(middleware, default_phase)
        )
    return resolved


def _normalize_injected_middleware(
    middleware: Sequence[AgentMiddleware[Any, Any]]
    | Mapping[FactoryPhase, Sequence[AgentMiddleware[Any, Any]]]
    | None,
) -> dict[FactoryPhase, list[AgentMiddleware[Any, Any]]]:
    """Resolve the ``middleware`` argument into one list per phase.

    Main-agent entry point. Kept as a distinct named function because
    `lc_factory.server_graph` imports it to normalize the
    `LC_FACTORY_MIDDLEWARE` transport's return value, and its behavior is
    pinned by that module's tests.

    Args:
        middleware: Caller-supplied middleware — a bare sequence (assigned to
            the default phase) or a mapping keyed by phase. ``None`` yields
            empty lists, which is what keeps the no-injection composition
            byte-identical to v0.

    Returns:
        Mapping of every phase in `_PHASE_ORDER` to its middleware list.

    Raises:
        ValueError: When a mapping contains a key that is not a known phase, or
            when an entry is not usable as middleware.
    """
    return _normalize_for_phases(
        middleware,
        phase_order=_PHASE_ORDER,
        default_phase=_DEFAULT_PHASE,
        argument="middleware",
    )


def _normalize_subagent_middleware(
    middleware: Sequence[AgentMiddleware[Any, Any]]
    | Mapping[SubagentPhase, Sequence[AgentMiddleware[Any, Any]]]
    | None,
) -> dict[SubagentPhase, list[AgentMiddleware[Any, Any]]]:
    """Resolve the ``subagent_middleware`` argument into one list per phase.

    Args:
        middleware: Caller-supplied middleware for every subagent stack — a
            bare sequence (assigned to `_DEFAULT_SUBAGENT_PHASE`) or a mapping
            keyed by `SubagentPhase`. ``None`` yields empty lists, keeping
            subagent composition byte-identical to v0.

    Returns:
        Mapping of every phase in `_SUBAGENT_PHASE_ORDER` to its list.

    Raises:
        ValueError: When a mapping contains an unknown phase key, or when an
            entry is not usable as middleware.
    """
    return _normalize_for_phases(
        middleware,
        phase_order=_SUBAGENT_PHASE_ORDER,
        default_phase=_DEFAULT_SUBAGENT_PHASE,
        argument="subagent_middleware",
    )


def _normalize_grader_middleware(
    middleware: Sequence[AgentMiddleware[Any, Any]]
    | Mapping[GraderPhase, Sequence[AgentMiddleware[Any, Any]]]
    | None,
) -> dict[GraderPhase, list[AgentMiddleware[Any, Any]]]:
    """Resolve the ``rubric_grader_middleware`` argument into one list per phase.

    Args:
        middleware: Caller-supplied middleware for the rubric grader stack — a
            bare sequence (assigned to `_DEFAULT_GRADER_PHASE`) or a mapping
            keyed by `GraderPhase`. ``None`` yields empty lists, keeping grader
            composition byte-identical to v0.

    Returns:
        Mapping of every phase in `_GRADER_PHASE_ORDER` to its list.

    Raises:
        ValueError: When a mapping contains an unknown phase key, or when an
            entry is not usable as middleware.
    """
    return _normalize_for_phases(
        middleware,
        phase_order=_GRADER_PHASE_ORDER,
        default_phase=_DEFAULT_GRADER_PHASE,
        argument="rubric_grader_middleware",
    )


def _validate_injected_middleware(
    agent_middleware: Sequence[AgentMiddleware[Any, Any]],
    injected: Mapping[FactoryPhase, Sequence[AgentMiddleware[Any, Any]]],
) -> None:
    """Reject injected middleware the SDK's merge would mishandle.

    Runs on the fully composed factory stack, immediately before it is handed
    to `create_deep_agent`, so there is a single site to re-apply on a pin bump.

    Args:
        agent_middleware: The composed factory stack, injections included.
        injected: Per-phase caller middleware, from
            `_normalize_injected_middleware`.

    Raises:
        ValueError: When injected middleware claims an SDK base name, or when
            any name appears twice in the composed stack.
    """
    injected_names = {item.name for items in injected.values() for item in items}
    if colliding := sorted(injected_names & _SDK_RESERVED_MIDDLEWARE_NAMES):
        msg = (
            f"Injected middleware uses name(s) reserved by the deepagents SDK: "
            f"{colliding}. The SDK merges custom middleware by name against the "
            f"base stack it assembles around ours: a name present in that base "
            f"is silently REPLACED in place instead of landing at the requested "
            f"phase, and a reserved name absent from the base at this pin ends "
            f"in langchain's bare duplicate-name assertion, or lands with "
            f"undocumented placement when its owner is not composed. Which "
            f"outcome applies to which name is the SDK's to change per "
            f"release, and a collision can disable load-bearing scaffolding — "
            f"up to the human approval gate — so every reserved name is "
            f"rejected here. Override `.name` on the injected middleware."
        )
        raise ValueError(msg)

    counts = Counter(item.name for item in agent_middleware)
    if duplicates := sorted(name for name, count in counts.items() if count > 1):
        # langchain rejects duplicates too, but with a message that names
        # nothing. Attribute correctly — by whether a duplicated name is
        # actually among the injected ones, not by whether anything was
        # injected at all: a factory self-collision (a port defect, most
        # likely surfacing during a pin bump) alongside an innocent, cleanly
        # named injection must not send the maintainer renaming middleware
        # that is not the cause.
        remedy = (
            "Override `.name` on the injected middleware."
            if injected_names & set(duplicates)
            else "The duplicated name(s) are not among the injected ones, so "
            "this is a collision inside the factory's own stack — check the "
            "port against upstream."
        )
        msg = (
            f"Duplicate middleware name(s) in the composed stack: {duplicates}. "
            f"Every middleware needs a unique `.name`. {remedy}"
        )
        raise ValueError(msg)


def _validate_subagent_reserved_names(
    injected: Mapping[SubagentPhase, Sequence[AgentMiddleware[Any, Any]]],
) -> None:
    """Reject subagent middleware claiming a name the SDK owns on that stack.

    Subagent specs go through the *same* name-based merge as the main stack:
    `graph.py` builds a subagent base (`FilesystemMiddleware`, summarization,
    `PatchToolCallsMiddleware`, optional `SkillsMiddleware`, then harness-profile
    extras and prompt caching) and calls `_apply_custom_middleware` on it with
    the spec's `middleware`. A collision is therefore replaced in place at the
    SDK's position instead of landing at the requested phase — silently, exactly
    as on the main agent.

    The subagent base is a strict *subset* of `_SDK_RESERVED_MIDDLEWARE_NAMES`
    (it has no `SubAgentMiddleware`, `AsyncSubAgentMiddleware`,
    `MemoryMiddleware`, or `HumanInTheLoopMiddleware` — the factory passes
    `interrupt_on={}` precisely so the SDK appends no stock HITL). Reusing the
    main constant therefore **over-rejects**, refusing a few names the subagent
    base could not actually own. That is the deliberate direction: over-rejection
    is an explicit error the caller can work around by renaming, while
    under-rejection silently replaces SDK scaffolding. It also matches the
    model-independent stance already taken for harness-profile names.

    `tests/test_seam.py` derives the real subagent base from the SDK and asserts
    it stays a subset of the constant, so an upstream addition that this guard
    would miss fails there rather than opening a hole.

    Args:
        injected: Per-phase caller middleware from
            `_normalize_subagent_middleware`.

    Raises:
        ValueError: When injected subagent middleware claims a reserved name.
    """
    injected_names = {item.name for items in injected.values() for item in items}
    if colliding := sorted(injected_names & _SDK_RESERVED_MIDDLEWARE_NAMES):
        msg = (
            f"Injected subagent middleware uses name(s) reserved by the "
            f"deepagents SDK: {colliding}. Subagent specs go through the same "
            f"name-based merge as the main agent, so a reserved name is "
            f"silently REPLACED in place on every subagent stack instead of "
            f"landing at the requested phase — including middleware that backs "
            f"filesystem tools and compaction. The guarded set is the main "
            f"stack's, which is a superset of the subagent base, so a few names "
            f"are refused that only the main stack could own; renaming is the "
            f"fix either way. Override `.name` on the injected middleware."
        )
        raise ValueError(msg)


def _validate_grader_stack(
    grader_stack: Sequence[AgentMiddleware[Any, Any]],
    injected_names: AbstractSet[str],
) -> None:
    """Reject duplicate names in the composed rubric-grader stack.

    **Deliberately narrower than the subagent guard, because the hazard is
    genuinely different.** The grader is not built by `create_deep_agent`:
    `ReliableRubricMiddleware` passes this list straight to langchain's
    `create_agent` (`reliable_rubric.py`, `_ensure_grader`). There is no SDK
    base stack to name-merge against, so the silent in-place replacement that
    drives `_SDK_RESERVED_MIDDLEWARE_NAMES` cannot happen here. Reusing that
    constant would refuse names for a reason that does not apply to this
    target, which is worse than useless — it teaches the wrong model of why the
    guard exists.

    What *can* happen is langchain's own duplicate-name assertion, which names
    nothing. So this checks uniqueness and attributes the failure.

    Runs at factory composition time even though the grader agent is
    constructed lazily and cached on first grading. A lazy-only check would
    surface a bad injection in the middle of a rubric evaluation rather than at
    boot, which is the failure mode this whole seam exists to prevent.

    Args:
        grader_stack: The composed grader middleware list, injections included.
        injected_names: Names supplied via `rubric_grader_middleware`, used only
            to attribute the failure correctly.

    Raises:
        ValueError: When any name appears twice in the composed stack.
    """
    counts = Counter(item.name for item in grader_stack)
    if duplicates := sorted(name for name, count in counts.items() if count > 1):
        remedy = (
            "Override `.name` on the injected grader middleware."
            if injected_names & set(duplicates)
            else "The duplicated name(s) are not among the injected ones, so "
            "this is a collision inside the factory's own grader stack — check "
            "the port against upstream."
        )
        msg = (
            f"Duplicate middleware name(s) in the composed rubric-grader "
            f"stack: {duplicates}. Every middleware needs a unique `.name`; "
            f"langchain's own check would otherwise fail at first grading with "
            f"a message naming nothing. {remedy}"
        )
        raise ValueError(msg)


def _validate_subagent_stack(
    subagent_stack: Sequence[AgentMiddleware[Any, Any]],
    injected_names: AbstractSet[str],
) -> None:
    """Reject duplicate names inside one composed subagent stack.

    Runs per subagent rather than once, because the stacks are not identical:
    `ConfigurableModelMiddleware` is present only without an explicit model,
    stall recovery only when headless, the shell allow-list only when
    restrictive, and the memory guard only with memory enabled. A name that is
    unique against one subagent's stack can collide on another's.

    Args:
        subagent_stack: One composed subagent middleware list, injections
            included.
        injected_names: Names supplied via `subagent_middleware`, used only to
            attribute the failure correctly.

    Raises:
        ValueError: When any name appears twice in the composed stack.
    """
    counts = Counter(item.name for item in subagent_stack)
    if duplicates := sorted(name for name, count in counts.items() if count > 1):
        remedy = (
            "Override `.name` on the injected subagent middleware."
            if injected_names & set(duplicates)
            else "The duplicated name(s) are not among the injected ones, so "
            "this is a collision inside the factory's own subagent stack — "
            "check the port against upstream."
        )
        msg = (
            f"Duplicate middleware name(s) in a composed subagent stack: "
            f"{duplicates}. Every middleware needs a unique `.name`. {remedy}"
        )
        raise ValueError(msg)


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
    auto_classifier_model: str | BaseChatModel | None = None,
    recursion_limit: int | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
    mcp_server_info: list[MCPServerInfo] | None = None,
    cwd: str | Path | None = None,
    project_context: ProjectContext | None = None,
    async_subagents: list[AsyncSubAgent] | None = None,
    goal_criteria_tools: Sequence[BaseTool | Callable[..., Any]] | None = None,
    rubric_grader_tools: Sequence[BaseTool | Callable[..., Any]] | None = None,
    middleware: Sequence[AgentMiddleware[Any, Any]]
    | Mapping[FactoryPhase, Sequence[AgentMiddleware[Any, Any]]]
    | None = None,
    subagent_middleware: Sequence[AgentMiddleware[Any, Any]]
    | Mapping[SubagentPhase, Sequence[AgentMiddleware[Any, Any]]]
    | None = None,
    rubric_grader_middleware: Sequence[AgentMiddleware[Any, Any]]
    | Mapping[GraderPhase, Sequence[AgentMiddleware[Any, Any]]]
    | None = None,
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
        auto_classifier_model: Model the Auto approval classifier reviews with.

            A `'provider:model'` string or `BaseChatModel`.

            When `None`, `DEEPAGENTS_CODE_AUTO_CLASSIFIER_MODEL` is consulted,
            then `[models].auto_classifier`, and the main `model` is reused when
            both are unset. A blank string is *not* the same as `None`: it means
            "inherit the main model" directly and, unlike `None`, does not
            consult the env var or `config.toml`. Only meaningful when
            `auto_mode_enabled` is `True`.
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
        middleware: Caller-supplied middleware spliced into the factory stack.
            **This is the factory's first capability beyond v0**;
            `create_cli_agent` has no equivalent.

            Pass a bare sequence to use the default `'before_verification'`
            phase, or a mapping keyed by `FactoryPhase` to place middleware
            explicitly:

            ```python
            create_factory_agent(..., middleware=[MyMiddleware()])
            create_factory_agent(..., middleware={"first": [MyMiddleware()]})
            ```

            `None` (default) composes exactly as v0 does — the seam is inert
            unless used.

            !!! warning "Position is an onion, not a timeline"

                Earlier phases are **outermost**, not "earlier in time". A
                middleware in `'first'` has its `before_*` hooks run first and
                its `after_*` hooks run **last**; one in `'last'` is the
                reverse. To have the final say on the way out, use `'first'`.

            Phases and what each guarantees:

            - `'first'`: ahead of every factory middleware — except those
                the SDK's name-based merge hoists into an SDK-default slot:
                the compaction middleware (named `SummarizationMiddleware`),
                always, and the factory's own `FilesystemMiddleware` when
                `fs_tools` is set.
            - `'before_verification'` (default): after everything that
                installs tools, context, memory, skills and approval policy
                (including the main agent's HITL middleware and server-owned
                hooks); ahead of the goal-criteria/rubric tail.
            - `'last'`: after every factory middleware, immediately ahead of
                the SDK's own tail (harness profile, prompt caching). That
                SDK tail is not addressable from here. The approval gate is
                no longer in it — HITL sits inside the factory stack, before
                this phase.

            This parameter reaches the **main agent only**. To cover work
            delegated through `task`, use `subagent_middleware`. The
            goal-criteria agent remains unreachable: upstream's
            `_create_goal_criteria_agent` takes no middleware argument.

            Every injected middleware needs a `.name` that is unique in the
            composed stack and is not one the deepagents SDK may use for its
            own middleware; both are rejected with an explicit error rather
            than silently mis-composed. The SDK merges by name, so a collision
            would otherwise *replace* its middleware in place — including the
            human approval gate — instead of landing at the requested phase.

            Reserved names include langchain stock middleware the SDK installs
            for some models (`TodoListMiddleware`, `ToolRetryMiddleware`,
            `HumanInTheLoopMiddleware`, ...). If you need one of those,
            override `.name` on your instance. The guard is deliberately
            model-independent, so it also refuses names that could only
            collide on a model you are not using.
        subagent_middleware: Caller-supplied middleware spliced into **every**
            subagent stack the factory composes, including the synthesized
            `general-purpose` subagent.

            Same two input forms as `middleware`, but a smaller phase
            vocabulary — subagent stacks have no verification tail:

            ```python
            create_factory_agent(..., subagent_middleware=[MyMiddleware()])
            create_factory_agent(..., subagent_middleware={"first": [Outer()]})
            ```

            - `'first'`: ahead of every factory subagent middleware, including
                the approval gate.
            - `'last'` (default): after every factory subagent middleware,
                still ahead of the SDK's own subagent tail. The default is the
                later position on purpose — a caller wanting to sit outside the
                approval gate must ask for `'first'` explicitly.

            `None` (default) composes subagents exactly as v0 does.

            !!! warning "One instance, every subagent"

                The middleware objects you pass are spliced into each
                subagent's stack **by reference**, so a single instance is
                shared across all of them. Middleware holding per-agent state
                will see that state shared; construct stateless middleware, or
                key any state by something available at runtime.

            Async subagents are unaffected — they run on their own remote
            backend and never receive the local stack.

            Names are guarded the same way as `middleware`: subagent specs go
            through the SDK's name-based merge too (`_apply_custom_middleware`
            against a subagent base), so a reserved name would be silently
            replaced rather than land at the requested phase. The guarded set
            is the main stack's, which is a superset of the subagent base — a
            few names are refused that only the main stack could own.
        rubric_grader_middleware: Caller-supplied middleware spliced into the
            rubric grader's own stack.

            Same two input forms, same two phases as `subagent_middleware`:

            - `'first'`: **outside** the factory's budget middlewares.
            - `'last'` (default): inside them.

            The default matters here. Earlier is outermost, so budget
            middlewares at the front still wrap — and therefore still count —
            whatever the injection does. `'first'` places middleware outside
            `_ContextToolCallBudgetMiddleware`, `_WebSearchBudgetMiddleware`,
            and `_CriteriaContextBudgetMiddleware`, which bound how much the
            grader can spend inspecting the repository and the web. That is a
            legitimate position, but it has to be asked for.

            `None` (default) composes the grader exactly as v0 does.

            Unlike `middleware` and `subagent_middleware`, **no SDK reserved
            names apply**: the grader is built by langchain's `create_agent`
            with this list passed through verbatim, so there is no SDK base
            stack for a name to silently replace. Only uniqueness within the
            composed grader stack is required, and it is checked at factory
            construction — not lazily when the grader is first built — so a bad
            injection fails at boot rather than mid-evaluation.


    Returns:
        2-tuple of `(agent_graph, backend)`

            - `agent_graph`: Configured LangGraph Pregel instance ready
                for execution
            - `composite_backend`: `CompositeBackend` for file operations

    Raises:
        ValueError: When `enable_interpreter=True` is paired with a
            non-`None` `sandbox`, when `settings.interpreter_ptc` contains
            unknown tool names, when `interpreter_ptc="all"` is used
            without `auto_approve` or `interpreter_ptc_acknowledge_unsafe`,
            when any of `middleware`, `subagent_middleware`, or
            `rubric_grader_middleware` names an unknown phase or carries an
            entry that is not usable as middleware, when injected main-agent or
            subagent middleware claims a name the deepagents SDK reserves, or
            when any composed stack — main, subagent, or rubric grader — ends
            up with a duplicate `.name`.
    """
    tools = tools or []
    mcp_tools = tuple(mcp_tools or ())
    # SEAM (resolve): up front, so a bad phase key or entry fails before any
    # setup work. The three splice sites below depend on this binding.
    injected_middleware = _normalize_injected_middleware(middleware)
    # SEAM (resolve, subagents): same reasoning, and the reserved-name check
    # runs here too rather than at the per-subagent splice — a collision is a
    # property of the caller's input, not of any one subagent, so failing here
    # keeps it ahead of directory creation and subagent discovery.
    injected_subagent_middleware = _normalize_subagent_middleware(subagent_middleware)
    _validate_subagent_reserved_names(injected_subagent_middleware)
    _injected_subagent_names = {
        item.name
        for items in injected_subagent_middleware.values()
        for item in items
    }
    # SEAM (resolve, grader): no reserved-name pass — the grader stack has no
    # SDK base to be replaced against (see `_validate_grader_stack`). Shape
    # errors still fail here, ahead of any setup work.
    injected_grader_middleware = _normalize_grader_middleware(rubric_grader_middleware)
    _injected_grader_names = {
        item.name for items in injected_grader_middleware.values() for item in items
    }
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
        from lc_factory.upstream import CostTrackingMiddleware

        middleware: list[AgentMiddleware[Any, Any]] = [
            # SEAM (subagent phase "first"): ahead of every factory subagent
            # middleware, including the approval gate.
            *injected_subagent_middleware["first"],
        ]
        if resolved_interrupt_on is not None:
            middleware.append(AsyncApprovalHITLMiddleware(resolved_interrupt_on))
        if not has_explicit_model:
            middleware.append(ConfigurableModelMiddleware(persist_model_state=False))
        # Checkpoint nested spend before HITL can pause the subgraph, then hand
        # the completed delta back through owner-scoped state for the parent
        # graph to add to its durable total.
        middleware.append(CostTrackingMiddleware(nested=True))
        # Interactive turns may legitimately be tool-free, so terminal-stall
        # recovery is installed only on headless stacks. The middleware itself
        # activates only for the measured Fireworks GLM-5.2 endpoint.
        if not interactive:
            middleware.append(_GlmTerminalStallRecovery())
        if restrictive_shell_allow_list is not None:
            middleware.append(ShellAllowListMiddleware(restrictive_shell_allow_list))
        # Server-owned hooks must wrap subagent tools too; otherwise Pre/Post
        # ToolUse only fire on the parent graph. Disable Stop so finishing a
        # subagent does not emit the main-agent Stop event (SubagentStop still
        # fires from the parent wrap around `task`).
        from lc_factory.upstream import ServerHooksMiddleware

        hooks_cwd = Path(effective_cwd) if effective_cwd is not None else Path.cwd()
        middleware.append(
            ServerHooksMiddleware(
                cwd=hooks_cwd,
                emit_stop=False,
                mcp_tools=mcp_tools,
            )
        )
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
        # SEAM (subagent phase "last"): after every factory subagent
        # middleware, still ahead of the SDK's own subagent tail.
        middleware.extend(injected_subagent_middleware["last"])
        # Validated per stack, not once: subagent stacks differ by
        # configuration, so a name unique against one can collide on another.
        _validate_subagent_stack(middleware, _injected_subagent_names)
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
        # Named `subagent_stack`, not `subagent_middleware` as upstream has it:
        # that name is now the factory's own parameter, and rebinding it here
        # would shadow it for the rest of the body. Deliberate divergence,
        # recorded in UPGRADING.md.
        subagent_stack = _subagent_cli_middleware(
            has_explicit_model=has_explicit_model,
        )
        if subagent_stack:
            subagent["middleware"] = subagent_stack
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
        # SEAM (phase "first"): ahead of every factory middleware.
        *injected_middleware["first"],
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
    # request. `CostTrackingMiddleware` is the sole writer of the cumulative
    # thread cost, pricing every model request recorded for this thread —
    # including subagent, offload, and Auto classifier calls that never reach
    # `after_model` — so thread-keyed draining makes that
    # coverage independent of position within the model loop. `after_agent`
    # hooks run in reverse list order, though, so this must stay *before*
    # `ReliableRubricMiddleware`: otherwise the grading agent's spend lands in
    # the next turn's checkpoint, or is lost on a session's final turn.
    # The CLI reads these channels back from `state_values` on thread resume.
    # Goal tools: exposes the read-only `get_goal`/`get_rubric` tools and the
    # constrained `update_goal` tool, and maintains goal-state notices.
    from lc_factory.upstream import CostTrackingMiddleware
    from lc_factory.upstream import GoalToolsMiddleware
    from lc_factory.upstream import ResumeStateMiddleware

    agent_middleware.extend(
        [ResumeStateMiddleware(), CostTrackingMiddleware(), GoalToolsMiddleware()]
    )

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

    interrupt_on: dict[str, bool | InterruptOnConfig] = {}
    auto_mode_config: tuple[Path, list[str]] | None = None
    if resolved_interrupt_on is not None and auto_mode_enabled:
        configured_allow_list = shell_allow_list or settings.shell_allow_list
        narrow_allow_list = (
            configured_allow_list if isinstance(configured_allow_list, list) else []
        )
        trusted_root = (
            project_context.project_root
            if project_context is not None and project_context.project_root is not None
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
        from lc_factory.upstream import resolve_auto_classifier_model
        from lc_factory.upstream import resolve_auto_classifier_timeout

        trusted_root, narrow_allow_list = auto_mode_config
        # An explicit argument wins; otherwise the env var / `config.toml`
        # preference is read here, where agent construction already runs off the
        # blockbuster-guarded server loop (see `server_graph._make_graph`).
        classifier_model = (
            auto_classifier_model
            if auto_classifier_model is not None
            else resolve_auto_classifier_model()
        )
        agent_middleware.append(
            AutoModeHITLMiddleware(
                resolved_interrupt_on,
                worktree_root=trusted_root,
                shell_allow_list=narrow_allow_list,
                classifier_model=classifier_model,
                classifier_timeout_seconds=resolve_auto_classifier_timeout(),
                trusted_ask_user_tool=trusted_ask_user_tool,
                trusted_compaction_tool=compaction_middleware.tools[0],
            )
        )
    elif resolved_interrupt_on is not None:
        # `AutoModeHITLMiddleware` reports the same `HumanInTheLoopMiddleware`
        # name, so installing both would trip `create_agent`'s duplicate-name
        # assertion. Auto mode's specialized replacement wins when active.
        agent_middleware.append(AsyncApprovalHITLMiddleware(resolved_interrupt_on))

    # Server-owned Hooks v2 lifecycle events (Pre/Post tool, Stop, subagent).
    # Gated at runtime by `hooks_server_events` on the per-run context so idle
    # sessions without configured handlers pay no interrupt round-trip. Appended
    # after the HITL middleware so `PreToolUse` resolves before approval routing.
    from lc_factory.upstream import ServerHooksMiddleware

    hooks_cwd = Path(effective_cwd) if effective_cwd is not None else Path.cwd()
    agent_middleware.append(ServerHooksMiddleware(cwd=hooks_cwd, mcp_tools=mcp_tools))

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

    # SEAM (phase "before_verification"): every middleware that installs
    # tools, context, memory, skills or approval policy is now composed; the
    # goal-criteria -> rubric verification tail follows (the compaction
    # middleware appended between them is hoisted into the SDK core's
    # summarization slot by name).
    agent_middleware.extend(injected_middleware["before_verification"])

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
        # SEAM (grader phase "first"): outside the budget middlewares. Opt-in
        # only — the default phase is "last", which keeps budgets wrapping.
        *injected_grader_middleware["first"],
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
    # SEAM (grader phase "last"): after every factory grader middleware.
    grader_middleware.extend(injected_grader_middleware["last"])
    # Validated here, at composition time. The grader AGENT is built lazily and
    # cached on first grading, so a lazy-only check would surface a bad
    # injection mid-evaluation instead of at boot.
    _validate_grader_stack(grader_middleware, _injected_grader_names)

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

    # SEAM (phase "last"): after every factory middleware, immediately ahead of
    # the SDK's own tail (harness profile, prompt caching). HITL is no longer
    # in that tail — since 0.1.52 it sits inside the factory stack, above.
    agent_middleware.extend(injected_middleware["last"])
    _validate_injected_middleware(agent_middleware, injected_middleware)

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
