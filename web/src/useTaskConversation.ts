import { useEffect, useRef, useState } from 'react';
import type { Agent } from './useAgent.ts';
import type { Message, Task } from './protocol.ts';
import { mergeTaskMessages, TaskReadQueue } from './task-conversation.ts';

export function useTaskConversation(agent: Agent, taskId: string) {
  const [detail, setDetail] = useState<Task | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [error, setError] = useState('');
  const [legacy, setLegacy] = useState(false);
  const [earlier, setEarlier] = useState(0);
  const [loading, setLoading] = useState(false);
  const generation = useRef(0);
  const cursor = useRef<number | undefined>(undefined);
  const start = useRef(0);
  const olderBusy = useRef<symbol | null>(null);
  const reads = useRef(new TaskReadQueue());
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const epoch = ++generation.current, current = agent.capture();
    let active = true, plain = false;
    const queue = new TaskReadQueue(); reads.current = queue;
    cursor.current = undefined; start.current = 0; olderBusy.current = null; setLoading(false);
    setDetail(null); setMessages([]); setError(''); setLegacy(false); setEarlier(0);
    const load = async () => {
      if (!active || !current() || generation.current !== epoch) return;
      try {
        let task: Task;
        if (!plain) {
          try { task = (await agent.data(agent.base + '/background', 'POST', { operation: 'conversation', task_id: taskId, after: cursor.current })).task; }
          catch (e: any) {
            if (e.status !== 422 && e.status !== 404) throw e;
            plain = true;
          }
        }
        if (plain) task = (await agent.data(agent.base + '/background', 'POST', { operation: 'inspect', task_id: taskId, transcript_page: -1 })).task;
        if (!active || !current() || generation.current !== epoch) return;
        const records = task!.conversation_records;
        if (records) {
          if (cursor.current === undefined) { start.current = records.before; setEarlier(records.before); }
          cursor.current = records.cursor;
          setMessages(old => mergeTaskMessages(old, records.messages, start.current));
        }
        setLegacy(plain); setDetail(task!); setError('');
      } catch (e: any) { if (active && current()) setError(e.message); }
    };
    const poll = () => { if (!queue.pending) void queue.read(load); };
    poll(); const timer = setInterval(poll, 3500);
    return () => { active = false; clearInterval(timer); generation.current++; };
  }, [agent.base, agent.data, taskId, revision]);
  const loadEarlier = async () => {
    if (olderBusy.current || !earlier) return;
    const epoch = generation.current, current = agent.capture();
    const claim = Symbol(); olderBusy.current = claim; setLoading(true);
    try {
      const task: Task | undefined = await reads.current.read(async () => {
        if (!current() || generation.current !== epoch) return;
        return (await agent.data(agent.base + '/background', 'POST', { operation: 'conversation', task_id: taskId, before: earlier })).task;
      });
      if (!current() || generation.current !== epoch || !task?.conversation_records) return;
      start.current = task.conversation_records.before;
      setEarlier(start.current); setMessages(old => mergeTaskMessages(old, task.conversation_records!.messages));
    } catch (e: any) { if (current() && generation.current === epoch) setError(e.message); }
    finally { if (generation.current === epoch && olderBusy.current === claim) { olderBusy.current = null; setLoading(false); } }
  };
  return { detail, messages, error, legacy, earlier, loading, loadEarlier, reload: () => setRevision(n => n + 1) };
}
