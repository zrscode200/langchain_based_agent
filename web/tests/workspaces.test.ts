import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, readFile, writeFile, symlink, stat } from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { workspaceStore, workspaceProject, browseFolders } from '../server/workspaces.ts';
import { createApp } from '../server/app.ts';

async function fixture() {
  const root = await mkdtemp(path.join(os.tmpdir(), 'lc-workspace-test-'));
  const first = path.join(root, 'First'), second = path.join(root, 'Second project');
  await mkdir(first); await mkdir(second);
  const initial = [await workspaceProject(first)];
  const file = path.join(root, 'state', 'workspaces.json');
  return { root, first, second, initial, file, store: workspaceStore(file, initial) };
}

test('added workspaces, initial roots and display names survive reload; aliases deduplicate without renaming', async () => {
  const { root, second, file, store, initial } = await fixture();
  const added = await store.add(second, '  Research  ');
  assert.equal(added.created, true); assert.equal(added.project.name, 'Research');
  assert.equal((await stat(file)).mode & 0o777, 0o600);
  const restored = workspaceStore(file, []);
  assert.deepEqual(await restored.list(), [...initial, added.project]);
  const alias = path.join(root, 'alias'); await symlink(second, alias);
  const duplicate = await restored.add(alias, 'Changed name');
  assert.equal(duplicate.created, false); assert.equal(duplicate.project.name, 'Research');
  assert.equal(duplicate.project.id, added.project.id); assert.equal(duplicate.projects.length, 2);
  assert.equal((await workspaceStore(file, [await workspaceProject(second)]).list()).find(p => p.id === added.project.id)?.name, 'Research');
});

test('invalid folders and names fail without publishing or overwriting saved state', async () => {
  const { root, second, store, file } = await fixture();
  await store.add(second);
  const before = await readFile(file, 'utf8');
  await writeFile(path.join(root, 'file.txt'), 'test');
  for (const candidate of [undefined, null, 3, '', 'relative/path', path.join(root, 'missing'), path.join(root, 'file.txt'), '/bad\0name']) await assert.rejects(store.add(candidate));
  for (const name of [12, {}, 'x'.repeat(81), 'bad\nname']) await assert.rejects(store.add(root, name));
  assert.equal(await readFile(file, 'utf8'), before);
});

test('folder picker returns directories only and supports parent navigation', async () => {
  const { root, first } = await fixture();
  await mkdir(path.join(root, '.private')); await mkdir(path.join(root, 'node_modules'));
  await symlink(first, path.join(root, 'alias')); await writeFile(path.join(root, 'file.txt'), 'private content');
  const listing = await browseFolders(root);
  assert.deepEqual(listing.entries.map(e => e.name), ['First', 'Second project']);
  assert.equal((await browseFolders(first)).parent, listing.path);
  assert.equal((await browseFolders('/')).parent, null);
  assert.equal(listing.limited, false);
  assert.equal((await workspaceProject('~')).path, (await workspaceProject(os.homedir())).path);
});

test('serialized additions retain both projects and a cross-process lock yields an actionable error', async () => {
  const { root, second, store, file, initial } = await fixture();
  const third = path.join(root, 'Third'); await mkdir(third);
  await Promise.all([store.add(second), store.add(third)]);
  assert.equal((await store.list()).length, 3);
  await writeFile(file + '.lock', '');
  await assert.rejects(workspaceStore(file, initial).add(root), /being updated/);
});

test('corrupt, symbolic-link and unwritable stores fail without claiming success or overwriting data', async () => {
  const { root, second, file, initial } = await fixture();
  await mkdir(path.dirname(file)); await writeFile(file, 'broken json');
  await assert.rejects(workspaceStore(file, initial).add(second), /Cannot read saved/);
  assert.equal(await readFile(file, 'utf8'), 'broken json');
  const alias = path.join(root, 'alias.json'); await symlink(file, alias);
  await assert.rejects(workspaceStore(alias, initial).list(), /Cannot read saved/);
  await assert.rejects(workspaceStore(path.join(file, 'workspaces.json'), initial).add(second), /Could not save/);
});

test('registration and browsing enforce origin/token checks; file access and threads use the registered root', async () => {
  const { store, second, first, initial } = await fixture();
  await writeFile(path.join(second, 'README.md'), '# Second workspace');
  const calls: { path: string; body: any }[] = [];
  const origin = 'http://127.0.0.1:3100';
  const app = createApp({ projects: initial, workspaces: store, origin, backend: 'http://127.0.0.1:2024', fetch: async (input, init) => {
    const url = new URL(String(input)), body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ path: url.pathname, body });
    if (url.pathname.endsWith('/workspace')) return Response.json({ workspace: { cwd: second } });
    return Response.json({ thread_id: url.pathname.split('/')[2], metadata: { cwd: second } });
  } });
  const { token } = await (await app(new Request(origin + '/api/bootstrap'))).json();
  const call = (endpoint: string, method = 'GET', body?: unknown, headers: Record<string, string> = {}) => app(new Request(origin + '/api/' + endpoint, { method, headers: { 'x-workspace-token': token, 'content-type': 'application/json', ...headers }, ...(body === undefined ? {} : { body: JSON.stringify(body) }) }));
  assert.equal((await call('projects', 'POST', { path: second }, { 'x-workspace-token': '' })).status, 403);
  assert.equal((await call('projects/browse', 'GET', undefined, { 'x-workspace-token': '' })).status, 403);
  for (const headers of [{ origin: 'https://evil.test' }, { 'sec-fetch-site': 'same-site' }, { 'sec-fetch-site': 'cross-site' }] as Record<string, string>[]) {
    assert.equal((await call('projects', 'POST', { path: second }, headers)).status, 403);
    assert.equal((await call('projects/browse', 'GET', undefined, headers)).status, 403);
  }
  assert.equal((await call('projects', 'POST', { path: second }, { 'content-type': 'text/plain' })).status, 415);
  assert.equal((await call('projects', 'POST', [])).status, 400);
  assert.equal((await call('projects', 'POST', { path: '/missing-folder' })).status, 400);
  const response = await call('projects', 'POST', { path: second, name: 'Second' });
  assert.equal(response.status, 201); const result = await response.json();
  assert.equal(calls.length, 0, 'adding must not start an agent or create a thread');
  assert.equal((await (await call('projects')).json()).length, 2);
  assert.equal((await (await app(new Request(origin + '/api/bootstrap'))).json()).projects.length, 2);
  assert.equal((await call('projects', 'POST', { path: second, name: 'Duplicate' })).status, 200);
  const id = result.project.id;
  assert.equal((await call(`projects/${id}/files?read=1&path=README.md`)).status, 200);
  assert.equal((await call(`projects/${id}/files?read=1&path=../First/README.md`)).status, 403);
  assert.equal((await call(`projects/${id}/threads`, 'POST', {})).status, 201);
  assert.equal(calls[0].body.cwd, result.project.path);
  // A thread bound to Second cannot be inspected through First.
  assert.equal((await call(`projects/${initial[0].id}/threads/other/state`)).status, 404);
  assert.equal((await call('projects/browse?path=' + encodeURIComponent(first))).status, 200);
});


test('a symlink with an invalid canonical target cannot poison the saved workspace list', async () => {
  const { root, second, store, file } = await fixture();
  await store.add(second);
  const before = await readFile(file, 'utf8');
  const target = path.join(root, 'bad\nfolder'), alias = path.join(root, 'safe-alias');
  await mkdir(target); await symlink(target, alias);
  await assert.rejects(store.add(alias, 'Safe name'), /unsupported characters/);
  await assert.rejects(browseFolders(alias), /unsupported characters/);
  assert.equal(await readFile(file, 'utf8'), before);
  assert.equal((await store.list()).length, 2);
});
