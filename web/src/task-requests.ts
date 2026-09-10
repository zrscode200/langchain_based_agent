import type { Interrupt, Json, Task } from './protocol.ts';

export function visibleTaskInterrupts(interrupts: Interrupt[] = []): Interrupt[] {
  return interrupts.map(({ id, value }) => {
    if (value?.type === 'ask_user') return { id, value: { type: 'ask_user', questions: value.questions } };
    if (Array.isArray(value?.action_requests)) return { id, value: { action_requests: value.action_requests, review_configs: value.review_configs } };
    return { id, value: { type: 'unsupported_input' } };
  });
}
export function taskRequestKey(task: Pick<Task, 'task_id' | 'interrupts'>) {
  return JSON.stringify([task.task_id, visibleTaskInterrupts(task.interrupts)]);
}
export function waitingTasks(tasks: Task[]) {
  return tasks.filter(task => ['needs_approval', 'needs_input'].includes(task.status) && task.interrupts?.length);
}
export function sameTaskRequest(tasks: Task[], requested: Task) {
  return waitingTasks(tasks).some(task => taskRequestKey(task) === taskRequestKey(requested));
}
export function responseMatches(interrupts: Interrupt[], responses: Json) {
  const ids = interrupts.map(i => i.id).sort();
  return JSON.stringify(Object.keys(responses).sort()) === JSON.stringify(ids);
}

export async function submitTaskResponse(task: Task, responses: Json, transport: {
  current: () => boolean; list: () => Promise<Task[]>; resume: (taskId: string, responses: Json) => Promise<unknown>;
}) {
  if (!responseMatches(task.interrupts, responses)) throw new Error('Review every request for this task before submitting.');
  const fresh = await transport.list();
  if (!transport.current()) throw new Error('The conversation changed.');
  if (!sameTaskRequest(fresh, task)) throw new Error('This request changed or was already handled. Review the current request.');
  await transport.resume(task.task_id, responses);
}
