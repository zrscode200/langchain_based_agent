import { test, expect, type Page } from '@playwright/test';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { artifact, catalog, files, project, snapshot, tasks, threads } from '../fixtures.ts';

const root = path.resolve(import.meta.dirname, '../..');
async function mount(page: Page, options: { empty?: boolean; approval?: boolean; delayOldState?: boolean } = {}) {
  const calls: { url: string; body: any }[] = [];
  const current = structuredClone(options.empty ? { ...snapshot, values: { messages: [] }, interrupts: [] } : snapshot) as any;
  if (options.approval) current.interrupts = [{ id: 'pause-1', value: { action_requests: [{ name: 'write_file', args: { file_path: 'notes.md', content: '# Notes' } }], review_configs: [{ action_name: 'write_file', allowed_decisions: ['approve', 'reject'] }] } }];
  await page.route('http://127.0.0.1:3100/**', async route => {
    const req = route.request(), url = new URL(req.url());
    if (!url.pathname.startsWith('/api/')) {
      const file = path.join(root, 'dist', url.pathname === '/' ? 'index.html' : url.pathname);
      await route.fulfill({ body: await readFile(file), contentType: file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html' }); return;
    }
    const body = req.postDataJSON() || {}; calls.push({ url: url.pathname, body });
    let result: unknown = {};
    if (url.pathname === '/api/bootstrap') result = { token: 'test-token', projects: [project], backend: 'http://127.0.0.1:2024' };
    else if (url.pathname.endsWith('/threads')) result = req.method() === 'POST' ? { ...threads[0], thread_id: 'new-thread' } : threads;
    else if (url.pathname.endsWith('/state')) {
      if (options.delayOldState && url.pathname.includes('conversation-1')) await new Promise(r => setTimeout(r, 450));
      result = url.pathname.includes('conversation-2') ? { values: { messages: [{ id: 'new', type: 'human', content: 'This belongs to the second conversation.' }] }, next: [], interrupts: [] } : current;
    }
    else if (url.pathname.endsWith('/background')) result = body.operation === 'inspect' ? { task: { ...tasks[0], conversation: { text: 'Retained task transcript', page: 0, pages: 1, limited: false, notice: '' } } } : { tasks, enabled: true, pending_results: [] };
    else if (url.pathname.endsWith('/catalog')) result = catalog;
    else if (url.pathname.endsWith('/mode')) result = { mode: body.mode || 'manual' };
    else if (url.pathname.endsWith('/runs')) result = [];
    else if (url.pathname.endsWith('/files')) result = url.searchParams.has('read') ? artifact : files;
    else if (url.pathname.endsWith('/run')) {
      current.interrupts = []; current.values.messages = [...(current.values.messages || []), { id: 'stream-ai', type: 'ai', content: 'Streamed response' }];
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
  await page.getByRole('button', { name: 'Start with a skill' }).click();
  await page.getByRole('button', { name: 'Use skill' }).first().click();
  await page.getByRole('textbox', { name: 'Message the agent' }).fill('Explore the memory system');
  await page.getByRole('button', { name: 'Send message', exact: true }).click();
  await expect(page.getByText('Streamed response', { exact: true })).toHaveCount(1);
  expect(calls.find(c => c.url.endsWith('/run'))?.body.skill).toBe(catalog.skills[0].path);
  await page.keyboard.press('Meta+k'); await expect(page.getByRole('dialog')).toBeVisible(); await page.keyboard.press('Escape');
  expect(errors).toEqual([]);
});
test('approval is never sent before an explicit decision and submit', async ({ page }) => {
  const calls = await mount(page, { approval: true });
  await expect(page.getByRole('button', { name: 'Submit decisions' })).toBeDisabled();
  await page.getByRole('button', { name: 'Approve', exact: true }).click();
  expect(calls.filter(c => c.url.endsWith('/run'))).toHaveLength(0);
  await page.getByRole('button', { name: 'Submit decisions' }).click();
  await expect(page.getByText('Your input is needed')).toHaveCount(0);
  expect(calls.find(c => c.url.endsWith('/run'))?.body.responses['pause-1'].decisions).toEqual([{ type: 'approve' }]);
});
test('navigation ignores late state, with project files and task inspection available', async ({ page }) => {
  await mount(page, { delayOldState: true });
  await page.getByRole('button', { name: 'Memory design exploration' }).click();
  await expect(page.getByText('This belongs to the second conversation.')).toBeVisible();
  await page.waitForTimeout(650);
  await expect(page.getByText('Explore the memory components of this agent.', { exact: false })).toHaveCount(0);
  await page.getByRole('button', { name: 'Browse project files' }).click();
  await page.getByRole('button', { name: 'architecture.md', exact: true }).click();
  await expect(page.getByRole('region', { name: 'File preview' })).toBeVisible();
  await page.getByRole('button', { name: 'Reference in chat' }).click();
  await expect(page.getByRole('textbox', { name: 'Message the agent' })).toHaveValue(/architecture.md/);
  await page.getByRole('button', { name: 'Close file preview' }).click();
  await page.getByRole('button', { name: /^Tasks/ }).click();
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
