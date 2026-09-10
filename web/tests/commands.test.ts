import test from 'node:test';
import assert from 'node:assert/strict';
import { composerCommands, filterCommands, parseSlash } from '../src/commands.ts';
import { conversationTitle, initialTitle } from '../src/conversations.ts';

test('commands use actual opaque skill keys and disambiguate builtin and duplicate names', () => {
  const skills = [
    { name: 'model', path: 'opaque:model', description: 'A project model skill' },
    { name: 'review', path: 'project:review', description: 'Project review' },
    { name: 'review', path: 'shared:review', description: 'Shared review' },
    { name: 'review', path: 'shared:review', description: 'Duplicate entry' },
    { name: 'skill-model', path: 'other', description: 'Another collision' },
  ];
  const entries = composerCommands(skills);
  assert.equal(new Set(entries.map(c => c.name)).size, entries.length);
  assert.equal(entries.find(c => c.name === '/model')?.kind, 'command');
  assert.equal(entries.filter(c => c.kind === 'skill').length, 4);
  assert.equal(entries.find(c => c.skillPath === 'opaque:model')?.kind, 'skill');
  assert.deepEqual(composerCommands(skills.reverse()), entries);
});
test('slash parsing preserves multiline arguments and only reads a leading command', () => {
  assert.deepEqual(parseSlash('  /Review Explain\nthese files'), { name: '/review', args: 'Explain\nthese files' });
  assert.deepEqual(parseSlash('/'), { name: '/', args: '' });
  assert.equal(parseSlash('Explain /review'), null);
  assert.equal(parseSlash(''), null);
  assert.equal(filterCommands(composerCommands([]), '/no-such-command').length, 0);
});
test('search matches command names and skill descriptions while prioritizing prefixes', () => {
  const entries = composerCommands([{ name: 'investigate', path: 'key', description: 'Research architecture and memory' }]);
  assert.equal(filterCommands(entries, '/memory')[0].skillPath, 'key');
  assert.equal(filterCommands(entries, '/model')[0].name, '/model');
});
test('conversation labels prefer manual titles, then the first user message', () => {
  const row = { thread_id: 't', status: 'idle', metadata: {}, values: { messages: [{ type: 'ai', content: 'Ignore' }, { type: 'human', content: [{ type: 'text', text: '  First\n request  ' }] }] } };
  assert.equal(conversationTitle(row), 'First request');
  assert.equal(conversationTitle({ ...row, metadata: { title: 'Chosen title' } }), 'Chosen title');
  assert.equal(conversationTitle(), 'New conversation');
  assert.equal(initialTitle('😀'.repeat(72)), '😀'.repeat(70));
});
test('an exact skill name wins over an earlier skill with the same prefix', () => {
  const entries = composerCommands([
    { name: 'review-check', path: 'a', description: 'Check changes' },
    { name: 'review', path: 'z', description: 'Review changes' },
  ]);
  assert.equal(filterCommands(entries, '/review')[0].skillPath, 'z');
  assert.equal(filterCommands(entries, '/review-check')[0].skillPath, 'a');
});
