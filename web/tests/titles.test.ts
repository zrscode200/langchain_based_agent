import test from 'node:test';
import assert from 'node:assert/strict';
import { conversationTitles } from '../server/titles.ts';
import type { Json } from '../src/protocol.ts';

function store() {
  const row: Json = { metadata: { cwd: '/project' } };
  const calls: Json[] = [];
  const titles = conversationTitles(async (_path, method, body: any) => {
    calls.push({ method, body });
    if (method === 'PATCH') Object.assign(row.metadata, body.metadata);
    return structuredClone(row);
  });
  return { titles, row, calls };
}
test('manual names win regardless of auto/manual request ordering', async () => {
  for (const manualFirst of [true, false]) {
    const { titles, row } = store();
    const automatic = () => titles.initialize('t', 'First message');
    const manual = () => titles.rename('t', '  My title  ');
    await Promise.all(manualFirst ? [manual(), automatic()] : [automatic(), manual()]);
    assert.equal(row.metadata.title, 'My title');
    assert.equal(row.metadata.title_source, 'manual');
    assert.equal(row.metadata.cwd, '/project');
  }
});
test('the first accepted request names an untitled thread and subsequent messages preserve it', async () => {
  const { titles, row, calls } = store();
  await Promise.all([titles.initialize('t', 'First\nmessage'), titles.initialize('t', 'Second message')]);
  assert.equal(row.metadata.title, 'First message');
  assert.equal(calls.filter(c => c.method === 'PATCH').length, 1);
});
test('metadata reads wait for a pending title write', async () => {
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  const row: Json = { metadata: {} };
  const titles = conversationTitles(async (_path, method, body: any) => {
    if (method === 'PATCH') { await gate; Object.assign(row.metadata, body.metadata); }
    return structuredClone(row);
  });
  const write = titles.initialize('t', 'Accepted message');
  const read = titles.read('t');
  release(); await write;
  assert.equal((await read).metadata.title, 'Accepted message');
});
test('a failed metadata write does not poison later renames', async () => {
  let fail = true;
  const titles = conversationTitles(async (_path, method, body: any) => {
    if (method === 'PATCH' && fail) { fail = false; throw new Error('offline'); }
    return { metadata: body?.metadata || {} };
  });
  await assert.rejects(titles.initialize('t', 'First'), /offline/);
  assert.equal((await titles.rename('t', 'Recovered')).metadata.title, 'Recovered');
});
