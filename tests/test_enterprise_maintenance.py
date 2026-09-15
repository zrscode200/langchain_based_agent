"""Enterprise launch policy against the installed upstream maintenance paths."""
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock

import pytest

from lc_factory_enterprise.maintenance import (
    DISABLED_MAINTENANCE_ENV,
    disabled_automatic_maintenance,
)


@pytest.fixture
def enabled_update_settings(tmp_path, monkeypatch):
    """Exercise real precedence with both user and managed updates enabled."""
    from deepagents_code import config, cost_tracking, update_check
    from deepagents_code.configuration import service
    from deepagents_code.configuration.providers import TomlFileProvider

    settings = "[update]\ncheck = true\nauto_update = true\nprices_auto_update = true\n"
    user = update_check.DEFAULT_CONFIG_PATH
    user.parent.mkdir(parents=True, exist_ok=True)
    user.write_text(settings)
    managed = tmp_path / "managed_config.toml"
    managed.write_text(settings)
    snapshot = TomlFileProvider("managed_config.toml", managed).load()
    monkeypatch.setattr(service, "get_managed_snapshot", lambda **kwargs: snapshot)
    monkeypatch.setattr(config, "_is_editable_install", lambda: False)
    service.invalidate_config_sources()
    # Even disabling environment values lose to these managed values upstream.
    for key, value in DISABLED_MAINTENANCE_ENV.items():
        monkeypatch.setenv(key, value)
    assert update_check.is_update_check_enabled()
    assert update_check.is_auto_update_enabled()
    assert cost_tracking._prices_auto_update_enabled()
    return user, managed, settings


def test_enterprise_launcher_blocks_cached_upgrade_and_price_download(
    enabled_update_settings, monkeypatch,
):
    from deepagents_code import cost_tracking, main, update_check
    from lc_factory_enterprise import tui, upstream_cli
    from rich.console import Console

    cached = Mock(return_value=(True, "99.0.0"))
    upgrade = Mock(side_effect=AssertionError("unexpected automatic upgrade"))
    refresh = Mock(side_effect=AssertionError("unexpected pricing refresh"))
    monkeypatch.setattr(update_check, "get_cached_update_available", cached)
    monkeypatch.setattr(update_check, "perform_upgrade", upgrade)
    monkeypatch.setattr(cost_tracking, "_build_price_updater", refresh)
    monkeypatch.setattr(cost_tracking, "_PRICE_UPDATER_ATTEMPTED", False)
    monkeypatch.setattr(sys, "argv", ["ddt-agent"])
    calls = []

    def launch():
        calls.append(True)
        assert not update_check.is_update_check_enabled()
        assert not update_check.is_auto_update_enabled()
        main._run_startup_auto_update(Console())
        cost = cost_tracking.estimate_cost(
            {"input_tokens": 1000, "output_tokens": 100}, "gpt-4o", "openai"
        )
        assert cost is not None and cost > 0
        assert not cost_tracking._PRICE_UPDATER_ATTEMPTED

    monkeypatch.setattr(upstream_cli, "cli_main", launch)
    tui.main()
    assert calls == [True]
    cached.assert_not_called()
    upgrade.assert_not_called()
    refresh.assert_not_called()
    # No global upstream change or saved-policy rewrite survives CLI exit.
    assert update_check.is_update_check_enabled()
    assert update_check.is_auto_update_enabled()
    user, managed, settings = enabled_update_settings
    assert user.read_text() == managed.read_text() == settings


@pytest.mark.parametrize("fail", [False, True])
def test_policy_restores_environment_and_hooks_on_exit(monkeypatch, fail):
    from deepagents_code import cost_tracking, update_check
    from deepagents_code.client.launch import server

    originals = [
        (update_check, "is_update_check_enabled"),
        (update_check, "is_auto_update_enabled"),
        (cost_tracking, "_start_price_updater"),
        (server, "_server_env_with_overrides"),
    ]
    originals = [(module, name, getattr(module, name)) for module, name in originals]
    for index, key in enumerate(DISABLED_MAINTENANCE_ENV):
        if index % 2:
            monkeypatch.setenv(key, "caller-value")
        else:
            monkeypatch.delenv(key, raising=False)
    before = {key: os.environ.get(key) for key in DISABLED_MAINTENANCE_ENV}
    try:
        with disabled_automatic_maintenance():
            assert all(os.environ[key] == value for key, value in DISABLED_MAINTENANCE_ENV.items())
            if fail:
                raise RuntimeError("CLI failed")
    except RuntimeError:
        assert fail
    assert {key: os.environ.get(key) for key in DISABLED_MAINTENANCE_ENV} == before
    assert all(getattr(module, name) is value for module, name, value in originals)


def test_policy_restores_environment_if_upstream_import_fails(monkeypatch):
    import builtins

    original_import = builtins.__import__
    before = {key: os.environ.get(key) for key in DISABLED_MAINTENANCE_ENV}

    def importing(name, *args, **kwargs):
        if name == "deepagents_code.client.launch":
            raise ImportError("missing launch dependency")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", importing)
    with pytest.raises(ImportError, match="missing launch dependency"):
        with disabled_automatic_maintenance():
            pytest.fail("should not launch")
    assert {key: os.environ.get(key) for key in DISABLED_MAINTENANCE_ENV} == before


@pytest.mark.parametrize("entry", ["server_graph", "offload_api"])
def test_child_server_disables_analytics_version_checks_and_pricing(
    tmp_path, monkeypatch, entry,
):
    from deepagents_code import config as upstream_config
    from deepagents_code.client.launch import server

    # Simulate dotenv cleanup removing the parent's flags, then restart overrides
    # trying to re-enable every feature. The final subprocess env must pin them.
    # The loader's record now lives in `config`; the launcher strips through it.
    monkeypatch.setattr(upstream_config, "_dotenv_loaded_values", dict(DISABLED_MAINTENANCE_ENV))
    enabled = {
        "LANGGRAPH_CLI_NO_ANALYTICS": "0",
        "LANGGRAPH_NO_VERSION_CHECK": "0",
        "DEEPAGENTS_CODE_NO_UPDATE_CHECK": "0",
        "DEEPAGENTS_CODE_AUTO_UPDATE": "1",
        "DEEPAGENTS_CODE_PRICES_AUTO_UPDATE": "1",
    }
    with disabled_automatic_maintenance():
        for persistent, scoped in [(enabled, {}), ({}, enabled), (enabled, enabled)]:
            child_env = server._server_env_with_overrides(
                {**persistent, "COMPANY_LLM_API_KEY": "fixture"}, scoped
            )
            assert all(child_env[key] == value for key, value in DISABLED_MAINTENANCE_ENV.items())
            assert child_env["COMPANY_LLM_API_KEY"] == "fixture"

    source = Path(__file__).resolve().parents[1] / "enterprise" / "src"
    code = '''
import importlib
import sys
from unittest.mock import Mock, patch
sys.path.insert(0, sys.argv[1])
from langgraph_cli.analytics import log_command
with patch("langgraph_cli.analytics.threading.Thread") as thread:
    @log_command
    def dev():
        return "started"
    assert dev() == "started"
    thread.assert_not_called()
from langgraph_api.cli import _check_newer_version
with patch("urllib.request.urlopen") as request:
    _check_newer_version("langgraph-api", "0.0.1")
    request.assert_not_called()
from deepagents_code import cost_tracking
# The server adapter must suppress refresh independently of config precedence.
cost_tracking._prices_auto_update_enabled = lambda: True
cost_tracking._PRICE_UPDATER_ATTEMPTED = False
refresh = Mock(side_effect=AssertionError("unexpected pricing refresh"))
cost_tracking._build_price_updater = refresh
importlib.import_module("lc_factory_enterprise." + sys.argv[2])
cost = cost_tracking.estimate_cost(
    {"input_tokens": 1000, "output_tokens": 100}, "gpt-4o", "openai"
)
assert cost is not None and cost > 0
assert not cost_tracking._PRICE_UPDATER_ATTEMPTED
refresh.assert_not_called()
'''
    result = subprocess.run(
        [sys.executable, "-c", code, str(source), entry], cwd=tmp_path,
        env=child_env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_policy_precedes_upstream_cli_import_and_help_still_works(tmp_path):
    source = Path(__file__).resolve().parents[1] / "enterprise" / "src"
    code = '''
import importlib.abc
import os
import sys
from lc_factory_enterprise.maintenance import DISABLED_MAINTENANCE_ENV
observed = []
class ImportCheck(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "lc_factory_enterprise.upstream_cli":
            assert all(os.environ[k] == v for k, v in DISABLED_MAINTENANCE_ENV.items())
            from deepagents_code.update_check import is_update_check_enabled
            assert not is_update_check_enabled()
            observed.append(fullname)
sys.meta_path.insert(0, ImportCheck())
sys.argv = ["ddt-agent", "--help"]
from lc_factory_enterprise.tui import main
try:
    main()
except SystemExit as exc:
    assert exc.code == 0
assert observed == ["lc_factory_enterprise.upstream_cli"]
'''
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": os.pathsep.join((str(source.parents[1] / "src"), str(source)))},
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout.lower()
