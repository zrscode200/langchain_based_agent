import { useMemo } from 'react';
import { Check, Circle, Sparkles, Workflow, X } from 'lucide-react';
import { CopyButton, Mark, Prose } from './components.tsx';
import { Approvals } from './approvals.tsx';
import { ActionPreview, Disclosure, ExpansionScope, ToolView } from './tool-view.tsx';
import { messageReasoning, textContent, type Interrupt, type Json, type Message } from './protocol.ts';
import { conversationTurns, describeTool, isTool, receiptAnchor, runDescription, toolState, turnActivityKey, type DecisionReceipt } from './timeline.ts';

export function Reasoning({ message, live }: { message: Message; live: boolean }) {
  const reasoning = messageReasoning(message);
  if (!reasoning) return null;
  return <Disclosure className="reasoning-section" defaultOpen={live} storageKey={message.id && 'reasoning:' + message.id} title={<><Sparkles size={14} /><strong>Reasoning</strong></>} hint={live ? 'Live' : 'From the model'}><div className="reasoning-text"><Prose>{reasoning}</Prose></div></Disclosure>;
}
export function DecisionReceiptView({ receipt }: { receipt: DecisionReceipt }) {
  const decisions = receipt.interrupts.flatMap(item => receipt.responses[item.id]?.decisions || []);
  const answers = receipt.interrupts.filter(item => Array.isArray(receipt.responses[item.id]?.answers));
  const label = !decisions.length ? 'Answer sent' : decisions.every(d => d.type === 'approve') ? 'Approval sent' : decisions.every(d => d.type === 'reject') ? 'Rejection sent' : 'Decisions sent';
  const count = decisions.length;
  return <Disclosure className="decision-receipt" storageKey={'receipt:' + receipt.id} title={<><Check size={13} /><strong>{label}</strong></>} hint={count ? `${count} action${count === 1 ? '' : 's'}` : `${answers.length} request${answers.length === 1 ? '' : 's'}`}>
    {receipt.interrupts.map(item => {
    const reply = receipt.responses[item.id];
    if (!reply) return null;
    if (item.value.type === 'ask_user') return <div key={item.id} className="receipt-detail"><strong>Answer sent</strong>{reply.answers?.map((answer: string, i: number) => <p key={i}>{answer}</p>)}</div>;
    return (item.value.action_requests || []).map((action: Json, i: number) => <div className="receipt-detail" key={`${item.id}:${i}`}><div className="receipt-detail-heading">{reply.decisions?.[i]?.type === 'approve' ? <Check size={13} /> : <X size={13} />}<strong>{reply.decisions?.[i]?.type === 'approve' ? 'Approval sent' : 'Rejection sent'}</strong><span>{describeTool(action).label}</span></div><ActionPreview action={action} identity={`receipt:${receipt.id}:${item.id}:${i}`} /></div>);
  })}<small>Decision submitted. Tool activity shows the execution result. This receipt is kept for this visit.</small></Disclosure>;
}
export function Conversation({ messages, interrupts = [], running = false, disconnected = false, receipts = [], submit, scope = '', showRequests = true, userLabel = 'You', agentLabel = 'Agent', manageExpansion = true }: { messages: Message[]; interrupts?: Interrupt[]; running?: boolean; disconnected?: boolean; receipts?: DecisionReceipt[]; submit: (responses: Json) => Promise<unknown>; scope?: string; showRequests?: boolean; userLabel?: string; agentLabel?: string; manageExpansion?: boolean }) {
  const turns = useMemo(() => conversationTurns(messages), [messages]);
  const status = runDescription(messages, running, interrupts, disconnected);
  const receiptMessages = new Map<string, DecisionReceipt[]>();
  for (const receipt of receipts) {
    const anchor = receiptAnchor(receipt, messages);
    if (anchor) receiptMessages.set(anchor, [...(receiptMessages.get(anchor) || []), receipt]);
  }
  const renderReceipts = (message?: Message) => (message?.id ? receiptMessages.get(message.id) || [] : []).map(receipt => <DecisionReceiptView receipt={receipt} key={receipt.id} />);
  const body = <div className="chat-timeline" key={scope}>
    {turns.map((turn, index) => {
      const latest = index === turns.length - 1, active = latest && running;
      const waiting = latest ? interrupts : [];
      const assistants = turn.messages.filter(m => !isTool(m));
      const last = assistants.at(-1);
      const answer = last && !last.tool_calls?.length ? last : undefined;
      const work = assistants.filter(m => m !== answer);
      const calls = work.flatMap(m => m.tool_calls || []);
      const failures = calls.filter(c => toolState(c, turn.messages, active, waiting) === 'failed').length;
      const intro = work[0] && textContent(work[0].content);
      const hasWork = work.some(m => textContent(m.content) || messageReasoning(m) || m.tool_calls?.length);
      const orphanOutputs = turn.messages.filter(m => isTool(m) && !assistants.some(a => a.tool_calls?.some(c => c.id && c.id === m.tool_call_id)));
      return <section className="chat-turn" key={turn.id} aria-label={`Turn ${index + 1}`}>
        {turn.user && <div className="user-message"><div className="turn-label">{userLabel}{turn.user.additional_kwargs?.__skill && <span className="pill">/{turn.user.additional_kwargs.__skill.name}</span>}<CopyButton text={textContent(turn.user.content)} /></div>{turn.user.additional_kwargs?.__skill ? <><Prose>{turn.user.additional_kwargs.__skill.args || `Invoked ${turn.user.additional_kwargs.__skill.name}`}</Prose><Disclosure storageKey={turn.user.id && 'skill:' + turn.user.id} title="Skill instructions sent"><Prose>{textContent(turn.user.content)}</Prose></Disclosure></> : <Prose>{textContent(turn.user.content)}</Prose>}</div>}
        <div className="agent-turn"><div className="agent-byline"><span className="agent-emblem"><Mark small /></span><strong>{agentLabel}</strong>{latest && status && <span className={'turn-status ' + (waiting.length ? 'waiting' : disconnected ? 'interrupted' : '')} role="status"><Circle size={7} fill="currentColor" />{status}</span>}</div>
          {renderReceipts(turn.user)}
          {intro && <div className="agent-update"><Prose>{intro}</Prose></div>}
          {hasWork && <Disclosure storageKey={turnActivityKey(turn)} className="work-log" defaultOpen={active || waiting.length > 0 || !answer || failures > 0} title={<><span className="work-log-mark">{active ? <span className="activity-pulse" /> : failures ? <X size={14} /> : <Workflow size={14} />}</span><strong>{active ? 'Working' : waiting.length ? 'Activity so far' : 'Activity'}</strong></>} hint={failures ? `${failures} failed · ${calls.length} actions` : calls.length ? `${calls.length} action${calls.length === 1 ? '' : 's'}` : 'Agent updates'}>
            {work.map((message, i) => <div className="work-step" key={message.id || i}>
              {i > 0 && textContent(message.content) && <div className="agent-update"><Prose>{textContent(message.content)}</Prose></div>}
              <Reasoning message={message} live={active && message === last && !message.tool_calls?.length} />
              {message.tool_calls?.map((call, c) => <ToolView key={call.id || c} call={call} messages={turn.messages} active={active} interrupts={waiting} />)}
              {renderReceipts(message)}
            </div>)}
          </Disclosure>}
          {orphanOutputs.map((message, i) => <div key={message.id || i}><Disclosure storageKey={message.id && 'output:' + message.id} className="retained-output" title={`Retained tool output · ${message.name || 'tool'}`}><pre>{textContent(message.content)}</pre></Disclosure>{renderReceipts(message)}</div>)}
          {answer && <><Reasoning message={answer} live={active && !textContent(answer.content)} /><div className={'agent-answer ' + (hasWork ? 'after-work' : '')}><Prose>{textContent(answer.content)}</Prose>{!active && <div className="answer-actions"><CopyButton text={textContent(answer.content)} /></div>}</div>{renderReceipts(answer)}</>}
          {latest && showRequests && <Approvals key={scope} interrupts={waiting} submit={submit} disabled={running} />}
        </div>
      </section>;
    })}
    {!turns.length && showRequests && interrupts.length > 0 && <Approvals key={scope} interrupts={interrupts} submit={submit} disabled={running} />}
    {!turns.length && running && <div className="live-progress" role="status"><span className="activity-pulse" />{status}</div>}
  </div>;
  return manageExpansion ? <ExpansionScope key={scope}>{body}</ExpansionScope> : body;
}
