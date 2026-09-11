import http from 'node:http';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { Readable } from 'node:stream';
import { parseArgs } from 'node:util';
import { launchProject } from './workspaces.ts';
import { createApp } from './app.ts';

const { values } = parseArgs({ options: { workspace: { type: 'string', multiple: true }, backend: { type: 'string', default: 'http://127.0.0.1:2024' }, port: { type: 'string', default: '3100' }, dev: { type: 'boolean' }, help: { type: 'boolean' } } });
if (values.help || !values.workspace?.length) {
  console.log('Usage: pnpm start --workspace /absolute/project [--backend http://127.0.0.1:2024] [--port 3100]\nUse pnpm dev with the same arguments for frontend development. Start the agent backend with python -m lc_factory.web_backend --cwd /absolute/project.');
  process.exit(values.help ? 0 : 1);
}
const port = Number(values.port);
if (!Number.isInteger(port) || port < 1024 || port > 65535) throw new Error('Choose a port between 1024 and 65535.');
const root = path.resolve(import.meta.dirname, '..');
const projects = [await launchProject(values.workspace)];
const origin = `http://127.0.0.1:${port}`;
const app = createApp({ projects, backend: values.backend!, apiKey: process.env.LC_WEB_API_KEY, origin });
const vite = values.dev ? await (await import('vite')).createServer({ root, server: { middlewareMode: true, hmr: false, allowedHosts: ['127.0.0.1'] } }) : null;
const csp = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; font-src 'self'; frame-src 'self' blob:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'";
const server = http.createServer(async (req, res) => {
  if (req.headers.host !== `127.0.0.1:${port}`) { res.writeHead(403).end('Use ' + origin); return; }
  res.setHeader('Content-Security-Policy', csp);
  res.setHeader('Referrer-Policy', 'no-referrer');
  res.setHeader('X-Content-Type-Options', 'nosniff');
  const url = new URL(req.url || '/', origin);
  if (url.pathname.startsWith('/api/')) {
    const controller = new AbortController();
    res.on('close', () => controller.abort());
    try {
      const chunks: Buffer[] = []; let size = 0;
      for await (const chunk of req) { size += chunk.length; if (size > 1_048_576) { res.writeHead(413).end('Request too large'); return; } chunks.push(chunk); }
      const response = await app(new Request(url, { method: req.method, headers: req.headers as Record<string, string>, body: chunks.length ? Buffer.concat(chunks) : undefined, signal: controller.signal }));
      res.writeHead(response.status, Object.fromEntries(response.headers));
      if (response.body) Readable.fromWeb(response.body as any).on('error', () => res.end()).pipe(res);
      else res.end();
    } catch { if (!res.headersSent) res.writeHead(500); res.end(); }
    return;
  }
  if (vite) { vite.middlewares(req, res); return; }
  try {
    const relative = decodeURIComponent(url.pathname).slice(1) || 'index.html';
    const file = path.resolve(root, 'dist', relative);
    if (!file.startsWith(path.join(root, 'dist') + path.sep)) { res.writeHead(403).end(); return; }
    const mime: Record<string, string> = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.map': 'application/json' };
    res.setHeader('Content-Type', (mime[path.extname(file)] || 'application/octet-stream') + '; charset=utf-8');
    res.setHeader('Cache-Control', relative.startsWith('assets/') ? 'public, max-age=31536000, immutable' : 'no-cache');
    res.end(await readFile(file));
  } catch { res.writeHead(404).end('Build the frontend with pnpm build.'); }
});
server.listen(port, '127.0.0.1', () => console.log(`\n  Agent Workspace → ${origin}\n  Agent backend   → ${values.backend}\n  Project         → ${projects.map(p => p.name).join(', ')}\n`));
const close = () => { server.close(); void vite?.close(); };
process.on('SIGINT', close); process.on('SIGTERM', close);
