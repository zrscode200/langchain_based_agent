import { useEffect, useRef, useState } from 'react';
import { ChevronDown, ChevronLeft, ChevronRight, Sparkles, Workflow, X } from 'lucide-react';
import type { Agent } from './useAgent.ts';
import type { Task } from './protocol.ts';
import type { TaskRequests } from './useTaskRequests.ts';
import { TaskDetail } from './task-detail.tsx';
import { useNow } from './use-now.ts';
import { activeStatuses, deliveryLabels, deliveryState, describeTask, groupTasks, summaryLine, taskLabels, timeContext, waitingStatuses } from './task-list.ts';
export { taskLabels };

function TaskRow({ task, now, pendingResults, open }: { task: Task; now: number; pendingResults: string[]; open: (id: string) => void }) {
  const delivery = deliveryState(task, pendingResults);
  const time = timeContext(task, now);
  return <button className="task-row" onClick={() => open(task.task_id)}>
    <span className={'task-rail ' + task.status} aria-hidden="true" />
    <span className="task-main"><span className="task-title"><strong>{task.name}</strong><span className={'status-label ' + task.status}>{taskLabels[task.status] || task.status}</span></span><span className="task-sub">{describeTask(task)}</span></span>
    <span className="task-meta">{time && <span>{time}</span>}{delivery && <span className={'task-delivery ' + delivery}>{deliveryLabels[delivery]}</span>}<ChevronRight size={14} /></span>
  </button>;
}

/** Finished work lives in its own dropdown, so the main list stays about live subagents. */
export function FinishedRows({ tasks, now, pendingResults, open }: { tasks: Task[]; now: number; pendingResults: string[]; open: (id: string) => void }) {
  if (!tasks.length) return <p className="tool-notice">No finished tasks in this conversation yet.</p>;
  return <>{tasks.map(task => <div className={'task-item ' + task.status} key={task.task_id}><TaskRow task={task} now={now} pendingResults={pendingResults} open={open} /></div>)}</>;
}

export function TasksPanel({ agent, compose, selected, setSelected, requests, review, expanded, setExpanded }: { agent: Agent; compose: (text: string) => void; selected: string; setSelected: (id: string) => void; requests: TaskRequests; review: (id: string) => void; expanded: boolean; setExpanded: (value: boolean) => void }) {
  const hasActive = agent.tasks.some(t => activeStatuses.includes(t.status));
  const now = useNow(!selected && hasActive ? 1000 : 30000);
  const [finishedOpen, setFinishedOpen] = useState(false);
  const strip = useRef<HTMLDivElement>(null);
  useEffect(() => setFinishedOpen(false), [agent.threadId]);
  useEffect(() => {
    if (!finishedOpen || selected) return;
    const away = (event: MouseEvent) => { if (!strip.current?.contains(event.target as Node)) setFinishedOpen(false); };
    const key = (event: KeyboardEvent) => { if (event.key === 'Escape') setFinishedOpen(false); };
    document.addEventListener('mousedown', away); document.addEventListener('keydown', key);
    return () => { document.removeEventListener('mousedown', away); document.removeEventListener('keydown', key); };
  }, [finishedOpen, selected]);
  if (selected) return <TaskDetail key={agent.base + selected} agent={agent} taskId={selected} requests={requests} compose={compose} back={() => { setSelected(''); setExpanded(false); }} review={() => review(selected)} expanded={expanded} setExpanded={setExpanded} />;
  const disconnected = agent.status === 'disconnected';
  const groups = groupTasks(agent.tasks);
  const live = groups.filter(group => group.key !== 'finished');
  const finished = groups.find(group => group.key === 'finished')?.tasks || [];
  const liveCount = agent.tasks.length - finished.length;
  const unsent = new Set(requests.pending.map(t => t.task_id));
  const unread = agent.pendingResults.length;
  const capacity = agent.capacity;
  return <div className="tasks-panel">
    <div className="panel-intro"><p>Work the agent handed to subagents. Keep chatting while it runs; anything that needs you also appears above the composer.</p></div>
    {disconnected && <p className="task-stale" role="status">Connection interrupted. Showing the last known task state.</p>}
    {agent.tasks.length > 0 && <div className="task-summary-strip" ref={strip}>
      <span className="task-summary-text">{summaryLine(agent.tasks) || 'Nothing running right now'}</span>
      {finished.length > 0 && <button className={'finished-toggle' + (finishedOpen ? ' open' : '')} aria-expanded={finishedOpen} aria-controls="finished-tasks" onClick={() => setFinishedOpen(!finishedOpen)}>Finished<span className="task-group-count">{finished.length}</span>{unread > 0 && <i className="unread-dot" role="img" aria-label={`${unread} not yet read by the agent`} />}<ChevronDown size={13} /></button>}
      {finishedOpen && <div id="finished-tasks" className="finished-menu" role="region" aria-label="Finished tasks"><div className="finished-menu-head"><span>Finished tasks · newest first</span><button className="icon-button" aria-label="Close finished tasks" onClick={() => setFinishedOpen(false)}><X size={14} /></button></div><FinishedRows tasks={finished} now={now} pendingResults={agent.pendingResults} open={setSelected} /></div>}
    </div>}
    {capacity && agent.tasks.length > 0 && <p className="task-capacity">{capacity.running} of {capacity.max_running} server slots busy{capacity.queued ? ` · ${capacity.queued} in line` : ''}</p>}
    {unread > 0 && <div className="results-ready"><Sparkles size={16} /><p>{unread} finished {unread === 1 ? 'task' : 'tasks'} the agent has not read yet. Results reach it automatically on its next turn.</p><button className="text-button" onClick={() => compose('Review the completed background work and summarize the results.')}>Ask it to review now <ChevronRight size={13} /></button></div>}
    {!agent.tasks.length ? <div className="panel-empty tall"><Workflow size={34} /><h3>No background tasks yet</h3><p>When the agent hands work to a subagent, it appears here with live status, and you can open its full conversation.</p></div>
      : !liveCount ? <div className="panel-empty"><Workflow size={28} /><h3>All quiet</h3><p>No subagent is running. {finished.length} finished {finished.length === 1 ? 'task is' : 'tasks are'} in the Finished list.</p><button className="button" onClick={() => setFinishedOpen(true)}>Show finished tasks</button></div>
      : live.map(group => group.tasks.length > 0 && <section className={'task-group task-group-' + group.key} key={group.key}>
        <div className="task-group-heading"><span className="eyebrow">{group.title}</span><span className="task-group-count">{group.tasks.length}</span></div>
        {group.tasks.map(task => <div className={'task-item ' + task.status} key={task.task_id}>
          <TaskRow task={task} now={now} pendingResults={agent.pendingResults} open={setSelected} />
          {waitingStatuses.includes(task.status) && (unsent.has(task.task_id)
            ? <button className="task-review" disabled={disconnected} onClick={() => review(task.task_id)}><ChevronLeft size={13} />{task.status === 'needs_input' ? 'Answer in main chat' : 'Review in main chat'}</button>
            : <span className="task-review sent">Decision sent · waiting for the task to continue</span>)}
        </div>)}
      </section>)}
    <p className="scope-note">Tasks and their conversations are kept by the running server. Restarting the backend clears them.</p>
  </div>;
}
