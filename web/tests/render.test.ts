import test from 'node:test';
import assert from 'node:assert/strict';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { Approvals, Prose, ArtifactView, MessageView } from '../src/components.tsx';

test('upstream choice prompts render real labels and optional questions allow submit', () => {
  const html = renderToStaticMarkup(createElement(Approvals, { interrupts: [{ id: 'q', value: { type: 'ask_user', questions: [{ question: 'Choose a language', type: 'multi_select', required: false, choices: [{ value: 'TypeScript' }, { value: 'Python' }] }] } }], submit: async () => {} }));
  assert.match(html, /TypeScript/); assert.match(html, /Python/); assert.match(html, /optional/); assert.match(html, /Add another answer/);
  assert.match(html, /class="button primary"[^>]*>.*?Submit response/s);
  assert.doesNotMatch(html, /class="button primary"[^>]*disabled/);
});
test('Markdown and tool text cannot create executable HTML or fetch external images', () => {
  const html = renderToStaticMarkup(createElement(Prose, { children: '<script>alert(1)</script>\n\n![tracking](https://evil.test/image)\n\n[bad](javascript:alert(1))' }));
  assert.doesNotMatch(html, /<script/); assert.doesNotMatch(html, /<img/); assert.doesNotMatch(html, /href="javascript:/);
  const tool = renderToStaticMarkup(createElement(MessageView, { message: { id: 'ai', type: 'ai', content: '', tool_calls: [{ id: 'call', name: 'shell', args: { command: '<script>bad()</script>' } }] }, all: [{ type: 'tool', content: '<img src=x onerror=bad()>', tool_call_id: 'call' }] }));
  assert.doesNotMatch(tool, /<script/); assert.doesNotMatch(tool, /<img/); assert.match(tool, /&lt;img/);
});
test('HTML preview uses an opaque sandbox and a restrictive resource policy', () => {
  const html = renderToStaticMarkup(createElement(ArtifactView, { file: { path: 'page.html', text: '<script>bad()</script>', kind: 'html', size: 20 }, close: () => {}, reference: () => {} }));
  assert.match(html, /sandbox=""/); assert.match(html, /default-src &#x27;none&#x27;/); assert.doesNotMatch(html, /allow-scripts|allow-same-origin/);
});
