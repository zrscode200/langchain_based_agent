import { ChevronDown, ChevronRight, Workflow } from 'lucide-react';
import { Approvals } from './approvals.tsx';
import type { Interrupt, Json, Task } from './protocol.ts';
import { visibleTaskInterrupts } from './task-requests.ts';
import type { TaskRequests } from './useTaskRequests.ts';

export function Attention({ requests, interrupts, running, atBottom, selected, select, inspect, submitMain }: {
  requests: TaskRequests; interrupts: Interrupt[]; running: boolean; atBottom: boolean; selected: string | null;
  select: (id: string | null) => void; inspect: (task: Task) => void; submitMain: (responses: Json) => Promise<unknown>;
}) {
  const rows = [...(interrupts.length ? [{ id: 'main', name: 'Main agent', interrupts, task: null }] : []), ...requests.pending.map(task => ({ id: task.task_id, name: task.name, interrupts: visibleTaskInterrupts(task.interrupts), task }))];
  if (!rows.length && !requests.notice) return null;
  const collapsed = selected === '__collapsed';
  const active = rows.find(row => row.id === selected) || (rows.length === 1 && atBottom && !collapsed ? rows[0] : undefined);
  const expanded = !!active && !collapsed;
  return <section className="attention" aria-label="Needs your attention">
    {!!rows.length && <><div className="attention-heading"><button aria-expanded={expanded} onClick={() => { select(expanded ? '__collapsed' : rows[0].id); }}><Workflow size={15} /><strong>Needs your attention</strong><span>{rows.length} {rows.length === 1 ? 'agent' : 'agents'}</span>{expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}</button></div>
    {rows.length > 1 && <div className="attention-queue" aria-label="Waiting agents">{rows.map(row => <button key={row.id} aria-pressed={expanded && active?.id === row.id} onClick={() => { select(row.id); }}>{row.name}<span>{row.interrupts.every(i => i.value.type === 'ask_user') ? 'Question' : 'Permission'}</span></button>)}</div>}
    {expanded && active && <div className="attention-body"><div className="attention-owner"><strong>{active.name}</strong>{active.task && <button className="text-button" onClick={() => inspect(active.task!)}>View task context</button>}</div><Approvals key={active.id} interrupts={active.interrupts} disabled={!active.task && running} submit={active.task ? responses => requests.submit(active.task!, responses) : submitMain} /></div>}</>}
    {requests.notice && <div className="attention-notice" role="status">{requests.notice}</div>}
  </section>;
}
