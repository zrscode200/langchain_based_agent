import { constants } from 'node:fs';
import { lstat, readdir, realpath, open } from 'node:fs/promises';
import path from 'node:path';

export class HttpError extends Error {
  constructor(public status: number, message: string) { super(message); }
}
const hidden = new Set(['.git', '.env', '.ssh', '.aws', '.azure', '.gnupg', 'node_modules', '.venv', '__pycache__']);
const privateName = (name: string) => hidden.has(name) || /^\.env\./.test(name) || /\.(pem|key|p12|pfx)$/i.test(name);
const textExtensions = new Set(['.md', '.txt', '.json', '.jsonl', '.yaml', '.yml', '.toml', '.ini', '.csv', '.ts', '.tsx', '.js', '.jsx', '.py', '.css', '.html', '.htm', '.xml', '.svg', '.sh', '.sql', '.log', '.rst', '.gitignore', '.example']);
const limit = 1024 * 1024;
export async function confinedPath(root: string, relative: string) {
  if (typeof relative !== 'string' || relative.includes('\0') || relative.includes('\\') || path.isAbsolute(relative)) throw new HttpError(400, 'Use a relative workspace path.');
  const parts = relative.split('/').filter(Boolean);
  if (parts.some(p => p === '..' || p === '.' || privateName(p))) throw new HttpError(403, 'This path is outside the file browser scope.');
  let candidate = root;
  for (const part of parts) {
    candidate = path.join(candidate, part);
    if ((await lstat(candidate)).isSymbolicLink()) throw new HttpError(403, 'Symbolic links are not exposed by the file browser.');
  }
  const resolved = await realpath(candidate);
  if (resolved !== root && !resolved.startsWith(root + path.sep)) throw new HttpError(403, 'Path escaped the workspace.');
  return resolved;
}
export async function listFiles(root: string, relative = '') {
  const full = await confinedPath(root, relative);
  const entries = await readdir(full, { withFileTypes: true });
  const visible = entries.filter(e => !privateName(e.name) && !e.isSymbolicLink() && (e.isDirectory() || e.isFile()));
  visible.sort((a, b) => Number(b.isDirectory()) - Number(a.isDirectory()) || a.name.localeCompare(b.name));
  return { path: relative, limited: visible.length > 500, entries: visible.slice(0, 500).map(e => ({ name: e.name, path: [relative, e.name].filter(Boolean).join('/'), directory: e.isDirectory() })) };
}
export async function readFile(root: string, relative: string) {
  const full = await confinedPath(root, relative);
  const file = await open(full, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
  try {
    const stat = await file.stat();
    if (!stat.isFile()) throw new HttpError(400, 'Select a regular file.');
    if (stat.size > limit) throw new HttpError(413, 'Preview supports files up to 1 MiB.');
    const ext = path.extname(full).toLowerCase();
    if (!textExtensions.has(ext) && !['LICENSE', 'Dockerfile', 'Makefile'].includes(path.basename(full))) throw new HttpError(415, 'This file type has no text preview.');
    // Recheck the opened inode and each path component before exposing bytes.
    const again = await confinedPath(root, relative);
    const current = await lstat(again);
    if (current.dev !== stat.dev || current.ino !== stat.ino) throw new HttpError(409, 'File changed while opening. Refresh and try again.');
    const bytes = Buffer.alloc(limit + 1);
    const { bytesRead } = await file.read(bytes, 0, bytes.length, 0);
    if (bytesRead > limit) throw new HttpError(413, 'File grew beyond the preview limit.');
    if (bytes.subarray(0, bytesRead).includes(0)) throw new HttpError(415, 'Binary files cannot be previewed as text.');
    return { path: relative, text: bytes.subarray(0, bytesRead).toString('utf8'), size: bytesRead, kind: ['.html', '.htm'].includes(ext) ? 'html' : ext === '.md' ? 'markdown' : 'code' };
  } finally { await file.close(); }
}
