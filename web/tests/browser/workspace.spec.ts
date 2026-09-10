import { test, expect, type Page } from '@playwright/test';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { artifact, catalog, files, project, snapshot, tasks, threads } from '../fixtures.ts';

const root = path.resolve(import.meta.dirname, '../..');
async function mount(page: Page, options: { empty?: boolean; approval?: boolean; delayOldState?: boolean; delayRun?: boolean; longContent?: boolean; multipleApprovals?: boolean; failApproval?: boolean; reasoningStream?: boolean } = {}) {
  const calls: { url: string; body: any }[] = [];
  const rows = structuredClone(threads);
  const current = structuredClone(options.empty ? { ...snapshot, values: { messages: [] }, interrupts: [] } : snapshot) as any;
  const currentCatalog = structuredClone(catalog);
  if (options.longContent) {
    current.values.messages = Array.from({ length: 40 }, (_, i) => ({
      id: `long-message-${i}`, type: i % 2 ? 'ai' : 'human',
      content: `Message ${i + 1}. ` + 'A long conversation must remain scrollable while the composer stays in view. '.repeat(8),
    }));
    currentCatalog.skills = Array.from({ length: 30 }, (_, i) => ({
      ...catalog.skills[0], name: `Example skill ${i + 1}`, path: `/sample/skills/${i}/SKILL.md`,
    }));
  }
  if (options.approval) current.interrupts = [{ id: 'pause-1', value: { action_requests: [{ name: 'write_file', args: { file_path: 'notes.md', content: '# Notes' } }], review_configs: [{ action_name: 'write_file', allowed_decisions: ['approve', 'reject'] }] } }];
  if (options.multipleApprovals) current.interrupts[0].value.action_requests.push({ name: 'write_file', args: { file_path: 'second.md', content: '# Second' } });
  const reasonMessages = [
    { id: 'reason-ai', type: 'ai', content: '', additional_kwargs: { reasoning_content: 'First second.' }, tool_calls: [{ id: 'read-stream', name: 'read_file', args: { file_path: 'missing.md' } }] },
    { id: 'reason-tool', type: 'tool', tool_call_id: 'read-stream', status: 'error', content: 'File does not exist.' },
    { id: 'stream-ai', type: 'ai', content: 'Streamed response' },
  ];
  if (options.reasoningStream) {
    const events = [
      { event: 'metadata', data: { run_id: 'r1' } },
      { event: 'messages', data: [{ id: 'reason-ai', type: 'AIMessageChunk', content: '', additional_kwargs: { reasoning_content: 'First ' } }, {}] },
      { event: 'messages', data: [{ id: 'reason-ai', type: 'AIMessageChunk', content: '', additional_kwargs: { reasoning_content: 'second.' } }, {}] },
      { event: 'messages', data: [{ id: 'reason-ai', type: 'AIMessageChunk', content: '', tool_call_chunks: [{ index: 0, id: 'read-stream', name: 'read_file', args: '{"file_path":"missing.md"}' }] }, {}] },
      { event: 'updates', data: { tools: { messages: [reasonMessages[1]] } } },
      { event: 'messages', data: [reasonMessages[2], {}] },
    ].map((event, i) => `event: ${event.event}\nid: ${i + 1}\ndata: ${JSON.stringify(event.data)}\n\n`);
    await page.addInitScript(({ events }) => {
      const original = window.fetch.bind(window);
      window.fetch = async (...args) => {
        const response = await original(...args);
        if (!String(args[0]).endsWith('/run') || !response.ok) return response;
        return new Response(new ReadableStream({ start(controller) {
          events.forEach((event, i) => setTimeout(() => controller.enqueue(new TextEncoder().encode(event)), i * 500));
          setTimeout(() => controller.close(), events.length * 500);
        } }), { headers: response.headers });
      };
    }, { events });
  }
  await page.route('http://127.0.0.1:3100/**', async route => {
    const req = route.request(), url = new URL(req.url());
    if (!url.pathname.startsWith('/api/')) {
      const file = path.join(root, 'dist', url.pathname === '/' ? 'index.html' : url.pathname);
      await route.fulfill({ body: await readFile(file), contentType: file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html' }); return;
    }
    const body = req.postDataJSON() || {}; calls.push({ url: url.pathname, body });
    let result: unknown = {};
    if (url.pathname === '/api/bootstrap') result = { token: 'test-token', projects: [project], backend: 'http://127.0.0.1:2024' };
    else if (url.pathname.endsWith('/threads')) result = req.method() === 'POST' ? { ...rows[0], thread_id: 'new-thread', metadata: { cwd: project.path } } : rows;
    else if (url.pathname.endsWith('/state')) {
      if (options.delayOldState && url.pathname.includes('conversation-1')) await new Promise(r => setTimeout(r, 450));
      result = url.pathname.includes('conversation-2') ? { values: { messages: [{ id: 'new', type: 'human', content: 'This belongs to the second conversation.' }] }, next: [], interrupts: [] } : current;
    }
    else if (url.pathname.endsWith('/background')) result = body.operation === 'inspect' ? { task: { ...tasks[0], conversation: { text: 'Retained task transcript', page: 0, pages: 1, limited: false, notice: '' } } } : { tasks, enabled: true, pending_results: [] };
    else if (url.pathname.endsWith('/catalog')) result = currentCatalog;
    else if (url.pathname.endsWith('/mode')) result = { mode: body.mode || 'manual' };
    else if (url.pathname.endsWith('/runs')) result = [];
    else if (url.pathname.endsWith('/files')) result = url.searchParams.has('read') ? artifact : files;
    else if (/\/threads\/[^/]+$/.test(url.pathname)) {
      const row = rows.find(t => url.pathname.endsWith('/' + t.thread_id));
      if (row && req.method() === 'PATCH') row.metadata.title = body.title;
      result = row || { thread_id: 'new-thread', metadata: { cwd: project.path } };
    }
    else if (url.pathname.endsWith('/run')) {
      if (options.delayRun) await new Promise(resolve => setTimeout(resolve, 800));
      if (options.failApproval && body.responses && calls.filter(c => c.url.endsWith('/run')).length === 1) {
        await route.fulfill({ status: 503, json: { detail: 'Approval was not accepted. Please retry.' } }); return;
      }
      current.interrupts = []; current.values.messages = [...(current.values.messages || []), ...(body.text ? [{ id: 'saved-user', type: 'human', content: body.text }] : []), ...(options.reasoningStream ? reasonMessages : [{ id: 'stream-ai', type: 'ai', content: 'Streamed response' }])];
      await route.fulfill({ contentType: 'text/event-stream', body: 'event: metadata\nid: 1\ndata: {"run_id":"r1"}\n\nevent: messages\nid: 2\ndata: [{"id":"stream-ai","type":"AIMessageChunk","content":"Streamed "},{}]\n\nevent: messages\nid: 3\ndata: [{"id":"stream-ai","type":"AIMessageChunk","content":"response"},{}]\n\n' }); return;
    }
    await route.fulfill({ json: result });
  });
  await page.goto('http://127.0.0.1:3100/');
  return calls;
}
test('welcome, skill selection, chat streaming and keyboard search', async ({ page }) => {
  const errors: string[] = []; page.on('pageerror', e => errors.push(e.message));
  const calls = await mount(page, { empty: true });
  await expect(page.getByRole('heading', { name: /What will we/ })).toBeVisible();
  const composer = page.getByRole('combobox', { name: 'Message the agent' });
  await composer.fill('/brain');
  await expect(page.getByRole('option', { name: /brainstorm/ })).toBeVisible();
  await composer.press('Tab');
  expect(calls.filter(c => c.url.endsWith('/run'))).toHaveLength(0);
  await page.getByRole('combobox', { name: 'Message the agent' }).fill('Explore the memory system');
  await page.getByRole('button', { name: 'Send message', exact: true }).click();
  await expect(page.getByText('Streamed response', { exact: true })).toHaveCount(1);
  expect(calls.find(c => c.url.endsWith('/run'))?.body.skill).toBe(catalog.skills[0].path);
  await page.keyboard.press('Meta+k'); await expect(page.getByRole('dialog')).toBeVisible(); await page.keyboard.press('Escape');
  expect(errors).toEqual([]);
});
test('single approval submits once directly; a receipt replaces the request', async ({ page }) => {
  const calls = await mount(page, { approval: true, delayRun: true });
  await expect(page.getByRole('button', { name: 'Submit decisions' })).toHaveCount(0);
  expect(calls.filter(c => c.url.endsWith('/run'))).toHaveLength(0);
  await page.getByRole('button', { name: 'Approve this action', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Approve this action', exact: true })).toBeDisabled();
  await expect(page.getByText('Streamed response', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Send message', exact: true })).toBeVisible();
  await expect(page.getByText('Approval sent', { exact: true })).toHaveCount(0);
  await page.locator('.work-log > .disclosure-toggle').last().click();
  await expect(page.getByText('Approval sent', { exact: true })).toBeVisible();
  await expect(page.getByRole('region', { name: 'Approval request' })).toHaveCount(0);
  expect(calls.filter(c => c.url.endsWith('/run'))).toHaveLength(1);
  expect(calls.find(c => c.url.endsWith('/run'))?.body.responses['pause-1'].decisions).toEqual([{ type: 'approve' }]);
});
test('multiple actions require complete choices and explicit batch submission', async ({ page }) => {
  const calls = await mount(page, { approval: true, multipleApprovals: true });
  const submit = page.getByRole('button', { name: 'Submit decisions' });
  await expect(submit).toBeDisabled();
  await page.getByRole('button', { name: 'Approve', exact: true }).nth(0).click();
  await expect(submit).toBeDisabled();
  await page.getByRole('button', { name: 'Reject', exact: true }).nth(1).click();
  expect(calls.filter(c => c.url.endsWith('/run'))).toHaveLength(0);
  await submit.click();
  await expect(page.getByText('Streamed response', { exact: true })).toBeVisible();
  expect(calls.find(c => c.url.endsWith('/run'))?.body.responses['pause-1'].decisions).toEqual([{ type: 'approve' }, { type: 'reject' }]);
});
test('an unaccepted approval retains its request and can be explicitly retried', async ({ page }) => {
  const calls = await mount(page, { approval: true, failApproval: true });
  await page.getByRole('button', { name: 'Approve this action', exact: true }).click();
  await expect(page.getByRole('alert').filter({ hasText: 'Decision was not accepted' })).toBeVisible();
  await expect(page.getByText('Approval sent', { exact: true })).toHaveCount(0);
  await page.getByRole('button', { name: 'Approve this action', exact: true }).click();
  await expect(page.getByRole('region', { name: 'Approval request' })).toHaveCount(0);
  await page.locator('.work-log > .disclosure-toggle').last().click();
  await expect(page.getByText('Approval sent', { exact: true })).toBeVisible();
  expect(calls.filter(c => c.url.endsWith('/run'))).toHaveLength(2);
});
test('navigation ignores late state, with project files and task inspection available', async ({ page }) => {
  await mount(page, { delayOldState: true });
  await page.getByRole('button', { name: 'Memory design exploration' }).click();
  await expect(page.getByText('This belongs to the second conversation.')).toBeVisible();
  await page.waitForTimeout(650);
  await expect(page.getByText('Explore the memory components of this agent.', { exact: false })).toHaveCount(0);
  await page.getByRole('button', { name: 'Project files', exact: true }).click();
  await page.getByRole('button', { name: 'architecture.md', exact: true }).click();
  await expect(page.getByRole('region', { name: 'File preview' })).toBeVisible();
  await page.getByRole('button', { name: 'Reference in chat' }).click();
  await expect(page.getByRole('combobox', { name: 'Message the agent' })).toHaveValue(/architecture.md/);
  await page.getByRole('button', { name: 'Close file preview' }).click();
  await page.getByRole('button', { name: /^Background work/ }).click();
  await page.getByRole('button', { name: /Memory & persistence/ }).click();
  await expect(page.getByText('Retained task transcript')).toBeVisible();
});
test('desktop and mobile avoid horizontal overflow', async ({ page }) => {
  await mount(page, { empty: true });
  await expect(page.getByRole('heading', { name: /What will we/ })).toBeVisible();
  await page.screenshot({ path: path.join(root, 'test-results/workspace-desktop.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole('button', { name: 'Hide sidebar' }).click();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: path.join(root, 'test-results/workspace-mobile.png'), fullPage: true });
});

test('slash commands open controls, unknown commands stay local and literal text requires a separate send', async ({ page }) => {
  const calls = await mount(page, { empty: true });
  const composer = page.getByRole('combobox', { name: 'Message the agent' });
  await composer.fill('/settings');
  await composer.press('Enter');
  await expect(page.getByRole('dialog', { name: 'Session settings' })).toBeVisible();
  expect(calls.filter(c => c.url.endsWith('/run'))).toHaveLength(0);
  await page.keyboard.press('Escape');
  await composer.fill('/unsupported argument');
  await composer.press('Enter');
  await expect(composer).toHaveValue('/unsupported argument');
  await expect(page.getByRole('alert')).toContainText('not a web command');
  expect(calls.filter(c => c.url.endsWith('/run'))).toHaveLength(0);
  await page.getByRole('button', { name: 'Use as message text' }).click();
  expect(calls.filter(c => c.url.endsWith('/run'))).toHaveLength(0);
  await page.getByRole('button', { name: 'Send message', exact: true }).click();
  await expect.poll(() => calls.filter(c => c.url.endsWith('/run')).length).toBe(1);
  expect(calls.find(c => c.url.endsWith('/run'))?.body.text).toBe('/unsupported argument');
});
test('inline and sidebar renaming persist and target the intended conversation', async ({ page }) => {
  const calls = await mount(page);
  await page.getByTitle('Rename conversation', { exact: true }).click();
  await page.getByRole('textbox', { name: 'Conversation title' }).fill('My named conversation');
  await page.getByRole('textbox', { name: 'Conversation title' }).press('Enter');
  await expect(page.getByTitle('Rename conversation', { exact: true })).toContainText('My named conversation');
  await page.getByRole('button', { name: 'Rename Memory design exploration', exact: true }).click();
  await page.getByRole('textbox', { name: 'Conversation title' }).fill('Another title');
  await page.getByRole('button', { name: 'Save', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Another title', exact: true })).toBeVisible();
  await expect(page.getByTitle('Rename conversation', { exact: true })).toContainText('My named conversation');
  expect(calls.find(c => c.body.title === 'Another title')?.url).toContain('conversation-2');
  await page.reload();
  await expect(page.getByTitle('Rename conversation', { exact: true })).toContainText('My named conversation');
});
test('slash navigation respects Escape, arrow selection, IME and multiline input', async ({ page }) => {
  const calls = await mount(page, { empty: true });
  const composer = page.getByRole('combobox', { name: 'Message the agent' });
  await composer.fill('/');
  await composer.press('ArrowDown');
  await expect(page.getByRole('option', { selected: true })).toContainText('/settings');
  await composer.press('Escape');
  await expect(page.getByRole('listbox')).toHaveCount(0);
  await composer.fill('/review my request');
  await composer.dispatchEvent('keydown', { key: 'Enter', code: 'Enter', isComposing: true });
  await expect(composer).toHaveValue('/review my request');
  await composer.press('Enter');
  await expect(composer).toHaveValue('my request');
  await expect(page.getByRole('button', { name: 'Remove selected skill' })).toBeVisible();
  expect(calls.filter(c => c.url.endsWith('/run'))).toHaveLength(0);
  await composer.press('Shift+Enter');
  await expect(composer).toHaveValue('my request\n');
  expect(calls.filter(c => c.url.endsWith('/run'))).toHaveLength(0);
});

test('slash tab commands still work after manually switching definition tabs', async ({ page }) => {
  await mount(page, { empty: true });
  await page.getByRole('button', { name: 'Agent definition', exact: true }).click();
  await page.getByRole('button', { name: /^Tools/ }).click();
  const composer = page.getByRole('combobox', { name: 'Message the agent' });
  await composer.fill('/skills'); await composer.press('Enter');
  await expect(page.getByRole('textbox', { name: 'Filter skills' })).toBeVisible();
  await composer.fill('/tools'); await composer.press('Enter');
  await expect(page.getByRole('textbox', { name: 'Filter tools' })).toBeVisible();
  await page.getByRole('button', { name: /^Skills/ }).click();
  await composer.fill('/tools'); await composer.press('Enter');
  await expect(page.getByRole('textbox', { name: 'Filter tools' })).toBeVisible();
});
test('accepting an earlier message preserves a newer literal slash draft', async ({ page }) => {
  const calls = await mount(page, { empty: true, delayRun: true });
  const composer = page.getByRole('combobox', { name: 'Message the agent' });
  await composer.fill('First request'); await composer.press('Enter');
  await composer.fill('/settings');
  await page.getByRole('button', { name: 'Use as message text' }).click();
  await expect(page.getByText('Streamed response', { exact: true })).toBeVisible();
  await expect(composer).toHaveValue('/settings');
  await page.getByRole('button', { name: 'Send message', exact: true }).click();
  await expect.poll(() => calls.filter(c => c.url.endsWith('/run')).length).toBe(2);
  expect(calls.filter(c => c.url.endsWith('/run'))[1].body.text).toBe('/settings');
  await expect(page.getByRole('dialog')).toHaveCount(0);
});
test('accepting an earlier message preserves a newer skill draft', async ({ page }) => {
  const calls = await mount(page, { empty: true, delayRun: true });
  const composer = page.getByRole('combobox', { name: 'Message the agent' });
  await composer.fill('First request'); await composer.press('Enter');
  await composer.fill('/review My next request'); await composer.press('Tab');
  await expect(page.getByText('Streamed response', { exact: true })).toBeVisible();
  await expect(composer).toHaveValue('My next request');
  await expect(page.getByRole('button', { name: 'Remove selected skill' })).toBeVisible();
  await page.getByRole('button', { name: 'Send message', exact: true }).click();
  await expect.poll(() => calls.filter(c => c.url.endsWith('/run')).length).toBe(2);
  expect(calls.filter(c => c.url.endsWith('/run'))[1].body.skill).toBe(catalog.skills[1].path);
});

for (const viewport of [{ width: 1440, height: 960 }, { width: 390, height: 844 }]) {
  test(`long chat and inspector scroll within the viewport at ${viewport.width}px`, async ({ page }) => {
    await page.setViewportSize(viewport);
    await mount(page, { longContent: true });
    await expect(page.getByText(/^Message 40\./)).toBeVisible();
    if (await page.getByRole('button', { name: 'Hide sidebar' }).isVisible()) await page.getByRole('button', { name: 'Hide sidebar' }).click();
    const chat = page.locator('.conversation-scroll');
    const composer = page.locator('.composer-region');
    await expect.poll(() => chat.evaluate(el => el.scrollHeight > el.clientHeight)).toBe(true);
    const composerBox = await composer.boundingBox();
    expect(composerBox).not.toBeNull();
    expect(composerBox!.y + composerBox!.height).toBeLessThanOrEqual(viewport.height + 1);
    expect(await page.evaluate(() => document.documentElement.scrollHeight)).toBeLessThanOrEqual(viewport.height + 1);
    await chat.evaluate(el => { el.scrollTop = 0; });
    await expect.poll(() => chat.evaluate(el => el.scrollTop)).toBe(0);
    await chat.hover();
    await page.mouse.wheel(0, 600);
    await expect.poll(() => chat.evaluate(el => el.scrollTop)).toBeGreaterThan(0);
    await page.getByRole('combobox', { name: 'Message the agent' }).fill('/skills');
    await page.getByRole('combobox', { name: 'Message the agent' }).press('Enter');
    const inspector = page.locator('.inspector-body');
    await expect.poll(() => inspector.evaluate(el => el.scrollHeight > el.clientHeight)).toBe(true);
    await inspector.hover();
    await page.mouse.wheel(0, 600);
    await expect.poll(() => inspector.evaluate(el => el.scrollTop)).toBeGreaterThan(0);
    expect(await page.evaluate(() => document.documentElement.scrollHeight)).toBeLessThanOrEqual(viewport.height + 1);
  });
}

test('reasoning streams, tool failures arrive during the run, and saved reload preserves reasoning', async ({ page }) => {
  await mount(page, { empty: true, reasoningStream: true });
  const composer = page.getByRole('combobox', { name: 'Message the agent' });
  await composer.fill('Check the file'); await composer.press('Enter');
  await expect(page.getByText('First second.', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: /Reasoning/ }).click();
  await expect(page.getByText('File does not exist.', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: /Reasoning/ })).toHaveAttribute('aria-expanded', 'false');
  await page.getByRole('button', { name: 'Exact tool arguments', exact: true }).click();
  const work = page.locator('.work-log > .disclosure-toggle').last();
  await work.click(); await work.click();
  await expect(page.getByRole('button', { name: 'Exact tool arguments', exact: true })).toHaveAttribute('aria-expanded', 'true');
  await expect(page.getByText('Streamed response', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Send message', exact: true })).toBeVisible();
  await expect(work).toHaveAttribute('aria-expanded', 'true');
  await expect(page.getByRole('button', { name: 'Exact tool arguments', exact: true })).toHaveAttribute('aria-expanded', 'true');
  await page.reload();
  await page.getByRole('button', { name: /Reasoning/ }).click();
  await expect(page.getByText('First second.', { exact: true })).toBeVisible();
});
test('reading earlier content stays anchored when new streaming activity arrives', async ({ page }) => {
  await mount(page, { longContent: true, reasoningStream: true });
  const composer = page.getByRole('combobox', { name: 'Message the agent' });
  await composer.fill('Check the file'); await composer.press('Enter');
  const chat = page.locator('.conversation-scroll');
  await chat.evaluate(el => { el.scrollTop = 100; });
  await expect.poll(() => chat.evaluate(el => el.scrollTop)).toBeLessThan(200);
  await page.waitForTimeout(1600);
  await expect.poll(() => chat.evaluate(el => el.scrollTop)).toBeLessThan(200);
  await page.getByRole('button', { name: 'New activity', exact: true }).click();
  await expect.poll(() => chat.evaluate(el => el.scrollHeight - el.clientHeight - el.scrollTop)).toBeLessThan(24);
});
test('populated preview is isolated, interactive, and responsive', async ({ page }) => {
  const apiCalls: string[] = [];
  await page.route('http://127.0.0.1:3100/**', async route => {
    const url = new URL(route.request().url());
    if (url.pathname.startsWith('/api/')) { apiCalls.push(url.pathname); await route.abort(); return; }
    const file = path.join(root, 'dist', url.pathname);
    await route.fulfill({ body: await readFile(file), contentType: file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html' });
  });
  await page.goto('http://127.0.0.1:3100/chat-preview.html');
  await expect(page.getByText('Chat design preview', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'View requested replacement' }).click();
  await expect(page.getByText('Before updating project notes:', { exact: false })).toBeVisible();
  await page.getByRole('button', { name: 'Approve this action', exact: true }).click();
  await expect(page.getByRole('region', { name: 'Approval request' })).toHaveCount(0);
  await page.locator('.work-log > .disclosure-toggle').last().click();
  await expect(page.getByText('Approval sent', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Failure', exact: true }).click();
  await expect(page.getByText(/Link check failed/)).toBeVisible();
  await page.screenshot({ path: path.join(root, 'test-results/chat-failure-desktop.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole('button', { name: 'Question', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Submit response', exact: true })).toBeDisabled();
  await page.getByRole('button', { name: 'By workflow', exact: true }).click();
  await page.getByRole('button', { name: 'Submit response', exact: true }).click();
  await page.locator('.work-log > .disclosure-toggle').last().click();
  await expect(page.getByText('Answer sent', { exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: path.join(root, 'test-results/chat-question-mobile.png'), fullPage: true });
  expect(apiCalls).toEqual([]);
});
