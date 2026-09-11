"""Launcher ownership, argument routing and native-client handoff contracts."""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import sys

import httpx
import pytest

from lc_factory import web_launcher as launch
from lc_factory.upstream_cli import app_module, web_client_modules, web_command_registry


@pytest.mark.parametrize('argv,expected', [
    (['lc-code', '--web-frontend', '--model', 'deepseek:test', '--no-mcp'], True),
    (['lc-code', '--', '--web-frontend'], False),
    (['ddt-agent'], False),
])
def test_scoped_cli_and_registry_restore(monkeypatch, argv, expected):
    main, *_ = web_client_modules()
    original = app_module.run_textual_app
    updater = main._run_startup_auto_update
    registry = web_command_registry()
    commands, queued = registry.COMMANDS, registry.QUEUE_BOUND
    monkeypatch.setattr(sys, 'argv', argv)
    with launch.client_web_frontend():
        assert (app_module.run_textual_app is launch.run_web_app) == expected
        assert any(x.name == '/web-frontend' for x in registry.get_slash_commands())
        assert '/web-frontend' in registry.QUEUE_BOUND
        if expected:
            parsed = main.parse_args()
            assert parsed.model == 'deepseek:test'
            assert parsed.no_mcp
    assert app_module.run_textual_app is original
    assert main._run_startup_auto_update is updater
    assert registry.COMMANDS is commands and registry.QUEUE_BOUND is queued
    assert sys.argv is argv


@pytest.mark.parametrize('options', [['threads'], ['-r'], ['--acp'], ['--goal', 'test'], ['-n', 'hello'], ['--sandbox', 'modal'], ['--install', 'modal'], ['--default-model', 'test'], ['--auto-update'], ['--stdin']])
def test_incompatible_cli_routes_are_rejected(monkeypatch, options):
    main, *_ = web_client_modules()
    monkeypatch.setattr(sys, 'argv', ['lc-code', '--web-frontend', *options])
    with launch.client_web_frontend(), pytest.raises(SystemExit):
        main.parse_args()


def fake_agent(url='http://127.0.0.1:9999', headers=None):
    http = SimpleNamespace(client=httpx.AsyncClient(base_url=url, headers=headers))
    graph = SimpleNamespace(_validate_client=lambda: SimpleNamespace(http=http))
    return SimpleNamespace(_get_graph=lambda: graph)


def test_transport_preserves_effective_auth_but_drops_framing():
    agent = fake_agent(headers={'authorization': 'Bearer test-only', 'host': 'old', 'x-test': 'retained'})
    url, headers = launch.backend_connection(agent)
    assert url == 'http://127.0.0.1:9999'
    assert headers['authorization'] == 'Bearer test-only'
    assert headers['x-test'] == 'retained'
    assert 'host' not in headers


@pytest.mark.parametrize('url', ['https://example.com', 'http://user:pass@127.0.0.1:9000', 'http://127.0.0.1:9000/path'])
def test_backend_connection_rejects_unowned_origins(url):
    with pytest.raises(launch.WebLaunchError):
        launch.backend_connection(fake_agent(url))


async def test_bind_revalidates_instead_of_using_cached_descriptor(tmp_path):
    agent = SimpleNamespace(abind_workspace=AsyncMock(return_value={'cwd': str(tmp_path)}))
    await launch.bind(agent, 'existing', tmp_path)
    agent.abind_workspace.assert_awaited_once_with({'configurable': {'thread_id': 'existing'}}, str(tmp_path))
    agent.abind_workspace.return_value = {'cwd': '/other'}
    with pytest.raises(launch.WebLaunchError):
        await launch.bind(agent, 'existing', tmp_path)


@pytest.mark.parametrize('option,value', [('resume_thread', 'old'), ('initial_prompt', 'hello'), ('initial_skill', 'review'), ('initial_goal', 'ship'), ('startup_cmd', 'touch file'), ('defer_server_start', True)])
async def test_unsupported_startup_fails_before_processes(monkeypatch, option, value):
    monkeypatch.setattr(launch, 'assets', lambda: pytest.fail('must validate options first'))
    with pytest.raises(launch.WebLaunchError):
        await launch.run_web_app(**{option: value})


async def test_native_resolved_configuration_and_failure_cleanup(monkeypatch, tmp_path):
    from lc_factory.upstream import server_manager_module
    events = []
    agent = object()
    @asynccontextmanager
    async def server_session(**kwargs):
        events.append(('start', kwargs))
        try:
            yield agent, object()
        finally:
            events.append(('stop',))
    monkeypatch.setattr(server_manager_module, 'server_session', server_session)
    monkeypatch.setattr(launch, 'assets', lambda: tmp_path)
    monkeypatch.setattr(launch, 'node_runtime', AsyncMock(return_value='node'))
    monkeypatch.setattr(launch, 'require_supported_hooks', lambda hooks: None)
    monkeypatch.setattr(launch, 'bind', AsyncMock())
    monkeypatch.setattr(launch, 'set_mode', AsyncMock())
    async def fail(**kwargs):
        events.append(('web', kwargs))
        raise launch.WebLaunchError('simulated frontend failure')
    monkeypatch.setattr(launch.WebProcess, 'start', fail)
    with pytest.raises(launch.WebLaunchError, match='simulated'):
        await launch.run_web_app(cwd=tmp_path, thread_id='new', approval_mode='auto', server_kwargs={
            'assistant_id': 'enterprise-profile', 'model_name': 'deepseek:test', 'no_mcp': True,
            'profile_overrides': {'name': 'custom'}, 'sandbox_type': 'none',
        })
    kwargs = events[0][1]
    assert kwargs['assistant_id'] == 'enterprise-profile' and kwargs['profile_overrides'] == {'name': 'custom'}
    assert kwargs['cwd'] == str(tmp_path) and kwargs['port'] == 0 and kwargs['no_mcp']
    assert events[-1] == ('stop',)
    launch.set_mode.assert_awaited_once_with(agent, 'new', 'auto')


async def test_forced_input_and_keyboard_cannot_mutate_during_handoff():
    fake = SimpleNamespace(_factory_web_handoff=True)
    with launch.client_web_frontend():
        assert await app_module.DeepAgentsApp._submit_input(fake, 'hello', force_bypass=True) is None
        assert await app_module.DeepAgentsApp.action_toggle_auto_approve(fake) is None
        assert app_module.DeepAgentsApp.action_interrupt(fake) is None


async def test_return_control_requires_drain_idle_and_history(monkeypatch):
    from textual.app import App
    from lc_factory.web_tui import BrowserHandoff
    import lc_factory.web_tui as tui
    events = []
    web = SimpleNamespace(process=SimpleNamespace(returncode=None), control=AsyncMock(), stop=AsyncMock())
    response = httpx.Response(200, json={'value': {'mode': 'auto'}}, request=httpx.Request('GET', 'http://local'))
    monkeypatch.setattr(tui, 'transport', lambda _: SimpleNamespace(get=AsyncMock(return_value=response)))
    idle = AsyncMock(return_value=False)
    monkeypatch.setattr(tui, 'quiescent', idle)
    class Harness(App):
        async def on_mount(self):
            self.handoff = BrowserHandoff(object(), 'thread', '/project')
            self.handoff.web = web
            self.handoff.transferred = True
            await self.push_screen(self.handoff)
        async def _fetch_thread_history_data(self, _):
            events.append('fetch'); return SimpleNamespace(messages=[])
        async def _clear_messages(self):
            events.append('clear')
        async def _load_thread_history(self, **kwargs):
            events.append('load')
        def _on_approval_mode_fallback(self, mode):
            events.append(mode)
    app = Harness()
    async with app.run_test() as pilot:
        await app.handoff.return_control()
        assert not app.handoff.completed.is_set()
        assert web.control.await_args_list[0].args == ('pause',)
        assert web.control.await_args_list[1].args == ('resume',)
        assert not events
        idle.return_value = True
        await app.handoff.return_control()
        assert app.handoff.completed.is_set()
        assert events == ['fetch', 'clear', 'load', 'auto']
        web.stop.assert_awaited_once()


async def test_frontend_crash_does_not_release_tui(monkeypatch):
    from textual.app import App
    from lc_factory.web_tui import BrowserHandoff
    import lc_factory.web_tui as tui
    monkeypatch.setattr(tui, 'quiescent', AsyncMock(side_effect=AssertionError('a crash is not a drain')))
    class Harness(App):
        async def on_mount(self):
            self.handoff = BrowserHandoff(object(), 'thread', '/project')
            self.handoff.transferred = True
            self.handoff.web = SimpleNamespace(process=SimpleNamespace(returncode=1))
            await self.push_screen(self.handoff)
    app = Harness()
    async with app.run_test():
        await app.handoff.return_control()
        assert not app.handoff.completed.is_set()
        assert app.handoff.uncertain


async def test_return_during_startup_cannot_complete_handoff(monkeypatch):
    from textual.app import App
    from lc_factory.web_tui import BrowserHandoff, handoff
    from lc_factory.upstream_cli import web_client_modules
    import lc_factory.web_tui as tui
    entered, release = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(tui, 'bind', AsyncMock())
    monkeypatch.setattr(tui, 'quiescent', AsyncMock(return_value=True))
    async def start(self):
        entered.set()
        await release.wait()
        self.web = SimpleNamespace(stop=AsyncMock(), process=SimpleNamespace(returncode=0))
        self.transferred = True
    monkeypatch.setattr(BrowserHandoff, 'start_browser', start)
    class Harness(App):
        async def on_mount(self):
            self._agent = SimpleNamespace(aget_state=AsyncMock(return_value=SimpleNamespace(values={})))
            self._lc_thread_id, self._cwd = 'thread', '/project'
            self._factory_web_handoff, self._exiting = True, False
            self.handoff_task = asyncio.create_task(handoff(self))
    app = Harness()
    async with app.run_test() as pilot:
        await entered.wait()
        screen = app.screen
        assert isinstance(screen, BrowserHandoff)
        await screen.return_control()
        assert not screen.completed.is_set() and app._factory_web_handoff
        release.set()
        await pilot.pause()
        # Simulate a successful later reconciliation to drain the owning task.
        screen.completed.set()
        await app.handoff_task


@pytest.mark.parametrize('state', [
    {'_pending_goal_objective': 'proposal'}, {'_pending_goal_rubric': 'criteria'},
    {'_pending_goal_kind': 'create'}, {'_pending_goal_request_id': 'request'},
    {'goal_criteria_request': {'objective': 'draft'}}, {'goal_criteria_request': {}}, {'rubric': 'one-shot criteria'},
    {'_goal_status': 'active'}, {'_sticky_rubric': 'always validate'},
])
def test_handoff_recognizes_pending_and_active_native_goal_state(state):
    from lc_factory.web_tui import has_native_goal_state
    assert has_native_goal_state(state)
    assert not has_native_goal_state({'messages': [], '_goal_objective': None})


async def test_supervision_detects_backend_exit_without_waiting_for_frontend():
    web = SimpleNamespace(process=SimpleNamespace(returncode=None))
    with pytest.raises(launch.WebLaunchError, match='agent backend stopped'):
        await launch.supervise_children(web, SimpleNamespace(running=False))
    web.process.returncode = 1
    with pytest.raises(launch.WebLaunchError, match='web frontend stopped'):
        await launch.supervise_children(web, SimpleNamespace(running=True))
