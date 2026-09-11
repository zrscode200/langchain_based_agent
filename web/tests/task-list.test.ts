import test from 'node:test';
import assert from 'node:assert/strict';
import type { Task } from '../src/protocol.ts';
import { deliveryState, describeTask, formatAge, formatDuration, groupTasks, ordinal, stateSentence, summaryLine, timeContext } from '../src/task-list.ts';

const task = (overrides: Partial<Task>): Task => ({ task_id: 'id', name: 'Task', description: '', status: 'running', interrupts: [], steerable: false, ...overrides });

test('groups put attention first, keep queue order and list finished work newest first', () => {
  const rows = [task({ task_id: 'old', status: 'completed', finished_at: 100 }), task({ task_id: 'q2', status: 'queued', queue_position: 2 }), task({ task_id: 'ask', status: 'needs_input' }), task({ task_id: 'run', status: 'running' }), task({ task_id: 'q1', status: 'queued', queue_position: 1 }), task({ task_id: 'new', status: 'failed', finished_at: 200 })];
  assert.deepEqual(groupTasks(rows).map(g => [g.key, g.tasks.map(t => t.task_id)]), [['attention', ['ask']], ['running', ['run']], ['queued', ['q1', 'q2']], ['finished', ['new', 'old']]]);
  // Older backends send no timestamps; the newest submission still comes first.
  assert.deepEqual(groupTasks([task({ task_id: 'a', status: 'completed' }), task({ task_id: 'b', status: 'cancelled' })])[3].tasks.map(t => t.task_id), ['b', 'a']);
});

test('summary line counts states in priority order', () => {
  assert.equal(summaryLine([task({ status: 'running' }), task({ status: 'needs_approval' }), task({ status: 'queued' }), task({ status: 'completed' }), task({ status: 'running' })]), '1 needs you · 2 running · 1 queued');
  assert.equal(summaryLine([task({ status: 'completed' })]), '');
  assert.equal(summaryLine([]), '');
});

test('descriptions say what a task is doing now or how it ended', () => {
  assert.equal(describeTask(task({ status: 'needs_approval', interrupts: [{ id: 'p', value: { action_requests: [{ name: 'write_file', args: {} }] } }] })), 'Waiting for your approval · write_file');
  assert.equal(describeTask(task({ status: 'needs_input' })), 'Waiting for your answer');
  assert.equal(describeTask(task({ status: 'queued', queue_position: 3 })), '3rd in line for a free slot');
  assert.equal(describeTask(task({ status: 'queued' })), 'Waiting for a free slot');
  assert.equal(describeTask(task({ status: 'running', latest: { kind: 'tool', tool_name: 'read_file', status: 'completed' } })), 'read_file · completed');
  assert.equal(describeTask(task({ status: 'running', latest: { kind: 'finding', text: 'First line\nSecond line' } })), 'First line');
  assert.equal(describeTask(task({ status: 'running' })), 'Working…');
  assert.equal(describeTask(task({ status: 'completed', result: '\n\nSummary of the result\nDetails' })), 'Summary of the result');
  assert.equal(describeTask(task({ status: 'failed', result: 'x'.repeat(200) })).length, 120);
  assert.equal(describeTask(task({ status: 'cancelled' })), 'Cancelled');
});

test('delivery state follows the unread result list', () => {
  assert.equal(deliveryState(task({ task_id: 'a', status: 'completed' }), ['a']), 'waiting');
  assert.equal(deliveryState(task({ task_id: 'a', status: 'failed' }), []), 'delivered');
  assert.equal(deliveryState(task({ task_id: 'a', status: 'running' }), ['a']), null);
});

test('task acknowledgement wins when the list and detail polls arrive in a different order', () => {
  assert.equal(deliveryState(task({ status: 'completed', acknowledged: false }), []), 'waiting');
  assert.equal(deliveryState(task({ status: 'failed', acknowledged: true }), ['id']), 'delivered');
  assert.equal(stateSentence(task({ status: 'completed', acknowledged: false }), Date.now(), []), 'Completed · waiting for the agent');
});

test('time context and detail sentence use server timestamps when present', () => {
  const now = 1_000_000_000;
  assert.equal(timeContext(task({ status: 'running', started_at: (now - 125_000) / 1000 }), now), '2m 5s');
  assert.equal(timeContext(task({ status: 'queued', updated_at: (now - 30_000) / 1000 }), now), 'waiting 30s');
  assert.equal(timeContext(task({ status: 'needs_approval', updated_at: (now - 3_600_000) / 1000 }), now), 'waiting 1h');
  assert.equal(timeContext(task({ status: 'completed', finished_at: (now - 120_000) / 1000 }), now), '2m ago');
  assert.equal(timeContext(task({ status: 'completed' }), now), '');
  assert.equal(stateSentence(task({ status: 'running', started_at: (now - 5_000) / 1000 }), now, []), 'Running for 5s');
  assert.equal(stateSentence(task({ task_id: 'a', status: 'completed', finished_at: (now - 10_000) / 1000 }), now, ['a']), 'Completed just now · waiting for the agent');
  assert.equal(stateSentence(task({ status: 'queued', queue_position: 1 }), now, []), '1st in line for a free slot');
  assert.equal(stateSentence(task({ status: 'running' }), now, []), 'Running');
});

test('duration, age and ordinal formatting', () => {
  assert.equal(formatDuration(-5), '0s');
  assert.equal(formatDuration(59_000), '59s');
  assert.equal(formatDuration(3_600_000 + 4 * 60_000), '1h 4m');
  assert.equal(formatAge(10_000), 'just now');
  assert.equal(formatAge(90_000), '2m ago');
  assert.equal(formatAge(2 * 86_400_000), '2d ago');
  assert.deepEqual([1, 2, 3, 4, 11, 12, 13, 21, 22, 111].map(ordinal), ['1st', '2nd', '3rd', '4th', '11th', '12th', '13th', '21st', '22nd', '111th']);
});
