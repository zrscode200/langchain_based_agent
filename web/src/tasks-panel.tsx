import { LoaderCircle, Workflow, Sparkles, ChevronRight } from 'lucide-react';
import type { Agent } from './useAgent.ts';
import type { Task } from './protocol.ts';
import type { TaskRequests } from './useTaskRequests.ts';
import { TaskDetail } from './task-detail.tsx';
export const taskLabels: Record<string, string> = { queued: 'Queued', running: 'Running', needs_approval: 'Needs approval', needs_input: 'Needs input', completed: 'Completed', failed: 'Failed', cancelled: 'Cancelled', timed_out: 'Timed out' };
const labels = taskLabels;
const activeStatuses = ['running', 'queued', 'needs_approval', 'needs_input'];
export function TasksPanel({ agent, compose, selected, setSelected, requests, review, expanded, setExpanded }: { agent: Agent; compose: (text: string) => void; selected: string; setSelected: (id: string) => void; requests: TaskRequests; review: (id: string) => void; expanded: boolean; setExpanded: (value: boolean) => void }) {
  if (selected) return <TaskDetail key={agent.base + selected} agent={agent} taskId={selected} requests={requests} compose={compose} back={() => { setSelected(''); setExpanded(false); }} review={() => review(selected)} expanded={expanded} setExpanded={setExpanded} />;
  const active = agent.tasks.filter(t => activeStatuses.includes(t.status));
  const done = agent.tasks.filter(t => !activeStatuses.includes(t.status));
  return <div className="tasks-panel"><div className="panel-intro"><p>Follow work delegated to background agents. You can keep chatting while these tasks run.</p></div>
    {agent.pendingResults.length > 0 && <div className="results-ready"><Sparkles size={16} /><p>{agent.pendingResults.length} result{agent.pendingResults.length === 1 ? '' : 's'} ready for the agent’s next turn.</p><button className="text-button" onClick={() => compose('Review the completed background work and summarize the results.')}>Review with agent <ChevronRight size={13} /></button></div>}
    {!agent.tasks.length ? <div className="panel-empty tall"><Workflow size={34} /><h3>Room for parallel work</h3><p>Delegated tasks appear here when the agent starts background work.</p></div> : <>{[[active, 'In progress'], [done, 'Finished']].map(([items, heading]) => (items as Task[]).length > 0 && <section className="task-group" key={heading as string}><span className="eyebrow">{heading as string} · {(items as Task[]).length}</span>{(items as Task[]).map(task => <button key={task.task_id} className="task-row" onClick={() => { setSelected(task.task_id);  }}><div className="task-row-top"><span className={'status-dot ' + task.status} /><strong>{task.name}</strong><ChevronRight size={14} /></div><p>{task.description}</p><span className={'status-label ' + task.status}>{labels[task.status]}</span></button>)}</section>)}</>}
    <p className="scope-note">Background tasks and transcripts are retained by the running server. Restart retention depends on the harness configuration.</p>
  </div>;
}
