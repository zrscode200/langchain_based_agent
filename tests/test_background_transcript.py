"""Host conversation capture retains complete text without exposing transport metadata."""
import asyncio
from types import SimpleNamespace

import pytest
from deepagents_code._fake_models import _ToolBindingFakeModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from textual.widgets import Button, Static

from lc_factory.background import BackgroundTasks, Job
from lc_factory.background_transcript import ChildTranscript, PAGE_CHARS, message_text
from lc_factory.background_activity import conversation_text
from lc_factory.runtime import FactoryRuntime, RuntimeOptions
from test_background_submission import args, call
from test_background_activity import ActivityAgent, active_job, open_activity
from test_background_ui import Harness


def all_text(transcript):
    return ''.join(transcript.page(i)['text'] for i in range(len(transcript.pages)))


def test_complete_unicode_tool_content_paged_deduplicated_and_spooled():
    transcript = ChildTranscript()
    content = '大完整🙂' * 110_000 + 'THE_END'
    message = ToolMessage(content, tool_call_id='call', name='read_file', id='tool-message')
    try:
        transcript.capture([message])
        transcript.capture([message])
        assert transcript.file._rolled
        text = all_text(transcript)
        assert content in text and text.count('THE_END') == 1
        assert all(len(transcript.page(i)['text']) <= PAGE_CHARS for i in range(len(transcript.pages)))
        assert transcript.page(-1)['text'].endswith('THE_END')
    finally:
        transcript.close()
    assert transcript.file.closed


def test_known_reasoning_visible_opaque_metadata_and_hooks_absent():
    transcript = ChildTranscript()
    message = AIMessage(content=[
        {'type': 'thinking', 'thinking': 'Visible thought', 'signature': 'OPAQUE_SIGNATURE'},
        {'type': 'reasoning', 'summary': [{'type': 'summary_text', 'text': 'Visible summary'}], 'encrypted_content': 'OPAQUE_ENCRYPTED'},
        {'type': 'text', 'text': 'Visible answer'},
    ], additional_kwargs={'reasoning_content': 'DeepSeek reasoning', 'hook_invocation': 'PRIVATE_HOOK', 'secret': 'PRIVATE_METADATA'},
    response_metadata={'private': 'PRIVATE_RESPONSE'}, id='answer')
    try:
        transcript.capture([message])
        text = all_text(transcript)
        assert all(value in text for value in ('Visible thought', 'Visible summary', 'Visible answer', 'DeepSeek reasoning'))
        assert 'PRIVATE_' not in text and 'OPAQUE_' not in text
        assert 'provider-exposed' in text
    finally: transcript.close()


def test_retention_limit_is_explicit_and_never_silently_truncates_a_message():
    transcript = ChildTranscript(max_bytes=100)
    try:
        transcript.capture([AIMessage('kept', id='one'), AIMessage('x' * 200, id='two')])
        value = transcript.page()
        assert 'kept' in value['text'] and 'x' not in value['text']
        assert value['limited'] and 'later messages were not retained' in value['notice']
        for invalid in (-2, True, '0', 3):
            with pytest.raises(ValueError): transcript.page(invalid)
    finally: transcript.close()


@pytest.mark.parametrize('mode', ['isolated', 'fork'])
async def test_real_child_chat_tools_reasoning_and_final_survive_completion(tmp_path, mode):
    entered, release = asyncio.Event(), asyncio.Event()
    long_output = 'FULL_TOOL_RESULT_' + 'q' * 30_000 + '_TOOL_END'
    @tool
    async def evidence(question: str) -> str:
        """Return local fixture evidence."""
        entered.set()
        await release.wait()
        return long_output
    child = dict(name='child', description='Child', mode=mode, tools=[evidence],
        model=_ToolBindingFakeModel(messages=iter([
            AIMessage('I will inspect the source.', additional_kwargs={'reasoning_content': 'Provider-visible planning'},
                      tool_calls=[{'name': 'evidence', 'args': {'question': 'COMPLETE_ARGUMENT'}, 'id': 'evidence', 'type': 'tool_call'}]),
            AIMessage('CHILD_FINAL_ANSWER'),
        ])))
    child['model'].profile = {'max_input_tokens': 1_000_000}
    main = _ToolBindingFakeModel(messages=iter([
        call('start_background_task', {'description': 'FULL_ASSIGNMENT', 'subagent_type': 'child'}, 'start'), AIMessage('Parent idle'),
    ]))
    main.profile = {'max_input_tokens': 1_000_000}
    async with await FactoryRuntime.create(agent_kwargs=args(tmp_path, main, subagents=[child]),
        options=RuntimeOptions(background=True), workspace_id='work') as runtime:
        await runtime.ainvoke({'messages': [HumanMessage('PARENT_USER_CONTEXT')]}, {'configurable': {'thread_id': 'owner'}})
        await asyncio.wait_for(entered.wait(), 5)
        key, job = next(iter(runtime.background.jobs.items()))
        live = all_text(job.transcript)
        assert 'I will inspect' in live and 'Provider-visible planning' in live and 'COMPLETE_ARGUMENT' in live
        assert 'CHILD_FINAL_ANSWER' not in live
        release.set()
        await asyncio.wait_for(runtime.background.wait('owner'), 5)
        assert job.status == 'completed' and job.graph is None
        complete = all_text(job.transcript)
        assert all(value in complete for value in ('FULL_ASSIGNMENT', long_output, 'CHILD_FINAL_ANSWER'))
        assert ('PARENT_USER_CONTEXT' in complete) == (mode == 'fork')
        assert complete.count('CHILD_FINAL_ANSWER') == 1
        assert 'conversation' not in runtime.background.inspect('owner', key)
        assert 'conversation' not in runtime.background.list('owner')[0]
        assert runtime.background.inspect('owner', key, transcript_page=-1)['conversation']['pages'] >= 2
        with pytest.raises(ValueError): runtime.background.inspect('other', key, transcript_page=0)
        assert runtime.background.pending('owner')  # Host inspection doesn't consume it.


@pytest.mark.parametrize('size', [(40, 32), (80, 32), (120, 40)])
async def test_conversation_detail_paging_activity_toggle_and_literal_rendering(size):
    transcript = ChildTranscript()
    transcript.capture([HumanMessage('Assignment'), AIMessage('[bold]literal[/bold]\x1b' + 'a' * 30_000 + 'FINAL_CHAT')])
    class TranscriptAgent(ActivityAgent):
        async def post(self, path, json):
            result = await super().post(path, json)
            if json['operation'] == 'inspect':
                result['task']['conversation'] = transcript.page(json.get('transcript_page', -1))
            return result
    app = Harness(TranscriptAgent([active_job('completed')]))
    try:
        async with app.run_test(size=size) as pilot:
            _, screen = await open_activity(app, pilot)
            assert 'FINAL_CHAT' in str(screen.query_one('#activity-body', Static).render())
            assert not screen.query_one('#conversation-previous', Button).disabled
            await screen.handle_button(Button.Pressed(screen.query_one('#conversation-previous', Button)))
            assert screen.page == 0
            text = str(screen.query_one('#activity-body', Static).render())
            assert '[bold]literal[/bold]' in text and '\\x1b' in text
            await screen.handle_button(Button.Pressed(screen.query_one('#conversation-next', Button)))
            assert 'FINAL_CHAT' in str(screen.query_one('#activity-body', Static).render())
            await screen.handle_button(Button.Pressed(screen.query_one('#conversation-toggle', Button)))
            assert 'Found the relevant section' in str(screen.query_one('#activity-body', Static).render())
            await screen.handle_button(Button.Pressed(screen.query_one('#conversation-toggle', Button)))
            assert 'FINAL_CHAT' in str(screen.query_one('#activity-body', Static).render())
            assert screen.query_one('#activity-review', Button).disabled
    finally: transcript.close()


def test_observation_storage_error_does_not_fail_task_or_hide_capture_gap(monkeypatch):
    transcript = ChildTranscript()
    try:
        transcript.capture([AIMessage('Already retained', id='one')])
        def unavailable(*args): raise OSError('private storage detail')
        monkeypatch.setattr(transcript.file, 'write', unavailable)
        transcript.capture([AIMessage('New content', id='two')])
        transcript.capture([AIMessage('Later content', id='three')])
        page = transcript.page()
        assert 'Already retained' in page['text'] and 'New content' not in page['text']
        assert 'storage failed' in page['notice'] and 'private storage detail' not in str(page)
    finally: transcript.close()


def test_tool_result_identity_survives_assigned_id_and_preserves_distinct_calls():
    transcript = ChildTranscript()
    try:
        first = ToolMessage('IDENTICAL_RESULT', name='evidence', tool_call_id='call-one')
        second = ToolMessage('IDENTICAL_RESULT', name='evidence', tool_call_id='call-two')
        transcript.capture([second, first])  # Parallel completion may be out of order.
        transcript.capture([first.model_copy(update={'id': 'saved-one'}), second.model_copy(update={'id': 'saved-two'})])
        text = all_text(transcript)
        assert text.count('IDENTICAL_RESULT') == 2
        assert 'Tool result · evidence · call-one · success' in text
        assert 'Tool result · evidence · call-two · success' in text
        transcript.capture([first.model_copy(update={'id': 'saved-one', 'content': 'REVISED_RESULT'})])
        assert 'Updated message' in all_text(transcript) and 'REVISED_RESULT' in all_text(transcript)
    finally: transcript.close()


async def test_real_tool_result_is_captured_once_across_checkpoint_id_assignment(tmp_path):
    @tool
    async def evidence() -> str:
        """Return one local fixture result."""
        return 'UNIQUE_TOOL_RESULT_MARKER'
    child = dict(name='child', description='Child', tools=[evidence], model=_ToolBindingFakeModel(messages=iter([
        call('evidence', {}, 'evidence'), AIMessage('CHILD_FINAL'),
    ])))
    main = _ToolBindingFakeModel(messages=iter([
        call('start_background_task', {'description': 'Work', 'subagent_type': 'child'}, 'start'), AIMessage('Parent idle'),
    ]))
    async with await FactoryRuntime.create(agent_kwargs=args(tmp_path, main, subagents=[child]),
        options=RuntimeOptions(background=True), workspace_id='work') as runtime:
        await runtime.ainvoke({'messages': [HumanMessage('Start')]}, {'configurable': {'thread_id': 'owner'}})
        await asyncio.wait_for(runtime.background.wait('owner'), 5)
        transcript = next(iter(runtime.background.jobs.values())).transcript
        assert all_text(transcript).count('UNIQUE_TOOL_RESULT_MARKER') == 1


async def test_owner_change_cannot_restore_old_conversation_by_toggling_view():
    class TranscriptAgent(ActivityAgent):
        async def post(self, path, json):
            result = await super().post(path, json)
            if json['operation'] == 'inspect':
                result['task']['conversation'] = {'text': 'PREVIOUS_OWNER_CHAT', 'page': 0, 'pages': 1, 'notice': ''}
            return result
    app = Harness(TranscriptAgent([active_job('completed')]))
    async with app.run_test() as pilot:
        _, screen = await open_activity(app, pilot)
        assert 'PREVIOUS_OWNER_CHAT' in str(screen.query_one('#activity-body', Static).render())
        app._lc_thread_id = 'other-owner'
        await screen.refresh_task()
        await screen.handle_button(Button.Pressed(screen.query_one('#conversation-toggle', Button)))
        body = str(screen.query_one('#activity-body', Static).render())
        assert 'Conversation changed' in body and 'PREVIOUS_OWNER_CHAT' not in body
        assert all(button.disabled for button in screen.query('#conversation-navigation Button'))


def test_structured_messages_replace_revisions_and_keep_call_identity():
    transcript = ChildTranscript()
    try:
        transcript.capture([HumanMessage('Assignment', id='user'), AIMessage('Working', id='ai',
            tool_calls=[{'name': 'read_file', 'id': 'read-one', 'args': {'path': 'notes.md'}, 'type': 'tool_call'}])])
        initial = transcript.structured()
        assert [m['type'] for m in initial['messages']] == ['human', 'ai']
        transcript.capture([ToolMessage('Result', tool_call_id='read-one', name='read_file')])
        first = transcript.structured(after=initial['cursor'])
        transcript.capture([ToolMessage('Result', id='assigned-id', tool_call_id='read-one', name='read_file')])
        assert transcript.structured(after=first['cursor'])['messages'] == []
        transcript.capture([ToolMessage('Revised', id='assigned-id', tool_call_id='read-one', name='read_file', status='error', artifact={'exit_code': 2, 'private': 'SECRET'})])
        revised = transcript.structured(after=first['cursor'])['messages'][0]
        assert revised['id'] == first['messages'][0]['id']
        assert revised['_transcript']['order'] == first['messages'][0]['_transcript']['order']
        assert revised['content'] == 'Revised' and revised['artifact'] == {'exit_code': 2}
        assert 'SECRET' not in str(transcript.structured())
    finally: transcript.close()
    assert transcript.records_file.closed


def test_structured_history_and_change_cursors_have_no_gaps():
    transcript = ChildTranscript()
    try:
        transcript.capture([AIMessage(f'Message {i}', id=str(i)) for i in range(40)])
        latest = transcript.structured()
        assert len(latest['messages']) == 12 and latest['before'] == 28
        middle = transcript.structured(before=latest['before'])
        assert [m['_transcript']['order'] for m in middle['messages']] == list(range(16, 28))
        cursor = latest['cursor']
        transcript.capture([AIMessage('Changed first', id='0'), *[AIMessage(f'New {i}', id=f'new{i}') for i in range(15)]])
        first = transcript.structured(after=cursor)
        second = transcript.structured(after=first['cursor'])
        assert len(first['messages']) == 12 and len(second['messages']) == 4
        assert first['messages'][0]['_transcript']['order'] == 0
        assert transcript.structured(after=second['cursor'])['messages'] == []
        for kwargs in ({'before': True}, {'after': -1}, {'after': '0'}, {'before': 1, 'after': 1}, {'after': 99999}):
            with pytest.raises(ValueError): transcript.structured(**kwargs)
    finally: transcript.close()


def test_structured_large_unicode_message_has_bounded_preview_and_complete_pages():
    transcript = ChildTranscript()
    try:
        message = AIMessage(content=[{'type': 'reasoning', 'reasoning': 'Known thought', 'encrypted_content': 'PRIVATE_OPAQUE'},
            {'type': 'text', 'text': '🙂全文' * 25000}], additional_kwargs={'secret': 'PRIVATE_TRANSPORT'}, id='long')
        transcript.capture([message])
        preview = transcript.structured()['messages'][0]
        assert preview['_transcript']['truncated'] and len(preview['content']) < 6100
        assert preview['additional_kwargs']['reasoning_content'] == 'Known thought'
        offset, text = 0, ''
        while True:
            page = transcript.record_page(preview['id'], offset=offset, revision=preview['_transcript']['revision'])
            text += page['text']
            if page['next'] == page['total']: break
            offset = page['next']
        import json
        assert json.loads(text)['content'] == '🙂全文' * 25000
        assert 'PRIVATE_' not in text
        assert transcript.size + transcript.records_size <= transcript.max_bytes
        transcript.capture([message.model_copy(update={'content': 'Changed'})])
        with pytest.raises(ValueError, match='changed'):
            transcript.record_page(preview['id'], revision=preview['_transcript']['revision'])
    finally: transcript.close()
