import { constants } from 'node:fs';
import { access, mkdir, open, opendir, realpath, rename, stat, unlink, writeFile } from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { createHash, randomUUID } from 'node:crypto';
import { HttpError } from './files.ts';
import type { Project } from '../src/protocol.ts';

const projectId = (root: string) => createHash('sha256').update(root).digest('hex').slice(0, 16);
function folderError(error: unknown): never {
  if (error instanceof HttpError) throw error;
  const code = (error as NodeJS.ErrnoException).code;
  if (code === 'ENOENT' || code === 'ENOTDIR') throw new HttpError(400, 'Folder not found. Check the path and try again.');
  if (code === 'EACCES' || code === 'EPERM') throw new HttpError(403, 'Cannot access this folder. Choose a readable folder.');
  throw new HttpError(400, 'Could not open this folder. Check the path and try again.');
}
export async function resolveFolder(input: unknown): Promise<string> {
  if (typeof input !== 'string' || !input.trim() || input.length > 4096 || /[\x00-\x1f\x7f]/.test(input)) throw new HttpError(400, 'Enter an absolute folder path.');
  const expanded = input === '~' ? os.homedir() : input.startsWith('~/') ? path.join(os.homedir(), input.slice(2)) : input;
  if (!path.isAbsolute(expanded)) throw new HttpError(400, 'Enter an absolute folder path, or start with ~/.');
  try {
    const full = await realpath(expanded);
    if (full.length > 4096 || /[\x00-\x1f\x7f]/.test(full)) throw new HttpError(400, 'This folder path contains unsupported characters or is too long.');
    if (!(await stat(full)).isDirectory()) throw new HttpError(400, 'Choose a folder, not a file.');
    await access(full, constants.R_OK | constants.X_OK);
    return full;
  } catch (error) { folderError(error); }
}
export async function workspaceProject(input: unknown, name?: unknown): Promise<Project> {
  if (name !== undefined && (typeof name !== 'string' || name.trim().length > 80 || /[\x00-\x1f\x7f]/.test(name))) throw new HttpError(400, 'Use a display name of up to 80 characters.');
  const full = await resolveFolder(input);
  return { id: projectId(full), name: (name as string | undefined)?.trim() || (path.basename(full) || full).slice(0, 80), path: full };
}
// Selection exposes directory names only. Files remain behind the project file API.
export async function browseFolders(input: unknown = os.homedir()) {
  const full = await resolveFolder(input);
  const entries: { name: string; path: string }[] = [];
  let limited = false, scanned = 0;
  try {
    for await (const entry of await opendir(full)) {
      if (++scanned > 10_000) { limited = true; break; }
      if (!entry.isDirectory() || entry.name.startsWith('.') || ['node_modules', '__pycache__'].includes(entry.name)) continue;
      if (entries.length === 500) { limited = true; break; }
      entries.push({ name: entry.name, path: path.join(full, entry.name) });
    }
  } catch (error) { folderError(error); }
  entries.sort((a, b) => a.name.localeCompare(b.name));
  return { path: full, parent: path.dirname(full) === full ? null : path.dirname(full), entries, limited };
}

export function workspaceStore(file: string, initial: Project[]) {
  async function saved(): Promise<Project[]> {
    let handle;
    try {
      handle = await open(file, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
      const info = await handle.stat();
      if (!info.isFile() || info.size > 1_048_576) throw new Error('Invalid registry');
      const bytes = Buffer.alloc(1_048_577);
      const { bytesRead } = await handle.read(bytes, 0, bytes.length, 0);
      if (bytesRead > 1_048_576) throw new Error('Invalid registry');
      const data = JSON.parse(bytes.subarray(0, bytesRead).toString('utf8'));
      if (data.version !== 1 || !Array.isArray(data.projects) || data.projects.length > 100) throw new Error('Invalid registry');
      return data.projects.map((p: Project) => {
        if (!p || typeof p.path !== 'string' || !path.isAbsolute(p.path) || p.path.length > 4096 || /[\x00-\x1f\x7f]/.test(p.path) || p.id !== projectId(p.path) || typeof p.name !== 'string' || !p.name.trim() || p.name.length > 80 || /[\x00-\x1f\x7f]/.test(p.name)) throw new Error('Invalid registry');
        return { id: p.id, path: p.path, name: p.name };
      });
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === 'ENOENT') return [];
      throw new HttpError(503, 'Cannot read saved workspaces. Check the workspace store file before retrying; it has not been overwritten.');
    } finally { await handle?.close(); }
  }
  async function list() {
    // Stored names win over startup defaults. Missing/offline roots remain listed.
    return [...new Map([...initial, ...await saved()].map(p => [p.id, p])).values()];
  }
  let pending: Promise<unknown> = Promise.resolve();
  function add(input: unknown, name?: unknown) {
    const operation = pending.then(async () => {
      const project = await workspaceProject(input, name);
      let lock;
      const temporary = `${file}.${randomUUID()}.tmp`;
      try {
        await mkdir(path.dirname(file), { recursive: true, mode: 0o700 });
        try { lock = await open(file + '.lock', 'wx', 0o600); }
        catch (error) {
          if ((error as NodeJS.ErrnoException).code === 'EEXIST') throw new HttpError(409, 'The workspace list is being updated. Try again shortly.');
          throw error;
        }
        const projects = await list();
        const existing = projects.find(p => p.id === project.id);
        if (existing) return { project: existing, projects, created: false };
        if (projects.length >= 100) throw new HttpError(400, 'This workspace list is full (100 folders).');
        projects.push(project);
        await writeFile(temporary, JSON.stringify({ version: 1, projects }, null, 2) + '\n', { flag: 'wx', mode: 0o600 });
        await rename(temporary, file);
        return { project, projects, created: true };
      } catch (error) {
        if (error instanceof HttpError) throw error;
        throw new HttpError(503, 'Could not save this workspace. Check that the workspace store is writable and try again.');
      } finally {
        await unlink(temporary).catch(() => {});
        if (lock) { await lock.close(); await unlink(file + '.lock'); }
      }
    });
    pending = operation.catch(() => {});
    return operation;
  }
  return { list, add, browse: browseFolders };
}
export type WorkspaceStore = ReturnType<typeof workspaceStore>;
