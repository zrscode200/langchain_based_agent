import test from 'node:test';
import assert from 'node:assert/strict';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { messageReasoning, mergeChunk, mergeUpdate, textContent, type Message } from '../src/protocol.ts';
import { conversationTurns, describeTool, runDescription, toolState, turnActivityKey, receiptAnchor, type DecisionReceipt } from '../src/timeline.ts';
import { Conversation, DecisionReceiptView } from '../src/conversation.tsx';
import { Approvals } from '../src/approvals.tsx';
import { previewScenario } from '../src/chat-samples.ts';

test('DeepSeek reasoning deltas accumulate without replacing earlier metadata or visible text', () => {
  let messages: Message[] = [];
  for (const chunk of [
    { id: 'a', content: '', additional_kwargs: { reasoning_content: 'First ', source: 'retained' } },
    { id: 'a', content: '', additional_kwargs: { reasoning_content: 'second.' } },
    { id: 'a', content: 'Answer', additional_kwargs: {} },
  ]) messages = mergeChunk(messages, chunk);
  assert.equal(messageReasoning(messages[0]), 'First second.');
  assert.equal(messages[0].additional_kwargs?.source, 'retained');
  assert.equal(textContent(messages[0].content), 'Answer');
  const restored = JSON.parse(JSON.stringify(messages[0]));
  assert.equal(messageReasoning(restored), 'First second.');
});
test('reasoning blocks and public summaries render while opaque payloads are excluded', () => {
  const content = [{ type: 'reasoning', summary: [{ type: 'summary_text', text: 'Public summary.' }], encrypted_content: 'PRIVATE' }, { type: 'redacted_thinking', data: 'PRIVATE' }];
  assert.equal(messageReasoning({ content, additional_kwargs: { reasoning_content: 'Duplicate fallback' } }), 'Public summary.');
  assert.equal(messageReasoning({ content: [{ type: 'reasoning', reasoning: { opaque: 'PRIVATE' } }] }), '');
  let messages = mergeChunk([], { id: 'a', content: [{ type: 'reasoning', reasoning: 'First ' }] });
  messages = mergeChunk(messages, { id: 'a', content: [{ type: 'reasoning', reasoning: 'second.' }, { type: 'text', text: 'Answer' }] });
  assert.equal(messageReasoning(messages[0]), 'First second.');
  assert.equal(textContent(messages[0].content), 'Answer');
});
test('completed node updates replace token drafts and immediately associate tool outcomes', () => {
  const draft = [{ id: 'a', content: 'Read', additional_kwargs: { reasoning_content: 'Reasoning' }, tool_calls: [{ id: 'call', name: 'read_file', args: {} }] }];
  const update = { model: { messages: [{ id: 'a', content: 'Reading a file', tool_calls: draft[0].tool_calls }] }, tools: { messages: [{ id: 't', type: 'tool', status: 'error', tool_call_id: 'call', content: 'Access denied' }] } };
  const messages = mergeUpdate(draft, update);
  assert.equal(messages.length, 2);
  assert.equal(textContent(messages[0].content), 'Reading a file');
  assert.equal(messageReasoning(messages[0]), 'Reasoning');
  assert.equal(toolState(draft[0].tool_calls[0], messages, true, []), 'failed');
  assert.deepEqual(mergeUpdate(messages, update), messages);
  assert.deepEqual(mergeUpdate(messages, [['child'], update]), messages);
});
test('tool status uses exact identity and does not turn missing or untyped output into success', () => {
  const call = { id: 'one', name: 'shell', args: { command: 'false' } };
  assert.equal(toolState(call, [{ type: 'tool', tool_call_id: 'other', content: 'ok', status: 'success' }], false, []), 'unavailable');
  assert.equal(toolState({ name: 'shell' }, [{ type: 'tool', content: 'ok' }], true, []), 'requested');
  assert.equal(toolState(call, [{ type: 'tool', tool_call_id: 'one', content: 'exit code 1' }], false, []), 'result');
  assert.equal(toolState(call, [{ type: 'tool', tool_call_id: 'one', content: '', status: 'cancelled' }], false, []), 'cancelled');
  const interrupts = [{ id: 'i', value: { action_requests: [{ name: 'shell', id: 'one' }] } }];
  assert.equal(toolState(call, [], false, interrupts), 'awaiting_approval');
  assert.equal(toolState({ ...call, id: 'two' }, [], false, interrupts), 'unavailable');
});
test('turns keep tool output with its user request and retain messages before a truncated user boundary', () => {
  const messages = [{ id: 'retained', type: 'ai', content: 'Earlier context' }, { id: 'u1', type: 'human', content: 'First' }, { id: 't1', type: 'tool', content: 'result' }, { id: 'u2', type: 'user', content: 'Second' }, { id: 'a2', type: 'ai', content: 'Reply' }];
  const turns = conversationTurns(messages);
  assert.deepEqual(turns.map(t => t.id), ['retained-context', 'u1', 'u2']);
  assert.equal(turns[1].messages[0].id, 't1');
  assert.equal(turns[2].messages[0].id, 'a2');
});
test('progress distinguishes a request, reasoning, writing, and waiting for input', () => {
  assert.equal(runDescription([{ id: 'a', content: '', additional_kwargs: { reasoning_content: 'Thinking' } }], true, []), 'Reasoning');
  assert.equal(runDescription([{ id: 'a', content: 'Reply' }], true, []), 'Writing a response');
  assert.equal(runDescription([], true, [{ id: 'i', value: { type: 'ask_user' } }]), 'Waiting for your answer');
  assert.equal(runDescription([], false, [], true), 'Connection interrupted');
  assert.equal(runDescription([], false, []), '');
  assert.equal(describeTool({ name: 'custom_tool', args: '{partial' }).label, 'custom tool');
});
test('a single permission request exposes direct decisions and no batch submit', () => {
  const { interrupts } = previewScenario('approval');
  const html = renderToStaticMarkup(createElement(Approvals, { interrupts, submit: async () => {} }));
  assert.match(html, /Approve this action/); assert.match(html, /Reject/);
  assert.doesNotMatch(html, /Submit decisions/);
  assert.match(html, /AGENTS.md/); assert.match(html, /View requested replacement/);
  const unsupported = renderToStaticMarkup(createElement(Approvals, { interrupts: [{ id: 'i', value: { action_requests: [{ name: 'shell', args: {} }] } }], submit: async () => {} }));
  assert.doesNotMatch(unsupported, /Approve this action/); assert.match(unsupported, /terminal client/);
});
test('completed conversation gives the answer prominence; failures expose their actual output', () => {
  const complete = previewScenario('complete');
  const html = renderToStaticMarkup(createElement(Conversation, { ...complete, submit: async () => {} }));
  assert.match(html, /Three targeted improvements/); assert.match(html, /Activity/);
  const failed = renderToStaticMarkup(createElement(Conversation, { ...previewScenario('failure'), submit: async () => {} }));
  assert.match(failed, /Error output/); assert.match(failed, /Link check failed/); assert.match(failed, /1 failed/);
});
test('provider reasoning is visible during an active reasoning-only message', () => {
  const html = renderToStaticMarkup(createElement(Conversation, { ...previewScenario('working'), submit: async () => {} }));
  assert.match(html, /These examples illustrate provider-exposed reasoning/);
  assert.match(html, /Reasoning/); assert.match(html, />Live</);
});


test('nonzero command exit codes are failures even inside a successful tool envelope', () => {
  const call = { id: 'execute-1', name: 'execute', args: { command: 'false' } };
  const result: Message = { id: 'result-1', type: 'tool', tool_call_id: call.id, status: 'success', artifact: { exit_code: 1 }, content: 'Process exited with code 1' };
  assert.equal(toolState(call, [result], false, []), 'failed');
  assert.equal(toolState(call, [{ ...result, artifact: { exit_code: 0 } }], false, []), 'succeeded');
  const html = renderToStaticMarkup(createElement(Conversation, { messages: [{ id: 'user', type: 'human', content: 'Check' }, { id: 'ai', type: 'ai', content: '', tool_calls: [call] }, result, { id: 'answer', type: 'ai', content: 'The command failed.' }], submit: async () => {} }));
  assert.match(html, /1 failed/); assert.match(html, /Error output/); assert.match(html, /Process exited with code 1/);
});
test('activity identity survives replacement of the optimistic user message by its saved ID', () => {
  const assistant = { id: 'stable-ai', type: 'ai', content: '', tool_calls: [{ id: 'call', name: 'read_file' }] };
  const live = conversationTurns([{ id: 'optimistic-123', type: 'human', content: 'Read this' }, assistant]);
  const saved = conversationTurns([{ id: 'backend-human-id', type: 'human', content: 'Read this' }, assistant]);
  assert.equal(turnActivityKey(live[0]), turnActivityKey(saved[0]));
});


const commandReceipt: DecisionReceipt = {
  id: 'receipt-1', afterMessage: 'request-ai',
  interrupts: [{ id: 'permission', value: { action_requests: [
    { name: 'execute', args: { command: 'FULL_COMMAND_ONE && ' + 'find ./example -type f; '.repeat(40) } },
    { name: 'execute', args: { command: 'FULL_COMMAND_TWO && git status --short' } },
  ] } }],
  responses: { permission: { decisions: [{ type: 'approve' }, { type: 'approve' }] } },
};
test('approval receipts start as a compact count without exposing full commands or retention text', () => {
  const html = renderToStaticMarkup(createElement(DecisionReceiptView, { receipt: commandReceipt }));
  assert.match(html, /Approval sent/); assert.match(html, /2 actions/); assert.match(html, /aria-expanded="false"/);
  assert.doesNotMatch(html, /FULL_COMMAND|find \.\/example|kept for this visit|Sent during this visit/);
  const mixed = renderToStaticMarkup(createElement(DecisionReceiptView, { receipt: { ...commandReceipt, responses: { permission: { decisions: [{ type: 'approve' }, { type: 'reject' }] } } } }));
  assert.match(mixed, /Decisions sent/);
});
test('receipts stay beside original work before later updates and collapse with completed activity', () => {
  const messages: Message[] = [
    { id: 'user', type: 'human', content: 'Explore the files' },
    { id: 'request-ai', type: 'ai', content: 'I will inspect the files.', tool_calls: [{ id: 'call-1', name: 'execute', args: { command: 'ls' } }] },
    { id: 'result', type: 'tool', tool_call_id: 'call-1', status: 'success', artifact: { exit_code: 0 }, content: 'README.md' },
    { id: 'later-ai', type: 'ai', content: 'Later assistant update.', tool_calls: [{ id: 'call-2', name: 'read_file', args: { file_path: 'README.md' } }] },
    { id: 'answer', type: 'ai', content: 'Final answer.' },
  ];
  const props = { messages, receipts: [commandReceipt], submit: async () => {} };
  const live = renderToStaticMarkup(createElement(Conversation, { ...props, running: true }));
  assert.ok(live.indexOf('Approval sent') > live.indexOf('Run command'));
  assert.ok(live.indexOf('Approval sent') < live.indexOf('Later assistant update.'));
  const complete = renderToStaticMarkup(createElement(Conversation, props));
  assert.match(complete, /Final answer/); assert.doesNotMatch(complete, /Approval sent|FULL_COMMAND/);
});
test('a receipt never moves into a newer turn if its original message was removed', () => {
  const messages: Message[] = [{ id: 'new-user', type: 'human', content: 'A different request' }, { id: 'new-answer', type: 'ai', content: 'New answer' }];
  assert.equal(receiptAnchor(commandReceipt, messages), undefined);
  const html = renderToStaticMarkup(createElement(Conversation, { messages, receipts: [commandReceipt], running: true, submit: async () => {} }));
  assert.doesNotMatch(html, /Approval sent/);
  const tool: Message = { id: 'tool-result', type: 'tool', tool_call_id: 'call', content: '' };
  assert.equal(receiptAnchor({ ...commandReceipt, afterMessage: tool.id }, [{ id: 'old-ai', type: 'ai', content: '', tool_calls: [{ id: 'call' }] }, ...messages.slice(0, 1), tool]), tool.id);
  assert.equal(receiptAnchor({ ...commandReceipt, afterMessage: tool.id }, [{ id: 'owner-ai', type: 'ai', content: '', tool_calls: [{ id: 'call' }] }, tool]), 'owner-ai');
});
