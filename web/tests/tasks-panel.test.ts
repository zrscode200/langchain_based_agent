import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { FinishedRows, TasksPanel } from '../src/tasks-panel.tsx';
import type { Agent } from '../src/useAgent.ts';
import type { TaskRequests } from '../src/useTaskRequests.ts';
import type { Task } from '../src/protocol.ts';

const now = Date.now() / 1000;
const task = (overrides: Partial<Task>): Task => ({ task_id: 'id', name: 'Task', description: 'd', status: 'running', interrupts: [], steerable: false, ...overrides });
const agentWith = (tasks: Task[], extra: Record<string, unknown> = {}) => ({ tasks, pendingResults: [], capacity: null, status: 'ready', base: '/projects/p/threads/t', threadId: 't', projectId: 'p', ...extra }) as unknown as Agent;
const requestsWith = (pending: Task[]) => ({ pending, submit: async () => {}, receipts: [], notice: '' }) as unknown as TaskRequests;
const render = (agent: Agent, requests: TaskRequests) => renderToStaticMarkup(createElement(TasksPanel, { agent, compose: () => {}, selected: '', setSelected: () => {}, requests, review: () => {}, expanded: false, setExpanded: () => {} }));
const done = task({ task_id: 'done', name: 'Finished job', status: 'completed', result: 'Summary line', finished_at: now - 600 });
const failed = task({ task_id: 'bad', name: 'Failed job', status: 'failed', result: 'Background task failed before returning a result.', finished_at: now - 60 });

test('live list groups by need, explains each row and keeps finished work behind its dropdown', () => {
  const waiting = task({ task_id: 'ask', name: 'Approve write', status: 'needs_approval', updated_at: now - 60, interrupts: [{ id: 'p', value: { action_requests: [{ name: 'write_file', args: {} }] } }] });
  const sent = task({ task_id: 'sent', name: 'Sent decision', status: 'needs_approval', interrupts: [{ id: 'q', value: { action_requests: [{ name: 'execute', args: {} }] } }] });
  const running = task({ task_id: 'run', name: 'Trace memory', status: 'running', started_at: now - 125, latest: { kind: 'tool', tool_name: 'read_file', status: 'completed' } });
  const queued = task({ task_id: 'q', name: 'Queued job', status: 'queued', queue_position: 2 });
  const html = render(agentWith([done, queued, sent, running, waiting, failed], { pendingResults: ['done'], capacity: { running: 1, max_running: 4, queued: 1, retained: 6, max_jobs: 128 } }), requestsWith([waiting]));
  const order = ['Needs you', 'Running', 'Queued'].map(heading => html.indexOf(heading));
  assert.ok(order.every((index, n) => index >= 0 && (n === 0 || index > order[n - 1])), order.join());
  assert.match(html, /2 need you · 1 running · 1 queued/);
  assert.match(html, /class="task-group task-group-attention"/);
  assert.ok([...html.matchAll(/class="([^"]+)"/g)].every(match => !match[1].split(' ').includes('attention')));
  assert.match(html, /1 of 4 server slots busy · 1 in line/);
  assert.match(html, /Waiting for your approval · write_file/);
  assert.match(html, /Review in main chat/);
  assert.match(html, /Decision sent/);
  assert.match(html, /read_file · completed/);
  assert.match(html, /2nd in line for a free slot/);
  assert.match(html, /1 finished task the agent has not read yet/);
  assert.match(html, /class="finished-toggle" aria-expanded="false"[^>]*>Finished<span class="task-group-count">2<\/span><i class="unread-dot"/);
  assert.doesNotMatch(html, /Finished job|Failed job|finished-menu/);
});

test('task attention modifiers do not inherit the composer attention panel layout', () => {
  const app = readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8');
  const css = readFileSync(new URL('../src/tasks.css', import.meta.url), 'utf8');
  assert.match(app, /taskAttention \? ' needs-attention'/);
  assert.doesNotMatch(app, /taskAttention \? ' attention'/);
  assert.match(css, /\.task-group\.task-group-attention /);
  assert.match(css, /\.toolbar-button\.needs-attention\{/);
  assert.doesNotMatch(css, /\.(?:task-group|toolbar-button)\.attention\b/);
});

test('finished rows list newest first with result summary and delivery state', () => {
  const html = renderToStaticMarkup(createElement(FinishedRows, { tasks: [failed, done], now: now * 1000, pendingResults: ['bad'], open: () => {} }));
  assert.ok(html.indexOf('Failed job') < html.indexOf('Finished job'));
  assert.match(html, /Summary line/);
  assert.match(html, /Waiting for the agent/);
  assert.match(html, /Delivered to the agent/);
  assert.match(html, /10m ago/);
  assert.match(renderToStaticMarkup(createElement(FinishedRows, { tasks: [], now: 0, pendingResults: [], open: () => {} })), /No finished tasks/);
});

test('quiet, empty and disconnected states explain themselves', () => {
  const quiet = render(agentWith([done, failed]), requestsWith([]));
  assert.match(quiet, /Nothing running right now/);
  assert.match(quiet, /All quiet/);
  assert.match(quiet, /2 finished tasks are in the Finished list/);
  assert.match(quiet, /Show finished tasks/);
  assert.doesNotMatch(quiet, /Finished job/);
  assert.match(render(agentWith([]), requestsWith([])), /No background tasks yet/);
  assert.match(render(agentWith([task({ status: 'running' })], { status: 'disconnected' }), requestsWith([])), /Connection interrupted/);
});
