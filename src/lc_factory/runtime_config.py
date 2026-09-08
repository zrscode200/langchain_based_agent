"""Trusted client preferences, frozen before launching the server process.

Direct embeddings retain explicit RuntimeOptions. Bundled clients default to
background tools and forward their selection through the existing environment
bridge so scaffold, saver and server all use the same snapshot.
"""
from contextlib import contextmanager
import os

from lc_factory.runtime import RuntimeOptions
from lc_factory.upstream import get_config_sources, server_manager_module

CAPABILITIES_ENV = "LC_FACTORY_CAPABILITIES"
CAPABILITIES = ("reload", "background", "history")


def client_runtime_options(environ=None):
    """Resolve shell override, then trusted preferences, then client defaults."""
    environment = os.environ if environ is None else environ
    if environment.get(CAPABILITIES_ENV, "").strip():
        return RuntimeOptions.from_environment(environment)
    sources = get_config_sources()
    if not sources.user.status.usable or not sources.managed.status.usable:
        raise ValueError("Cannot read runtime capabilities from invalid configuration")
    data, _ = sources.merged()
    section = data.get("lc_factory", {})
    if not isinstance(section, dict):
        raise ValueError("[lc_factory] must be a table")
    values = section.get("capabilities", ["background"])
    if not isinstance(values, list) or any(
        not isinstance(value, str) or value not in CAPABILITIES for value in values
    ):
        raise ValueError("[lc_factory].capabilities must be a list of reload, background, history")
    return RuntimeOptions(**{value: True for value in values})


@contextmanager
def client_runtime_environment():
    """Resolve at first server scaffold, leaving diagnostic commands available.

    The installed scaffold may be OG's or the enterprise adaptation's. Resolve
    once for the whole client lifetime, including later server rescaffolding.
    """
    original_scaffold = server_manager_module._scaffold_workspace
    options = None
    previous = None

    def prepare():
        nonlocal options, previous
        if options is None:
            selected = client_runtime_options()
            previous = os.environ.get(CAPABILITIES_ENV)
            os.environ[CAPABILITIES_ENV] = ",".join(
                name for name in CAPABILITIES if getattr(selected, name)
            ) or "none"
            options = selected
        return options

    def scaffold(*args, **kwargs):
        prepare()
        return original_scaffold(*args, **kwargs)

    server_manager_module._scaffold_workspace = scaffold
    try:
        yield prepare
    finally:
        server_manager_module._scaffold_workspace = original_scaffold
        if options is not None:
            if previous is None:
                os.environ.pop(CAPABILITIES_ENV, None)
            else:
                os.environ[CAPABILITIES_ENV] = previous
