"""Wave 1.2: the ported assembly matches v0's surface and constructs a graph."""

from __future__ import annotations

import inspect

from lc_factory import upstream
from lc_factory.assembly import create_factory_agent


def test_signature_parity_with_v0():
    """The port keeps v0's exact public signature (params, defaults, return)."""
    ours = inspect.signature(create_factory_agent)
    v0 = inspect.signature(upstream.create_cli_agent)
    assert ours == v0


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
