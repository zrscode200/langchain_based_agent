import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, symlink, readFile } from 'node:fs/promises';
import { spawnSync } from 'node:child_process';
import path from 'node:path';
import os from 'node:os';
import { launchProject } from '../server/workspaces.ts';
import { createApp } from '../server/app.ts';

async function fixture() {
  const root = await mkdtemp(path.join(os.tmpdir(), 'lc-single-project-test-'));
  const first = path.join(root, 'Project A'), second = path.join(root, 'Project B');
  await mkdir(first); await mkdir(second);
  return { root, first, second, project: await launchProject([first]) };
}

test('launch resolves exactly one project, keeps canonical IDs and rejects invalid folders', async () => {
  const { root, first, second, project } = await fixture();
  for (const folders of [undefined, [], [first, second], [first, first]]) await assert.rejects(launchProject(folders), /exactly one/);
  const alias = path.join(root, 'Alias'); await symlink(first, alias);
  assert.deepEqual(await launchProject([alias]), project);
  assert.equal(project.name, 'Project A');
  assert.equal((await launchProject(['~'])).path, (await launchProject([os.homedir()])).path);
  const file = path.join(root, 'file.txt'); await writeFile(file, 'hello');
  await assert.rejects(launchProject([file]), /must be a folder/);
  await assert.rejects(launchProject([path.join(root, 'missing')]));
  const bad = path.join(root, 'bad\nfolder'), goodAlias = path.join(root, 'good-alias');
  await mkdir(bad); await symlink(bad, goodAlias);
  await assert.rejects(launchProject([goodAlias]), /unsupported characters/);
});

test('launcher rejects additional workspaces and legacy store flags before starting a listener', async () => {
  const { first, second } = await fixture();
  const cwd = path.resolve(import.meta.dirname, '..');
  const repeated = spawnSync(process.execPath, ['--import', 'tsx', 'server/main.ts', '--workspace', first, '--workspace', second], { cwd, encoding: 'utf8', timeout: 5000 });
  assert.equal(repeated.status, 1); assert.match(repeated.stderr, /exactly one/);
  const legacy = spawnSync(process.execPath, ['--import', 'tsx', 'server/main.ts', '--workspace', first, '--workspace-store', '/unused.json'], { cwd, encoding: 'utf8', timeout: 5000 });
  assert.equal(legacy.status, 1); assert.match(legacy.stderr, /workspace-store/);
});

test('server rejects multi-project configuration instead of silently picking one', async () => {
  const { project, second } = await fixture();
  const base = { origin: 'http://127.0.0.1:3100', backend: 'http://127.0.0.1:2024' };
  assert.throws(() => createApp({ ...base, projects: [] }), /exactly one/);
  assert.throws(() => createApp({ ...base, projects: [project, { ...project, path: second, id: 'second' }] }), /exactly one/);
});

test('old registry data cannot widen project scope; registration and browsing routes are retired', async () => {
  const { root, first, second, project } = await fixture();
  const saved = path.join(root, 'workspaces.json');
  const registry = JSON.stringify({ version: 1, projects: [project, { id: 'other', path: second, name: 'Other' }] });
  await writeFile(saved, registry);
  const calls: { route: string; body: any }[] = [];
  const origin = 'http://127.0.0.1:3100';
  const config = { projects: [project], origin, backend: 'http://127.0.0.1:2024',
    // A legacy caller cannot reactivate the old registry seam.
    workspaces: { list: () => { throw new Error('Must not read saved hub state'); }, add: () => { throw new Error('Must not register'); } },
    fetch: (async (input, init) => {
      const route = new URL(String(input)).pathname, body = init?.body ? JSON.parse(String(init.body)) : undefined;
      calls.push({ route, body });
      return Response.json({ thread_id: route.split('/')[2], metadata: { cwd: project.path } });
    }) as typeof fetch,
  };
  const app = createApp(config);
  // Snapshot startup identity: mutating the caller's array cannot retarget an app.
  config.projects.push({ id: 'second', name: 'Other', path: second });
  const { token, projects } = await (await app(new Request(origin + '/api/bootstrap'))).json();
  assert.deepEqual(projects, [project]);
  const call = (route: string, method = 'GET', body?: unknown, headers: Record<string, string> = {}) => app(new Request(origin + '/api/' + route, { method, headers: { 'x-workspace-token': token, 'content-type': 'application/json', ...headers }, ...(body === undefined ? {} : { body: JSON.stringify(body) }) }));
  assert.equal((await call('projects', 'POST', { path: second })).status, 405);
  assert.equal((await call('projects/browse?path=' + encodeURIComponent(second))).status, 404);
  assert.equal((await call('projects/second/files')).status, 404);
  assert.equal((await call('projects/second/threads', 'POST', {})).status, 404);
  assert.equal(calls.length, 0);
  assert.deepEqual(await (await call('projects')).json(), [project]);
  assert.equal(await readFile(saved, 'utf8'), registry);
  await writeFile(path.join(first, 'README.md'), '# Project A');
  assert.equal((await call(`projects/${project.id}/files?read=1&path=README.md`)).status, 200);
  assert.equal((await call(`projects/${project.id}/files?read=1&path=../Project%20B/README.md`)).status, 403);
  assert.equal((await call(`projects/${project.id}/threads`, 'POST', {})).status, 201);
  assert.deepEqual(calls[0].body, { cwd: project.path });
  assert.equal((await call('projects', 'GET', undefined, { 'x-workspace-token': '' })).status, 403);
  assert.equal((await call('projects', 'GET', undefined, { origin: 'https://evil.test' })).status, 403);
});
