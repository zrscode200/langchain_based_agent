import { useMemo } from 'react';
import { Check, Circle, Sparkles, Workflow, X } from 'lucide-react';
import { CopyButton, Mark, Prose } from './components.tsx';
import { Approvals } from './approvals.tsx';
import { Disclosure, ExpansionScope, ToolView } from './tool-view.tsx';
import { messageReasoning, textContent, type Interrupt, type Json, type Message } from './protocol.ts';
import { conversationTurns, describeTool, isTool, runDescription, toolState, turnActivityKey, type DecisionReceipt } from './timeline.ts';

function Reasoning({ message, live }: { message: Message; live: boolean }) {
  const reasoning = messageReasoning(message);
  if (!reasoning) return null;
  return <Disclosure className="reasoning-section" defaultOpen={live} storageKey={message.id && 'reasoning:' + message.id} title={<><Sparkles size={14} /><strong>Reasoning</strong></>} hint={live ? 'Live' : 'From the model'}><div className="reasoning-text"><Prose>{reasoning}</Prose></div></Disclosure>;
}
function Receipt({ receipt }: { receipt: DecisionReceipt }) {
  return <div className="decision-receipt">{receipt.interrupts.map(item => {
    const reply = receipt.responses[item.id];
    if (!reply) return null;
    if (item.value.type === 'ask_user') return <span key={item.id}><Check size={13} />Answer sent</span>;
    return (item.value.action_requests || []).map((action: Json, i: number) => <span key={`${item.id}:${i}`}>{reply.decisions?.[i]?.type === 'approve' ? <Check size={13} /> : <X size={13} />}<strong>{reply.decisions?.[i]?.type === 'approve' ? 'Approval sent' : 'Rejection sent'}</strong> · {describeTool(action).target || describeTool(action).label}</span>);
  })}<small>Sent during this visit</small></div>;
}
export function Conversation({ messages, interrupts = [], running = false, disconnected = false, receipts = [], submit, scope = '' }: { messages: Message[]; interrupts?: Interrupt[]; running?: boolean; disconnected?: boolean; receipts?: DecisionReceipt[]; submit: (responses: Json) => Promise<unknown>; scope?: string }) {
  const turns = useMemo(() => conversationTurns(messages), [messages]);
  const status = runDescription(messages, running, interrupts, disconnected);
  const receiptTurns = new Map<string, DecisionReceipt[]>();
  for (const receipt of receipts) {
    const turn = turns.find(t => t.user?.id === receipt.afterMessage || t.messages.some(m => m.id && m.id === receipt.afterMessage)) || turns.at(-1);
    if (turn) receiptTurns.set(turn.id, [...(receiptTurns.get(turn.id) || []), receipt]);
  }
  return <ExpansionScope key={scope}><div className="chat-timeline" key={scope}>
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
        {turn.user && <div className="user-message"><div className="turn-label">You{turn.user.additional_kwargs?.__skill && <span className="pill">/{turn.user.additional_kwargs.__skill.name}</span>}<CopyButton text={textContent(turn.user.content)} /></div>{turn.user.additional_kwargs?.__skill ? <><Prose>{turn.user.additional_kwargs.__skill.args || `Invoked ${turn.user.additional_kwargs.__skill.name}`}</Prose><Disclosure storageKey={turn.user.id && 'skill:' + turn.user.id} title="Skill instructions sent"><Prose>{textContent(turn.user.content)}</Prose></Disclosure></> : <Prose>{textContent(turn.user.content)}</Prose>}</div>}
        <div className="agent-turn"><div className="agent-byline"><span className="agent-emblem"><Mark small /></span><strong>Agent</strong>{latest && status && <span className={'turn-status ' + (waiting.length ? 'waiting' : disconnected ? 'interrupted' : '')} role="status"><Circle size={7} fill="currentColor" />{status}</span>}</div>
          {intro && <div className="agent-update"><Prose>{intro}</Prose></div>}
          {hasWork && <Disclosure storageKey={turnActivityKey(turn)} className="work-log" defaultOpen={active || waiting.length > 0 || !answer || failures > 0} title={<><span className="work-log-mark">{active ? <span className="activity-pulse" /> : failures ? <X size={14} /> : <Workflow size={14} />}</span><strong>{active ? 'Working' : waiting.length ? 'Activity so far' : 'Activity'}</strong></>} hint={failures ? `${failures} failed · ${calls.length} actions` : calls.length ? `${calls.length} action${calls.length === 1 ? '' : 's'}` : 'Agent updates'}>
            {work.map((message, i) => <div className="work-step" key={message.id || i}>
              {i > 0 && textContent(message.content) && <div className="agent-update"><Prose>{textContent(message.content)}</Prose></div>}
              <Reasoning message={message} live={active && message === last && !message.tool_calls?.length} />
              {message.tool_calls?.map((call, c) => <ToolView key={call.id || c} call={call} messages={turn.messages} active={active} interrupts={waiting} />)}
            </div>)}
          </Disclosure>}
          {orphanOutputs.map((message, i) => <Disclosure storageKey={message.id && 'output:' + message.id} className="retained-output" key={message.id || i} title={`Retained tool output · ${message.name || 'tool'}`}><pre>{textContent(message.content)}</pre></Disclosure>)}
          {answer && <><Reasoning message={answer} live={active && !textContent(answer.content)} /><div className={'agent-answer ' + (hasWork ? 'after-work' : '')}><Prose>{textContent(answer.content)}</Prose>{!active && <div className="answer-actions"><CopyButton text={textContent(answer.content)} /></div>}</div></>}
          {(receiptTurns.get(turn.id) || []).map(receipt => <Receipt receipt={receipt} key={receipt.id} />)}
          {latest && <Approvals key={scope} interrupts={waiting} submit={submit} disabled={running} />}
        </div>
      </section>;
    })}
    {!turns.length && interrupts.length > 0 && <Approvals key={scope} interrupts={interrupts} submit={submit} disabled={running} />}
    {!turns.length && running && <div className="live-progress" role="status"><span className="activity-pulse" />{status}</div>}
  </div></ExpansionScope>;
}
