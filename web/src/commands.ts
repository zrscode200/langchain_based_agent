import type { Catalog } from './protocol.ts';

export type CommandAction = 'model' | 'settings' | 'skills' | 'tools' | 'tasks' | 'files' | 'rename' | 'new' | 'compact' | 'help';
export type ComposerCommand = {
  id: string;
  name: string;
  description: string;
  kind: 'command' | 'skill';
  action?: CommandAction;
  skillPath?: string;
};

const commands: [CommandAction, string][] = [
  ['model', 'Choose the model for your next message'],
  ['settings', 'Open appearance and app settings'],
  ['skills', 'Browse the workspace’s loaded skills'],
  ['tools', 'Inspect available tools'],
  ['tasks', 'Open background work'],
  ['files', 'Browse project files'],
  ['rename', 'Rename this conversation'],
  ['new', 'Start a conversation'],
  ['compact', 'Review conversation compaction options'],
  ['help', 'Show web commands and keyboard shortcuts'],
];

export function composerCommands(skills: Catalog['skills']): ComposerCommand[] {
  const result: ComposerCommand[] = commands.map(([action, description]) => ({ id: action, name: '/' + action, description, kind: 'command', action }));
  const used = new Set(result.map(c => c.name));
  const paths = new Set<string>();
  for (const skill of [...skills].sort((a, b) => a.path.localeCompare(b.path) || a.name.localeCompare(b.name) || a.description.localeCompare(b.description))) {
    if (paths.has(skill.path)) continue;
    paths.add(skill.path);
    const slug = skill.name.toLowerCase().replace(/[^a-z0-9_-]+/g, '-').replace(/^-+|-+$/g, '') || 'skill';
    const base = used.has('/' + slug) ? '/skill-' + slug : '/' + slug;
    let name = base, suffix = 2;
    while (used.has(name)) name = base + '-' + suffix++;
    used.add(name);
    result.push({ id: 'skill:' + skill.path, name, description: skill.description, kind: 'skill', skillPath: skill.path });
  }
  return result;
}

export function parseSlash(text: string): { name: string; args: string } | null {
  const match = text.trimStart().match(/^\/([^\s]*)([\s\S]*)$/);
  return match ? { name: '/' + match[1].toLowerCase(), args: match[2].trimStart() } : null;
}

export function filterCommands(entries: ComposerCommand[], name: string): ComposerCommand[] {
  const query = name.replace(/^\//, '').toLowerCase();
  return entries.filter(c => c.name.slice(1).includes(query) || c.description.toLowerCase().includes(query))
    .sort((a, b) => Number(b.name === '/' + query) - Number(a.name === '/' + query)
      || Number(b.name.startsWith('/' + query)) - Number(a.name.startsWith('/' + query)));
}
