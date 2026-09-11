import test from 'node:test';
import assert from 'node:assert/strict';
import { requestGate } from '../server/request-gate.ts';
import { createApp } from '../server/app.ts';

test('handoff closes admission before waiting for already admitted requests', async () => {
  const gate = requestGate();
  let release!: (r: Response) => void;
  const first = gate.run(() => new Promise<Response>(resolve => { release = resolve; }));
  await Promise.resolve();
  let acknowledged = false;
  const paused = gate.pause().then(() => { acknowledged = true; });
  assert.equal((await gate.run(() => { throw new Error('must not run'); })).status, 503);
  assert.equal(acknowledged, false);
  release(new Response('accepted'));
  await first; await paused;
  assert.equal(acknowledged, true);
  gate.resume();
  assert.equal((await gate.run(async () => new Response())).status, 200);
});

test('attached UI exposes only its conversation and rejects other mutations', async () => {
  const origin = 'http://127.0.0.1:8765';
  const calls: string[] = [];
  const app = createApp({ projects: [{ id: 'p', path: '/project', name: 'Project' }], backend: 'http://127.0.0.1:9999', origin, attached: true, initialThread: 'existing', backendHeaders: { authorization: 'Bearer test-only' }, fetch: async (url, init) => {
    const route = new URL(String(url)).pathname; calls.push(route);
    assert.equal(new Headers(init?.headers).get('authorization'), 'Bearer test-only');
    if (route === '/threads/existing') return Response.json({ thread_id: 'existing', metadata: { cwd: '/project' } });
    if (route.endsWith('/workspace')) return Response.json({ workspace: { cwd: '/project' } });
    if (route.endsWith('/state')) return Response.json({ values: {}, next: [] });
    if (route === '/store/items') return Response.json(null);
    throw new Error('Unexpected backend operation: ' + route);
  } });
  const boot = await (await app(new Request(origin + '/api/bootstrap'))).json();
  assert.equal(boot.initialThread, 'existing'); assert.equal(boot.attached, true);
  assert.equal(JSON.stringify(boot).includes('test-only'), false);
  const call = (suffix: string, method = 'GET', body?: unknown) => app(new Request(origin + '/api/projects/p/threads' + suffix, { method, headers: { 'x-workspace-token': boot.token, 'content-type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) }));
  assert.deepEqual((await (await call('')).json()).map((t: any) => t.thread_id), ['existing']);
  const count = calls.length;
  assert.equal((await call('/other/state')).status, 404);
  assert.equal((await call('', 'POST', {})).status, 409);
  assert.equal(calls.length, count);
  assert.equal((await call('/existing/compact', 'POST', {})).status, 409);
  assert.equal((await call('/existing/run', 'POST', { text: 'hello', model: 'deepseek:new' })).status, 409);
});

test('unconfirmed backend mutation prevents a successful handoff acknowledgment', async () => {
  const gate = requestGate();
  await gate.run(async () => { gate.markUncertain(); return Response.json({}, { status: 502 }); });
  await assert.rejects(gate.pause(), /not confirmed/);
  gate.resume();
  await assert.rejects(gate.pause(), /not confirmed/);
});

test('tab disconnect cannot abort attached run admission; transport failure makes return uncertain', async () => {
  const origin = 'http://127.0.0.1:8765';
  for (const fail of [false, true]) {
    const gate = requestGate();
    const browser = new AbortController();
    let admitted!: () => void;
    let reached!: () => void;
    const started = new Promise<void>(resolve => { reached = resolve; });
    const app = createApp({ projects: [{ id: 'p', path: '/project', name: 'Project' }], backend: 'http://127.0.0.1:9999', origin, attached: true, initialThread: 'existing', onUncertain: gate.markUncertain, fetch: async (url, init) => {
      const route = new URL(String(url)).pathname;
      if (route === '/threads/existing') return Response.json({ thread_id: 'existing', metadata: { cwd: '/project', title: 'Named' } });
      if (route.endsWith('/workspace')) return Response.json({ workspace: { cwd: '/project' } });
      if (route.endsWith('/state')) return Response.json({ values: {}, next: [] });
      if (route === '/store/items') return Response.json(null);
      if (route.endsWith('/runs/stream')) {
        reached();
        await new Promise<void>(resolve => { admitted = resolve; });
        assert.equal(init?.signal?.aborted, false);
        if (fail) throw new Error('lost backend connection');
        return new Response('accepted');
      }
      throw new Error('Unexpected backend route');
    } });
    const boot = await (await app(new Request(origin + '/api/bootstrap'))).json();
    const run = gate.run(() => app(new Request(origin + '/api/projects/p/threads/existing/run', { method: 'POST', body: JSON.stringify({ text: 'hello' }), headers: { 'x-workspace-token': boot.token, 'content-type': 'application/json' }, signal: browser.signal })));
    await started;
    browser.abort();
    let acknowledged = false;
    const pause = gate.pause().then(() => { acknowledged = true; });
    await Promise.resolve();
    assert.equal(acknowledged, false);
    admitted();
    await run;
    if (fail) await assert.rejects(pause, /not confirmed/);
    else { await pause; assert.equal(acknowledged, true); }
  }
});

test('native pending proposals and one-shot rubrics are recognized by browser execution', async () => {
  const { hasNativeGoalState } = await import('../server/app.ts');
  for (const state of [{ _pending_goal_objective: 'proposal' }, { _pending_goal_kind: 'create' }, { _pending_goal_request_id: 'id' }, { _pending_goal_rubric: 'criteria' }, { rubric: 'one-shot' }, { goal_criteria_request: {} }]) assert.equal(hasNativeGoalState(state), true);
  assert.equal(hasNativeGoalState({ messages: [], _goal_objective: null }), false);
});

test('new browser conversations inherit CLI approval mode without changing saved conversations', async () => {
  const origin = 'http://127.0.0.1:8765';
  const writes: any[] = [];
  const app = createApp({ projects: [{ id: 'p', path: '/project', name: 'Project' }], backend: 'http://127.0.0.1:9999', origin, initialMode: 'auto', fetch: async (url, init) => {
    const route = new URL(String(url)).pathname;
    if (route === '/store/items') { writes.push(JSON.parse(String(init?.body))); return new Response(null, { status: 204 }); }
    if (route.endsWith('/workspace')) return Response.json({ workspace: { cwd: '/project' } });
    return Response.json({ thread_id: route.split('/').at(-1), metadata: { cwd: '/project' } });
  } });
  const boot = await (await app(new Request(origin + '/api/bootstrap'))).json();
  const headers = { 'x-workspace-token': boot.token, 'content-type': 'application/json' };
  assert.equal((await app(new Request(origin + '/api/projects/p/threads', { method: 'POST', headers, body: '{}' }))).status, 201);
  assert.equal(writes.length, 1); assert.equal(writes[0].value.mode, 'auto');
  await app(new Request(origin + '/api/projects/p/threads/saved', { headers }));
  assert.equal(writes.length, 1);
});

test('standalone history with a deferred goal cannot submit a run', async () => {
  const origin = 'http://127.0.0.1:8765';
  let submitted = false;
  const app = createApp({ projects: [{ id: 'p', path: '/project', name: 'Project' }], backend: 'http://127.0.0.1:9999', origin, fetch: async url => {
    const route = new URL(String(url)).pathname;
    if (route === '/threads/old') return Response.json({ metadata: { cwd: '/project' } });
    if (route.endsWith('/workspace')) return Response.json({ workspace: { cwd: '/project' } });
    if (route.endsWith('/state')) return Response.json({ values: { _pending_goal_objective: 'Draft goal' }, next: [] });
    if (route.endsWith('/runs/stream')) submitted = true;
    return Response.json(null);
  } });
  const boot = await (await app(new Request(origin + '/api/bootstrap'))).json();
  const response = await app(new Request(origin + '/api/projects/p/threads/old/run', { method: 'POST', headers: { 'x-workspace-token': boot.token, 'content-type': 'application/json' }, body: JSON.stringify({ text: 'hello' }) }));
  assert.equal(response.status, 409); assert.equal(submitted, false);
});
