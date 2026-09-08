"""Enterprise workspace, native reasoning and OG delegation display contracts."""
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest


def test_nested_workspace_capture_and_home_exclusion(tmp_path, monkeypatch):
    from lc_factory_enterprise import workspace
    from lc_factory_enterprise.tui import client_adaptations
    from deepagents_code.project_utils import ProjectContext
    home = Path.home()
    (home / ".deepagents").mkdir(exist_ok=True)
    repo = home / "repo"
    nested = repo / "consumer"
    leaf = nested / "work"
    leaf.mkdir(parents=True)
    (nested / ".deepagents").mkdir()
    monkeypatch.setattr(workspace, "upstream_find_git_root", lambda path: repo if path.is_relative_to(repo) else None)
    assert workspace.find_workspace_root(leaf) == nested
    assert workspace.find_workspace_root(repo) == repo
    assert workspace.find_workspace_root(home) is None
    linked = tmp_path / "linked"
    linked.symlink_to(leaf, target_is_directory=True)
    assert workspace.find_workspace_root(linked) == nested
    original = workspace.project_utils_module.find_git_root
    with client_adaptations():
        context = ProjectContext.from_user_cwd(leaf)
        assert context.project_root == nested
        assert context.user_cwd == leaf
    assert workspace.project_utils_module.find_git_root is original


def test_scaffold_keeps_og_graph_lifespans_and_declares_both_packages(tmp_path, monkeypatch):
    from lc_factory_enterprise.launch import scaffold_workspace
    monkeypatch.setenv("LC_FACTORY_CAPABILITIES", "reload,history")
    scaffold_workspace(tmp_path)
    config = json.loads((tmp_path / "langgraph.json").read_text())
    assert "lc_factory_enterprise.server_graph:make_graph" in config["graphs"].values()
    assert config["http"]["app"] == "lc_factory_enterprise.offload_api:app"
    assert "lc_factory.server_checkpointer" in (tmp_path / "checkpointer.py").read_text()
    dependencies = tomllib.loads((tmp_path / "pyproject.toml").read_text())["project"]["dependencies"]
    assert len(dependencies) == 2
    assert dependencies[0].startswith("lc_factory @ file:")
    assert dependencies[1].startswith("lc-factory-enterprise @ file:")


def test_server_entries_delegate_to_og_with_matching_workspace_policy(tmp_path):
    source = Path(__file__).resolve().parents[1] / "enterprise" / "src"
    (tmp_path / ".deepagents").mkdir()
    leaf = tmp_path / "work"
    leaf.mkdir()
    code = '''
from pathlib import Path
from lc_factory_enterprise import server_graph, offload_api
from lc_factory import server_graph as og_graph, offload_api as og_api
from deepagents_code.workspace import resolve_workspace
assert server_graph.make_graph is og_graph.make_graph
assert offload_api.app is og_api.app
assert resolve_workspace(str(Path.cwd())).project_root == str(Path.cwd().parent)
'''
    result = subprocess.run([sys.executable, "-c", code], cwd=leaf,
        env={**os.environ, "PYTHONPATH": str(source)}, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


async def test_panel_preserves_replay_and_separates_eval_phases():
    from textual.app import App
    from lc_factory_enterprise.replay import _ReplayAwareSubagentPanel
    from lc_factory_enterprise.chrome import _QuietStatusBar, _RebrandedBanner
    class Display(App):
        def get_theme_variable_defaults(self):
            from deepagents_code.theme import get_css_variable_defaults
            return get_css_variable_defaults()
        def compose(self):
            yield _ReplayAwareSubagentPanel(id="panel")
            yield _QuietStatusBar()
            yield _RebrandedBanner()
    async with Display().run_test() as pilot:
        panel = pilot.app.query_one("#panel")
        event = {"type": "subagent", "phase": "start", "id": "child", "eval_id": "eval", "description": "Read"}
        panel.on_subagent_event(event)
        record = panel._phases["eval"].records["child"]
        record.started_monotonic -= 2
        panel.on_subagent_event(event)
        assert panel._phases["eval"].records["child"] is record
        panel.on_subagent_event({**event, "phase": "complete", "duration_ms": 1})
        assert record.status == "done" and record.duration_ms >= 2000
        panel.on_subagent_event({**event, "id": "pending"})
        panel.on_subagent_event({**event, "id": "separate", "eval_id": "other"})
        panel.on_subagent_event({"phase": "phase_complete", "eval_id": "eval"})
        assert panel._phases["eval"].records["pending"].status == "cancelled"
        assert panel._phases["other"].records["separate"].status == "running"
        panel.on_subagent_event({"phase": "phase_complete", "eval_id": []})
        panel.on_subagent_event({"phase": "start", "id": []})
        assert len(panel._phases) == 2
        bar = pilot.app.query_one(_QuietStatusBar)
        assert bar._cost_text() == ""
        assert "Tokens:" not in bar._context_segment(100).plain
        banner = pilot.app.query_one(_RebrandedBanner)._build_banner().plain
        assert "DDT-agent" in banner and "dcode" not in banner


def test_reasoning_survives_remote_conversion_without_stdout_or_replay_leak(capsys):
    from deepagents_code.client.remote_client import _convert_ai_message
    from deepagents_code.client.non_interactive import _process_ai_message, StreamState
    from lc_factory_enterprise.merck_models import MerckAnthropicChatModel
    message = MerckAnthropicChatModel._parse_message({"content": [
        {"type": "thinking", "thinking": "provider reasoning"},
        {"type": "text", "text": "visible answer"},
    ]})
    remote = _convert_ai_message(json.loads(message.model_dump_json()))
    assert any(block["type"] == "reasoning" for block in remote.content_blocks)
    from rich.console import Console
    _process_ai_message(remote, StreamState(), Console(stderr=True))
    captured = capsys.readouterr()
    assert "visible answer" in captured.out
    assert "provider reasoning" not in captured.out
    assert "\x00" not in captured.out + captured.err
    assert "provider reasoning" not in json.dumps(MerckAnthropicChatModel._convert_message(remote))


def test_model_import_does_not_load_client(tmp_path):
    source = Path(__file__).resolve().parents[1] / "enterprise" / "src"
    env = {**os.environ, "PYTHONPATH": str(source)}
    code = "import sys; from lc_factory.merck_models import MerckChatModel; assert 'deepagents_code.app' not in sys.modules; assert 'lc_factory_enterprise.tui' not in sys.modules"
    result = subprocess.run([sys.executable, "-c", code], env=env, cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_example_profile_constructs_models_and_verification(isolated_environment):
    from deepagents_code.config import create_model, use_environment, MODEL_RETRIES_ATTR
    from deepagents_code.model_config import clear_caches
    from deepagents_code.configuration.service import invalidate_config_sources
    from lc_factory.verification import configured_verification_model
    source = Path(__file__).resolve().parents[1] / "enterprise" / "examples" / "config.toml"
    target = Path.home() / ".deepagents" / "config.toml"
    target.parent.mkdir(exist_ok=True)
    target.write_text(source.read_text())
    clear_caches()
    invalidate_config_sources()
    with use_environment({"COMPANY_LLM_API_KEY": "fixture"}):
        main = create_model("merck_gpt:company-gpt")
        verifier = configured_verification_model({"COMPANY_LLM_API_KEY": "fixture"})
    assert main.model._url.startswith("https://gateway.example.invalid/")
    assert main.model.max_retries == verifier.max_retries == 0
    assert getattr(verifier, MODEL_RETRIES_ATTR) == 3
