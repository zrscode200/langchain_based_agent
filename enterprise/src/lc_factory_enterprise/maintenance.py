"""Disable automatic analytics and maintenance for the enterprise TUI.

This is a launch policy, not a network sandbox. Explicit model, tool and MCP
requests are unaffected. No user or managed configuration is rewritten.
"""
from contextlib import ExitStack, contextmanager
from functools import wraps
import os
from types import MappingProxyType


DISABLED_MAINTENANCE_ENV = MappingProxyType({
    "LANGGRAPH_CLI_NO_ANALYTICS": "1",
    "LANGGRAPH_NO_VERSION_CHECK": "1",
    "DEEPAGENTS_CODE_NO_UPDATE_CHECK": "1",
    "DEEPAGENTS_CODE_AUTO_UPDATE": "0",
    "DEEPAGENTS_CODE_PRICES_AUTO_UPDATE": "0",
})


def _restore_environment(key, previous):
    if previous is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = previous


def _disabled():
    return False


def _skip_price_refresh():
    """Keep local cost estimation without starting the catalogue downloader."""


def install_server_maintenance_policy():
    """Disable price refresh in the dedicated enterprise graph/offload server.

    Analytics and version checks occur before graph import; the TUI's server
    environment must disable those before the child interpreter starts.
    """
    from deepagents_code import cost_tracking

    cost_tracking._start_price_updater = _skip_price_refresh


@contextmanager
def disabled_automatic_maintenance():
    """Apply before CLI import; restore all launch-local adaptations on exit.

    Upstream managed update settings outrank environment flags. Its update
    gates therefore also need to be disabled for this client invocation.
    Reapply the flags after server environment sanitization/restart overrides.
    """
    with ExitStack() as stack:
        for key, value in DISABLED_MAINTENANCE_ENV.items():
            stack.callback(_restore_environment, key, os.environ.get(key))
            os.environ[key] = value

        from deepagents_code import cost_tracking, update_check
        from deepagents_code.client.launch import server

        original_server_environment = server._server_env_with_overrides

        @wraps(original_server_environment)
        def server_environment(*args, **kwargs):
            environment = original_server_environment(*args, **kwargs)
            environment.update(DISABLED_MAINTENANCE_ENV)
            return environment

        replacements = (
            (update_check, "is_update_check_enabled", _disabled),
            (update_check, "is_auto_update_enabled", _disabled),
            (cost_tracking, "_start_price_updater", _skip_price_refresh),
            (server, "_server_env_with_overrides", server_environment),
        )
        for module, name, replacement in replacements:
            stack.callback(setattr, module, name, getattr(module, name))
            setattr(module, name, replacement)
        yield
