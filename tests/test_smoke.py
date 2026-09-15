"""Wave 1.1 smoke: pinned upstream installs and the import boundary resolves."""

import json
from importlib.metadata import distribution, version

import pytest

PINNED_DEEPAGENTS_CODE = "0.1.69"
PINNED_DEEPAGENTS = "0.7.14"
PINNED_SOURCE_COMMIT = "1d3232c0852c47af09119edea10eeec887e4f0da"


def test_upstream_pins_installed():
    assert version("deepagents-code") == PINNED_DEEPAGENTS_CODE
    assert version("deepagents") == PINNED_DEEPAGENTS
    assert version("langchain-quickjs") == "0.3.7"


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


@pytest.mark.parametrize(("package", "subdirectory"), [
    ("deepagents-code", "libs/code"), ("deepagents", "libs/deepagents"),
])
def test_upstream_source_provenance(package, subdirectory):
    # Unreleased changes retain the release version strings, so version-only
    # assertions would silently accept the older behavior.
    source = json.loads(distribution(package).read_text("direct_url.json"))
    assert source["url"] == "https://github.com/langchain-ai/deepagents.git"
    assert source["subdirectory"] == subdirectory
    assert source["vcs_info"]["commit_id"] == PINNED_SOURCE_COMMIT
