import type { Json, Message } from './protocol.ts';
import { isTool, toolOutput } from './timeline.ts';

export type SkillActivity = { name: string; path: string; description?: string; source?: string; origin: 'agent'; status: 'loading' | 'loaded' | 'partial' | 'empty' | 'failed' | 'unavailable'; offset?: number; limit?: number };
const states = new Set(['loading', 'loaded', 'partial', 'empty', 'failed', 'unavailable']);
function recorded(message: Message | undefined, id: string): SkillActivity | undefined {
  const value = message?.additional_kwargs?.lc_skill_activity?.[id];
  if (!value || value.origin !== 'agent' || !states.has(value.status) || typeof value.name !== 'string' || !value.name.trim() || typeof value.path !== 'string' || !value.path.startsWith('/')) return;
  return { name: value.name.slice(0, 200), path: value.path.slice(0, 4096), origin: 'agent', status: value.status,
    description: typeof value.description === 'string' ? value.description.slice(0, 2000) : undefined,
    source: typeof value.source === 'string' ? value.source.slice(0, 200) : undefined,
    offset: Number.isInteger(value.offset) ? value.offset : undefined,
    limit: Number.isInteger(value.limit) ? value.limit : undefined };
}

/** Consume server observations; filenames and assistant prose are not evidence. */
export function skillActivity(call: Json, messages: Message[]): SkillActivity | undefined {
  if (call.name !== 'read_file' || typeof call.id !== 'string' || !call.id) return;
  const output = toolOutput(call, messages);
  const result = recorded(output, call.id);
  if (result) return result;
  const request = messages.find(m => !isTool(m) && m.tool_calls?.some(c => c.id === call.id));
  const pending = recorded(request, call.id);
  if (!pending) return;
  return output ? { ...pending, status: output.status === 'error' ? 'failed' : 'unavailable' } : { ...pending, status: 'loading' };
}

export function skillLabel(skill: SkillActivity, active: boolean, approval = false) {
  const labels = { loading: approval ? 'Skill read needs approval' : active ? 'Loading skill' : 'Skill read interrupted', loaded: 'Loaded skill', partial: 'Loaded skill excerpt', empty: 'No skill instructions loaded', failed: "Couldn't load skill", unavailable: 'Skill read result unavailable' };
  return labels[skill.status];
}
