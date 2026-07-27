"""Wave 1.3 parity suite: the port composes the SAME agent as v0.

Mechanism: monkeypatch ``create_deep_agent`` in both assembly modules to
capture the composition kwargs instead of compiling a graph, call both
assemblies with identical inputs, and compare normalized fingerprints.

The fingerprint goes down to middleware *state*, not just class identity:
the port's real job is re-applying upstream's constructor arguments
line-by-line, so a fingerprint comparing only class names would pass while
the shell allow-list widened or HITL gating went dead. This suite is the
tripwire that gates every upstream pin bump (see UPGRADING.md).
"""

from __future__ import annotations

from pathlib import PurePath
from typing import Any
from unittest.mock import MagicMock

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
}

# Verification-pipeline and MCP cases need tool/model objects, so they are
# built per-test rather than declared as literals.
DYNAMIC_CASES = ("server_realistic", "headless_mcp", "sandbox")

# `_sandbox_id` is freshly generated per backend instance; everything else in
# the composed middleware state is deterministic.
_UNSTABLE_STATE_KEYS = frozenset({"_sandbox_id"})


def _fake_model():
    # Test layer may reach upstream's test fakes directly (the boundary rule
    # governs src/lc_factory runtime code).
    from deepagents_code._fake_models import _ToolBindingFakeModel

    return _ToolBindingFakeModel(messages=iter([]))


def _normalize(value: Any, depth: int = 0) -> Any:
    """Recursively reduce composed objects to a comparable structure."""
    if depth > 6:
        return "<max-depth>"
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, PurePath):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [_normalize(item, depth + 1) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(str(_normalize(item, depth + 1)) for item in value)
    if isinstance(value, dict):
        # Keys are not always mutually comparable (e.g. TypedDict classes
        # used as schema keys), so order by their string form.
        return {
            str(key): _normalize(item, depth + 1)
            for key, item in sorted(value.items(), key=lambda kv: str(kv[0]))
        }
    # Tools compare by name; their bound callables differ by identity.
    name = getattr(value, "name", None)
    if isinstance(name, str) and hasattr(value, "description"):
        return {"tool": name}
    if callable(value) and not hasattr(value, "__dict__"):
        return {"callable": getattr(value, "__qualname__", type(value).__name__)}
    state = getattr(value, "__dict__", None)
    if state is not None:
        return {
            type(value).__name__: {
                key: _normalize(item, depth + 1)
                for key, item in sorted(state.items())
                if key not in _UNSTABLE_STATE_KEYS
            }
        }
    if callable(value):
        return {"callable": getattr(value, "__qualname__", type(value).__name__)}
    return {"repr": type(value).__name__}


def _middleware_fingerprint(middleware: Any) -> list[Any]:
    """Full state of each composed middleware, in composition order."""
    return [_normalize(item) for item in middleware or []]


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
        # `.with_config({...recursion_limit...})` is applied to the compiled
        # graph, so it never reaches `kwargs` — compare it separately.
        "with_config": _normalize(captured["with_config"]),
    }


def _capture_composition(module: Any, attr: str, call: Any, /, **call_kwargs: Any):
    """Run one assembly with create_deep_agent intercepted; return what it composed."""
    captured: dict[str, Any] = {}
    original = getattr(module, attr)
    graph = MagicMock()

    def interceptor(**kwargs: Any) -> Any:
        captured["kwargs"] = kwargs
        return graph

    try:
        setattr(module, attr, interceptor)
        call(**call_kwargs)
    finally:
        setattr(module, attr, original)

    assert "kwargs" in captured, (
        f"{module.__name__}.{attr} was never called — the interception point "
        "moved (e.g. became a function-local import), so this comparison "
        "would be vacuous."
    )
    with_config_calls = graph.with_config.call_args
    captured["with_config"] = with_config_calls[0][0] if with_config_calls else None
    return captured


def _run_both(case_kwargs: dict[str, Any], tmp_path):
    import deepagents_code.agent as v0_module

    import lc_factory.assembly as ours_module

    common = {"assistant_id": "lc-factory-parity", "cwd": tmp_path, **case_kwargs}
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
    return ours, v0


@pytest.mark.parametrize("case", sorted(CONFIG_MATRIX))
def test_composition_parity(case, tmp_path):
    ours, v0 = _run_both(CONFIG_MATRIX[case], tmp_path)
    assert _fingerprint(ours) == _fingerprint(v0)


def test_composition_parity_server_realistic(tmp_path):
    """The exact shape `lc_factory.server_graph` boots with.

    `server_graph` always passes goal-criteria and rubric-grader context
    tools (upstream's `_criteria_context_tools` returns a list, never
    `None`), which activates `GoalCriteriaMiddleware`, the nested criteria
    agent, and grader HITL — none of which the static matrix reaches.
    """
    from deepagents_code.tools import fetch_url

    context_tools = [fetch_url]
    ours, v0 = _run_both(
        {
            "goal_criteria_tools": context_tools,
            "rubric_grader_tools": context_tools,
        },
        tmp_path,
    )
    assert _fingerprint(ours) == _fingerprint(v0)


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
    from deepagents_code._fake_models import _ToolBindingFakeModel  # noqa: F401

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

    assert _fingerprint(drifted) != _fingerprint(v0), (
        "parity suite is blind to middleware constructor drift"
    )
