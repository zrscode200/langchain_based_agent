"""Trusted client preferences, frozen before launching the server process.

Direct embeddings retain explicit constructor and RuntimeOptions defaults.
Bundled clients resolve their own defaults here and forward them through the
existing environment bridge so scaffold, saver and server share one snapshot.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import os

from lc_factory.runtime import RuntimeOptions
from lc_factory.upstream import get_config_sources, server_manager_module

CAPABILITIES_ENV = "LC_FACTORY_CAPABILITIES"
SETTLED_ENV = "LC_FACTORY_SETTLED_DISPATCH"
INTERPRETER_SUBAGENTS_ENV = "LC_FACTORY_INTERPRETER_SUBAGENTS"
CAPABILITIES = ("reload", "background", "history")


@dataclass(frozen=True)
class ClientOptions:
    runtime: RuntimeOptions
    settled_dispatch: bool
    interpreter_subagents: bool


def client_options(environ=None):
    """Resolve each shell override, then trusted preferences, then defaults.

    Read the trusted sources once, only if a setting needs them. Explicit shell
    selections can recover from an invalid profile when all settings are given.
    """
    environment = os.environ if environ is None else environ
    section = None

    def preferences():
        nonlocal section
        if section is None:
            sources = get_config_sources()
            if not sources.user.status.usable or not sources.managed.status.usable:
                raise ValueError("Cannot read client features from invalid configuration")
            data, _ = sources.merged()
            section = data.get("lc_factory", {})
            if not isinstance(section, dict):
                raise ValueError("[lc_factory] must be a table")
        return section

    def boolean(name, key):
        override = environment.get(key, "").strip().lower()
        if override:
            if override not in {"1", "true", "yes", "on", "0", "false", "no", "off"}:
                raise ValueError(f"{key} must be a boolean (1/0, true/false, yes/no, on/off)")
            return override in {"1", "true", "yes", "on"}
        value = preferences().get(name, True)
        if not isinstance(value, bool):
            raise ValueError(f"[lc_factory].{name} must be a boolean")
        return value

    if environment.get(CAPABILITIES_ENV, "").strip():
        runtime = RuntimeOptions.from_environment(environment)
    else:
        values = preferences().get("capabilities", ["background", "reload"])
        if not isinstance(values, list) or any(
            not isinstance(value, str) or value not in CAPABILITIES for value in values
        ):
            raise ValueError("[lc_factory].capabilities must be a list of reload, background, history")
        runtime = RuntimeOptions(**{value: True for value in values})
    return ClientOptions(
        runtime=runtime,
        settled_dispatch=boolean("settled_dispatch", SETTLED_ENV),
        interpreter_subagents=boolean("interpreter_subagents", INTERPRETER_SUBAGENTS_ENV),
    )


@contextmanager
def client_runtime_environment():
    """Resolve at first server scaffold, leaving diagnostic commands available.

    The installed scaffold may be OG's or the enterprise adaptation's. Resolve
    once for the whole client lifetime, including later server rescaffolding.
    """
    original_scaffold = server_manager_module._scaffold_workspace
    options = None
    previous = {}

    def prepare():
        nonlocal options
        if options is None:
            selected = client_options()
            for key in (CAPABILITIES_ENV, SETTLED_ENV, INTERPRETER_SUBAGENTS_ENV):
                previous[key] = os.environ.get(key)
            os.environ[CAPABILITIES_ENV] = ",".join(
                name for name in CAPABILITIES if getattr(selected.runtime, name)
            ) or "none"
            os.environ[SETTLED_ENV] = "1" if selected.settled_dispatch else "0"
            os.environ[INTERPRETER_SUBAGENTS_ENV] = "1" if selected.interpreter_subagents else "0"
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
            if previous[CAPABILITIES_ENV] is None:
                os.environ.pop(CAPABILITIES_ENV, None)
            else:
                os.environ[CAPABILITIES_ENV] = previous[CAPABILITIES_ENV]
            if previous[SETTLED_ENV] is None:
                os.environ.pop(SETTLED_ENV, None)
            else:
                os.environ[SETTLED_ENV] = previous[SETTLED_ENV]
            if previous[INTERPRETER_SUBAGENTS_ENV] is None:
                os.environ.pop(INTERPRETER_SUBAGENTS_ENV, None)
            else:
                os.environ[INTERPRETER_SUBAGENTS_ENV] = previous[INTERPRETER_SUBAGENTS_ENV]
