import test from 'node:test';
import assert from 'node:assert/strict';
import { readSSE, mergeChunk, pendingInterrupts, textContent, validAnswer } from '../src/protocol.ts';
import { validateResponses } from '../server/app.ts';

test('SSE preserves UTF-8, CRLF and multiline JSON across arbitrary boundaries', async () => {
  const bytes = new TextEncoder().encode(': heartbeat\r\nid: 2\r\nevent: messages\r\ndata: [{"content":"世界"},\r\ndata: {}]\r\n\r\n');
  const events: any[] = [];
  const body = new ReadableStream({ start(controller) { for (const byte of bytes) controller.enqueue(Uint8Array.of(byte)); controller.close(); } });
  await readSSE(new Response(body), e => events.push(e));
  assert.equal(events.length, 1); assert.equal(events[0].event, 'messages'); assert.equal(events[0].id, '2'); assert.deepEqual(events[0].data, [{ content: '世界' }, {}]);
});
test('SSE handles a final event without a trailing separator', async () => {
  const events: any[] = []; await readSSE(new Response('event: custom\ndata: "hello"'), e => events.push(e)); assert.equal(events[0].data, 'hello');
});
test('message accumulation preserves blocks and keeps different IDs separate', () => {
  let messages = mergeChunk([], { id: 'a', content: 'Hello' });
  messages = mergeChunk(messages, { id: 'a', content: [{ type: 'text', text: ' world' }] });
  messages = mergeChunk(messages, { id: 'b', content: 'Next' });
  assert.equal(textContent(messages[0].content), 'Hello world'); assert.equal(messages.length, 2);
  assert.deepEqual(mergeChunk(messages, { content: 'unidentified' }), messages);
});
const interrupt = { id: 'approval', value: { action_requests: [{ name: 'write_file', args: { file_path: 'a.md' } }], review_configs: [{ action_name: 'write_file', allowed_decisions: ['approve', 'reject'] }] } };
test('checkpoint and task copies of an interrupt are deduplicated', () => {
  assert.deepEqual(pendingInterrupts({ values: {}, interrupts: [interrupt], tasks: [{ interrupts: [interrupt] }] }), [interrupt]);
});
test('stale, incomplete, extra and unsupported approval replies are refused', () => {
  const snapshot = { values: {}, interrupts: [interrupt] };
  for (const responses of [{}, { stale: {} }, { approval: { decisions: [] } }, { approval: { decisions: [{ type: 'edit' }] } }, { approval: { decisions: [{ type: 'approve', edited_action: {} }] } }]) assert.throws(() => validateResponses(snapshot, responses));
  assert.doesNotThrow(() => validateResponses(snapshot, { approval: { decisions: [{ type: 'approve' }] } }));
});
test('missing review policy fails closed and hooks are not human approvals', () => {
  assert.throws(() => validateResponses({ values: {}, interrupts: [{ id: 'i', value: { action_requests: [{ name: 'shell' }] } }] }, { i: { decisions: [{ type: 'approve' }] } }));
  assert.throws(() => validateResponses({ values: {}, interrupts: [{ id: 'hook', value: { type: 'hook_invocation' } }] }, { hook: { decisions: [] } }));
});
test('questions require all answers, with exact interrupt identity', () => {
  const snapshot = { values: {}, interrupts: [{ id: 'q', value: { type: 'ask_user', questions: [{ question: 'Where?' }, { question: 'When?' }] } }] };
  assert.throws(() => validateResponses(snapshot, { q: { answers: ['Here'] } }));
  assert.throws(() => validateResponses(snapshot, { q: { answers: ['Here', ' '] } }));
  assert.doesNotThrow(() => validateResponses(snapshot, { q: { answers: ['Here', 'Today'] } }));
});
test('real LangChain tool fragments preserve name, ID and JSON arguments', () => {
  const fragments = [
    { id: 'ai', content: '', tool_calls: [{ name: 'read_file', id: 'call-1', args: {} }], tool_call_chunks: [{ index: 0, name: 'read_file', id: 'call-1', args: '' }] },
    { id: 'ai', content: '', tool_calls: [{ name: '', id: null, args: {} }], tool_call_chunks: [{ index: 0, name: null, id: null, args: '{"file_path":' }] },
    { id: 'ai', content: '', tool_calls: [], tool_call_chunks: [{ index: 0, name: null, id: null, args: '"README.md"}' }] },
  ];
  let messages: any[] = []; for (const part of fragments) messages = mergeChunk(messages, part);
  assert.deepEqual(messages[0].tool_calls, [{ name: 'read_file', id: 'call-1', args: { file_path: 'README.md' }, partial: false }]);
});
test('upstream optional questions and multi-select answers retain exact values', () => {
  const question = { question: 'Pick languages', type: 'multi_select', choices: [{ value: 'A, B' }, { value: 'C"D' }] };
  assert.equal(validAnswer(question, 'A, B'), false);
  assert.equal(validAnswer(question, '[]'), false);
  assert.equal(validAnswer(question, JSON.stringify(['A, B', 'C"D'])), true);
  assert.equal(validAnswer({ ...question, required: false }, '[]'), true);
  assert.equal(validAnswer({ type: 'text', required: false }, ''), true);
  assert.equal(validAnswer({ type: 'multiple_choice' }, 'Custom answer'), true);
});
