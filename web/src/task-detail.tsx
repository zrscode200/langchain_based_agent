import { useLayoutEffect, useRef, useState, useEffect } from 'react';
import { ChevronLeft, ChevronDown, Maximize2, Minimize2, Square, LoaderCircle } from 'lucide-react';
import { Modal, Prose } from './components.tsx';
import { Conversation, DecisionReceiptView, Reasoning } from './conversation.tsx';
import { Disclosure, ExpansionScope } from './tool-view.tsx';
import { textContent, type Json, type Message } from './protocol.ts';
import type { Agent } from './useAgent.ts';
import type { TaskRequests } from './useTaskRequests.ts';
import { useTaskConversation } from './useTaskConversation.ts';
import { activeStatuses, stateSentence, taskLabels } from './task-list.ts';
import { useNow } from './use-now.ts';

function FullMessage({ agent, taskId, message, close }: { agent: Agent; taskId: string; message: Message; close: () => void }) {
  const [offset, setOffset] = useState(0), [value, setValue] = useState<Json | null>(null), [error, setError] = useState('');
  useEffect(() => {
    const current = agent.capture(); let active = true; setValue(null); setError('');
    void agent.data(agent.base + '/background', 'POST', { operation: 'message', task_id: taskId, message_id: message.id, revision: message._transcript?.revision, offset }).then(result => { if (active && current()) setValue(result); }).catch(e => { if (active && current()) setError(e.message); });
    return () => { active = false; };
  }, [agent.base, agent.data, taskId, message.id, message._transcript?.revision, offset]);
  return <Modal title="Full message" close={close} wide><div className="full-task-message">{error ? <p role="alert">{error}</p> : value ? <pre>{value.text}</pre> : <p>Loading message…</p>}<div className="task-message-pages"><button className="button" disabled={!offset || !value} onClick={() => setOffset(Math.max(0, offset - 24000))}>Earlier</button><span>Complete retained message · page {Math.floor(offset / 24000) + 1}</span><button className="button" disabled={!value || value.next >= value.total} onClick={() => setOffset(value!.next)}>Later</button></div></div></Modal>;
}
function LegacyTranscript({ agent, taskId, text, pages }: { agent: Agent; taskId: string; text: string; pages: number }) {
  const [selected, setSelected] = useState<{ page: number; text: string } | null>(null), [busy, setBusy] = useState(false), [error, setError] = useState('');
  const page = selected?.page ?? Math.max(0, pages - 1);
  const load = async (target: number) => {
    const current = agent.capture(); setBusy(true); setError('');
    try {
      const result = await agent.data(agent.base + '/background', 'POST', { operation: 'inspect', task_id: taskId, transcript_page: target });
      if (current()) setSelected({ page: result.task.conversation.page, text: result.task.conversation.text });
    } catch (e: any) { if (current()) setError(e.message); }
    finally { if (current()) setBusy(false); }
  };
  return <Disclosure title="Retained text transcript" storageKey="legacy"><p className="tool-notice">This task uses the earlier text format. Structured messages become available for tasks captured by the updated backend.</p><div className="task-message-pages"><button className="button" disabled={busy || page <= 0} onClick={() => void load(page - 1)}>Earlier</button><span>Page {page + 1} / {Math.max(1, pages)}</span><button className="button" disabled={busy || page >= pages - 1} onClick={() => void load(page + 1)}>Later</button>{selected && <button className="text-button" disabled={busy} onClick={() => setSelected(null)}>Latest</button>}</div>{error && <p role="alert">{error}</p>}<pre className="legacy-task-text">{selected?.text ?? text}</pre></Disclosure>;
}

export function TaskDetail({ agent, taskId, requests, compose, back, review, expanded, setExpanded }: { agent: Agent; taskId: string; requests: TaskRequests; compose: (text: string) => void; back: () => void; review: () => void; expanded: boolean; setExpanded: (value: boolean) => void }) {
  const view = useTaskConversation(agent, taskId);
  const now = useNow(view.detail && activeStatuses.includes(view.detail.status) ? 1000 : 60000);
  const [busy, setBusy] = useState(false), [actionError, setActionError] = useState(''), [full, setFull] = useState<Message | null>(null), [follow, setFollow] = useState(true);
  const [confirmCancel, setConfirmCancel] = useState(false);
  const disconnected = agent.status === 'disconnected';
  const scroll = useRef<HTMLDivElement>(null);
  const prepend = useRef<{ height: number; top: number } | null>(null);
  const detail = view.detail;
  const completionHistory = useRef<boolean | null>(null);
  if (detail?.result && completionHistory.current === null) completionHistory.current = !follow;
  useLayoutEffect(() => {
    const el = scroll.current; if (!el) return;
    if (prepend.current && !view.loading) { el.scrollTop = prepend.current.top + el.scrollHeight - prepend.current.height; prepend.current = null; }
    else if (follow && !prepend.current) el.scrollTop = detail?.result ? 0 : el.scrollHeight;
  }, [view.messages, view.loading, detail?.result, follow]);
  const cancel = async () => {
    const current = agent.capture(); setBusy(true); setActionError('');
    try { await agent.data(agent.base + '/background', 'POST', { operation: 'cancel', task_id: taskId }); if (current()) { await agent.refresh(); view.reload(); } }
    catch (e: any) { if (current()) setActionError(e.message); }
    finally { if (current()) setBusy(false); }
  };
  const pending = requests.pending.some(task => task.task_id === taskId);
  const receipts = requests.receipts.filter(receipt => receipt.taskId === taskId);
  const active = detail && ['running', 'queued', 'needs_approval', 'needs_input'].includes(detail.status);
  const last = view.messages.filter(m => m.type === 'ai').at(-1);
  const result = detail?.result;
  const finalMessage = result && last && !last.tool_calls?.length && textContent(last.content).trim() === result.trim() ? last : undefined;
  const messages = view.messages.filter(m => m.type !== 'system' && m !== finalMessage);
  const instructions = view.messages.filter(m => m.type === 'system');
  return <ExpansionScope><div className="task-detail organized-task">
    <div className="task-nav"><button className="text-button" onClick={back}><ChevronLeft size={14} />All tasks</button><button className="text-button" onClick={() => setExpanded(!expanded)}>{expanded ? <Minimize2 size={14} /> : <Maximize2 size={14} />}{expanded ? 'Return to sidebar' : 'Expand view'}</button></div>
    {detail && <header className="task-summary"><div><h3>{detail.name}</h3><span className={'status-label ' + detail.status}><span className={'status-dot ' + detail.status} />{taskLabels[detail.status] || detail.status}</span></div><p className="task-state">{stateSentence(detail, now, agent.pendingResults)}</p><Disclosure title="Assignment" storageKey="assignment"><Prose>{detail.description}</Prose><small className="task-id">{taskId}</small></Disclosure>{active && <div className="task-actions">{detail.steerable && <button className="text-button" onClick={() => { compose(`Please guide background task ${taskId}: `); setExpanded(false); }}>Guide through main agent</button>}{confirmCancel ? <span className="task-cancel-confirm" role="group" aria-label="Confirm cancellation"><span>Cancel this task?</span><button className="text-button danger" disabled={busy} onClick={() => { setConfirmCancel(false); void cancel(); }}>Yes, cancel it</button><button className="text-button" onClick={() => setConfirmCancel(false)}>Keep it running</button></span> : <button className="text-button" disabled={busy || disconnected} onClick={() => setConfirmCancel(true)}><Square size={11} />Cancel task</button>}</div>}{pending && <button className="task-review-link" onClick={review}>{detail.status === 'needs_input' ? 'Answer in main chat' : 'Review in main chat'} <ChevronLeft size={14} /></button>}</header>}
    <div className="task-conversation-scroll" ref={scroll} onWheel={event => { if (event.deltaY < 0) setFollow(false); }} onClickCapture={event => { if ((event.target as HTMLElement).closest('.disclosure-toggle')) setFollow(false); }} onScroll={() => { const el = scroll.current!; if (!prepend.current) setFollow(el.scrollHeight - el.scrollTop - el.clientHeight < 24); }}>
      {(view.error || actionError) && <p className="inline-error" role="alert">{view.error || actionError} <button className="text-button" onClick={view.reload}>Refresh task</button></p>}
      {!detail && !view.error && <div className="panel-empty"><LoaderCircle className="spin" />Loading task</div>}
      {detail && <>
        {result && <section className="task-final"><span className="eyebrow">{detail.status === 'completed' ? 'Result' : 'Task outcome'}</span><Prose>{result}</Prose>{finalMessage && <div className="chat-timeline"><Reasoning message={finalMessage} live={false} /></div>}</section>}
        <Disclosure title={result ? 'Work history' : 'Conversation'} storageKey="history" defaultOpen={!result || completionHistory.current === true}>
          {!!view.earlier && <button className="button load-task-history" disabled={view.loading} onClick={() => { const el = scroll.current; if (el) prepend.current = { height: el.scrollHeight, top: el.scrollTop }; setFollow(false); void view.loadEarlier(); }}>{view.loading ? 'Loading…' : 'Load earlier messages'}</button>}
          {instructions.length > 0 && <Disclosure title="Agent instructions" storageKey="instructions">{instructions.map(message => <Prose key={message.id}>{textContent(message.content)}</Prose>)}</Disclosure>}
          {!!messages.length && <Conversation messages={messages} interrupts={pending ? detail.interrupts : []} running={detail.status === 'running'} scope={taskId} showRequests={false} manageExpansion={false} userLabel="Assignment / guidance" agentLabel={detail.name} submit={async () => { throw new Error('Review this request in main chat.'); }} />}
          {view.messages.filter(message => message._transcript?.truncated).map(message => <button key={message.id} className="text-button full-message-link" onClick={() => setFull(message)}>Open full {message.type === 'tool' ? `${message.name || 'tool'} output` : 'message'} · item {(message._transcript?.order || 0) + 1}</button>)}
          {view.legacy && detail.conversation && <LegacyTranscript agent={agent} taskId={taskId} text={detail.conversation.text} pages={detail.conversation.pages} />}
          {!view.legacy && !messages.length && !result && <p className="tool-notice">Waiting for the first agent update.</p>}
          {receipts.map(receipt => <DecisionReceiptView key={receipt.id} receipt={receipt} />)}
          {!!detail.steering?.length && <Disclosure title="Guidance delivery" storageKey="guidance">{detail.steering.map((item: Json) => <div className="guidance-record" key={item.message_id}><Prose>{item.message}</Prose><small>{item.delivery_outcome || item.status}</small></div>)}</Disclosure>}
          {!!detail.activity?.some(item => item.kind === 'finding') && <Disclosure title="Reported findings" storageKey="findings">{detail.activity.filter(item => item.kind === 'finding').map(item => <Prose key={item.sequence}>{item.text}</Prose>)}</Disclosure>}
        </Disclosure>
        <p className="tool-notice">{detail.conversation_records?.notice || detail.conversation?.notice}</p>
      </>}
    </div>
    {!follow && detail?.status === 'running' && <button className="task-new-activity text-button" onClick={() => setFollow(true)}>Latest activity <ChevronDown size={14} /></button>}
    {full && <FullMessage agent={agent} taskId={taskId} message={full} close={() => setFull(null)} />}
  </div></ExpansionScope>;
}
