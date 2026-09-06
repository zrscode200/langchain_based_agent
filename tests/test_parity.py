"""Wave 1.3 parity suite: the port composes the SAME agent as v0.

Mechanism: monkeypatch ``create_deep_agent`` in both assembly modules to
capture the composition kwargs and supply a minimal compiled graph, call both
assemblies with identical inputs, and compare normalized fingerprints of the
composition and the effective returned graph configuration.

The fingerprint goes down to middleware *state*, not just class identity:
the port's real job is re-applying upstream's constructor arguments
line-by-line, so a fingerprint comparing only class names would pass while
the shell allow-list widened or HITL gating went dead. This suite is the
tripwire that gates every upstream pin bump (see UPGRADING.md).
"""

from __future__ import annotations

import dataclasses
import functools
import hashlib
import json
import inspect
from pathlib import Path, PurePath
from typing import Any

import pytest

CONFIG_MATRIX: dict[str, dict[str, Any]] = {
    "default_local": {},
    "auto_approve": {"auto_approve": True},
    "auto_mode": {"auto_mode_enabled": True},
    "shell_allow_list": {
        "interrupt_shell_only": True,
        "shell_allow_list": ["echo", "ls"],
    },
    "fs_tools_allowlist": {"fs_tools": ["read_file", "write_file"]},
    "no_memory": {"enable_memory": False},
    "no_skills": {"enable_skills": False},
    "headless": {"interactive": False},
    # Below: configurations the real server actually boots with, and the
    # branches that carry the most assembly logic.
    "no_shell": {"enable_shell": False},
    "memory_read_only": {"memory_auto_save": False},
    "rubric_tuned": {"rubric_max_iterations": 5, "recursion_limit": 42},
    "explicit_prompt": {"system_prompt": "parity fixture prompt"},
    "no_ask_user": {"enable_ask_user": False},
    "summary_model": {"summarization_model": "openai:summary-fixture"},
}

# Random backend identities are outside the constructor contract. Environment
# snapshots now ARE port-controlled: compare their digests without printing
# credentials in a failing assertion.
_UNSTABLE_STATE_KEYS = frozenset({"_sandbox_id"})
_ENVIRONMENT_STATE_KEYS = frozenset({"_env", "_environ"})


def _environment_fingerprint(value):
    if value is None:
        return None
    payload = json.dumps(dict(value), sort_keys=True).encode()
    return {"environment_sha256": hashlib.sha256(payload).hexdigest()}


# Nested compiled graphs are summarized rather than walked: full recursion
# produced megabytes of failure output, but they cannot be fully opaque —
# `GoalCriteriaMiddleware`'s entire state is two compiled graphs, so an
# opaque marker would put every argument the port passes to
# `_create_goal_criteria_agent` (tools, repository root, fs_tools,
# auto_mode) outside the tripwire.
_SUMMARIZED_TYPES = frozenset({"CompiledStateGraph", "Pregel"})

_MAX_DEPTH = 9
"""Recursion bound for `_normalize`, to keep failure output readable.

State nested deeper than this is invisible to the tripwire. Nothing is
truncated at the current pin, and
`test_composition_parity_criteria_agent_with_repository` asserts that stays
true — see UPGRADING.md's "Known blind spots".
"""


def _fake_model():
    # Test layer may reach upstream's test fakes directly (the boundary rule
    # governs src/lc_factory runtime code).
    from deepagents_code._fake_models import _ToolBindingFakeModel

    return _ToolBindingFakeModel(messages=iter([]))


def _normalize(value: Any, depth: int = 0, memo: dict[int, str] | None = None) -> Any:
    """Recursively reduce composed objects to a comparable structure.

    `memo` gives structural sharing: the same backend instance is referenced
    by nearly every middleware, so without it a single failure prints the
    same object ~100 times. The first occurrence is expanded in full (so no
    drift signal is lost) and later ones become a positional reference —
    which still compares correctly, since identical composition yields
    identical first-occurrence ordering on both sides.
    """
    if memo is None:
        memo = {}
    if depth > _MAX_DEPTH:
        return "<max-depth>"
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, PurePath):
        return str(value)
    if inspect.isclass(value):
        # Schema classes are constructor arguments, not instances. Preserve
        # their identity and declared fields without recursively walking
        # Pydantic's generated validators and typing's self-referential caches.
        return {
            "class": f"{value.__module__}.{value.__qualname__}",
            "annotations": {
                key: repr(annotation)
                for key, annotation in sorted(
                    getattr(value, "__annotations__", {}).items()
                )
            },
        }
    if type(value).__name__ in _SUMMARIZED_TYPES:
        return _graph_summary(value)
    # Routines must be checked BEFORE `__dict__`: functions, lambdas, bound
    # methods and partials all carry a (usually empty) `__dict__`, so without
    # this they would all collapse to `{"function": {}}` and compare equal.
    if inspect.isroutine(value) or isinstance(value, functools.partial):
        target = getattr(value, "func", value)
        return {"callable": getattr(target, "__qualname__", type(target).__name__)}
    if isinstance(value, (list, tuple)):
        return [_normalize(item, depth + 1, memo) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(str(_normalize(item, depth + 1, memo)) for item in value)
    if isinstance(value, dict):
        # Keys are not always mutually comparable (e.g. TypedDict classes
        # used as schema keys), so order by their string form.
        return {
            str(key): _normalize(item, depth + 1, memo)
            for key, item in sorted(value.items(), key=lambda kv: str(kv[0]))
        }
    # Tools compare by name + description; their bound callables differ by
    # identity. Gated on the real type so a middleware (which also has a
    # string `.name`) can never collapse into this branch and lose its state.
    if _is_tool(value):
        return {"tool": value.name, "description": getattr(value, "description", None)}
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        # Slotted dataclasses (e.g. MCPServerInfo) have no `__dict__`.
        return {
            type(value).__name__: {
                field.name: _normalize(getattr(value, field.name, None), depth + 1, memo)
                for field in dataclasses.fields(value)
            }
        }
    state = getattr(value, "__dict__", None)
    if state is not None:
        if id(value) in memo:
            return {"ref": memo[id(value)]}
        memo[id(value)] = f"{type(value).__name__}#{len(memo)}"
        return {
            type(value).__name__: {
                key: (_environment_fingerprint(item) if key in _ENVIRONMENT_STATE_KEYS
                      else _normalize(item, depth + 1, memo))
                for key, item in sorted(state.items())
                if key not in _UNSTABLE_STATE_KEYS
            }
        }
    if callable(value):
        return {"callable": getattr(value, "__qualname__", type(value).__name__)}
    return {"repr": type(value).__name__}


def _graph_summary(graph: Any) -> Any:
    """Structural summary of a nested compiled graph.

    Keeps the part the port controls — which nodes exist and which tools the
    nested agent was given — without walking the whole upstream-built graph.
    """
    nodes = getattr(graph, "nodes", {}) or {}
    # Union across nodes, not assignment: only one tool-bearing node exists at
    # the current pin, but assigning would silently drop every earlier node's
    # tools the moment upstream splits them across nodes.
    tools: set[str] = set()
    for node in nodes.values():
        by_name = getattr(getattr(node, "bound", None), "_tools_by_name", None)
        if by_name:
            tools.update(by_name)
    return {"graph": {"nodes": sorted(nodes), "tools": sorted(tools)}}


@functools.lru_cache(maxsize=1)
def _tool_type():
    from langchain_core.tools import BaseTool

    return BaseTool


def _is_tool(value: Any) -> bool:
    return isinstance(value, _tool_type())


def _middleware_fingerprint(middleware: Any, memo: dict[int, str] | None = None) -> list[Any]:
    """Full state of each composed middleware, in composition order.

    One memo spans the whole stack so the shared backend is expanded once.
    """
    if memo is None:
        memo = {}
    return [_normalize(item, 0, memo) for item in middleware or []]


def _interrupt_fingerprint(interrupt_on: Any) -> Any:
    """Interrupt config including the `when` predicate and description.

    `when` is a fresh closure per call, so it compares by qualname; the
    predicate decides whether an interrupt fires at all and must not be
    dropped from the comparison.
    """
    if not interrupt_on:
        return interrupt_on
    out = {}
    for name, cfg in sorted(interrupt_on.items()):
        if not isinstance(cfg, dict):
            out[name] = cfg
            continue
        # Fail loudly if upstream adds a key, rather than silently ignoring
        # it — catching upstream change is this suite's whole purpose.
        unknown = set(cfg) - {"allowed_decisions", "description", "when"}
        assert not unknown, f"unfingerprinted InterruptOnConfig keys for {name}: {unknown}"
        out[name] = {
            "allowed_decisions": sorted(cfg.get("allowed_decisions", [])),
            "description": _normalize(cfg.get("description")),
            "when": getattr(cfg.get("when"), "__qualname__", None),
        }
    return out


def _subagent_fingerprint(subagents: Any) -> list[Any]:
    out = []
    for spec in subagents or []:
        if isinstance(spec, dict):
            out.append(
                {
                    "name": spec.get("name"),
                    "mode": spec.get("mode", "fresh"),
                    "description": spec.get("description"),
                    "system_prompt": spec.get("system_prompt"),
                    "model": _normalize(spec.get("model")),
                    "middleware": _middleware_fingerprint(spec.get("middleware")),
                    "interrupt_on": _interrupt_fingerprint(spec.get("interrupt_on")),
                }
            )
        else:
            out.append(_normalize(spec))
    return out


def _fingerprint(captured: dict[str, Any]) -> dict[str, Any]:
    kwargs = captured["kwargs"]
    backend = kwargs.get("backend")
    return {
        "kwarg_names": sorted(kwargs.keys()),
        "middleware": _middleware_fingerprint(kwargs.get("middleware")),
        "interrupt_on": _interrupt_fingerprint(kwargs.get("interrupt_on")),
        "subagents": _subagent_fingerprint(kwargs.get("subagents")),
        "context_schema": getattr(kwargs.get("context_schema"), "__name__", None),
        "system_prompt": kwargs.get("system_prompt"),
        "agent_name": kwargs.get("name"),
        "backend": _normalize(backend),
        "default_backend": type(getattr(backend, "default", None)).__name__,
        "backend_routes": sorted(getattr(backend, "routes", {}) or {}),
        "model_type": type(kwargs.get("model")).__name__,
        "tools": [_normalize(tool) for tool in kwargs.get("tools") or []],
        "checkpointer": _normalize(kwargs.get("checkpointer")),
        # Configuration is applied after SDK construction and may use either
        # `.copy` or `.with_config`. Inspect the effective returned graph,
        # not calls on the original graph (which can miss a replacement).
        "graph_config": _normalize(captured["graph_config"]),
    }


def _capture_composition(module: Any, attr: str, call: Any, /, **call_kwargs: Any):
    """Run one assembly with create_deep_agent intercepted; return what it composed."""
    from langgraph.graph import END, START, StateGraph

    captured: dict[str, Any] = {}
    original = getattr(module, attr)
    # Use real LangGraph config semantics without compiling the full agent.
    # An inherited limit makes a dropped override observable; unrelated
    # metadata also exposes accidental loss of existing SDK configuration.
    builder = StateGraph(dict)
    builder.add_edge(START, END)
    graph = builder.compile().copy(
        {"config": {"recursion_limit": 9999, "metadata": {"parity_fixture": True}}}
    )

    def interceptor(**kwargs: Any) -> Any:
        captured["kwargs"] = kwargs
        return graph

    try:
        setattr(module, attr, interceptor)
        returned_graph, _backend = call(**call_kwargs)
    finally:
        setattr(module, attr, original)

    assert "kwargs" in captured, (
        f"{module.__name__}.{attr} was never called — the interception point "
        "moved (e.g. became a function-local import), so this comparison "
        "would be vacuous."
    )
    captured["graph_config"] = returned_graph.config
    return captured


class _FixedArtifactsStorage:
    """Deterministic stand-in for upstream's `_ArtifactsStorage`.

    `_artifacts_root()` falls back to a fresh `mkdtemp()` when the predictable
    artifacts directory is unusable (wrong owner on a shared runner, hostile
    umask). The two composes would then land on different directories and the
    whole suite would report what looks exactly like port drift. Pinning one
    storage across both sides keeps a real mismatch the only way to fail.
    """

    def __init__(self, root: Path, *, large_results: bool = True) -> None:
        self.root = str(root)
        self.large_results_dir = root / "large_tool_results" if large_results else None


def _run_both(case_kwargs: dict[str, Any], tmp_path, *, large_results: bool = True):
    import deepagents_code.agent as v0_module

    import lc_factory.assembly as ours_module

    storage = _FixedArtifactsStorage(tmp_path / "artifacts", large_results=large_results)
    fixed_root = lambda: storage  # noqa: E731
    originals = [
        (v0_module, v0_module._artifacts_root),
        (ours_module, ours_module._artifacts_root),
    ]
    common = {"assistant_id": "lc-factory-parity", "cwd": tmp_path, **case_kwargs}
    try:
        for module, _ in originals:
            module._artifacts_root = fixed_root
        v0 = _capture_composition(
            v0_module,
            "create_deep_agent",
            v0_module.create_cli_agent,
            model=_fake_model(),
            **common,
        )
        ours = _capture_composition(
            ours_module,
            "create_deep_agent",
            ours_module.create_factory_agent,
            model=_fake_model(),
            **common,
        )
    finally:
        for module, original in originals:
            module._artifacts_root = original
    return ours, v0


@pytest.mark.parametrize("case", sorted(CONFIG_MATRIX))
def test_composition_parity(case, tmp_path):
    ours, v0 = _run_both(CONFIG_MATRIX[case], tmp_path)
    assert _fingerprint(ours) == _fingerprint(v0)


def test_fingerprint_detects_recursion_limit_drift(tmp_path, monkeypatch):
    """An accepted recursion-limit argument must reach the returned graph.

    The old capture inspected only `.with_config` calls on the original
    graph, so both fingerprints remained identical when a constructor used
    `.copy` and silently replaced the requested limit with 1.
    """
    import lc_factory.assembly as ours_module

    case = {"recursion_limit": 42}
    ours, v0 = _run_both(case, tmp_path)
    baseline = _fingerprint(v0)
    assert _fingerprint(ours) == baseline, "precondition: parity holds"
    assert baseline["graph_config"] == {
        "recursion_limit": 42,
        "metadata": {"parity_fixture": True},
    }

    original = ours_module.create_factory_agent

    def wrong_limit(**kwargs):
        # Simulate a factory that accepts the caller's limit but propagates
        # a different value into the compiled graph configuration.
        return original(**{**kwargs, "recursion_limit": 1})

    monkeypatch.setattr(ours_module, "create_factory_agent", wrong_limit)
    drifted, v0 = _run_both(case, tmp_path)
    actual = _fingerprint(drifted)
    expected = _fingerprint(v0)
    assert actual["graph_config"]["recursion_limit"] == 1
    assert expected["graph_config"]["recursion_limit"] == 42
    assert actual != expected, "parity suite is blind to recursion-limit drift"
    # The config must be the sole delta, so incidental nondeterminism cannot
    # make this negative control pass for the wrong reason.
    assert {k: v for k, v in actual.items() if k != "graph_config"} == {
        k: v for k, v in expected.items() if k != "graph_config"
    }


def test_composition_parity_server_realistic(tmp_path):
    """The server-boot shape: verification pipeline + MCP server metadata.

    `server_graph` always passes goal-criteria and rubric-grader context
    tools (upstream's `_criteria_context_tools` returns a list, never
    `None`), activating `GoalCriteriaMiddleware`, the nested criteria agent,
    and grader HITL. It also always passes `mcp_server_info`, which is the
    only input that makes `LocalContextMiddleware`'s constructor arguments
    observable in composed state — without it, three of its four kwargs fold
    into an empty string and dropping them is invisible.

    Not covered here (server also passes these): `async_subagents`,
    `project_context`, and the resolved `tools`/`mcp_tools` lists.
    """
    from deepagents_code.mcp_tools import MCPServerInfo
    from deepagents_code.tools import fetch_url

    context_tools = [fetch_url]
    ours, v0 = _run_both(
        {
            "goal_criteria_tools": context_tools,
            "rubric_grader_tools": context_tools,
            "mcp_server_info": [
                MCPServerInfo(name="fixture-fs", transport="stdio"),
            ],
        },
        tmp_path,
    )
    assert _fingerprint(ours) == _fingerprint(v0)


def test_composition_parity_with_project_context(tmp_path):
    """A resolved project context, which the real server always supplies.

    `server_graph` always passes `project_context`, and it drives six live
    branches — including `AutoModeHITLMiddleware`'s trusted root, i.e. the
    auto-approval boundary. `cwd` is a subdirectory of the git root so
    `project_root != user_cwd` and the distinction is observable.
    """
    import subprocess

    from lc_factory.upstream import ProjectContext

    repo = tmp_path / "repo"
    (repo / "pkg" / "deep").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    workdir = repo / "pkg" / "deep"

    context = ProjectContext.from_user_cwd(workdir)
    ours, v0 = _run_both({"project_context": context, "cwd": workdir}, tmp_path)
    fingerprint = _fingerprint(ours)
    assert fingerprint == _fingerprint(v0)
    # Guard the guard: the case is only meaningful while the project root is
    # actually distinct from the working directory in composed state.
    assert str(repo.resolve()) in str(fingerprint["middleware"]), (
        "project root is no longer observable in composed state — this case "
        "no longer covers project-context-dependent branches"
    )


def test_composition_parity_criteria_agent_with_repository(tmp_path):
    """Criteria tools AND a project context, so the nested agent gets a backend.

    Neither ingredient alone is enough: without `goal_criteria_tools` the
    middleware is not installed, and without `project_context` the criteria
    agent gets `repository_backend=None` and therefore no filesystem tools —
    so the arguments the port passes to `_create_goal_criteria_agent` have
    nothing observable to differ in. Together they put the nested agent's
    tool surface inside the tripwire.
    """
    import subprocess

    from deepagents_code.tools import fetch_url

    from lc_factory.upstream import ProjectContext

    repo = tmp_path / "crit_repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)

    ours, v0 = _run_both(
        {
            "goal_criteria_tools": [fetch_url],
            "rubric_grader_tools": [fetch_url],
            "project_context": ProjectContext.from_user_cwd(repo),
            "cwd": repo,
        },
        tmp_path,
    )
    fingerprint = _fingerprint(ours)
    assert fingerprint == _fingerprint(v0)
    # Guard the guard: the nested agent must actually carry filesystem tools,
    # otherwise this case silently stops covering what it claims to.
    assert "read_file" in str(fingerprint["middleware"]), (
        "criteria agent has no repository tools — this case no longer covers "
        "the arguments passed to _create_goal_criteria_agent"
    )
    # `_normalize` truncates below `_MAX_DEPTH` to bound failure output.
    # Nothing is truncated at the current pin; assert that stays true on the
    # richest composition, so deepening upstream state fails here instead of
    # quietly leaving the tripwire.
    assert "<max-depth>" not in str(fingerprint), (
        "composed state now nests deeper than the normalizer walks — raise "
        "_MAX_DEPTH or record what is being truncated in UPGRADING.md"
    )


def test_composition_parity_with_tracing(tmp_path, monkeypatch):
    """Tracing configured, so `LocalContextMiddleware`'s tracing args matter.

    `get_langsmith_project_name()` returns a value only when a key and the
    tracing flag are both set; without them the tracing arguments fold into
    an empty string and dropping them would be invisible.
    """
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2-parity-fixture")
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_PROJECT", "parity-fixture-project")

    ours, v0 = _run_both({}, tmp_path)
    fingerprint = _fingerprint(ours)
    assert fingerprint == _fingerprint(v0)
    # Guard the guard: if upstream stops surfacing tracing in composed state,
    # this case silently stops covering what it claims to.
    assert "parity-fixture-project" in str(fingerprint["middleware"]), (
        "tracing context is no longer observable in composed state — this "
        "case no longer covers LocalContextMiddleware's tracing arguments"
    )


def test_composition_parity_auto_classifier_configured(tmp_path, monkeypatch):
    """Auto mode with the classifier configured away from its defaults.

    In the isolated suite environment `resolve_auto_classifier_model()`
    returns `None` and `resolve_auto_classifier_timeout()` returns exactly
    the constructor default, so the plain `auto_mode` case cannot see a port
    that drops the 0.1.52 classifier kwargs — the drop fingerprints
    identically. Configure both knobs away from their defaults so a dropped
    kwarg diverges loudly.
    """
    monkeypatch.setenv(
        "DEEPAGENTS_CODE_AUTO_CLASSIFIER_MODEL", "openai:parity-classifier-fixture"
    )
    monkeypatch.setenv("DEEPAGENTS_CODE_AUTO_CLASSIFIER_TIMEOUT", "7.5")

    ours, v0 = _run_both({"auto_mode_enabled": True}, tmp_path)
    fingerprint = _fingerprint(ours)
    assert fingerprint == _fingerprint(v0)
    # Guard the guard: both knobs must be observable in composed state, or
    # this case has silently stopped covering what it claims to.
    rendered = str(fingerprint["middleware"])
    assert "parity-classifier-fixture" in rendered, (
        "the classifier model spec is no longer observable in composed state "
        "— this case no longer covers classifier_model threading"
    )
    assert "7.5" in rendered, (
        "the classifier timeout is no longer observable in composed state — "
        "this case no longer covers classifier_timeout_seconds threading"
    )


def test_composition_parity_without_large_results_route(tmp_path):
    """The healthy-machine artifacts shape: no `large_tool_results` route.

    `_artifacts_root()` supplies a `large_results_dir` only on its fallback
    branch, so this covers the false side of the route guard in the assembly
    (the fixed storage used elsewhere always supplies one).
    """
    ours, v0 = _run_both({}, tmp_path, large_results=False)
    fingerprint = _fingerprint(ours)
    assert fingerprint == _fingerprint(v0)
    assert not any(
        "large_tool_results" in route for route in fingerprint["backend_routes"]
    )


def test_composition_parity_headless_mcp(tmp_path):
    """Headless + MCP tools activates the headless MCP guard branch."""
    from langchain_core.tools import tool

    @tool
    def mutating_mcp_tool(payload: str) -> str:
        """A tool with no read-only annotation (so it must be gated)."""
        return payload

    ours, v0 = _run_both(
        {
            "interactive": False,
            "mcp_tools": [mutating_mcp_tool],
            "tools": [mutating_mcp_tool],
        },
        tmp_path,
    )
    assert _fingerprint(ours) == _fingerprint(v0)


def test_composition_parity_sandbox(tmp_path):
    """Sandbox mode diverges backend routing and grader backend selection."""
    class StubSandbox:
        """Minimal stand-in for a SandboxBackendProtocol backend."""

        id = "stub-sandbox"

        def execute(self, *args: Any, **kwargs: Any) -> Any:
            raise NotImplementedError

    ours, v0 = _run_both(
        {"sandbox": StubSandbox(), "sandbox_type": "modal"},
        tmp_path,
    )
    assert _fingerprint(ours) == _fingerprint(v0)


def test_graph_summary_unions_tools_across_nodes():
    """A summarized graph keeps tools from every tool-bearing node.

    Only one such node exists at the current pin, so no live case would
    notice a regression to per-node assignment — which is exactly why this
    asserts the property directly on a stub.
    """

    class StubBound:
        def __init__(self, tools):
            self._tools_by_name = {name: object() for name in tools}

    class StubNode:
        def __init__(self, tools):
            self.bound = StubBound(tools)

    class StubGraph:
        nodes = {
            "tools": StubNode(["read_file", "glob"]),
            "extra_tools": StubNode(["fetch_url"]),
            "model": object(),
        }

    summary = _graph_summary(StubGraph())
    assert summary["graph"]["tools"] == ["fetch_url", "glob", "read_file"]
    assert summary["graph"]["nodes"] == ["extra_tools", "model", "tools"]


def test_parity_suite_is_not_vacuous(tmp_path):
    """A deliberately different config must change the fingerprint."""
    import lc_factory.assembly as ours_module

    base = _capture_composition(
        ours_module,
        "create_deep_agent",
        ours_module.create_factory_agent,
        model=_fake_model(),
        assistant_id="lc-factory-parity",
        cwd=tmp_path,
    )
    no_memory = _capture_composition(
        ours_module,
        "create_deep_agent",
        ours_module.create_factory_agent,
        model=_fake_model(),
        assistant_id="lc-factory-parity",
        cwd=tmp_path,
        enable_memory=False,
    )
    assert _fingerprint(base) != _fingerprint(no_memory)


def test_fingerprint_detects_constructor_argument_drift(tmp_path):
    """Negative control: same middleware classes, different arguments.

    This is the drift a pin bump actually produces — the port keeps the
    upstream middleware but misses a changed constructor argument. A
    class-name-only fingerprint passes it; this suite must not.
    """
    import lc_factory.assembly as ours_module
    from lc_factory.upstream import ShellAllowListMiddleware

    case = {"interrupt_shell_only": True, "shell_allow_list": ["echo", "ls"]}
    ours, v0 = _run_both(case, tmp_path)
    assert _fingerprint(ours) == _fingerprint(v0), "precondition: parity holds"

    original = ours_module.ShellAllowListMiddleware
    try:
        # Simulate a port that silently widened the allow-list.
        ours_module.ShellAllowListMiddleware = lambda _allow_list: (
            ShellAllowListMiddleware(["sudo", "curl", "rm"])
        )
        drifted = _capture_composition(
            ours_module,
            "create_deep_agent",
            ours_module.create_factory_agent,
            model=_fake_model(),
            assistant_id="lc-factory-parity",
            cwd=tmp_path,
            **case,
        )
    finally:
        ours_module.ShellAllowListMiddleware = original

    # Assert the SPECIFIC delta: inequality alone could be satisfied by any
    # nondeterminism, which would make this control pass for the wrong reason.
    def _allow_lists(captured):
        return [
            state
            for entry in _middleware_fingerprint(captured["kwargs"].get("middleware"))
            for name, state in (entry.items() if isinstance(entry, dict) else [])
            if name == "ShellAllowListMiddleware"
        ]

    assert _allow_lists(drifted) != _allow_lists(v0), (
        "parity suite is blind to middleware constructor drift"
    )


def test_fingerprint_detects_dropped_fork_mode(tmp_path, monkeypatch):
    """A port that omits the new mode must not compare equal to upstream."""
    monkeypatch.setenv("DEEPAGENTS_CODE_FORKED_SUBAGENTS", "true")
    ours, v0 = _run_both({}, tmp_path)
    expected = _fingerprint(v0)
    assert _fingerprint(ours) == expected
    general = next(
        spec for spec in ours["kwargs"]["subagents"]
        if spec["name"] == "general-purpose"
    )
    assert general.pop("mode") == "fork"
    actual = _fingerprint(ours)
    assert actual["subagents"] != expected["subagents"]
    assert {k for k in actual if actual[k] != expected[k]} == {"subagents"}


def test_fingerprint_detects_grader_schema_drift(tmp_path, monkeypatch):
    """Class-valued schema arguments remain visible without Pydantic internals."""
    from typing_extensions import TypedDict

    import lc_factory.upstream as boundary

    class WrongGraderState(TypedDict):
        wrong_field: str

    ours, v0 = _run_both({}, tmp_path)
    expected = _fingerprint(v0)
    assert _fingerprint(ours) == expected
    monkeypatch.setattr(boundary, "RubricGraderState", WrongGraderState)
    drifted, _ = _run_both({}, tmp_path)
    actual = _fingerprint(drifted)
    assert actual["middleware"] != expected["middleware"]
    assert "WrongGraderState" in str(actual["middleware"])
    assert {k for k in actual if actual[k] != expected[k]} == {"middleware"}


def test_fingerprint_detects_dropped_constructor_kwargs(tmp_path):
    """Negative control: a middleware silently losing constructor kwargs.

    `LocalContextMiddleware` folds three of its four arguments into derived
    state, so this drift is only observable when the composition actually
    supplies MCP server metadata — which is why the server-realistic case
    passes it.
    """
    from deepagents_code.mcp_tools import MCPServerInfo
    from deepagents_code.tools import fetch_url

    import lc_factory.assembly as ours_module
    from lc_factory.upstream import LocalContextMiddleware

    case = {
        "goal_criteria_tools": [fetch_url],
        "rubric_grader_tools": [fetch_url],
        "mcp_server_info": [MCPServerInfo(name="fixture-fs", transport="stdio")],
    }
    ours, v0 = _run_both(case, tmp_path)
    assert _fingerprint(ours) == _fingerprint(v0), "precondition: parity holds"

    original = ours_module.LocalContextMiddleware
    try:
        # Simulate a port that dropped the context kwargs on a bump.
        ours_module.LocalContextMiddleware = lambda **kwargs: LocalContextMiddleware(
            backend=kwargs["backend"]
        )
        drifted = _capture_composition(
            ours_module,
            "create_deep_agent",
            ours_module.create_factory_agent,
            model=_fake_model(),
            assistant_id="lc-factory-parity",
            cwd=tmp_path,
            **case,
        )
    finally:
        ours_module.LocalContextMiddleware = original

    assert _fingerprint(drifted) != _fingerprint(v0), (
        "parity suite is blind to dropped constructor kwargs"
    )


def test_composition_parity_with_workspace_snapshot(tmp_path, monkeypatch):
    from types import MappingProxyType
    from deepagents_code.config import Credentials, ModelResult
    from lc_factory import assembly

    environment = MappingProxyType({
        "PATH": "/usr/bin:/bin", "FACTORY_WORKSPACE_PROBE": "workspace-a",
        "OPENAI_API_KEY": "test-key-a", "OPENAI_BASE_URL": "https://a.invalid/v1",
    })
    kwargs = {
        "environ": environment,
        "credentials_snapshot": Credentials.snapshot_from_environment(
            start_path=tmp_path, environ=environment,
        ),
        "model_result": ModelResult(
            model=_fake_model(), model_name="workspace-model", provider="openai",
            context_limit=12345, unsupported_modalities=frozenset({"video"}),
        ),
    }
    ours, upstream = _run_both(kwargs, tmp_path)
    assert _fingerprint(ours) == _fingerprint(upstream)
    # Negative control: a port that drops the environment must fail parity,
    # even though both constructors still receive the same model object.
    original = assembly.ConfigurableModelMiddleware
    def drop_environment(*args, **options):
        options.pop("environ", None)
        return original(*args, **options)
    monkeypatch.setattr(assembly, "ConfigurableModelMiddleware", drop_environment)
    drifted, baseline = _run_both(kwargs, tmp_path)
    assert _fingerprint(drifted) != _fingerprint(baseline)


def test_environment_fingerprint_redacts_values():
    from types import SimpleNamespace, MappingProxyType
    left = _normalize(SimpleNamespace(_environ=MappingProxyType({"KEY": "secret-a"})))
    right = _normalize(SimpleNamespace(_environ=MappingProxyType({"KEY": "secret-b"})))
    assert left != right
    assert "secret-a" not in repr(left)
    assert "secret-b" not in repr(right)
