import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, symlink, realpath } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { createApp } from '../server/app.ts';
import { confinedPath, listFiles, readFile } from '../server/files.ts';

const origin = 'http://127.0.0.1:3100';
const workspace = { cwd: '/work/project', workspace_id: 'w', schema_version: 2, generation: 3, project_root: '/work/project', resource_key: 'r', config_fingerprint: 'f' };
async function setup(overrides: Record<string, any> = {}) {
  const calls: { path: string; method: string; body?: any; headers: Headers }[] = [];
  const fetcher: typeof fetch = async (url, init) => {
    const pathname = new URL(String(url)).pathname, method = init?.method || 'GET';
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ path: pathname, method, body, headers: new Headers(init?.headers) });
    if (overrides[pathname]) return overrides[pathname](body, method);
    if (pathname === '/threads/t') return Response.json({ thread_id: 't', metadata: { cwd: workspace.cwd } });
    if (pathname === '/dcode/threads/t/workspace') return Response.json({ workspace });
    if (pathname === '/store/items') return method === 'GET' ? Response.json({ value: { mode: 'manual' } }) : new Response(null, { status: 204 });
    if (pathname === '/threads/t/state') return Response.json({ values: {}, next: [] });
    if (pathname.endsWith('/runs/stream')) return new Response('event: metadata\ndata: {"run_id":"run"}\n\n', { headers: { 'content-type': 'text/event-stream' } });
    if (pathname.endsWith('/cancel')) return new Response(null, { status: 204 });
    if (pathname.endsWith('/background')) return Response.json({ tasks: [], enabled: false });
    return Response.json([]);
  };
  const app = createApp({ origin, backend: 'http://127.0.0.1:2024', projects: [{ id: 'p', name: 'Project', path: workspace.cwd }], apiKey: 'server-only-key', fetch: fetcher });
  const { token } = await (await app(new Request(origin + '/api/bootstrap'))).json();
  const call = (endpoint: string, method = 'GET', body?: unknown, headers: Record<string, string> = {}) => app(new Request(origin + '/api/projects/p/threads/t/' + endpoint, { method, headers: { 'x-workspace-token': token, 'content-type': 'application/json', ...headers }, body: body === undefined ? undefined : JSON.stringify(body) }));
  return { app, call, token, calls };
}
test('API enforces local origin, CSRF token and same-origin fetch metadata', async () => {
  const { app, call } = await setup();
  assert.equal((await app(new Request('http://evil.test/api/bootstrap'))).status, 403);
  assert.equal((await app(new Request(origin + '/api/projects/p/threads/t/state'))).status, 403);
  assert.equal((await call('state', 'GET', undefined, { origin: 'https://evil.test' })).status, 403);
  assert.equal((await call('state', 'GET', undefined, { 'sec-fetch-site': 'same-site' })).status, 403);
  assert.equal((await call('state')).status, 200);
});
test('browser cannot select another workspace or inject a run context', async () => {
  const { call, calls } = await setup();
  assert.equal((await call('run', 'POST', { text: 'Hello', context: { auto_approve: true, workspace: { cwd: '/' } } })).status, 200);
  const payload = calls.find(c => c.path.endsWith('/runs/stream'))!.body;
  assert.deepEqual(payload.context.workspace, workspace); assert.equal(payload.context.auto_approve, false); assert.equal(payload.context.approval_mode, 'manual');
  assert.equal(payload.stream_resumable, true); assert.equal(payload.multitask_strategy, 'reject'); assert.equal(payload.on_disconnect, 'continue');
  assert.deepEqual(payload.input.messages[0].additional_kwargs.deepagents_code_user_prompt, { literal_user_text: 'Hello', referenced_paths: [], turn_id: payload.context.turn_id });
});
test('cross-project conversation is refused before binding or reading its state', async () => {
  const { call, calls } = await setup({ '/threads/t': () => Response.json({ metadata: { cwd: '/another' } }) });
  assert.equal((await call('state')).status, 404); assert.equal(calls.length, 1);
});
test('mode changes use the live server store and invalid modes are refused', async () => {
  const { call, calls } = await setup();
  assert.equal((await call('mode', 'PUT', { mode: 'yolo' })).status, 200);
  const write = calls.find(c => c.path === '/store/items' && c.method === 'PUT')!;
  assert.deepEqual(write.body.namespace, ['deepagents_code', 'approval_mode']); assert.equal(write.body.key.length, 64); assert.equal(write.body.value.mode, 'yolo');
  assert.equal((await call('mode', 'PUT', { mode: 'bypass' })).status, 400);
});
test('mode lookup failure cannot create a run with guessed consent', async () => {
  const { call, calls } = await setup({ '/store/items': () => Response.json({ detail: 'unavailable' }, { status: 503 }) });
  assert.equal((await call('run', 'POST', { text: 'Hello' })).status, 503);
  assert.equal(calls.some(c => c.path.endsWith('/runs/stream')), false);
});
test('a new conversation defaults to Manual when the store returns JSON null', async () => {
  const { call, calls } = await setup({ '/store/items': () => Response.json(null) });
  const mode = await call('mode');
  assert.equal(mode.status, 200);
  assert.deepEqual(await mode.json(), { mode: 'manual' });
  assert.equal((await call('run', 'POST', { text: 'Hello' })).status, 200);
  const payload = calls.find(c => c.path.endsWith('/runs/stream'))!.body;
  assert.equal(payload.context.approval_mode, 'manual');
  assert.equal(payload.context.auto_approve, false);
  assert.equal(calls.some(c => c.path === '/store/items' && c.method === 'PUT'), false);
});
test('reading a saved mode preserves the user selection', async () => {
  for (const mode of ['manual', 'auto', 'yolo']) {
    const { call, calls } = await setup({ '/store/items': () => Response.json({ value: { mode } }) });
    assert.deepEqual(await (await call('mode')).json(), { mode });
    assert.equal((await call('run', 'POST', { text: 'Hello' })).status, 200);
    const payload = calls.find(c => c.path.endsWith('/runs/stream'))!.body;
    assert.equal(payload.context.approval_mode, mode);
    assert.equal(payload.context.auto_approve, mode !== 'manual');
  }
});
test('run cancellation uses POST and handles an empty successful response', async () => {
  const { call, calls } = await setup();
  assert.equal((await call('cancel', 'POST', { run_id: 'run' })).status, 200);
  assert.equal(calls.at(-1)?.path, '/threads/t/runs/run/cancel'); assert.equal(calls.at(-1)?.method, 'POST');
});
test('pending work requires explicit continue and a user message cannot resume it', async () => {
  const { call, calls } = await setup({ '/threads/t/state': () => Response.json({ values: { messages: [{ type: 'human', additional_kwargs: { deepagents_code_user_prompt: { turn_id: 'original-turn' } } }] }, next: ['tools'] }) });
  assert.equal((await call('run', 'POST', { text: 'New message' })).status, 409);
  assert.equal((await call('run', 'POST', { continue: true })).status, 200);
  assert.equal(calls.at(-1)?.body.input, null);
  assert.equal(calls.at(-1)?.body.context.turn_id, 'original-turn');
});
test('skill instructions do not become trusted user authorization evidence', async () => {
  const { call, calls } = await setup({ '/lc-factory/threads/t/web': () => Response.json({ role: 'user', content: 'Expanded instructions from SKILL.md', additional_kwargs: { __skill: { name: 'review' } } }) });
  assert.equal((await call('run', 'POST', { text: 'Review the changes', skill: '/project/skill/SKILL.md' })).status, 200);
  const message = calls.at(-1)?.body.input.messages[0];
  assert.equal(message.content, 'Expanded instructions from SKILL.md');
  assert.equal(message.additional_kwargs.deepagents_code_user_prompt.literal_user_text, 'Review the changes');
  assert.equal(message.additional_kwargs.__skill.name, 'review');
});
test('approval response uses exact IDs and omits new input', async () => {
  const interrupt = { id: 'i', value: { action_requests: [{ name: 'shell' }], review_configs: [{ action_name: 'shell', allowed_decisions: ['reject'] }] } };
  const { call, calls } = await setup({ '/threads/t/state': () => Response.json({ values: {}, interrupts: [interrupt] }) });
  assert.equal((await call('run', 'POST', { responses: { i: { decisions: [{ type: 'approve' }] } } })).status, 400);
  assert.equal((await call('run', 'POST', { responses: { i: { decisions: [{ type: 'reject' }] } } })).status, 200);
  assert.equal(calls.at(-1)?.body.input, undefined); assert.equal(calls.at(-1)?.body.command.resume.i.decisions[0].type, 'reject');
});
test('backend target is fixed to loopback and credentials stay in server headers', async () => {
  assert.throws(() => createApp({ origin, backend: 'https://example.com', projects: [] }));
  const { call, calls } = await setup(); const response = await call('state');
  assert.equal(calls[0].headers.get('x-api-key'), 'server-only-key'); assert.equal(response.headers.get('x-api-key'), null);
});
test('compaction preserves results, cancels hook pauses and never fabricates a hook reply', async () => {
  const completed = await setup({ '/dcode/threads/t/offload': () => Response.json({ status: 'complete', result: { status: 'compacted', messages_kept: 4, messages_offloaded: 10 } }) });
  const response = await completed.call('compact', 'POST', { operation_id: 'op' });
  assert.equal((await response.json()).result.messages_kept, 4);
  const paused = await setup({ '/dcode/threads/t/offload': () => Response.json({ status: 'interrupt', request: { type: 'hook_invocation' } }) });
  assert.equal((await paused.call('compact', 'POST', { operation_id: 'op' })).status, 409);
  assert.equal(paused.calls.at(-1)?.path, '/dcode/threads/t/offload/op/cancel');
  assert.deepEqual(paused.calls.find(c => c.path.endsWith('/offload'))?.body.hook_responses, {});
});
test('file browser confines paths, excludes private names and refuses symlinks', async () => {
  const root = await realpath(await mkdtemp(path.join(os.tmpdir(), 'lc-web-files-')));
  await writeFile(path.join(root, 'readme.md'), '# Hello'); await writeFile(path.join(root, '.env'), 'secret');
  await mkdir(path.join(root, 'notes')); await symlink(os.tmpdir(), path.join(root, 'outside'));
  await symlink(path.join(root, '.env'), path.join(root, 'trick.md'));
  for (const candidate of ['../outside', '/etc/passwd', '.env', 'outside/x.md', 'trick.md', 'a\\b', 'readme.md\0']) await assert.rejects(() => confinedPath(root, candidate));
  const listed = await listFiles(root); assert.deepEqual(listed.entries.map(e => e.name), ['notes', 'readme.md']);
  assert.equal((await readFile(root, 'readme.md')).text, '# Hello');
});
test('large and binary artifacts fail without returning partial secret data', async () => {
  const root = await realpath(await mkdtemp(path.join(os.tmpdir(), 'lc-web-bounds-')));
  await writeFile(path.join(root, 'large.txt'), 'a'.repeat(1_048_577)); await writeFile(path.join(root, 'binary.txt'), Buffer.from([1, 0, 2]));
  await assert.rejects(() => readFile(root, 'large.txt'), /1 MiB/); await assert.rejects(() => readFile(root, 'binary.txt'), /Binary/);
});
