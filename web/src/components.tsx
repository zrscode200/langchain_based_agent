import { useEffect, useRef, useState, type ReactNode } from 'react';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { Check, ChevronDown, ChevronRight, Copy, FileText, Folder, ArrowLeft, X, ShieldCheck, CircleHelp, LoaderCircle, ExternalLink } from 'lucide-react';
import { allowedDecisions, reasoningContent, textContent, validAnswer, type Interrupt, type Json, type Message } from './protocol.ts';
import type { Agent } from './useAgent.ts';

export function IconButton({ label, children, ...props }: React.ButtonHTMLAttributes<HTMLButtonElement> & { label: string; children: ReactNode }) {
  return <button type="button" className="icon-button" aria-label={label} title={label} {...props}>{children}</button>;
}
export function Prose({ children }: { children: string }) {
  return <div className="prose"><Markdown remarkPlugins={[remarkGfm]} components={{
    a: props => <a href={props.href} target="_blank" rel="noopener noreferrer">{props.children}<ExternalLink size={12} /></a>,
    img: props => <span className="image-reference">Image: {props.alt || 'attachment'} {props.src ? `(external image withheld)` : ''}</span>,
  }}>{children}</Markdown></div>;
}
export function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return <IconButton label={copied ? 'Copied' : 'Copy'} onClick={() => { void navigator.clipboard.writeText(text).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1500); }); }}>{copied ? <Check size={14} /> : <Copy size={14} />}</IconButton>;
}
export function MessageView({ message, all }: { message: Message; all: Message[] }) {
  const role = message.type || message.role;
  const human = ['human', 'user'].includes(role || '');
  if (['tool', 'ToolMessage'].includes(role || '')) return null;
  const calls = message.tool_calls || [];
  const text = textContent(message.content);
  const thought = reasoningContent(message.content);
  const skill = message.additional_kwargs?.__skill;
  if (!text && !calls.length && !thought) return null;
  return <article className={'message ' + (human ? 'human' : 'assistant')}>
    <div className="message-gutter"><span className={'avatar ' + (human ? 'user-avatar' : '')}>{human ? 'Y' : <Mark small />}</span></div>
    <div className="message-body"><div className="message-byline">{human ? 'You' : 'Agent'}{skill && <span className="pill">/{skill.name}</span>}<div className="message-copy"><CopyButton text={text} /></div></div>
      {thought && <details className="tool-card"><summary>Reasoning provided by the model</summary><pre>{thought}</pre></details>}
      {skill ? <><p>{skill.args || `Invoked ${skill.name}`}</p><details className="tool-card"><summary>Skill instructions sent</summary><pre>{text}</pre></details></> : <Prose>{text}</Prose>}
      {calls.map((call, i) => {
        const output = all.find(m => m.tool_call_id === call.id);
        return <details className="tool-card" key={call.id || i}><summary><span className={'status-dot ' + (output ? 'completed' : 'running')} /><code>{call.name}</code><span>{output ? 'Finished' : 'Requested'}</span><ChevronDown size={14} /></summary><div className="tool-content"><span className="eyebrow">Arguments</span><pre>{JSON.stringify(call.args, null, 2)}</pre>{output && <><span className="eyebrow">Result</span><pre>{textContent(output.content)}</pre></>}</div></details>;
      })}
    </div>
  </article>;
}
export function Mark({ small = false }: { small?: boolean }) {
  return <svg width={small ? 18 : 26} height={small ? 18 : 26} viewBox="0 0 28 28" fill="none" aria-hidden="true"><path d="M14 2 25 8.5v11L14 26 3 19.5v-11L14 2Z" stroke="currentColor" strokeWidth="1.6"/><path d="m3.5 8.5 10.5 6 10.5-6M14 14.5V26M8 5.5 19 12v10" stroke="currentColor" strokeWidth="1.6"/></svg>;
}
export function Modal({ title, close, children, wide = false }: { title: string; close: () => void; children: ReactNode; wide?: boolean }) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => { const dialog = ref.current!; const previous = document.activeElement as HTMLElement | null; dialog.showModal(); return () => { dialog.close(); previous?.focus(); }; }, []);
  return <dialog ref={ref} className={'modal ' + (wide ? 'wide' : '')} aria-label={title} onCancel={e => { e.preventDefault(); close(); }} onClick={e => { if (e.target === e.currentTarget) close(); }}><div className="modal-header"><h2>{title}</h2><IconButton label="Close dialog" onClick={close}><X size={18} /></IconButton></div>{children}</dialog>;
}
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
export function Approvals({ interrupts, submit }: { interrupts: Interrupt[]; submit: (responses: Json) => Promise<unknown> }) {
  const [choices, setChoices] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const identity = interrupts.map(i => i.id).join();
  useEffect(() => { setChoices({}); setError(''); }, [identity]);
  if (!interrupts.length) return null;
  let complete = true;
  const responses: Json = {};
  for (const interrupt of interrupts) {
    const value = interrupt.value;
    if (value.type === 'ask_user') {
      responses[interrupt.id] = { answers: (value.questions || []).map((q: Json, i: number) => choices[`${interrupt.id}:${i}`] || (q.type === 'multi_select' ? '[]' : '')) };
      if (responses[interrupt.id].answers.some((a: string, i: number) => !validAnswer(value.questions[i], a))) complete = false;
    } else if (Array.isArray(value.action_requests)) {
      responses[interrupt.id] = { decisions: value.action_requests.map((_: unknown, i: number) => ({ type: choices[`${interrupt.id}:${i}`] })) };
      if (responses[interrupt.id].decisions.some((d: Json) => !d.type)) complete = false;
    } else complete = false;
  }
  return <section className="approval-card" aria-label="Agent requests"><div className="approval-heading"><ShieldCheck size={19} /><div><h3>Your input is needed</h3><p>The agent is paused until you respond.</p></div></div>
    {interrupts.map(item => <div key={item.id}>
      {item.value.type === 'ask_user' ? (item.value.questions || []).map((q: Json, i: number) => <QuestionField key={`${item.id}:${i}`} question={q} answer={choices[`${item.id}:${i}`] || ''} change={value => setChoices(old => ({ ...old, [`${item.id}:${i}`]: value }))} />)
      : Array.isArray(item.value.action_requests) ? item.value.action_requests.map((action: Json, i: number) => <div className="approval-action" key={i}><code>{action.name}</code><pre>{JSON.stringify(action.args, null, 2)}</pre><div className="decision-options">{['approve', 'reject'].filter(d => allowedDecisions(item.value, action.name).includes(d)).map(d => <button key={d} aria-pressed={choices[`${item.id}:${i}`] === d} className={choices[`${item.id}:${i}`] === d ? 'selected' : ''} onClick={() => setChoices(old => ({ ...old, [`${item.id}:${i}`]: d }))}>{d === 'approve' ? <Check size={14} /> : <X size={14} />}{d === 'approve' ? 'Approve' : 'Reject'}</button>)}</div></div>)
      : <p className="muted"><CircleHelp size={16} /> This request needs a native client capability. Continue in the TUI that owns the request; a browser decision cannot fulfill a command hook.</p>}
    </div>)}
    {error && <p role="alert" className="inline-error">{error}</p>}
    <button className="button primary" disabled={!complete || busy} onClick={() => { setBusy(true); setError(''); void submit(responses).catch(e => setError(e.message)).finally(() => setBusy(false)); }}>{busy ? <LoaderCircle className="spin" size={15} /> : <Check size={15} />}Submit {interrupts.some(i => i.value.type === 'ask_user') ? 'response' : 'decisions'}</button>
  </section>;
}
export type Artifact = { path: string; text: string; kind: string; size: number };
export function Files({ agent, open }: { agent: Agent; open: (file: Artifact) => void }) {
  const [folder, setFolder] = useState('');
  const [entries, setEntries] = useState<Json[]>([]);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [filter, setFilter] = useState('');
  const [revision, setRevision] = useState(0);
  useEffect(() => { setFolder(''); }, [agent.projectId]);
  useEffect(() => {
    if (!agent.projectId) return;
    let active = true; setLoading(true); setError('');
    agent.data(`/projects/${agent.projectId}/files?path=${encodeURIComponent(folder)}`).then(result => { if (active) { setEntries(result.entries); if (result.limited) setError('Showing the first 500 entries. Open a folder to narrow the view.'); } }).catch(e => { if (active) setError(e.message); }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [agent.projectId, folder, agent.data, revision]);
  return <div className="files-panel"><div className="panel-intro"><p>Browse this project and open files alongside the conversation.</p></div>
    <input className="search-input" aria-label="Filter files" placeholder="Filter this folder…" value={filter} onChange={e => setFilter(e.target.value)} />
    <div className="file-breadcrumb"><IconButton label="Parent folder" disabled={!folder} onClick={() => setFolder(folder.split('/').slice(0, -1).join('/'))}><ArrowLeft size={15} /></IconButton><span title={folder}>{folder || agent.project?.name}</span><button className="text-button" onClick={() => setRevision(v => v + 1)}>Refresh</button></div>
    {error && <p className="inline-error" role="alert">{error}</p>}
    {loading ? <div className="panel-empty"><LoaderCircle className="spin" />Loading files</div> : entries.filter(e => e.name.toLowerCase().includes(filter.toLowerCase())).map(entry => <button className="file-row" key={entry.path} onClick={() => {
      if (entry.directory) { setFolder(entry.path); setFilter(''); return; }
      const p = agent.projectId, current = agent.capture();
      void agent.data(`/projects/${p}/files?read=1&path=${encodeURIComponent(entry.path)}`).then(file => { if (current()) open(file); }).catch(e => { if (current()) setError(e.message); });
    }}>{entry.directory ? <Folder size={16} /> : <FileText size={16} />}<span>{entry.name}</span>{entry.directory && <ChevronRight size={14} />}</button>)}
    {!loading && !entries.length && <div className="panel-empty"><Folder size={28} /><p>This folder is empty.</p></div>}
    <p className="scope-note">Read-only · 1 MiB preview limit · symlinks and common credential files excluded</p>
  </div>;
}
export function ArtifactView({ file, close, reference }: { file: Artifact; close: () => void; reference: () => void }) {
  const [source, setSource] = useState(false);
  const html = `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'"><style>body{font:15px system-ui;padding:20px;color:#26313a}img{max-width:100%}</style>${file.text}`;
  return <section className="artifact" aria-label="File preview"><div className="artifact-toolbar"><FileText size={16} /><strong title={file.path}>{file.path}</strong><span>{Math.ceil(file.size / 1024)} KB</span><IconButton label="Close file preview" onClick={close}><X size={16} /></IconButton></div><div className="artifact-actions"><div className="segmented"><button className={!source ? 'active' : ''} onClick={() => setSource(false)}>Preview</button><button className={source ? 'active' : ''} onClick={() => setSource(true)}>Source</button></div><button className="text-button" onClick={reference}>Reference in chat</button><CopyButton text={file.text} /></div>
    <div className="artifact-content">{source || file.kind === 'code' ? <pre className="source-code">{file.text}</pre> : file.kind === 'html' ? <iframe title={file.path} sandbox="" referrerPolicy="no-referrer" srcDoc={html} /> : <Prose>{file.text}</Prose>}</div>
  </section>;
}
