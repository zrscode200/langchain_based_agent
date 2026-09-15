import test from 'node:test';
import assert from 'node:assert/strict';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { ToolView } from '../src/tool-view.tsx';
import { Conversation } from '../src/conversation.tsx';
import { skillActivity, skillLabel } from '../src/skill-activity.ts';
import { toolOutput } from '../src/timeline.ts';
import { mergeUpdate, type Message } from '../src/protocol.ts';

const call = { id: 'read', name: 'read_file', args: { file_path: '/skills/review/SKILL.md' } };
const row = { name: 'review', path: call.args.file_path, description: 'Review changes', origin: 'agent', status: 'loading' };
const ai: Message = { id: 'a', type: 'ai', content: '', tool_calls: [call], additional_kwargs: { lc_skill_activity: { read: row } } };
const result = (status: string): Message => ({ id: 't', type: 'tool', content: '1  Instructions', tool_call_id: 'read', status: status === 'failed' ? 'error' : 'success', additional_kwargs: { lc_skill_activity: { read: { ...row, status } } } });
const render = (messages: Message[], active = true) => renderToStaticMarkup(createElement(ToolView, { call, messages, active, interrupts: [] }));

test('server observations update one row; saved and repeated updates retain identity', () => {
  let messages = mergeUpdate([], { observe: { messages: [ai] } });
  assert.match(render(messages), /Loading skill: review/);
  messages = mergeUpdate(messages, { tools: { messages: [result('loaded')] } });
  messages = mergeUpdate(messages, { tools: { messages: [result('loaded')] } });
  assert.equal(messages.length, 2);
  const html = render(JSON.parse(JSON.stringify(messages)), false);
  assert.equal([...html.matchAll(/Loaded skill: review/g)].length, 1);
  assert.match(html, /Agent selected/);
  assert.doesNotMatch(html, /<strong>Read file/);
});

test('ordinary skill-looking files and assistant claims do not produce badges', () => {
  const plain = { ...ai, additional_kwargs: {}, content: 'I used the review skill' };
  assert.equal(skillActivity(call, [plain]), undefined);
  assert.match(render([plain]), /Read file/);
  assert.equal(skillActivity({ ...call, id: 'different' }, [ai]), undefined);
  assert.equal(skillActivity({ ...call, name: 'execute' }, [ai]), undefined);
});

test('partial, empty, failure, approval and interrupted states stay truthful', () => {
  assert.match(render([ai, result('partial')]), /Loaded skill excerpt: review/);
  assert.match(render([ai, result('empty')]), /No skill instructions loaded: review/);
  assert.match(render([ai, result('failed')]), /Couldn&#x27;t load skill: review/);
  assert.match(render([ai], false), /Skill read interrupted: review/);
  assert.equal(skillLabel(skillActivity(call, [ai])!, true, true), 'Skill read needs approval');
  const unknown = { ...result('loaded'), additional_kwargs: {} };
  assert.match(render([ai, unknown]), /Skill read result unavailable/);
});

test('invalid display metadata is ignored and child owner is supplied by its conversation', () => {
  assert.equal(skillActivity(call, [{ ...ai, additional_kwargs: { lc_skill_activity: { read: { ...row, status: {} } } } }]), undefined);
  const html = renderToStaticMarkup(createElement(Conversation, { messages: [ai, result('loaded')], agentLabel: 'Reviewer child', running: true, submit: async () => {} }));
  assert.match(html, /Reviewer child/);
  assert.match(html, /Loaded skill: review/);
});

test('explicit user invocation remains distinct from agent-selected reads', () => {
  const html = renderToStaticMarkup(createElement(Conversation, { messages: [{ type: 'human', content: 'Instructions', additional_kwargs: { __skill: { name: 'review', args: 'Review my change' } } }], submit: async () => {} }));
  assert.match(html, /Invoked skill: review/);
  assert.match(html, /Review my change/);
  assert.doesNotMatch(html, /Agent selected/);
});

test('same-turn reused IDs retain the exact proposal and its own result', () => {
  const ordinary = { ...call, args: { file_path: '/project/README.md' } };
  const retry = { ...call };
  const done = result('loaded');
  const messages: Message[] = [ai, done, { id: 'second', type: 'ai', content: '', tool_calls: [ordinary] }];
  assert.equal(skillActivity(ordinary, messages), undefined);
  assert.equal(toolOutput(ordinary, messages), undefined);
  const ordinaryResult: Message = { id: 'ordinary-result', type: 'tool', content: 'Project readme', tool_call_id: call.id, status: 'success' };
  messages.push(ordinaryResult, { ...ai, id: 'retry', tool_calls: [retry] });
  assert.equal(toolOutput(ordinary, messages), ordinaryResult);
  assert.equal(skillActivity(call, messages)?.status, 'loaded');
  assert.equal(skillActivity(retry, messages)?.status, 'loading');
  assert.equal(toolOutput(retry, messages), undefined);
  assert.equal(skillActivity({ ...retry }, messages), undefined); // Ambiguous detached identity.
  const saved = JSON.parse(JSON.stringify(messages));
  assert.equal(skillActivity(saved[2].tool_calls[0], saved), undefined);
  assert.equal(skillActivity(saved[4].tool_calls[0], saved)?.status, 'loading');
});
