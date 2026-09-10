"""Native background batches share a bounded FIFO scheduler, not a batch lock."""
import asyncio
import json

import pytest
from deepagents_code._fake_models import _ToolBindingFakeModel
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from lc_factory.background import _CURRENT_JOB
from lc_factory.runtime import FactoryRuntime, RuntimeOptions
from test_background_submission import args, call
from test_background_delivery import Recorder


async def until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(.005)


def batch(start, count):
    return AIMessage('', tool_calls=[dict(name='start_background_task',
        args={'description': str(i), 'subagent_type': 'child'}, id=f'submit-{i}', type='tool_call')
        for i in range(start, start + count)])


@pytest.mark.parametrize('mode', ['isolated', 'fork'])
async def test_second_batch_queues_then_starts_fifo_with_cancel_and_retention(tmp_path, mode):
    entered = []
    gates = {str(i): asyncio.Event() for i in range(9)}
    class Gate(AgentMiddleware):
        async def abefore_model(self, state, runtime):
            label = _CURRENT_JOB.get().description
            entered.append(label)
            await gates[label].wait()
    main = Recorder(messages=iter([
        batch(0, 4), AIMessage('First batch'), batch(4, 4), AIMessage('Second batch'),
        AIMessage('Results received'), batch(8, 1), AIMessage('Third batch'),
    ]))
    main.profile = {'max_input_tokens': 1_000_000}
    spec = dict(name='child', description='Child', mode=mode, middleware=[Gate()],
                model=_ToolBindingFakeModel(messages=iter([AIMessage('done')] * 9)))
    config = {'configurable': {'thread_id': 'owner'}}
    async with await FactoryRuntime.create(agent_kwargs=args(tmp_path, main, subagents=[spec]),
        options=RuntimeOptions(background=True), workspace_id='work') as runtime:
        await runtime.ainvoke({'messages': [HumanMessage('First batch')]}, config)
        await until(lambda: len(entered) == 4)
        result = await runtime.ainvoke({'messages': [HumanMessage('Second batch')]}, config)
        handles = [json.loads(m.content) for m in result['messages'] if isinstance(m, ToolMessage)]
        assert len(handles) == 8 and all(handle['ok'] for handle in handles)
        assert [handle['status'] for handle in handles] == ['running'] * 4 + ['queued'] * 4
        tasks = runtime.background
        queued = {j.description: (key, j) for key, j in tasks.jobs.items()}
        assert all(queued[str(i)][1].worker is None for i in range(4, 8))
        await tasks.cancel('owner', task_id=queued['6'][0])
        assert queued['6'][1].status == 'cancelled' and queued['6'][1].graph is None
        gates['0'].set()
        await until(lambda: '4' in entered)
        assert '5' not in entered and '6' not in entered and '7' not in entered
        gates['1'].set()
        await until(lambda: '5' in entered)
        gates['2'].set()
        await until(lambda: '7' in entered)
        assert sum(j.worker is not None and not j.worker.done() for j in tasks.jobs.values()) <= 4
        for gate in gates.values(): gate.set()
        await until(lambda: all(j.result is not None for j in tasks.jobs.values()))
        assert '6' not in entered
        await runtime.ainvoke({'messages': []}, config)
        assert not tasks.pending('owner')
        # New work does not immediately erase acknowledged conversations.
        await runtime.ainvoke({'messages': [HumanMessage('Third batch')]}, config)
        assert len(tasks.jobs) == 9 and queued['0'][0] in tasks.jobs


async def test_approved_resume_queues_behind_running_job_without_replay(tmp_path):
    release = asyncio.Event()
    entered = asyncio.Event()
    class Block(AgentMiddleware):
        async def abefore_model(self, state, runtime):
            entered.set()
            await release.wait()
    target = tmp_path / 'approved.txt'
    protected = dict(name='protected', description='Protected', model=_ToolBindingFakeModel(messages=iter([
        call('write_file', {'file_path': str(target), 'content': 'approved'}, 'write'), AIMessage('done')
    ])))
    blocked = dict(name='blocked', description='Blocked', middleware=[Block()],
                   model=_ToolBindingFakeModel(messages=iter([AIMessage('done')])))
    model = _ToolBindingFakeModel(messages=iter([
        call('start_background_task', {'description': 'Protected', 'subagent_type': 'protected'}, 'one'),
        AIMessage('Paused child'),
        call('start_background_task', {'description': 'Block', 'subagent_type': 'blocked'}, 'two'), AIMessage('Running child'),
    ]))
    # Main submission auto-approved, protected child uses its explicit interrupt policy.
    from langchain.agents.middleware import HumanInTheLoopMiddleware
    class FixtureReview(HumanInTheLoopMiddleware): pass
    protected['middleware'] = [FixtureReview(interrupt_on={'write_file': True})]
    config = {'configurable': {'thread_id': 'owner'}}
    async with await FactoryRuntime.create(agent_kwargs=args(tmp_path, model, subagents=[protected, blocked]),
        options=RuntimeOptions(background=True), workspace_id='work') as runtime:
        tasks = runtime.background
        tasks.max_running = 1
        await runtime.ainvoke({'messages': [HumanMessage('One')]}, config)
        await until(lambda: any(j.status == 'needs_approval' and j.worker.done() for j in tasks.jobs.values()))
        key, paused = next(iter(tasks.jobs.items()))
        await runtime.ainvoke({'messages': [HumanMessage('Two')]}, config)
        await asyncio.wait_for(entered.wait(), 5)
        responses = {i['id']: {'decisions': [{'type': 'approve'}]} for i in paused.interrupts}
        tasks.resume('owner', key, responses)
        assert paused.status == 'queued' and not paused.interrupts and not target.exists()
        with pytest.raises(ValueError): tasks.resume('owner', key, responses)
        release.set()
        await until(lambda: paused.status == 'completed')
        assert target.read_text() == 'approved'


async def test_close_cancels_queue_without_starting_it(tmp_path):
    entered = []
    class Gate(AgentMiddleware):
        async def abefore_model(self, state, runtime):
            entered.append(_CURRENT_JOB.get().description)
            await asyncio.Event().wait()
    spec = dict(name='child', description='Child', middleware=[Gate()],
                model=_ToolBindingFakeModel(messages=iter([])))
    model = _ToolBindingFakeModel(messages=iter([batch(0, 3), AIMessage('done')]))
    runtime = await FactoryRuntime.create(agent_kwargs=args(tmp_path, model, subagents=[spec]),
        options=RuntimeOptions(background=True), workspace_id='work')
    runtime.background.max_running = 1
    await runtime.ainvoke({'messages': [HumanMessage('Start')]}, {'configurable': {'thread_id': 'owner'}})
    await until(lambda: bool(entered))
    await runtime.close()
    assert entered == ['0']
    assert all(j.status == 'cancelled' and j.graph is None and j.transcript.closed for j in runtime.background.jobs.values())
