import type { Task, TaskActivity } from './protocol.ts';

export const taskLabels: Record<string, string> = { queued: 'Queued', running: 'Running', needs_approval: 'Needs approval', needs_input: 'Needs input', completed: 'Completed', failed: 'Failed', cancelled: 'Cancelled', timed_out: 'Timed out' };
export const waitingStatuses = ['needs_approval', 'needs_input'];
export const activeStatuses = ['running', 'queued', ...waitingStatuses];

export type TaskGroupKey = 'attention' | 'running' | 'queued' | 'finished';
export type TaskGroup = { key: TaskGroupKey; title: string; tasks: Task[] };

const millis = (value?: number | null) => typeof value === 'number' && Number.isFinite(value) ? value * 1000 : undefined;
const clip = (text: string, limit: number) => text.length > limit ? text.slice(0, limit - 1).trimEnd() + '…' : text;
const firstLine = (text?: string | null) => (text || '').split('\n').map(line => line.trim()).find(Boolean) || '';

/** Attention first, then live work, then the queue in order, then history newest first. */
export function groupTasks(tasks: Task[]): TaskGroup[] {
  const finished = tasks.filter(t => !activeStatuses.includes(t.status)).reverse();
  if (finished.every(t => millis(t.finished_at) !== undefined)) finished.sort((a, b) => (b.finished_at as number) - (a.finished_at as number));
  return [
    { key: 'attention', title: 'Needs you', tasks: tasks.filter(t => waitingStatuses.includes(t.status)) },
    { key: 'running', title: 'Running', tasks: tasks.filter(t => t.status === 'running') },
    { key: 'queued', title: 'Queued', tasks: tasks.filter(t => t.status === 'queued').sort((a, b) => (a.queue_position ?? Infinity) - (b.queue_position ?? Infinity)) },
    { key: 'finished', title: 'Finished', tasks: finished },
  ];
}

export function summaryLine(tasks: Task[]): string {
  const count = (statuses: string[]) => tasks.filter(t => statuses.includes(t.status)).length;
  const attention = count(waitingStatuses), running = count(['running']), queued = count(['queued']);
  const finished = tasks.length - attention - running - queued;
  const parts: string[] = [];
  if (attention) parts.push(`${attention} need${attention === 1 ? 's' : ''} you`);
  if (running) parts.push(`${running} running`);
  if (queued) parts.push(`${queued} queued`);
  if (finished) parts.push(`${finished} finished`);
  return parts.join(' · ');
}

export function formatDuration(ms: number): string {
  const total = Math.max(0, Math.round(ms / 1000));
  if (total < 60) return `${total}s`;
  const minutes = Math.floor(total / 60), seconds = total % 60;
  if (minutes < 60) return seconds ? `${minutes}m ${seconds}s` : `${minutes}m`;
  const hours = Math.floor(minutes / 60), rest = minutes % 60;
  return rest ? `${hours}h ${rest}m` : `${hours}h`;
}
export function formatAge(ms: number): string {
  const total = Math.max(0, Math.round(ms / 1000));
  if (total < 45) return 'just now';
  if (total < 3600) return `${Math.round(total / 60)}m ago`;
  if (total < 86400) return `${Math.round(total / 3600)}h ago`;
  return `${Math.round(total / 86400)}d ago`;
}
export function ordinal(n: number): string {
  const tens = n % 100;
  const suffix = tens >= 11 && tens <= 13 ? 'th' : ['th', 'st', 'nd', 'rd'][n % 10] || 'th';
  return `${n}${suffix}`;
}

export function describeActivity(latest?: TaskActivity | null): string {
  if (!latest) return '';
  if (latest.kind === 'tool' && latest.tool_name) return `${latest.tool_name}${latest.status ? ` · ${latest.status.replace(/_/g, ' ')}` : ''}`;
  return clip(firstLine(latest.text), 120);
}

/** One line on what the task is doing now, or how it ended. */
export function describeTask(task: Task): string {
  switch (task.status) {
    case 'needs_approval': {
      const action = (task.interrupts || []).flatMap(i => Array.isArray(i.value?.action_requests) ? i.value.action_requests : [])[0]?.name;
      return action ? `Waiting for your approval · ${action}` : 'Waiting for your approval';
    }
    case 'needs_input': return 'Waiting for your answer';
    case 'queued': return task.queue_position ? `${ordinal(task.queue_position)} in line for a free slot` : 'Waiting for a free slot';
    case 'running': return describeActivity(task.latest) || 'Working…';
    default: return clip(firstLine(task.result), 120) || taskLabels[task.status] || task.status;
  }
}

export type Delivery = 'waiting' | 'delivered';
export const deliveryLabels: Record<Delivery, string> = { waiting: 'Waiting for the agent', delivered: 'Delivered to the agent' };
/** Finished outcomes reach the main agent at its next turn; until then they are unread. */
export function deliveryState(task: Task, pendingResults: string[]): Delivery | null {
  if (activeStatuses.includes(task.status)) return null;
  if (typeof task.acknowledged === 'boolean') return task.acknowledged ? 'delivered' : 'waiting';
  return pendingResults.includes(task.task_id) ? 'waiting' : 'delivered';
}

/** Short time context for a row; empty when the backend sent no timestamps. */
export function timeContext(task: Task, now: number): string {
  const started = millis(task.started_at), updated = millis(task.updated_at), finished = millis(task.finished_at), queued = millis(task.queued_at);
  switch (task.status) {
    case 'running': return started === undefined ? '' : formatDuration(now - started);
    case 'queued': { const since = updated ?? queued; return since === undefined ? '' : `waiting ${formatDuration(now - since)}`; }
    case 'needs_approval': case 'needs_input': return updated === undefined ? '' : `waiting ${formatDuration(now - updated)}`;
    default: return finished === undefined ? '' : formatAge(now - finished);
  }
}

/** Sentence for the detail header. */
export function stateSentence(task: Task, now: number, pendingResults: string[]): string {
  const time = timeContext(task, now);
  switch (task.status) {
    case 'running': return time ? `Running for ${time}` : 'Running';
    case 'queued': return `${task.queue_position ? `${ordinal(task.queue_position)} in line for a free slot` : 'Waiting for a free slot'}${time ? ` · ${time}` : ''}`;
    case 'needs_approval': return `Paused for your approval${time ? ` · ${time}` : ''}`;
    case 'needs_input': return `Paused for your answer${time ? ` · ${time}` : ''}`;
    default: {
      const delivery = deliveryState(task, pendingResults);
      return `${taskLabels[task.status] || task.status}${time ? ` ${time}` : ''}${delivery ? ` · ${deliveryLabels[delivery].toLowerCase()}` : ''}`;
    }
  }
}
