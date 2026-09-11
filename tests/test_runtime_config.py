"""Client defaults reach the server without trusting workspace configuration."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from lc_factory.runtime import RuntimeOptions
from lc_factory.runtime_config import client_runtime_environment, client_options

ENV = "LC_FACTORY_CAPABILITIES"
SETTLED_ENV = "LC_FACTORY_SETTLED_DISPATCH"
JS_ENV = "LC_FACTORY_INTERPRETER_SUBAGENTS"
CLIENT_KEYS = (ENV, SETTLED_ENV, JS_ENV)


def write_config(home, text):
    from deepagents_code.configuration.service import invalidate_config_sources
    path = home / ".deepagents" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    invalidate_config_sources()
    return path


def test_client_defaults_do_not_change_embedding_defaults():
    options = client_options({})
    assert options.runtime == RuntimeOptions(background=True, reload=True)
    assert options.settled_dispatch and options.interpreter_subagents
    assert RuntimeOptions() == RuntimeOptions.from_environment({})
    assert not RuntimeOptions().enabled


@pytest.mark.parametrize("values, expected", [
    ('[]', RuntimeOptions()),
    ('["history", "reload"]', RuntimeOptions(history=True, reload=True)),
    ('["background"]', RuntimeOptions(background=True)),
])
def test_trusted_profile_selects_complete_capability_list(isolated_environment, values, expected):
    write_config(isolated_environment, f"[lc_factory]\ncapabilities = {values}\n")
    assert client_options({}).runtime == expected
    assert client_options({ENV: " "}).runtime == expected


@pytest.mark.parametrize("override, expected", [
    ("background", RuntimeOptions(background=True)),
    ("none", RuntimeOptions()),
    (" reload, history ", RuntimeOptions(reload=True, history=True)),
])
def test_shell_override_wins_over_profile(isolated_environment, override, expected):
    write_config(isolated_environment, '[lc_factory]\ncapabilities = ["background", "history"]\n')
    assert client_options({ENV: override}).runtime == expected


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
    with pytest.raises(ValueError, match="capabilities|lc_factory|configuration"):
        client_options({})


@pytest.mark.parametrize("value", ["unknown", "none,background"])
def test_invalid_override_does_not_fall_back(value):
    with pytest.raises(ValueError, match=ENV):
        client_options({ENV: value})


def test_unreadable_profile_fails_but_explicit_override_still_resolves(isolated_environment):
    path = isolated_environment / ".deepagents" / "config.toml"
    path.mkdir(parents=True)
    with pytest.raises(ValueError, match="invalid configuration"):
        client_options({})
    with pytest.raises(ValueError, match="invalid configuration"):
        client_options({ENV: "none"})
    options = client_options({ENV: "none", SETTLED_ENV: "0", JS_ENV: "0"})
    assert options.runtime == RuntimeOptions()
    assert not options.settled_dispatch and not options.interpreter_subagents


@pytest.mark.parametrize("prior", [None, "", "background,history", "none"])
@pytest.mark.parametrize("boolean_prior", [None, "", "false", "true"])
def test_client_restores_environment_after_failure(monkeypatch, prior, boolean_prior):
    for key in (SETTLED_ENV, JS_ENV):
        if boolean_prior is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, boolean_prior)
    if prior is None:
        monkeypatch.delenv(ENV, raising=False)
    else:
        monkeypatch.setenv(ENV, prior)
    with pytest.raises(RuntimeError, match="client exit"):
        with client_runtime_environment() as prepare:
            options = prepare()
            assert RuntimeOptions.from_environment() == options.runtime
            assert os.environ[SETTLED_ENV] == ("1" if options.settled_dispatch else "0")
            assert os.environ[JS_ENV] == ("1" if options.interpreter_subagents else "0")
            raise RuntimeError("client exit")
    assert os.environ.get(ENV) == prior
    assert os.environ.get(SETTLED_ENV) == boolean_prior
    assert os.environ.get(JS_ENV) == boolean_prior


@pytest.mark.parametrize("entry", ["og", "enterprise"])
@pytest.mark.parametrize("enabled", [True, False])
def test_client_launch_freezes_preferences_for_scaffold_and_server(
    tmp_path, isolated_environment, monkeypatch, entry, enabled,
):
    from lc_factory.upstream import server_manager_module
    from deepagents_code.configuration.service import invalidate_config_sources
    if not enabled:
        write_config(isolated_environment, '[lc_factory]\ncapabilities = []\nsettled_dispatch = false\ninterpreter_subagents = false\n')
    for key in CLIENT_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(sys, "argv", ["lc-code" if entry == "og" else "ddt-agent"])
    # OG retains its existing process-wide scaffold rebind; restore it for peers.
    monkeypatch.setattr(server_manager_module, "_scaffold_workspace", server_manager_module._scaffold_workspace)
    reached = []

    def cli():
        assert all(key not in os.environ for key in CLIENT_KEYS)  # lazy startup
        folder = tmp_path / "server"
        folder.mkdir()
        server_manager_module._scaffold_workspace(folder)
        assert RuntimeOptions.from_environment() == RuntimeOptions(background=enabled, reload=enabled)
        assert os.environ[SETTLED_ENV] == os.environ[JS_ENV] == ("1" if enabled else "0")
        saver = (folder / "checkpointer.py").read_text()
        assert ("lc_factory.server_checkpointer" in saver) is enabled
        # Later file edits must not give the server a different startup choice.
        replacement = [] if enabled else ["background"]
        write_config(isolated_environment, f'[lc_factory]\ncapabilities = {json.dumps(replacement)}\nsettled_dispatch = {str(not enabled).lower()}\ninterpreter_subagents = {str(not enabled).lower()}\n')
        invalidate_config_sources()
        second = tmp_path / "rescaffold"
        second.mkdir()
        server_manager_module._scaffold_workspace(second)
        assert ("lc_factory.server_checkpointer" in (second / "checkpointer.py").read_text()) is enabled
        result = subprocess.run([sys.executable, "-c",
            "import json, os; from lc_factory.runtime import RuntimeOptions; print(json.dumps([RuntimeOptions.from_environment().background, RuntimeOptions.from_environment().reload, os.environ[\"LC_FACTORY_SETTLED_DISPATCH\"], os.environ[\"LC_FACTORY_INTERPRETER_SUBAGENTS\"]]))"],
            cwd=tmp_path, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == [enabled, enabled, "1" if enabled else "0", "1" if enabled else "0"]
        reached.append(True)

    if entry == "og":
        from lc_factory import tui
        monkeypatch.setattr(tui, "cli_main", cli)
    else:
        from lc_factory_enterprise import tui, upstream_cli
        monkeypatch.setattr(upstream_cli, "cli_main", cli)
    tui.main()
    assert reached and all(key not in os.environ for key in CLIENT_KEYS)


def test_project_config_and_dotenv_cannot_select_client_capabilities(tmp_path, isolated_environment):
    # A subprocess exercises the package-import dotenv reservation itself.
    project = tmp_path / "project"
    (project / ".deepagents").mkdir(parents=True)
    (project / ".deepagents" / "config.toml").write_text('[lc_factory]\ncapabilities = ["history"]\nsettled_dispatch = true\ninterpreter_subagents = true\n')
    (project / ".env").write_text(f"{ENV}=history\n{SETTLED_ENV}=1\n{JS_ENV}=1\n")
    write_config(isolated_environment, '[lc_factory]\ncapabilities = []\nsettled_dispatch = false\ninterpreter_subagents = false\n')
    environment = dict(os.environ)
    for key in CLIENT_KEYS:
        environment.pop(key, None)
    code = '''
import json
from pathlib import Path
from lc_factory.runtime_config import client_runtime_environment
from deepagents_code.config import credentials
credentials.reload_from_environment(start_path=Path.cwd())
with client_runtime_environment() as prepare:
    options = prepare()
    print(json.dumps([options.runtime.reload, options.runtime.background, options.runtime.history, options.settled_dispatch, options.interpreter_subagents]))
'''
    result = subprocess.run([sys.executable, "-c", code], cwd=project, env=environment,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [False, False, False, False, False]


@pytest.mark.parametrize("entry", ["lc_factory", "lc_factory_enterprise"])
@pytest.mark.parametrize("arguments", [["--version"], ["--help"], ["config", "path"],
                                       ["auth", "path"], ["doctor", "--help"]])
def test_diagnostic_commands_remain_available_with_invalid_preferences(
    isolated_environment, tmp_path, entry, arguments,
):
    write_config(isolated_environment, '[lc_factory]\ncapabilities = ["typo"]\n')
    environment = dict(os.environ)
    for key in CLIENT_KEYS:
        environment.pop(key, None)
    source = Path(__file__).resolve().parents[1]
    environment["PYTHONPATH"] = os.pathsep.join(str(p) for p in (source / "src", source / "enterprise" / "src"))
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
    assert all(key not in os.environ for key in CLIENT_KEYS)
    assert server_manager_module._scaffold_workspace is forbidden_scaffold


@pytest.mark.parametrize("setting,key", [
    ("settled_dispatch", SETTLED_ENV), ("interpreter_subagents", JS_ENV),
])
@pytest.mark.parametrize("saved", [True, False])
def test_boolean_preferences_and_independent_overrides(isolated_environment, setting, key, saved):
    write_config(isolated_environment, f'[lc_factory]\ncapabilities = ["history"]\n{setting} = {str(saved).lower()}\n')
    assert getattr(client_options({}), setting) is saved
    assert getattr(client_options({key: " "}), setting) is saved
    options = client_options({key: "0" if saved else "1"})
    assert getattr(options, setting) is not saved
    assert options.runtime == RuntimeOptions(history=True)
    # A runtime override must not replace an independent saved boolean.
    options = client_options({ENV: "none"})
    assert options.runtime == RuntimeOptions()
    assert getattr(options, setting) is saved


@pytest.mark.parametrize("setting,key", [
    ("settled_dispatch", SETTLED_ENV), ("interpreter_subagents", JS_ENV),
])
@pytest.mark.parametrize("value", ['"false"', '0', '[]'])
def test_invalid_boolean_preferences_fail_before_export(isolated_environment, setting, key, value):
    write_config(isolated_environment, f'[lc_factory]\n{setting} = {value}\n')
    previous = {name: os.environ.get(name) for name in CLIENT_KEYS}
    with client_runtime_environment() as prepare:
        with pytest.raises(ValueError, match=setting):
            prepare()
    assert {name: os.environ.get(name) for name in CLIENT_KEYS} == previous
    assert getattr(client_options({key: "false"}), setting) is False


@pytest.mark.parametrize("key", [SETTLED_ENV, JS_ENV])
@pytest.mark.parametrize("value", ["yes", " ON ", "True", "no", "OFF", "False", "typo"])
def test_boolean_environment_values(key, value):
    if value == "typo":
        with pytest.raises(ValueError, match=key):
            client_options({key: value})
    else:
        options = client_options({key: value})
        actual = options.settled_dispatch if key == SETTLED_ENV else options.interpreter_subagents
        assert actual is (value.strip().lower() in {"yes", "on", "true"})


def test_one_trusted_source_snapshot_for_all_settings(monkeypatch):
    from types import SimpleNamespace
    from lc_factory import runtime_config
    calls = []
    def get_sources():
        calls.append(True)
        def merged():
            return {"lc_factory": {"capabilities": ["reload"], "settled_dispatch": False,
                                   "interpreter_subagents": False}}, {}
        source = SimpleNamespace(status=SimpleNamespace(usable=True))
        return SimpleNamespace(user=source, managed=source, merged=merged)
    monkeypatch.setattr(runtime_config, "get_config_sources", get_sources)
    options = client_options({})
    assert calls == [True]
    assert options.runtime == RuntimeOptions(reload=True)
    assert not options.settled_dispatch and not options.interpreter_subagents


@pytest.mark.parametrize("enabled", [True, False])
async def test_client_selection_builds_expected_server_tools(isolated_environment, tmp_path, monkeypatch, enabled):
    from deepagents_code._fake_models import _ToolBindingFakeModel
    from deepagents_code.config import ModelResult
    from lc_factory import server_graph
    from lc_factory.upstream import ServerConfig

    if not enabled:
        write_config(isolated_environment, '[lc_factory]\ncapabilities = []\nsettled_dispatch = false\ninterpreter_subagents = false\n')
    monkeypatch.setattr(server_graph, "create_model", lambda *a, **k:
                        ModelResult(model=_ToolBindingFakeModel(), model_name="main", provider="fixture"))
    monkeypatch.setattr(server_graph, "get_server_project_context", lambda: None)
    config = ServerConfig(model="fixture:main", cwd=str(tmp_path), no_mcp=True,
                          enable_memory=False, enable_skills=False, enable_shell=False,
                          enable_interpreter=True, interactive=False)
    with client_runtime_environment() as prepare:
        prepare()
        try:
            runtime = await server_graph._make_graphs(config_override=config)
            tools = runtime.agent.nodes["tools"].bound.tools_by_name
            assert {"task", "js_eval"} <= tools.keys()
            for name in ("task_settled", "start_background_task", "list_background_tasks",
                         "cancel_background_task", "reload_mcp_configuration", "reload_subagent_configuration"):
                assert (name in tools) is enabled
            if enabled:
                owner = server_graph._factory_runtime_owners[id(runtime.agent)]
                assert owner.options == RuntimeOptions(background=True, reload=True)
                assert owner.archive is None
                assert owner.reload_tools is not None
        finally:
            await server_graph.close_factory_runtimes()


@pytest.mark.parametrize("layout", ["readme", "notes_folder", "invalid_definition"])
async def test_default_reload_preserves_tolerant_initial_subagent_loading(tmp_path, layout):
    from deepagents_code._fake_models import _ToolBindingFakeModel
    from lc_factory.runtime import FactoryRuntime
    from lc_factory.upstream import get_user_agents_dir

    assistant_id = "client-startup-compatibility"
    directory = get_user_agents_dir(assistant_id)
    directory.mkdir(parents=True, exist_ok=True)
    if layout == "readme":
        (directory / "README.md").write_text("Notes about these agents")
    elif layout == "notes_folder":
        (directory / "notes").mkdir()
        (directory / "notes" / "README.md").write_text("Notes about these agents")
    else:
        (directory / "incomplete").mkdir()
        (directory / "incomplete" / "AGENTS.md").write_text("Incomplete definition")
    specialist = directory / "specialist"
    specialist.mkdir()
    (specialist / "AGENTS.md").write_text("---\ndescription: Specialist\n---\nWork carefully")
    options = client_options({})
    async with await FactoryRuntime.create(
        agent_kwargs=dict(model=_ToolBindingFakeModel(), assistant_id=assistant_id,
                          cwd=tmp_path, enable_memory=False, enable_skills=False,
                          enable_shell=False, interactive=False,
                          enable_settled_dispatch=options.settled_dispatch),
        options=options.runtime, workspace_id="startup-compatibility",
    ) as runtime:
        initial = runtime.current
        assert "specialist" in initial.agent.nodes["tools"].bound.tools_by_name["task"].description
        runtime.request_reload()
        assert await runtime.select() is initial
        assert runtime.generation == 1
        assert runtime.last_reload_error == "ValueError"
