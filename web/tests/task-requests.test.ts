import test from 'node:test';
import assert from 'node:assert/strict';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { responseMatches, sameTaskRequest, taskRequestKey, visibleTaskInterrupts, waitingTasks } from '../src/task-requests.ts';
import { mergeTaskMessages } from '../src/task-conversation.ts';
import { Attention } from '../src/attention.tsx';
import { Conversation } from '../src/conversation.tsx';
import type { Task, Message } from '../src/protocol.ts';

const task: Task = { task_id: 'child-1', name: 'Researcher', description: 'Research', status: 'needs_approval', steerable: true,
  interrupts: [{ id: 'pause-1', value: { action_requests: [{ name: 'write_file', args: { file_path: 'notes.md', content: '# Notes' } }], review_configs: [{ action_name: 'write_file', allowed_decisions: ['approve', 'reject'] }] } }] };
test('pending responses are scoped to exact task and pause contents, including every interrupt', () => {
  assert.equal(sameTaskRequest([task], task), true);
  for (const changed of [{ ...task, task_id: 'child-2' }, { ...task, status: 'running' }, { ...task, interrupts: [{ ...task.interrupts[0], id: 'pause-2' }] }, { ...task, interrupts: [{ id: 'pause-1', value: { ...task.interrupts[0].value, action_requests: [] } }] }]) assert.equal(sameTaskRequest([changed], task), false);
  assert.equal(responseMatches(task.interrupts, { 'pause-1': {} }), true);
  assert.equal(responseMatches(task.interrupts, { 'pause-1': {}, 'other-pause': {} }), false);
  assert.equal(responseMatches(task.interrupts, {}), false);
  assert.equal(waitingTasks([{ ...task, interrupts: [] }]).length, 0);
  assert.notEqual(taskRequestKey(task), taskRequestKey({ ...task, task_id: 'other' }));
});
test('unsupported hook transport stays out of visible cards and receipt identities', () => {
  const raw = [{ id: 'hook', value: { type: 'hook_invocation', request: { private: 'SECRET' } } }];
  assert.equal(JSON.stringify(visibleTaskInterrupts(raw)).includes('SECRET'), false);
  assert.equal(taskRequestKey({ task_id: 'child', interrupts: raw }).includes('SECRET'), false);
  const ask = [{ id: 'question', value: { type: 'ask_user', questions: [{ question: 'Choose', type: 'text' }] } }];
  assert.deepEqual(visibleTaskInterrupts(ask), ask);
});
test('main attention owns subagent actions without disabling them during the parent run', () => {
  const props = { requests: { pending: [task], submit: async () => {}, receipts: [], notice: '' }, interrupts: [], running: true, atBottom: true, selected: null, select: () => {}, inspect: () => {}, submitMain: async () => {} };
  const html = renderToStaticMarkup(createElement(Attention, props));
  assert.match(html, /Researcher/); assert.match(html, /Approve this action/); assert.match(html, /View task context/);
  assert.doesNotMatch(html, /disabled=""/);
  const reading = renderToStaticMarkup(createElement(Attention, { ...props, atBottom: false }));
  assert.match(reading, /Needs your attention/); assert.doesNotMatch(reading, /Approve this action/);
  const multiple = renderToStaticMarkup(createElement(Attention, { ...props, requests: { ...props.requests, pending: [task, { ...task, task_id: 'child-2', name: 'Reviewer' }] } }));
  assert.match(multiple, /Reviewer/); assert.doesNotMatch(multiple, /Approve this action/);
});
test('conversation keeps task ownership labels and waiting context without duplicate approval controls', () => {
  const html = renderToStaticMarkup(createElement(Conversation, { messages: [{ type: 'human', content: 'Assignment', id: 'u' }, { type: 'ai', content: 'Working', id: 'a' }], interrupts: task.interrupts, showRequests: false, userLabel: 'Assignment / guidance', agentLabel: task.name, submit: async () => {} }));
  assert.match(html, /Researcher/); assert.match(html, /Assignment \/ guidance/); assert.doesNotMatch(html, /Approve this action/);
});
test('structured task messages merge revisions without duplication or undoing newer observations', () => {
  const record = (id: string, order: number, revision: number, content: string): Message => ({ id, content, _transcript: { order, revision, truncated: false } });
  const old = [record('b', 1, 2, 'Old B'), record('c', 2, 3, 'C')];
  const updated = mergeTaskMessages(old, [record('b', 1, 5, 'New B'), record('a', 0, 4, 'A'), record('d', 3, 6, 'D')], 1);
  assert.deepEqual(updated.map(m => m.content), ['New B', 'C', 'D']);
  assert.deepEqual(mergeTaskMessages(updated, [record('b', 1, 2, 'Stale B'), record('a', 0, 4, 'A')]).map(m => m.content), ['A', 'New B', 'C', 'D']);
});

test('submission cannot resume a different task, stale pause, or navigated-away conversation', async () => {
  const { submitTaskResponse } = await import('../src/task-requests.ts');
  const sent: unknown[] = [];
  const responses = { 'pause-1': { decisions: [{ type: 'approve' }] } };
  let current = true;
  const transport = { current: () => current, list: async () => [task], resume: async (id: string, payload: unknown) => { sent.push([id, payload]); } };
  await submitTaskResponse(task, responses, transport);
  assert.deepEqual(sent, [[task.task_id, responses]]);
  await assert.rejects(submitTaskResponse(task, responses, { ...transport, list: async () => [{ ...task, task_id: 'other' }] }), /changed/);
  await assert.rejects(submitTaskResponse(task, responses, { ...transport, list: async () => [{ ...task, status: 'running' }] }), /changed/);
  await assert.rejects(submitTaskResponse(task, { other: {} }, transport), /every request/);
  await assert.rejects(submitTaskResponse(task, responses, { ...transport, list: async () => { current = false; return [task]; } }), /conversation changed/);
  assert.equal(sent.length, 1);
});

test('history reads wait for in-flight live updates and failures do not wedge the queue', async () => {
  const { TaskReadQueue } = await import('../src/task-conversation.ts');
  const queue = new TaskReadQueue();
  let release!: () => void, revision = 21;
  const wait = new Promise<void>(resolve => { release = resolve; });
  const live = queue.read(async () => { await wait; revision = 41; });
  const older = queue.read(async () => revision);
  assert.equal(queue.pending, 2);
  release(); await live;
  assert.equal(await older, 41);
  assert.equal(queue.pending, 0);
  await assert.rejects(queue.read(async () => { throw new Error('Unavailable'); }));
  assert.equal(await queue.read(async () => 'Recovered'), 'Recovered');
});
test('final-result dedup leaves the shared reasoning disclosure available', async () => {
  const { Reasoning } = await import('../src/conversation.tsx');
  const final = { id: 'final', type: 'ai', content: 'Completed result', additional_kwargs: { reasoning_content: 'Provider-exposed final reasoning' } };
  const html = renderToStaticMarkup(createElement(Reasoning, { message: final, live: false }));
  assert.match(html, /Reasoning/); assert.match(html, /aria-expanded="false"/);
  assert.doesNotMatch(html, /Completed result/);
});
