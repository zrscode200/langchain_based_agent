"""Client defaults reach the server without trusting workspace configuration."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from lc_factory.runtime import RuntimeOptions
from lc_factory.runtime_config import client_runtime_environment, client_runtime_options

ENV = "LC_FACTORY_CAPABILITIES"


def write_config(home, text):
    from deepagents_code.configuration.service import invalidate_config_sources
    path = home / ".deepagents" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    invalidate_config_sources()
    return path


def test_client_defaults_do_not_change_embedding_defaults():
    assert client_runtime_options({}) == RuntimeOptions(background=True)
    assert RuntimeOptions() == RuntimeOptions.from_environment({})
    assert not RuntimeOptions().enabled


@pytest.mark.parametrize("values, expected", [
    ('[]', RuntimeOptions()),
    ('["history", "reload"]', RuntimeOptions(history=True, reload=True)),
    ('["background"]', RuntimeOptions(background=True)),
])
def test_trusted_profile_selects_complete_capability_list(isolated_environment, values, expected):
    write_config(isolated_environment, f"[lc_factory]\ncapabilities = {values}\n")
    assert client_runtime_options({}) == expected
    assert client_runtime_options({ENV: " "}) == expected


@pytest.mark.parametrize("override, expected", [
    ("background", RuntimeOptions(background=True)),
    ("none", RuntimeOptions()),
    (" reload, history ", RuntimeOptions(reload=True, history=True)),
])
def test_shell_override_wins_over_profile(isolated_environment, override, expected):
    write_config(isolated_environment, '[lc_factory]\ncapabilities = ["background", "history"]\n')
    assert client_runtime_options({ENV: override}) == expected


@pytest.mark.parametrize("text", [
    '[lc_factory]\ncapabilities = "background"',
    '[lc_factory]\ncapabilities = ["unknown"]',
    '[lc_factory]\ncapabilities = ["none"]',
    '[lc_factory]\ncapabilities = [true]',
    'lc_factory = false',
    '[lc_factory',
])
def test_invalid_preferences_are_not_silently_enabled(isolated_environment, text):
    write_config(isolated_environment, text)
    with pytest.raises(ValueError, match="capabilities|lc_factory"):
        client_runtime_options({})


@pytest.mark.parametrize("value", ["unknown", "none,background"])
def test_invalid_override_does_not_fall_back(value):
    with pytest.raises(ValueError, match=ENV):
        client_runtime_options({ENV: value})


def test_unreadable_profile_fails_but_explicit_override_still_resolves(isolated_environment):
    path = isolated_environment / ".deepagents" / "config.toml"
    path.mkdir(parents=True)
    with pytest.raises(ValueError, match="invalid configuration"):
        client_runtime_options({})
    assert client_runtime_options({ENV: "none"}) == RuntimeOptions()


@pytest.mark.parametrize("prior", [None, "", "background,history", "none"])
def test_client_restores_environment_after_failure(monkeypatch, prior):
    if prior is None:
        monkeypatch.delenv(ENV, raising=False)
    else:
        monkeypatch.setenv(ENV, prior)
    with pytest.raises(RuntimeError, match="client exit"):
        with client_runtime_environment() as prepare:
            options = prepare()
            assert RuntimeOptions.from_environment() == options
            raise RuntimeError("client exit")
    assert os.environ.get(ENV) == prior


@pytest.mark.parametrize("entry", ["og", "enterprise"])
@pytest.mark.parametrize("enabled", [True, False])
def test_client_launch_freezes_preferences_for_scaffold_and_server(
    tmp_path, isolated_environment, monkeypatch, entry, enabled,
):
    from lc_factory.upstream import server_manager_module
    from deepagents_code.configuration.service import invalidate_config_sources
    if not enabled:
        write_config(isolated_environment, '[lc_factory]\ncapabilities = []\n')
    monkeypatch.delenv(ENV, raising=False)
    monkeypatch.setattr(sys, "argv", ["lc-code" if entry == "og" else "ddt-agent"])
    # OG retains its existing process-wide scaffold rebind; restore it for peers.
    monkeypatch.setattr(server_manager_module, "_scaffold_workspace", server_manager_module._scaffold_workspace)
    reached = []

    def cli():
        assert ENV not in os.environ  # selection waits for agent startup
        folder = tmp_path / "server"
        folder.mkdir()
        server_manager_module._scaffold_workspace(folder)
        assert RuntimeOptions.from_environment().background is enabled
        saver = (folder / "checkpointer.py").read_text()
        assert ("lc_factory.server_checkpointer" in saver) is enabled
        # Later file edits must not give the server a different startup choice.
        replacement = [] if enabled else ["background"]
        write_config(isolated_environment, f'[lc_factory]\ncapabilities = {json.dumps(replacement)}\n')
        invalidate_config_sources()
        second = tmp_path / "rescaffold"
        second.mkdir()
        server_manager_module._scaffold_workspace(second)
        assert ("lc_factory.server_checkpointer" in (second / "checkpointer.py").read_text()) is enabled
        result = subprocess.run([sys.executable, "-c",
            "from lc_factory.runtime import RuntimeOptions; print(RuntimeOptions.from_environment().background)"],
            cwd=tmp_path, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == str(enabled)
        reached.append(True)

    if entry == "og":
        from lc_factory import tui
        monkeypatch.setattr(tui, "cli_main", cli)
    else:
        from lc_factory_enterprise import tui, upstream_cli
        monkeypatch.setattr(upstream_cli, "cli_main", cli)
    tui.main()
    assert reached and ENV not in os.environ


def test_project_config_and_dotenv_cannot_select_client_capabilities(tmp_path, isolated_environment):
    # A subprocess exercises the package-import dotenv reservation itself.
    project = tmp_path / "project"
    (project / ".deepagents").mkdir(parents=True)
    (project / ".deepagents" / "config.toml").write_text('[lc_factory]\ncapabilities = ["history"]\n')
    (project / ".env").write_text(f"{ENV}=history\n")
    write_config(isolated_environment, '[lc_factory]\ncapabilities = []\n')
    environment = dict(os.environ)
    environment.pop(ENV, None)
    code = '''
import json
from pathlib import Path
from lc_factory.runtime_config import client_runtime_environment
from deepagents_code.config import credentials
credentials.reload_from_environment(start_path=Path.cwd())
with client_runtime_environment() as prepare:
    options = prepare()
    print(json.dumps([options.reload, options.background, options.history]))
'''
    result = subprocess.run([sys.executable, "-c", code], cwd=project, env=environment,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [False, False, False]


@pytest.mark.parametrize("entry", ["lc_factory", "lc_factory_enterprise"])
@pytest.mark.parametrize("arguments", [["--version"], ["--help"], ["config", "path"],
                                       ["auth", "path"], ["doctor", "--help"]])
def test_diagnostic_commands_remain_available_with_invalid_preferences(
    isolated_environment, tmp_path, entry, arguments,
):
    write_config(isolated_environment, '[lc_factory]\ncapabilities = ["typo"]\n')
    environment = dict(os.environ)
    environment.pop(ENV, None)
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "enterprise" / "src")
    environment["DEEPAGENTS_CODE_NO_UPDATE_CHECK"] = "1"
    result = subprocess.run([sys.executable, "-c", f"from {entry}.tui import main; main()", *arguments],
        cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ValueError" not in result.stderr


def test_invalid_preferences_fail_before_server_scaffolding(isolated_environment, tmp_path, monkeypatch):
    from lc_factory.upstream import server_manager_module
    write_config(isolated_environment, '[lc_factory]\ncapabilities = ["typo"]\n')
    def forbidden_scaffold(*args, **kwargs):
        pytest.fail("Invalid preferences must fail before server setup")
    monkeypatch.setattr(server_manager_module, "_scaffold_workspace", forbidden_scaffold)
    monkeypatch.delenv(ENV, raising=False)
    with client_runtime_environment():
        with pytest.raises(ValueError, match="capabilities"):
            server_manager_module._scaffold_workspace(tmp_path)
    assert ENV not in os.environ
    assert server_manager_module._scaffold_workspace is forbidden_scaffold
