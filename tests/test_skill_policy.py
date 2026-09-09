"""Project selection excludes ambient personal skills across graph/client paths."""
from pathlib import Path
from types import SimpleNamespace

import pytest
from deepagents_code._fake_models import _ToolBindingFakeModel
from langchain_core.messages import AIMessage, HumanMessage

from lc_factory.assembly import create_factory_agent
from lc_factory.skill_policy import (
    resolve_skill_policy, project_skill_sources, discover_project_skills,
    ProjectSkillsMiddleware, client_skill_policy,
)
from lc_factory.upstream import FilesystemBackend


def skill(root, name):
    path = root / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\ndescription: Description for {name}\n---\nUse this skill.\n")
    return path


def policy(root, text='[skills]\nmode="project"\n'):
    path = root / ".deepagents" / "skills.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_default_and_explicit_empty(tmp_path):
    assert resolve_skill_policy(tmp_path).mode == "personal"
    assert project_skill_sources(tmp_path, resolve_skill_policy(tmp_path, {"sources": []})) == []


@pytest.mark.parametrize("text", ['[skills', '[skills]\nmode="typo"', '[skills]\nsources="bad"',
                                      '[skills]\ninclude_builtin="yes"', '[other]\nmode="project"'])
def test_invalid_policy_fails_closed(tmp_path, text):
    policy(tmp_path, text)
    with pytest.raises(ValueError):
        resolve_skill_policy(tmp_path)


def test_project_catalogue_excludes_personal_and_symlink_escape(tmp_path, isolated_environment):
    tmp_path = tmp_path / "project"
    own = skill(tmp_path / ".agents/skills", "own")
    personal = skill(isolated_environment / ".claude/skills", "personal")
    (own.parent.parent / "escaped").symlink_to(personal.parent)
    policy(tmp_path)
    selected = resolve_skill_policy(tmp_path)
    rows, roots = discover_project_skills(tmp_path, selected)
    assert [row["name"] for row in rows] == ["own"]
    assert all(p.is_relative_to(tmp_path) for p in roots)
    with pytest.raises(ValueError, match="relative"):
        project_skill_sources(tmp_path, resolve_skill_policy(tmp_path, {"sources": [str(personal.parent)]}))


async def test_resumed_graph_drops_old_catalogue(tmp_path):
    from langchain.agents.middleware import AgentMiddleware
    seen = []

    class CaptureSkills(AgentMiddleware):
        async def abefore_model(self, state, runtime):
            seen.extend(s["name"] for s in state.get("skills_metadata", []))

    skill(tmp_path / ".agents/skills", "own")
    policy(tmp_path)
    graph, _ = create_factory_agent(
        _ToolBindingFakeModel(messages=iter([AIMessage("done")])), "skills-test", cwd=tmp_path,
        enable_memory=False, enable_ask_user=False, enable_shell=False,
        middleware={"last": [CaptureSkills()]},
    )
    result = await graph.ainvoke({"messages": [HumanMessage("hello")],
                                 "skills_metadata": [{"name": "personal", "description": "old", "path": "/old"}]})
    assert seen == ["own"]


def test_client_menu_invocation_listing_and_restoration(tmp_path, monkeypatch):
    import deepagents_code.app as app
    import deepagents_code.agent as agent
    import deepagents_code.skills.invocation as invocation
    import deepagents_code.skills.load as loader
    from deepagents_code.config import credentials

    skill(tmp_path / ".agents/skills", "own")
    policy(tmp_path)
    monkeypatch.setattr(credentials, "project_root", tmp_path)
    original = app.DeepAgentsApp._discover_skills_and_roots
    with client_skill_policy():
        rows, roots = invocation.discover_skills_and_roots("x", path_base=tmp_path)
        ui_rows, ui_roots = app.DeepAgentsApp._discover_skills_and_roots(SimpleNamespace(_cwd=str(tmp_path)))
        assert rows == ui_rows == loader.list_skills()
        assert roots == ui_roots
        assert [s["name"] for s in rows] == ["own"]
        assert all("User" not in source[1] for source in agent.get_skill_sources("x"))
    assert app.DeepAgentsApp._discover_skills_and_roots is original


@pytest.mark.parametrize("value", [{"mode": "project"}, {"sources": []}])
def test_project_policy_without_root_is_rejected(value):
    with pytest.raises(ValueError, match="workspace root"):
        resolve_skill_policy(None, value)


def test_explicit_policy_overrides_file_and_builtins_are_opt_in(tmp_path, monkeypatch):
    from lc_factory import skill_policy as module

    policy(tmp_path, "malformed [")
    selected = resolve_skill_policy(tmp_path, {"sources": []})
    assert discover_project_skills(tmp_path, selected) == ([], [])
    builtin = tmp_path / "bundled"
    skill(builtin, "bundled")
    monkeypatch.setattr(module, "built_in_skills_dir", lambda: builtin)
    selected = resolve_skill_policy(tmp_path, {"sources": [], "include_builtin": True})
    rows, _ = discover_project_skills(tmp_path, selected)
    assert [(s["name"], s["source"]) for s in rows] == [("bundled", "built-in")]


def test_policy_and_source_symlinks_cannot_escape_project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / ".deepagents").symlink_to(outside)
    with pytest.raises(ValueError, match="policy escapes"):
        resolve_skill_policy(root)
    (root / "selected").symlink_to(outside)
    with pytest.raises(ValueError, match="source escapes"):
        project_skill_sources(root, resolve_skill_policy(root, {"sources": ["selected"]}))


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_discovery_never_opens_escaped_skill_contents(tmp_path, monkeypatch, asynchronous):
    import os

    root = tmp_path / "project"
    source = root / ".agents/skills"
    own = skill(source, "own")
    outside = skill(tmp_path / "personal", "secret")
    (source / "directory-link").symlink_to(outside.parent)
    linked_file = source / "secret" / "SKILL.md"
    linked_file.parent.mkdir()
    linked_file.symlink_to(outside)
    opened = []
    original = os.open

    def checked_open(path, *args, **kwargs):
        if isinstance(path, (str, bytes, Path)):
            resolved = Path(path).resolve()
            assert resolved != outside, "Discovery attempted to read personal skill contents"
            opened.append(resolved)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", checked_open)
    middleware = ProjectSkillsMiddleware(sources=[(str(source), "Project")])
    stale = {"skills_metadata": [{"name": "secret", "path": str(outside)}],
             "skills_load_errors": ["stale personal error"]}
    result = (await middleware.abefore_agent(stale, None, {}) if asynchronous
              else middleware.before_agent(stale, None, {}))
    assert [s["name"] for s in result["skills_metadata"]] == ["own"]
    assert result["skills_load_errors"] == []
    assert own in opened and outside not in opened


@pytest.mark.parametrize("managed", [False, True])
@pytest.mark.parametrize("mode,child_sources,expected", [
    ("isolated", None, ["own"]),
    ("isolated", [], []),
    ("isolated", ["child-skills"], ["child"]),
    ("fork", None, ["own"]),
])
async def test_child_catalogue_and_prompt_follow_policy(tmp_path, mode, child_sources, expected, managed):
    from copy import deepcopy
    from dataclasses import replace
    import json
    from langchain.agents.middleware import AgentMiddleware
    from lc_factory.runtime import FactoryRuntime, RuntimeOptions
    from lc_factory.upstream import Credentials

    seen = []
    prompts = []

    class CaptureChild(AgentMiddleware):
        async def awrap_model_call(self, request, handler):
            seen.append([s["name"] for s in request.state.get("skills_metadata", [])])
            return await handler(request)

    from langchain_core.callbacks import BaseCallbackHandler

    class CapturePrompts(BaseCallbackHandler):
        def on_chat_model_start(self, serialized, messages, **kwargs):
            prompts.append(messages[0][0].content)

    skill(tmp_path / ".agents/skills", "own")
    skill(tmp_path / "child-skills", "child")
    policy(tmp_path)
    spec = dict(name="child", description="Child", mode=mode)
    if managed:
        (tmp_path / ".deepagents/subagents.toml").write_text(
            '[subagents.child]\n' + (f'skills={json.dumps(child_sources)}\n' if child_sources is not None else ""))
    elif child_sources is not None:
        spec["skills"] = child_sources
    before = deepcopy(spec)
    model = _ToolBindingFakeModel(messages=iter([
        AIMessage("", tool_calls=[dict(name="task", args={"description": "Inspect skills",
                    "subagent_type": "child"}, id="delegate", type="tool_call")]),
        AIMessage("child done"), AIMessage("parent done"),
    ]))
    kwargs = dict(
        model=model, assistant_id="skills-child-test", cwd=tmp_path, enable_memory=False,
        enable_ask_user=False, enable_shell=False, auto_approve=True,
        subagents=[spec], subagent_middleware=[CaptureChild()],
    )
    config = {"callbacks": [CapturePrompts()], "configurable": {"thread_id": "skills-child"}}
    if managed:
        nested = tmp_path / "nested"
        nested.mkdir()
        kwargs.update(cwd=nested, credentials_snapshot=replace(
            Credentials.snapshot_from_environment(start_path=tmp_path), project_root=tmp_path))
        async with await FactoryRuntime.create(agent_kwargs=kwargs,
            options=RuntimeOptions(reload=True), workspace_id="skills-workspace") as runtime:
            result = await runtime.ainvoke({"messages": [HumanMessage("delegate")]}, config)
    else:
        graph, _ = create_factory_agent(**kwargs)
        result = await graph.ainvoke({"messages": [HumanMessage("delegate")]}, config)
    assert result["messages"][-1].content == "parent done"
    assert seen == [expected]
    assert spec == before
    for name in expected:
        assert f"Description for {name}" in str(prompts[1])
    for name in {"own", "child", "secret"} - set(expected):
        assert f"Description for {name}" not in str(prompts[1])


@pytest.mark.parametrize("nested", [False, True])
async def test_runtime_reload_changes_catalogue_and_invalid_policy_retains_graph(tmp_path, nested):
    from lc_factory.runtime import FactoryRuntime, RuntimeOptions
    from langchain.agents.middleware import AgentMiddleware
    from dataclasses import replace
    from lc_factory.upstream import Credentials

    seen = []

    class CaptureSkills(AgentMiddleware):
        async def abefore_model(self, state, runtime):
            seen.append([s["name"] for s in state.get("skills_metadata", [])])

    skill(tmp_path / ".agents/skills", "own")
    policy(tmp_path)
    cwd = tmp_path / "nested" if nested else tmp_path
    cwd.mkdir(exist_ok=True)
    snapshot = replace(Credentials.snapshot_from_environment(start_path=tmp_path), project_root=tmp_path)
    async with await FactoryRuntime.create(
        agent_kwargs=dict(model=_ToolBindingFakeModel(messages=iter([
            AIMessage("first"), AIMessage("second"),
        ])), assistant_id="skills-reload", cwd=cwd, credentials_snapshot=snapshot, enable_memory=False,
            enable_shell=False, enable_ask_user=False, middleware={"last": [CaptureSkills()]}),
        options=RuntimeOptions(reload=True), workspace_id="workspace",
    ) as runtime:
        config = {"configurable": {"thread_id": "test"}}
        await runtime.ainvoke({"messages": [HumanMessage("hello")]}, config)
        assert seen == [["own"]]
        policy(tmp_path, '[skills]\nmode="project"\nsources=[]\n')
        runtime.request_reload()
        await runtime.ainvoke({"messages": [HumanMessage("again")],
                              "skills_metadata": [{"name": "old", "path": "/old"}]}, config)
        assert seen == [["own"], []]
        current = runtime.current
        policy(tmp_path, "bad [")
        runtime.request_reload()
        assert await runtime.select() is current
        assert runtime.last_reload_error == "ValueError"


def test_nested_client_root_empty_sources_and_restore_after_failure(tmp_path, monkeypatch):
    from lc_factory.upstream import import_skill_policy_modules, ProjectContext

    agent, app, loader, invocation, credentials, _ = import_skill_policy_modules()
    root = tmp_path / "application"
    nested = root / "nested"
    nested.mkdir(parents=True)
    policy(root, '[skills]\nmode="project"\nsources=[]\n')
    monkeypatch.setattr(credentials, "project_root", root)
    original = agent.get_skill_sources
    original_app = app.DeepAgentsApp._discover_skills_and_roots
    with pytest.raises(ValueError, match="mode"):
        with client_skill_policy():
            assert agent.get_skill_sources("x", ProjectContext(user_cwd=root)) == []
            assert invocation.discover_skills_and_roots("x", path_base=nested) == ([], [])
            assert app.DeepAgentsApp._discover_skills_and_roots(SimpleNamespace(_cwd=nested)) == ([], [])
            assert loader.list_skills(user_skills_dir=tmp_path / "personal") == []
            policy(root, '[skills]\nmode="invalid"')
            invocation.discover_skills_and_roots("x", path_base=nested)
    assert agent.get_skill_sources is original
    assert app.DeepAgentsApp._discover_skills_and_roots is original_app


def test_policy_root_prefers_context_and_matching_snapshot(tmp_path):
    from lc_factory.skill_policy import skill_policy_root
    from lc_factory.upstream import ProjectContext

    root = tmp_path / "project"
    root.mkdir()
    nested = root / "nested"
    nested.mkdir()
    snapshot = SimpleNamespace(project_root=root)
    assert skill_policy_root(nested, credentials=snapshot) == root
    other = tmp_path / "other"
    other.mkdir()
    assert skill_policy_root(other, credentials=snapshot) == other
    assert skill_policy_root(other, credentials=snapshot,
                             project_context=ProjectContext(user_cwd=nested)) == nested


async def test_constructor_nested_cwd_uses_project_policy(tmp_path):
    from dataclasses import replace
    from langchain.agents.middleware import AgentMiddleware
    from lc_factory.upstream import Credentials

    seen = []

    class CaptureSkills(AgentMiddleware):
        async def abefore_model(self, state, runtime):
            seen.extend(s["name"] for s in state.get("skills_metadata", []))

    root = tmp_path / "project"
    nested = root / "nested"
    nested.mkdir(parents=True)
    policy(root)
    skill(root / ".agents/skills", "own")
    snapshot = replace(Credentials.snapshot_from_environment(start_path=root), project_root=root)
    graph, _ = create_factory_agent(
        _ToolBindingFakeModel(messages=iter([AIMessage("done")])), "skills-nested",
        cwd=nested, credentials_snapshot=snapshot, enable_memory=False,
        enable_shell=False, enable_ask_user=False, middleware={"last": [CaptureSkills()]},
    )
    await graph.ainvoke({"messages": [HumanMessage("hello")]})
    assert seen == ["own"]


def test_personal_client_policy_preserves_upstream_arguments(tmp_path, monkeypatch):
    from unittest.mock import Mock
    from lc_factory.upstream import import_skill_policy_modules

    agent, _, loader, invocation, credentials, _ = import_skill_policy_modules()
    monkeypatch.setattr(credentials, "project_root", tmp_path)
    list_original = Mock(return_value=[{"name": "personal"}])
    source_original = Mock(return_value=[("/personal", "User")])
    discover_original = Mock(return_value=([{"name": "personal"}], [Path("/personal")]))
    content_original = Mock(return_value="personal instructions")
    monkeypatch.setattr(loader, "list_skills", list_original)
    monkeypatch.setattr(agent, "get_skill_sources", source_original)
    monkeypatch.setattr(invocation, "discover_skills_and_roots", discover_original)
    monkeypatch.setattr(loader, "load_skill_content", content_original)
    with client_skill_policy():
        assert loader.list_skills(user_skills_dir=tmp_path) == [{"name": "personal"}]
        assert agent.get_skill_sources("assistant") == [("/personal", "User")]
        assert invocation.discover_skills_and_roots("assistant", path_base=tmp_path)[1] == [Path("/personal")]
        assert loader.load_skill_content("path", allowed_roots=[tmp_path]) == "personal instructions"
    list_original.assert_called_once_with(user_skills_dir=tmp_path)
    source_original.assert_called_once_with("assistant", None)
    discover_original.assert_called_once_with("assistant", path_base=tmp_path)
    content_original.assert_called_once_with("path", allowed_roots=[tmp_path])


@pytest.mark.parametrize("empty", [False, True])
async def test_tui_invocation_refreshes_stale_personal_cache(tmp_path, monkeypatch, empty):
    from unittest.mock import AsyncMock
    from lc_factory.upstream import import_skill_policy_modules

    _, app, loader, _, credentials, _ = import_skill_policy_modules()
    root = tmp_path / "project"
    own = skill(root / ".agents/skills", "same-name")
    secret = skill(tmp_path / "personal", "same-name")
    policy(root, '[skills]\nmode="project"\n' + ('sources=[]\n' if empty else ""))
    monkeypatch.setattr(credentials, "project_root", root)
    instance = SimpleNamespace(
        _cwd=str(root),
        _discovered_skills=[{"name": "same-name", "description": "secret", "path": str(secret)}],
        _skill_allowed_roots=[secret.parent],
        _mount_message=AsyncMock(), _send_to_agent=AsyncMock(),
    )
    instance._discover_skills_and_roots_with_import_lock = lambda: app.DeepAgentsApp._discover_skills_and_roots(instance)
    original_read = Path.read_text

    def checked_read(path, *args, **kwargs):
        assert path.resolve() != secret, "Personal skill body must never be read"
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", checked_read)
    with client_skill_policy():
        await app.DeepAgentsApp._invoke_skill(instance, "same-name")
        with pytest.raises(ValueError, match="selected skill sources"):
            loader.load_skill_content(str(secret), allowed_roots=[secret.parent])
    if empty:
        instance._send_to_agent.assert_not_called()
        assert instance._discovered_skills == []
    else:
        instance._send_to_agent.assert_awaited_once()
        assert instance._discovered_skills[0]["path"] == str(own)


async def test_invalid_policy_during_tui_invocation_is_visible(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock
    from lc_factory.upstream import import_skill_policy_modules

    _, app, _, _, credentials, _ = import_skill_policy_modules()
    policy(tmp_path, "bad [")
    monkeypatch.setattr(credentials, "project_root", tmp_path)
    instance = SimpleNamespace(_cwd=str(tmp_path), _mount_message=AsyncMock(), _send_to_agent=AsyncMock())
    with client_skill_policy():
        await app.DeepAgentsApp._invoke_skill(instance, "old-personal")
    instance._mount_message.assert_awaited_once()
    instance._send_to_agent.assert_not_called()


def run_skills_list_command(arguments):
    """Exercise upstream argparse and command dispatch, not just the loader."""
    import argparse
    from lc_factory.upstream import import_skill_command_modules

    commands, _, _ = import_skill_command_modules()
    parser = argparse.ArgumentParser()

    def add_output_args(command_parser):
        command_parser.add_argument("--json", dest="output_format", action="store_const",
                                    const="json", default="text")

    commands.setup_skills_parser(parser.add_subparsers(),
                                 make_help_action=lambda callback: argparse._HelpAction,
                                 add_output_args=add_output_args)
    commands.execute_skills_command(parser.parse_args(["skills", "list", *arguments]))


@pytest.mark.parametrize("json_output", [False, True])
@pytest.mark.parametrize("project_only", [False, True])
@pytest.mark.parametrize("custom,builtin", [(True, False), (False, False), (True, True), (False, True)])
def test_skills_list_command_uses_selected_roots_without_default_directories(
    tmp_path, monkeypatch, capsys, json_output, project_only, custom, builtin,
):
    import json
    from lc_factory import skill_policy as module
    from lc_factory.upstream import import_skill_policy_modules

    _, _, _, _, credentials, _ = import_skill_policy_modules()
    root = tmp_path / "project"
    root.mkdir()
    # A non-Git application is valid; the upstream precheck also rejects this.
    monkeypatch.chdir(root)
    monkeypatch.setattr(credentials, "project_root", None)
    skill(root / "custom-skills", "custom-choice")
    builtin_root = tmp_path / "bundled"
    skill(builtin_root, "builtin-choice")
    monkeypatch.setattr(module, "built_in_skills_dir", lambda: builtin_root)
    sources = '["custom-skills"]' if custom else '[]'
    policy(root, f'[skills]\nmode="project"\nsources={sources}\ninclude_builtin={str(builtin).lower()}\n')
    assert not (root / ".agents/skills").exists()
    assert not (root / ".deepagents/skills").exists()
    arguments = (["--project"] if project_only else []) + (["--json"] if json_output else [])
    with client_skill_policy():
        run_skills_list_command(arguments)
    captured = capsys.readouterr().out
    expected = ({"custom-choice"} if custom else set()) | (
        {"builtin-choice"} if builtin and not project_only else set())
    if json_output:
        envelope = json.loads(captured)
        assert envelope["command"] == "skills list"
        assert envelope["schema_version"] == 1
        assert {row["name"] for row in envelope["data"]} == expected
    else:
        for name in {"custom-choice", "builtin-choice"}:
            assert (name in captured) == (name in expected)
        assert "Not in a project" not in captured
        if not expected:
            assert "No project skills selected." in captured if project_only else "No skills selected." in captured


def test_personal_skills_command_keeps_upstream_dispatch_and_restores(tmp_path, monkeypatch):
    from unittest.mock import Mock
    from lc_factory.upstream import import_skill_command_modules, import_skill_policy_modules

    commands, _, _ = import_skill_command_modules()
    _, _, _, _, credentials, _ = import_skill_policy_modules()
    monkeypatch.setattr(credentials, "project_root", tmp_path)
    original = Mock()
    monkeypatch.setattr(commands, "_list", original)
    with client_skill_policy():
        run_skills_list_command(["--project", "--json", "--agent", "custom-agent"])
    original.assert_called_once_with("custom-agent", project=True, output_format="json")
    assert commands._list is original
