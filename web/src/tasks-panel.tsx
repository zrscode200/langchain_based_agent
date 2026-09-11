import { useState } from 'react';
import { ChevronDown, ChevronLeft, ChevronRight, Sparkles, Workflow } from 'lucide-react';
import type { Agent } from './useAgent.ts';
import type { TaskRequests } from './useTaskRequests.ts';
import { TaskDetail } from './task-detail.tsx';
import { useNow } from './use-now.ts';
import { activeStatuses, deliveryLabels, deliveryState, describeTask, groupTasks, summaryLine, taskLabels, timeContext, waitingStatuses } from './task-list.ts';
export { taskLabels };

export function TasksPanel({ agent, compose, selected, setSelected, requests, review, expanded, setExpanded }: { agent: Agent; compose: (text: string) => void; selected: string; setSelected: (id: string) => void; requests: TaskRequests; review: (id: string) => void; expanded: boolean; setExpanded: (value: boolean) => void }) {
  const hasActive = agent.tasks.some(t => activeStatuses.includes(t.status));
  const now = useNow(!selected && hasActive ? 1000 : 30000);
  const [finishedOpen, setFinishedOpen] = useState<boolean | null>(null);
  if (selected) return <TaskDetail key={agent.base + selected} agent={agent} taskId={selected} requests={requests} compose={compose} back={() => { setSelected(''); setExpanded(false); }} review={() => review(selected)} expanded={expanded} setExpanded={setExpanded} />;
  const disconnected = agent.status === 'disconnected';
  const showFinished = finishedOpen ?? !hasActive;
  const unsent = new Set(requests.pending.map(t => t.task_id));
  const unread = agent.pendingResults.length;
  const capacity = agent.capacity;
  return <div className="tasks-panel">
    <div className="panel-intro"><p>Work the agent handed to subagents. Keep chatting while it runs; anything that needs you also appears above the composer.</p></div>
    {disconnected && <p className="task-stale" role="status">Connection interrupted. Showing the last known task state.</p>}
    {agent.tasks.length > 0 && <div className="task-summary-strip"><span>{summaryLine(agent.tasks)}</span>{capacity && <span title="Execution slots are shared by every conversation on this server">{capacity.running}/{capacity.max_running} slots busy{capacity.queued ? ` · ${capacity.queued} in line` : ''}</span>}</div>}
    {unread > 0 && <div className="results-ready"><Sparkles size={16} /><p>{unread} finished {unread === 1 ? 'task' : 'tasks'} the agent has not read yet. Results reach it automatically on its next turn.</p><button className="text-button" onClick={() => compose('Review the completed background work and summarize the results.')}>Ask it to review now <ChevronRight size={13} /></button></div>}
    {!agent.tasks.length ? <div className="panel-empty tall"><Workflow size={34} /><h3>No background tasks yet</h3><p>When the agent hands work to a subagent, it appears here with live status, and you can open its full conversation.</p></div>
      : groupTasks(agent.tasks).map(group => group.tasks.length > 0 && <section className={'task-group task-group-' + group.key} key={group.key}>
        {group.key === 'finished'
          ? <button className="task-group-heading" aria-expanded={showFinished} onClick={() => setFinishedOpen(!showFinished)}><span className="eyebrow">{group.title}</span><span className="task-group-count">{group.tasks.length}</span><ChevronDown size={14} /></button>
          : <div className="task-group-heading"><span className="eyebrow">{group.title}</span><span className="task-group-count">{group.tasks.length}</span></div>}
        {(group.key !== 'finished' || showFinished) && group.tasks.map(task => {
          const delivery = deliveryState(task, agent.pendingResults);
          const time = timeContext(task, now);
          return <div className={'task-item ' + task.status} key={task.task_id}>
            <button className="task-row" onClick={() => setSelected(task.task_id)}>
              <span className={'task-rail ' + task.status} aria-hidden="true" />
              <span className="task-main"><span className="task-title"><strong>{task.name}</strong><span className={'status-label ' + task.status}>{taskLabels[task.status] || task.status}</span></span><span className="task-sub">{describeTask(task)}</span></span>
              <span className="task-meta">{time && <span>{time}</span>}{delivery && <span className={'task-delivery ' + delivery}>{deliveryLabels[delivery]}</span>}<ChevronRight size={14} /></span>
            </button>
            {waitingStatuses.includes(task.status) && (unsent.has(task.task_id)
              ? <button className="task-review" disabled={disconnected} onClick={() => review(task.task_id)}><ChevronLeft size={13} />{task.status === 'needs_input' ? 'Answer in main chat' : 'Review in main chat'}</button>
              : <span className="task-review sent">Decision sent · waiting for the task to continue</span>)}
          </div>;
        })}
      </section>)}
    <p className="scope-note">Tasks and their conversations are kept by the running server. Restarting the backend clears them.</p>
  </div>;
}
