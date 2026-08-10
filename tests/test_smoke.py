"""Wave 1.1 smoke: pinned upstream installs and the import boundary resolves."""

from importlib.metadata import version

PINNED_DEEPAGENTS_CODE = "0.1.54"
PINNED_DEEPAGENTS = "0.7.5"


def test_upstream_pins_installed():
    assert version("deepagents-code") == PINNED_DEEPAGENTS_CODE
    assert version("deepagents") == PINNED_DEEPAGENTS


def test_lc_factory_imports():
    import lc_factory

    assert lc_factory.__version__


def test_import_boundary_resolves():
    from lc_factory import upstream

    assert callable(upstream.create_cli_agent)
    assert callable(upstream.create_deep_agent)
    assert callable(upstream.generate_langgraph_json)
    assert callable(upstream.start_server_and_get_agent)
    for symbol in ("ServerConfig", "ServerProcess", "RemoteAgent"):
        assert callable(getattr(upstream, symbol))
