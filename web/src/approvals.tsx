import { useRef, useState } from 'react';
import { Check, X, ShieldCheck, CircleHelp, LoaderCircle, ArrowUpRight } from 'lucide-react';
import { allowedDecisions, validAnswer, type Interrupt, type Json } from './protocol.ts';
import { ActionPreview } from './tool-view.tsx';
import { describeTool } from './timeline.ts';

function QuestionField({ question, answer, change }: { question: Json; answer: string; change: (value: string) => void }) {
  const [other, setOther] = useState('');
  const multi = question.type === 'multi_select';
  let selected: string[] = [];
  if (multi) { try { selected = JSON.parse(answer || '[]'); } catch { /* empty initial input */ } }
  const update = (value: string) => change(JSON.stringify(selected.includes(value) ? selected.filter(v => v !== value) : [...selected, value]));
  return <div className="question"><span>{question.question}{question.required === false && <small> (optional)</small>}</span>
    {question.choices && <div className="choice-options">{question.choices.map((choice: Json, i: number) => <button key={i} type="button" aria-pressed={multi ? selected.includes(choice.value) : answer === choice.value} className={(multi ? selected.includes(choice.value) : answer === choice.value) ? 'selected' : ''} onClick={() => multi ? update(choice.value) : change(answer === choice.value && question.required === false ? '' : choice.value)}>{multi && selected.includes(choice.value) && <Check size={12} />}{choice.value}</button>)}</div>}
    {multi ? <><div className="choice-options">{selected.filter(v => !question.choices?.some((c: Json) => c.value === v)).map(v => <button key={v} className="selected" onClick={() => update(v)}>{v}<X size={12} /></button>)}</div><div className="other-answer"><input aria-label={`${question.question} — other answer`} value={other} placeholder="Add another answer…" onChange={e => setOther(e.target.value)} /><button className="button" disabled={!other.trim()} onClick={() => { if (!selected.includes(other.trim())) update(other.trim()); setOther(''); }}>Add</button></div></>
      : <textarea aria-label={question.question} value={answer} onChange={e => change(e.target.value)} placeholder={question.choices ? 'Or write another answer…' : 'Your answer…'} rows={2} />}
  </div>;
}
export function Approvals({ interrupts, submit, disabled = false }: { interrupts: Interrupt[]; submit: (responses: Json) => Promise<unknown>; disabled?: boolean }) {
  const identity = JSON.stringify(interrupts);
  const current = useRef(identity); current.current = identity;
  const pending = useRef('');
  const [selection, setSelection] = useState<{ identity: string; values: Record<string, string> }>({ identity: '', values: {} });
  const [feedback, setFeedback] = useState({ identity: '', busy: false, error: '' });
  const choices = selection.identity === identity ? selection.values : {};
  const busy = disabled || (feedback.identity === identity && feedback.busy);
  const error = feedback.identity === identity ? feedback.error : '';
  if (!interrupts.length) return null;
  const change = (key: string, value: string) => setSelection({ identity, values: { ...choices, [key]: value } });
  const single = interrupts.length === 1 && interrupts[0].value.type !== 'ask_user' && interrupts[0].value.action_requests?.length === 1;
  const questionsOnly = interrupts.every(i => i.value.type === 'ask_user');
  let complete = true;
  const responses: Json = {};
  for (const interrupt of interrupts) {
    const value = interrupt.value;
    if (value.type === 'ask_user') {
      responses[interrupt.id] = { answers: (value.questions || []).map((q: Json, i: number) => choices[`${interrupt.id}:${i}`] || (q.type === 'multi_select' ? '[]' : '')) };
      if (responses[interrupt.id].answers.some((a: string, i: number) => !validAnswer(value.questions[i], a))) complete = false;
    } else if (Array.isArray(value.action_requests)) {
      responses[interrupt.id] = { decisions: value.action_requests.map((_: unknown, i: number) => ({ type: choices[`${interrupt.id}:${i}`] })) };
      if (responses[interrupt.id].decisions.some((d: Json, i: number) => !allowedDecisions(value, value.action_requests[i].name).includes(d.type))) complete = false;
    } else complete = false;
  }
  const perform = async (payload: Json) => {
    if (busy || pending.current === identity) return;
    pending.current = identity;
    setFeedback({ identity, busy: true, error: '' });
    try { await submit(payload); }
    catch (e: any) { if (current.current === identity) setFeedback({ identity, busy: false, error: e.message || 'Could not submit. Review this request and try again.' }); }
    finally {
      if (pending.current === identity) pending.current = '';
      if (current.current === identity) setFeedback(old => old.identity === identity ? { ...old, busy: false } : old);
    }
  };
  return <section className={'request-card ' + (questionsOnly ? 'question-card' : '')} aria-label={questionsOnly ? 'Agent question' : 'Approval request'}>
    <header className="request-heading">{questionsOnly ? <CircleHelp size={20} /> : <ShieldCheck size={20} />}<div><span className="request-kicker">{questionsOnly ? 'A question for you' : 'Your permission is needed'}</span><h3>{questionsOnly ? 'Help the agent continue' : single ? describeTool(interrupts[0].value.action_requests[0]).label : 'Review requested actions'}</h3></div><span className="request-waiting">Paused</span></header>
    {interrupts.map(item => <div key={item.id}>
      {item.value.type === 'ask_user' ? <fieldset disabled={busy} className="question-fields">{(item.value.questions || []).map((q: Json, i: number) => <QuestionField key={`${item.id}:${i}`} question={q} answer={choices[`${item.id}:${i}`] || ''} change={value => change(`${item.id}:${i}`, value)} />)}</fieldset>
        : Array.isArray(item.value.action_requests) ? item.value.action_requests.map((action: Json, i: number) => {
          const info = describeTool(action), allowed = allowedDecisions(item.value, action.name).filter(d => d === 'approve' || d === 'reject');
          return <div className="request-action" key={i}>
            {!single && <h4>{info.label}</h4>}{info.target && info.kind !== 'command' && <div className="request-target">{info.target}</div>}
            {typeof action.description === 'string' && <p className="request-description">{action.description}</p>}
            <ActionPreview action={action} identity={`approval:${item.id}:${i}`} />
            <div className="request-decisions">{allowed.map(decision => <button key={decision} disabled={busy} className={single ? 'button ' + (decision === 'approve' ? 'primary' : '') : 'decision-choice ' + (choices[`${item.id}:${i}`] === decision ? 'selected' : '')} aria-pressed={single ? undefined : choices[`${item.id}:${i}`] === decision} onClick={() => single ? void perform({ [item.id]: { decisions: [{ type: decision }] } }) : change(`${item.id}:${i}`, decision)}>{decision === 'approve' ? <Check size={15} /> : <X size={15} />}{single ? decision === 'approve' ? 'Approve this action' : 'Reject' : decision === 'approve' ? 'Approve' : 'Reject'}</button>)}</div>
            {!allowed.length && <p className="tool-notice">This action has no browser-supported decisions. Continue in the terminal client.</p>}
          </div>;
        }) : <p className="request-description">This request requires a native client capability. Continue in the terminal client that owns the request.</p>}
    </div>)}
    {error && <p role="alert" className="inline-error">{error}</p>}
    <footer className="request-footer"><span>{busy ? <><LoaderCircle size={13} className="spin" />Sending your response…</> : questionsOnly ? 'Your answer will be sent to the agent.' : 'Your decision applies to the actions shown here.'}</span>{!single && <button className="button primary" disabled={!complete || busy} onClick={() => void perform(responses)}><ArrowUpRight size={14} />{questionsOnly ? 'Submit response' : 'Submit decisions'}</button>}</footer>
  </section>;
}
