import { constants } from 'node:fs';
import { access, realpath, stat } from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { createHash } from 'node:crypto';

/** Resolve the one project explicitly selected at launch. Never reads saved hub state. */
export async function launchProject(folders: string[] | undefined) {
  if (folders?.length !== 1) throw new Error('Start this web UI with exactly one --workspace folder. To work on another project, launch its agent and web UI separately.');
  const input = folders[0];
  if (!input || input.length > 4096 || /[\x00-\x1f\x7f]/.test(input)) throw new Error('Choose a valid project folder.');
  const expanded = input === '~' ? os.homedir() : input.startsWith('~/') ? path.join(os.homedir(), input.slice(2)) : input;
  const full = await realpath(path.resolve(expanded));
  if (full.length > 4096 || /[\x00-\x1f\x7f]/.test(full)) throw new Error('This project path contains unsupported characters or is too long.');
  if (!(await stat(full)).isDirectory()) throw new Error('The project workspace must be a folder.');
  await access(full, constants.R_OK | constants.X_OK);
  return { id: createHash('sha256').update(full).digest('hex').slice(0, 16), name: (path.basename(full) || full).slice(0, 80), path: full };
}
