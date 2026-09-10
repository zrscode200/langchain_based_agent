import { useEffect, useRef, useState } from 'react';
import type { Agent } from './useAgent.ts';
import type { Json, Task } from './protocol.ts';
import type { DecisionReceipt } from './timeline.ts';
import { submitTaskResponse, taskRequestKey, visibleTaskInterrupts, waitingTasks } from './task-requests.ts';

export type TaskReceipt = DecisionReceipt & { taskId: string };
export function useTaskRequests(agent: Agent) {
  const owner = agent.projectId + ':' + agent.threadId;
  const locks = useRef(new Map<string, symbol>());
  const [sent, setSent] = useState<{ owner: string; keys: string[] }>({ owner, keys: [] });
  const [receipts, setReceipts] = useState<{ owner: string; items: TaskReceipt[] }>({ owner, items: [] });
  const [notice, setNotice] = useState<{ owner: string; text: string }>({ owner, text: '' });
  const sentKeys = sent.owner === owner ? sent.keys : [];
  const pending = waitingTasks(agent.tasks).filter(task => !sentKeys.includes(taskRequestKey(task)));
  useEffect(() => { locks.current.clear(); setSent({ owner, keys: [] }); setReceipts({ owner, items: [] }); setNotice({ owner, text: '' }); }, [owner]);
  useEffect(() => {
    if (!notice.text) return;
    const timer = setTimeout(() => setNotice(old => old === notice ? { owner, text: '' } : old), 3000);
    return () => clearTimeout(timer);
  }, [notice, owner]);
  const submit = async (task: Task, responses: Json) => {
    const current = agent.capture(), base = agent.base, key = taskRequestKey(task), lock = owner + key;
    if (locks.current.has(lock) || sentKeys.includes(key)) throw new Error('This response is already being submitted.');
    const claim = Symbol(); locks.current.set(lock, claim);
    try {
      await submitTaskResponse(task, responses, {
        current, list: async () => (await agent.data(base + '/background', 'POST', { operation: 'list' })).tasks,
        resume: (taskId, payload) => agent.data(base + '/background', 'POST', { operation: 'resume', task_id: taskId, responses: payload }),
      });
      if (!current()) return;
      setSent(old => ({ owner, keys: [...(old.owner === owner ? old.keys : []), key] }));
      setReceipts(old => ({ owner, items: [...(old.owner === owner ? old.items : []), { id: crypto.randomUUID(), taskId: task.task_id, interrupts: visibleTaskInterrupts(task.interrupts), responses }] }));
      setNotice({ owner, text: `Decision received · ${task.name}` });
      await agent.refresh().catch(() => { if (current()) setNotice({ owner, text: 'Decision received. Reconnect to refresh task status.' }); });
    } catch (error) {
      if (current()) void agent.refresh().catch(() => {});
      throw error;
    } finally { if (locks.current.get(lock) === claim) locks.current.delete(lock); }
  };
  return { pending, submit, receipts: receipts.owner === owner ? receipts.items : [], notice: notice.owner === owner ? notice.text : '' };
}
export type TaskRequests = ReturnType<typeof useTaskRequests>;
