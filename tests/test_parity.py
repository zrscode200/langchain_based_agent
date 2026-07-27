"""Wave 1.3 parity suite: the port composes the SAME agent as v0.

Mechanism: monkeypatch ``create_deep_agent`` in both assembly modules to
capture the composition kwargs instead of compiling a graph, call both
assemblies with identical inputs, and compare normalized fingerprints:
middleware class sequence, interrupt gating, subagent wiring, backend
composition, prompts, and schema. This is the tripwire that gates every
upstream pin bump (see UPGRADING.md).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

CONFIG_MATRIX = {
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
}


def _fake_model():
    # Test layer may reach upstream's test fakes directly (boundary rule
    # governs src/lc_factory runtime code).
    from deepagents_code._fake_models import _ToolBindingFakeModel

    return _ToolBindingFakeModel(messages=iter([]))


def _subagent_fingerprint(subagents: Any) -> list[tuple[Any, ...]]:
    out = []
    for spec in subagents or []:
        if isinstance(spec, dict):
            out.append(
                (
                    spec.get("name"),
                    [type(m).__name__ for m in spec.get("middleware", [])],
                    spec.get("interrupt_on"),
                    bool(spec.get("model")),
                )
            )
        else:
            out.append((type(spec).__name__,))
    return out


def _interrupt_fingerprint(interrupt_on: Any) -> Any:
    if not interrupt_on:
        return interrupt_on
    return {
        name: sorted(cfg.get("allowed_decisions", []))
        if isinstance(cfg, dict)
        else cfg
        for name, cfg in sorted(interrupt_on.items())
    }


def _fingerprint(kwargs: dict[str, Any]) -> dict[str, Any]:
    backend = kwargs.get("backend")
    return {
        "kwarg_names": sorted(kwargs.keys()),
        "middleware": [type(m).__name__ for m in kwargs.get("middleware", [])],
        "interrupt_on": _interrupt_fingerprint(kwargs.get("interrupt_on")),
        "subagents": _subagent_fingerprint(kwargs.get("subagents")),
        "context_schema": getattr(kwargs.get("context_schema"), "__name__", None),
        "system_prompt": kwargs.get("system_prompt"),
        "agent_name": kwargs.get("name"),
        "backend_type": type(backend).__name__,
        "backend_routes": sorted(getattr(backend, "routes", {}) or {}),
        "model_type": type(kwargs.get("model")).__name__,
        "tools": list(kwargs.get("tools") or []),
        "checkpointer": kwargs.get("checkpointer"),
    }


def _capture_composition(module: Any, attr: str, call: Any, /, **call_kwargs: Any):
    """Run one assembly with create_deep_agent intercepted; return its kwargs."""
    captured: dict[str, Any] = {}
    original = getattr(module, attr)

    def interceptor(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return MagicMock()

    try:
        setattr(module, attr, interceptor)
        call(**call_kwargs)
    finally:
        setattr(module, attr, original)
    return captured


@pytest.mark.parametrize("case", sorted(CONFIG_MATRIX))
def test_composition_parity(case, tmp_path):
    import deepagents_code.agent as v0_module

    import lc_factory.assembly as ours_module

    overrides = CONFIG_MATRIX[case]
    common = {
        "assistant_id": "lc-factory-parity",
        "cwd": tmp_path,
        **overrides,
    }

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
