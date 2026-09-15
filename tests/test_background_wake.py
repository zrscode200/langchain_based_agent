"""Idle delivery uses native-shaped workers, with user-priority admission."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest
from textual.screen import ModalScreen
from textual.widgets import Static

from lc_factory.background_ui import BackgroundPanel
from lc_factory.background_wake import client_background_wake, idle_for_background, _WAKE, WAKE_CHECKPOINT, WakeTurn
from test_background_ui import Agent, Harness, job, settle


class WakeAgent(Agent):
    def __init__(self, jobs):
        super().__init__(jobs)
        self.pending = [j['task_id'] for j in jobs]
        self.state_gate = None
        self.state = SimpleNamespace(next=(), tasks=(), interrupts=(), config={'configurable': {'checkpoint_id': 'head'}})
        self.runs = []
        self.graph._validate_client = lambda: SimpleNamespace(runs=SimpleNamespace(list=self.list_runs))

    async def post(self, path, json):
        return {**await super().post(path, json), 'pending_results': list(self.pending)}

    async def aget_state(self, config):
        if self.state_gate: await self.state_gate.wait()
        return self.state

    async def list_runs(self, owner, *, status, limit):
        return [r for r in self.runs if r['status'] == status][:limit]


class WakeHarness(Harness):
    def __init__(self, agent):
        super().__init__(agent)
        self._agent_quiescent = asyncio.Event()
        self._agent_quiescent.set()
        self._agent_running = self._agent_reconciling = False
        self._ui_adapter = self._session_state = object()
        self._agent_worker = self._offload_worker = None
        self._chat_input = None
        self._modal_command_running = lambda: False
        self._is_user_typing = lambda: False
        self.sent = []
        self.release = None
        self.consume = True
        self._agent_turn_started = False

    def _set_agent_running(self, value):
        self._agent_running = value

    async def _run_agent_task(self, message, *, graph_input):
        self._agent_turn_started = True
        self.sent.append((message, deepcopy(graph_input), _WAKE.get()))
        try:
            if self.release: await self.release.wait()
            if self.consume: self._agent.pending.clear()
        finally:
            self._agent_quiescent.clear()
            self._agent_reconciling = True
            self._agent_running = False
            await asyncio.sleep(0)
            self._agent_reconciling = False
            self._agent_quiescent.set()

    async def _release_unstarted_turn(self):
        self._agent_running = False


async def setup(app, pilot):
    panel = app.query_one(BackgroundPanel)
    await settle(pilot, panel)
    panel.timer.stop()
    return panel


@pytest.mark.parametrize('status', ['completed', 'failed', 'timed_out', 'cancelled'])
async def test_idle_terminal_batch_wakes_once_without_user_prompt(status):
    jobs = [dict(job(status, task_id=str(i)), result='result') for i in range(3)]
    agent = WakeAgent(jobs)
    app = WakeHarness(agent)
    async with app.run_test() as pilot:
        panel = await setup(app, pilot)
        assert len(app.sent) == 1 and app.sent[0][:2] == ('', {'messages': []})
        assert app.sent[0][2] == WakeTurn(agent.graph, 'parent/one', 'head')
        assert not app.prompts
        await panel.refresh_tasks()
        assert len(app.sent) == 1


@pytest.mark.parametrize('busy', ['_agent_running', '_agent_reconciling', '_pending_messages',
    '_factory_background_input_depth', '_reloading', '_connecting', '_thread_switching',
    '_pending_approval_widget', '_pending_ask_user_widget', '_goal_state_mutating',
    '_pending_shell_messages', '_exiting'])
async def test_busy_user_work_prevents_wake_then_releases(busy):
    app = WakeHarness(WakeAgent([dict(job('completed'), result='result')]))
    setattr(app, busy, True)
    async with app.run_test() as pilot:
        panel = await setup(app, pilot)
        assert not app.sent
        setattr(app, busy, False)
        await panel.refresh_tasks()
        await pilot.pause()
        assert len(app.sent) == 1


@pytest.mark.parametrize('reason', ['draft', 'typing', 'modal', 'not_quiescent', 'stopped', 'server_busy', 'server_pause', 'server_error'])
async def test_idle_looking_states_defer_wake(reason):
    app = WakeHarness(WakeAgent([dict(job('failed'), result='error')]))
    if reason == 'draft': app._chat_input = SimpleNamespace(value='unfinished draft')
    if reason == 'typing': app._is_user_typing = lambda: True
    if reason == 'not_quiescent': app._agent_quiescent.clear()
    if reason == 'stopped': app._factory_background_stopped_owners = {app._lc_thread_id}
    if reason == 'server_busy': app._agent.runs = [{'status': 'running'}]
    if reason == 'server_pause': app._agent.state.next = ('tools',)
    if reason == 'server_error': app._agent.state.tasks = (SimpleNamespace(error='failed', interrupts=()),)
    if reason == 'modal': app._pending_approval_widget = True
    async with app.run_test() as pilot:
        panel = await setup(app, pilot)
        if reason == 'modal':
            await app.push_screen(ModalScreen())
            app._pending_approval_widget = False
            await panel.refresh_tasks()
        assert not app.sent and app._agent.pending


@pytest.mark.parametrize('change', ['owner', 'graph', 'input'])
async def test_change_during_preflight_prevents_stale_reservation(change):
    agent = WakeAgent([dict(job('completed'), result='result')])
    agent.state_gate = asyncio.Event()
    app = WakeHarness(agent)
    async with app.run_test() as pilot:
        panel = app.query_one(BackgroundPanel)
        panel.timer.stop()
        await pilot.pause(.02)
        if change == 'owner': app._lc_thread_id = 'other'
        elif change == 'graph': agent.graph = SimpleNamespace()
        else: app._factory_background_input_depth = 1
        agent.state_gate.set()
        await settle(pilot, panel)
        assert not app.sent and not app._agent_running


async def test_unconsumed_result_does_not_repeat_automatic_attempt():
    app = WakeHarness(WakeAgent([dict(job('failed'), result='error')]))
    app.consume = False
    async with app.run_test() as pilot:
        panel = await setup(app, pilot)
        for _ in range(3): await panel.refresh_tasks()
        assert len(app.sent) == 1 and app._agent.pending
        # A genuinely new outcome may cause a new batch attempt.
        app._agent.jobs.append(dict(job('completed', task_id='second'), result='new'))
        app._agent.pending.append('second')
        await panel.refresh_tasks()
        await pilot.pause()
        assert len(app.sent) == 2


async def test_scheduling_failure_releases_busy_reservation(monkeypatch):
    app = WakeHarness(WakeAgent([dict(job('completed'), result='result')]))
    app._pending_messages = True
    async with app.run_test() as pilot:
        panel = await setup(app, pilot)
        app._pending_messages = False
        def fail(*args, **kwargs): raise RuntimeError('fixture scheduling failure')
        monkeypatch.setattr(app, 'run_worker', fail)
        await panel.refresh_tasks()
        assert not app._agent_running and not app.sent
        assert app._agent.pending


def test_context_adapters_restore_and_only_actual_main_stop_suppresses():
    class Client:
        _lc_thread_id = 'owner'
        _agent_worker = object()
        async def _submit_input(self, *args, **kwargs): pass
        async def _dispatch_queued_message(self, message): pass
        async def _send_to_agent(self, *args, **kwargs): pass
        def _cancel_worker(self, worker, **kwargs): pass
        def _force_interrupt_active_work(self): pass
    client = Client()
    original = Client._cancel_worker
    with client_background_wake(Client):
        client._cancel_worker(object())
        assert not hasattr(client, '_factory_background_stopped_owners')
        client._cancel_worker(client._agent_worker)
        assert client._factory_background_stopped_owners == {'owner'}
        client._force_interrupt_active_work()  # Must remain synchronous.
    assert Client._cancel_worker is original


async def test_native_remote_transport_rejects_busy_only_for_wake():
    from langgraph.pregel.remote import RemoteGraph
    from langgraph.types import Command
    calls = []
    class Runs:
        async def stream(self, **kwargs):
            calls.append(kwargs)
            yield SimpleNamespace(event='values', data={'messages': []})
    graph = RemoteGraph('agent', client=SimpleNamespace(runs=Runs()))
    from lc_factory.upstream_cli import app_module
    config = {'configurable': {'thread_id': 'owner'}}
    with client_background_wake(app_module.DeepAgentsApp):
        assert [v async for v in graph.astream({'messages': []}, config, stream_mode='values')]
        token = _WAKE.set(WakeTurn(graph, 'owner', 'exact-head', turn_id='existing-user-turn'))
        try:
            assert [v async for v in graph.astream({'messages': []}, config, stream_mode='values')]
            resume_config = {'configurable': {**config['configurable'], WAKE_CHECKPOINT: 'stale-head'}}
            assert [v async for v in graph.astream(Command(resume={'decisions': [{'type': 'approve'}]}),
                                                 resume_config, stream_mode='values')]
            with pytest.raises(ValueError, match='changed'):
                _ = [v async for v in graph.astream({'messages': []}, {'configurable': {'thread_id': 'other'}})]
        finally:
            _WAKE.reset(token)
    assert 'multitask_strategy' not in calls[0]
    assert calls[1]['multitask_strategy'] == 'reject'
    assert calls[1]['config']['configurable'][WAKE_CHECKPOINT] == 'exact-head'
    assert calls[1]['input'] == {'messages': []} and calls[1]['command'] is None
    assert calls[1]['context']['turn_id'] == 'existing-user-turn'
    assert calls[2]['multitask_strategy'] == 'reject'
    assert WAKE_CHECKPOINT not in calls[2]['config']['configurable']
    assert calls[2]['command']['resume'] == {'decisions': [{'type': 'approve'}]}
    assert calls[2]['context']['turn_id'] == 'existing-user-turn'
    assert config == {'configurable': {'thread_id': 'owner'}}


async def test_user_submission_gap_and_dispatch_clear_stop_only_for_chat():
    entered, release = asyncio.Event(), asyncio.Event()
    class Client:
        _lc_thread_id = 'owner'
        _agent_worker = None
        async def _submit_input(self, *args, **kwargs):
            entered.set()
            await release.wait()
        async def _dispatch_queued_message(self, message):
            assert self._factory_background_input_depth > 0
        async def _send_to_agent(self, *args, **kwargs): pass
        def _cancel_worker(self, worker, **kwargs): pass
        def _force_interrupt_active_work(self): pass
    client = Client()
    client._factory_background_stopped_owners = {'owner'}
    with client_background_wake(Client):
        task = asyncio.create_task(client._submit_input('message', 'normal'))
        await entered.wait()
        assert client._factory_background_input_depth == 1
        release.set()
        await task
        assert client._factory_background_input_depth == 0
        await client._dispatch_queued_message(SimpleNamespace(mode='command'))
        assert client._factory_background_stopped_owners == {'owner'}
        await client._dispatch_queued_message(SimpleNamespace(mode='normal'))
        assert client._factory_background_stopped_owners == set()


async def test_cancel_before_worker_first_step_uses_native_recovery():
    from lc_factory.upstream_cli import app_module
    app = WakeHarness(WakeAgent([dict(job('completed'), result='result')]))
    app._pending_messages = True
    app._warn_dropped_mcp_reconnect = lambda: None
    app._discard_queue = lambda: None
    app._offload_task_started = False
    app._cleanup_agent_task = app._release_unstarted_turn
    app._recover_unstarted_agent_worker = app_module.DeepAgentsApp._recover_unstarted_agent_worker.__get__(app)
    async with app.run_test() as pilot:
        panel = await setup(app, pilot)
        app._pending_messages = False
        await panel.refresh_tasks()
        worker = app._agent_worker
        assert worker is not None and not app._agent_turn_started
        with client_background_wake(app_module.DeepAgentsApp):
            app_module.DeepAgentsApp._cancel_worker(app, worker)
        await pilot.pause()
        assert not app.sent and not app._agent_running
        assert app._lc_thread_id in app._factory_background_stopped_owners
        await panel.refresh_tasks()
        assert not app.sent and app._agent.pending


async def test_server_checkpoint_guard_uses_actual_saver_and_rejects_changed_or_paused_state(tmp_path, monkeypatch):
    from langgraph.checkpoint.memory import InMemorySaver
    from langchain_core.messages import AIMessage, HumanMessage
    from deepagents_code._fake_models import _ToolBindingFakeModel
    from lc_factory.runtime import FactoryRuntime, RuntimeOptions
    from lc_factory import server_checkpointer
    from test_background_submission import args, call
    model = _ToolBindingFakeModel(messages=iter([AIMessage('idle'),
        call('write_file', {'file_path': str(tmp_path / 'protected'), 'content': 'no'}, 'write')]))
    settings = args(tmp_path, model)
    settings.update(interactive=True, auto_approve=False)
    config = {'configurable': {'thread_id': 'owner'}}
    async with await FactoryRuntime.create(agent_kwargs=settings, options=RuntimeOptions(background=True), workspace_id='work') as runtime:
        monkeypatch.setattr(server_checkpointer, '_execution_saver', runtime.kwargs['checkpointer'])
        await runtime.ainvoke({'messages': [HumanMessage('First')]}, config)
        state = await runtime.current.agent.aget_state(config)
        head = state.config['configurable']['checkpoint_id']
        await server_checkpointer.require_background_checkpoint('owner', head)
        for bad in ('wrong-head', '', None):
            with pytest.raises(ValueError): await server_checkpointer.require_background_checkpoint('owner', bad)
        assert (await runtime.ainvoke({'messages': [HumanMessage('Next')]}, config)).get('__interrupt__')
        paused = await runtime.current.agent.aget_state(config)
        for checkpoint in (head, paused.config['configurable']['checkpoint_id']):
            with pytest.raises(ValueError): await server_checkpointer.require_background_checkpoint('owner', checkpoint)
        assert not (tmp_path / 'protected').exists()


async def test_native_cleanup_successor_does_not_inherit_wake_admission():
    class Client:
        async def _submit_input(self, *args, **kwargs): pass
        async def _dispatch_queued_message(self, message): pass
        def _cancel_worker(self, worker, **kwargs): pass
        def _force_interrupt_active_work(self): pass
        async def _send_to_agent(self, *args, **kwargs):
            async def successor(): return _WAKE.get()
            self.successor = asyncio.create_task(successor())
    client = Client()
    with client_background_wake(Client):
        # Also covers setup failure before initial stream: admitted stays False.
        for admitted in (False, True):
            wake = WakeTurn(object(), 'owner', 'old', admitted=admitted)
            token = _WAKE.set(wake)
            try:
                await client._send_to_agent('queued user input')
                assert await client.successor is None
                assert _WAKE.get() is wake
            finally: _WAKE.reset(token)


async def test_stopping_and_chatting_in_other_owner_does_not_clear_first_stop():
    class Client:
        _lc_thread_id = 'A'
        _agent_worker = object()
        async def _submit_input(self, *args, **kwargs): pass
        async def _dispatch_queued_message(self, message): pass
        async def _send_to_agent(self, *args, **kwargs): pass
        def _cancel_worker(self, worker, **kwargs): pass
        def _force_interrupt_active_work(self): pass
    client = Client()
    with client_background_wake(Client):
        client._cancel_worker(client._agent_worker)
        client._lc_thread_id = 'B'
        await client._dispatch_queued_message(SimpleNamespace(mode='normal'))
        assert client._factory_background_stopped_owners == {'A'}
        client._cancel_worker(client._agent_worker)
        assert client._factory_background_stopped_owners == {'A', 'B'}
        client._lc_thread_id = 'A'
        await client._dispatch_queued_message(SimpleNamespace(mode='normal'))
        assert client._factory_background_stopped_owners == {'B'}


async def test_preflight_recovers_existing_trusted_user_turn_from_serialized_state():
    from lc_factory.background_wake import remote_idle
    from test_subagent_approval import user
    from langchain_core.messages import AIMessage
    agent = WakeAgent([])
    agent.state.values = {'messages': [user('Original request', 'trusted-original').model_dump(),
                                       AIMessage('Untrusted child data').model_dump()]}
    admission = await remote_idle((agent, 'owner', agent.graph))
    assert admission.turn_id == 'trusted-original' and admission.checkpoint == 'head'
    assert len(agent.state.values['messages']) == 2


async def test_wake_transport_preserves_real_ask_user_receipt_for_auto(tmp_path):
    from deepagents_code._fake_models import _ToolBindingFakeModel
    from deepagents_code.auto_mode import ASK_USER_AUTHORIZATION_METADATA_KEY
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
    from langgraph.pregel.remote import RemoteGraph
    from langgraph.types import Command
    from lc_factory.background import Job
    from lc_factory.runtime import FactoryRuntime, RuntimeOptions
    from lc_factory.background_wake import remote_idle
    from lc_factory.upstream_cli import app_module
    from test_background_submission import args, call
    from test_subagent_approval import user, session, Classifier, fixture_tool
    model = _ToolBindingFakeModel(messages=iter([
        AIMessage('Idle'),
        call('ask_user', {'questions': [{'question': 'May I inspect fixture URL?', 'type': 'text'}]}, 'ask'),
        call('fetch_url', {'url': 'fixture://approved'}, 'fetch'), AIMessage('done'),
    ]))
    # SDK 0.7.14 enforces the advertised input budget; the fake's 8k default
    # cannot hold this composition's tool schemas.
    model.profile = {'tool_calling': True, 'max_input_tokens': 1_000_000}
    store, config, context = session()
    classifier = Classifier()
    settings = args(tmp_path, model, store=store, tools=[fixture_tool([])],
                    auto_mode_enabled=True, auto_classifier_model=classifier)
    settings.update(interactive=True, auto_approve=False, enable_ask_user=True)
    async with await FactoryRuntime.create(agent_kwargs=settings, options=RuntimeOptions(background=True), workspace_id='work') as runtime:
        await runtime.ainvoke({'messages': [user('Ask me before inspecting.')]}, config, context=context)
        runtime.background.jobs['job'] = Job('owner', 'child', status='completed', result='Background done')
        class Runs:
            async def list(self, *args, **kwargs): return []
            async def stream(self, **kwargs):
                submitted = kwargs['config']
                submitted['configurable']['thread_id'] = kwargs['thread_id']
                value = Command(resume=kwargs['command']['resume']) if kwargs['command'] else kwargs['input']
                result = await runtime.ainvoke(value, submitted, context=kwargs['context'])
                yield SimpleNamespace(event='values', data=result)
        graph = RemoteGraph('agent', client=SimpleNamespace(runs=Runs()))
        agent = SimpleNamespace(aget_state=runtime.current.agent.aget_state)
        admission = await remote_idle((agent, 'owner', graph))
        assert admission.turn_id == 'turn-1'
        native_context = dict(context)
        native_context.pop('turn_id')  # Native graph_input path removes this.
        with client_background_wake(app_module.DeepAgentsApp):
            token = _WAKE.set(admission)
            try:
                paused = [v async for v in graph.astream({'messages': []}, config, context=native_context, stream_mode='values')][-1]
                assert paused.get('__interrupt__')
                result = [v async for v in graph.astream(Command(resume={'answers': ['Yes, inspect fixture URL.']}),
                    config, context=native_context, stream_mode='values')][-1]
            finally: _WAKE.reset(token)
        answer = next(m for m in result['messages'] if isinstance(m, ToolMessage) and m.name == 'ask_user')
        assert answer.additional_kwargs.get(ASK_USER_AUTHORIZATION_METADATA_KEY)
        assert classifier.reviews[-1]['same_turn_user_answers']
        assert sum(isinstance(m, HumanMessage) for m in result['messages']) == 1
        assert not runtime.background.pending('owner')
        assert 'turn_id' not in native_context
