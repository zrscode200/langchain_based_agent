"""Wave 1.2: the ported assembly matches v0's surface and constructs a graph."""

from __future__ import annotations

import inspect

from lc_factory import upstream
from lc_factory.assembly import create_factory_agent


def test_signature_parity_with_v0():
    """Every v0 parameter survives in the port, unchanged.

    A subset check, not equality: later groups add factory-only parameters
    (the middleware injection seam first). Equality would fail on the first
    such delta, and the only available "fix" would be deleting the test —
    losing the guarantee that actually matters, which is that no v0
    parameter silently changes kind, default, or annotation.
    """
    ours = inspect.signature(create_factory_agent).parameters
    v0 = inspect.signature(upstream.create_cli_agent).parameters

    missing = [name for name in v0 if name not in ours]
    assert not missing, f"v0 parameters dropped by the port: {missing}"

    for name, v0_param in v0.items():
        assert ours[name].kind == v0_param.kind, f"{name}: parameter kind changed"
        assert ours[name].default == v0_param.default, f"{name}: default changed"
        assert ours[name].annotation == v0_param.annotation, f"{name}: annotation changed"

    assert set(ours) >= set(v0)


def test_factory_only_parameters_are_keyword_only_and_default_off():
    """Every parameter the factory adds beyond v0 must be inert by default.

    This is what keeps the no-injection composition byte-identical to v0 — the
    property `test_parity.py` relies on. A factory-only parameter that was
    positional, or defaulted to anything but "do nothing", would break parity
    for every caller rather than only for callers who opted in.
    """
    ours = inspect.signature(create_factory_agent).parameters
    v0 = inspect.signature(upstream.create_cli_agent).parameters

    # An explicit registry, not a count: adding a factory-only parameter must be
    # a deliberate edit here, so an accidental one fails rather than widening
    # the factory's surface silently.
    added = sorted(set(ours) - set(v0))
    assert added == [
        "enable_settled_dispatch",
        "interpreter_subagents",
        "middleware",
        "rubric_grader_middleware",
        "skill_policy",
        "subagent_definitions",
        "subagent_middleware",
        "subagent_policy",
        "subagents",
        "verification_model",
    ], f"undeclared factory-only parameters: {added}"
    for name in added:
        assert ours[name].kind is inspect.Parameter.KEYWORD_ONLY, (
            f"{name}: factory-only parameters must be keyword-only"
        )
        assert ours[name].default is (False if name == "enable_settled_dispatch" else None), (
            f"{name}: factory-only parameters must default to inert"
        )


def test_construction_smoke(tmp_path):
    """The ported assembly compiles a graph + composite backend end to end."""
    # Test layer may reach upstream's test fakes directly; the import-boundary
    # rule governs src/lc_factory runtime code, and the fakes are deliberately
    # not part of the factory's runtime surface.
    from deepagents_code._fake_models import _ToolBindingFakeModel

    model = _ToolBindingFakeModel(messages=iter([]))
    graph, backend = create_factory_agent(
        model=model,
        assistant_id="lc-factory-smoke",
        cwd=tmp_path,
    )
    assert graph is not None
    assert backend is not None
    # The compiled graph exposes the LangGraph Pregel surface the server needs.
    assert hasattr(graph, "astream")
    assert hasattr(graph, "get_graph")
