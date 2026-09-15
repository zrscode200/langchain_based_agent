import { createContext, useContext, useId, useState, type ReactNode } from 'react';
import { BookOpen, Check, ChevronRight, Circle, FileText, Search, Terminal, Workflow, Wrench, X, ShieldCheck } from 'lucide-react';
import { CopyButton } from './components.tsx';
import { textContent, type Interrupt, type Json, type Message } from './protocol.ts';
import { describeTool, toolOutput, toolState } from './timeline.ts';
import { skillActivity, skillLabel } from './skill-activity.ts';

const ExpansionContext = createContext<{ choices: Record<string, boolean>; change: (key: string, open: boolean) => void } | null>(null);
export function ExpansionScope({ children }: { children: ReactNode }) {
  const [choices, setChoices] = useState<Record<string, boolean>>({});
  return <ExpansionContext.Provider value={{ choices, change: (key, open) => setChoices(old => ({ ...old, [key]: open })) }}>{children}</ExpansionContext.Provider>;
}
function useExpansion(storageKey?: string, defaultOpen = false): [boolean, (open: boolean) => void] {
  const shared = useContext(ExpansionContext);
  const [choice, setChoice] = useState<boolean>();
  return [storageKey && shared ? shared.choices[storageKey] ?? defaultOpen : choice ?? defaultOpen,
    open => { if (storageKey && shared) shared.change(storageKey, open); else setChoice(open); }];
}
export function Disclosure({ title, children, defaultOpen = false, className = '', hint, storageKey }: { title: ReactNode; children: ReactNode; defaultOpen?: boolean; className?: string; hint?: string; storageKey?: string }) {
  const [open, setOpen] = useExpansion(storageKey, defaultOpen);
  const id = useId();
  return <div className={`disclosure ${className} ${open ? 'is-open' : ''}`}>
    <button className="disclosure-toggle" aria-expanded={open} aria-controls={id} onClick={() => setOpen(!open)}><ChevronRight size={14} className="disclosure-chevron" />{title}{hint && <span className="disclosure-hint">{hint}</span>}</button>
    <div id={id} hidden={!open} className="disclosure-content">{open && children}</div>
  </div>;
}
function TextOutput({ text, storageKey }: { text: string; storageKey?: string }) {
  const [full, setFull] = useExpansion(storageKey);
  const limit = 8000;
  return <><pre>{full ? text : text.slice(0, limit)}</pre>{text.length > limit && <button className="text-button" onClick={() => setFull(!full)}>{full ? 'Show less' : `Show full output (${text.length.toLocaleString()} characters)`}</button>}</>;
}
export function ActionPreview({ action, identity }: { action: Json; identity?: string }) {
  const info = describeTool(action);
  const before = info.args.old_string ?? info.args.old_str;
  const after = info.args.new_string ?? info.args.new_str;
  return <div className="action-preview">
    {info.kind === 'command' && info.target && <div className="command-preview"><Terminal size={14} /><pre>{info.target}</pre></div>}
    {info.kind === 'write' && typeof info.args.content === 'string' && <Disclosure storageKey={identity && identity + ':content'} title="View proposed content"><TextOutput storageKey={identity && identity + ':content:full'} text={info.args.content} /></Disclosure>}
    {info.kind === 'edit' && typeof before === 'string' && typeof after === 'string' && <Disclosure storageKey={identity && identity + ':replacement'} title="View requested replacement"><div className="replacement removed"><span>Remove</span><TextOutput storageKey={identity && identity + ':before:full'} text={before} /></div><div className="replacement added"><span>Insert</span><TextOutput storageKey={identity && identity + ':after:full'} text={after} /></div></Disclosure>}
    <Disclosure storageKey={identity && identity + ':args'} title="Exact tool arguments"><div className="detail-label">{String(action.name || 'Tool')}<CopyButton text={JSON.stringify(action.args ?? {}, null, 2)} /></div><TextOutput storageKey={identity && identity + ':args:full'} text={typeof action.args === 'string' ? action.args : JSON.stringify(action.args ?? {}, null, 2)} /></Disclosure>
  </div>;
}
const labels = { requested: 'Requested', awaiting_approval: 'Needs approval', succeeded: 'Completed', result: 'Result received', failed: 'Failed', cancelled: 'Cancelled', unavailable: 'No result recorded' };
export function ToolView({ call, messages, active, interrupts }: { call: Json; messages: Message[]; active: boolean; interrupts: Interrupt[] }) {
  const identity = call.id ? `tool:${call.id}` : undefined;
  const info = describeTool(call), output = toolOutput(call, messages), state = toolState(call, messages, active, interrupts);
  const skill = skillActivity(call, messages);
  const Icon = skill ? BookOpen : info.kind === 'command' ? Terminal : info.kind === 'search' ? Search : info.kind === 'task' ? Workflow : ['read', 'write', 'edit'].includes(info.kind) ? FileText : Wrench;
  const StatusIcon = state === 'succeeded' ? Check : state === 'failed' || state === 'cancelled' ? X : state === 'awaiting_approval' ? ShieldCheck : Circle;
  return <div className={'tool-entry ' + state}>
    <Disclosure storageKey={identity} defaultOpen={state === 'failed'} title={<><Icon size={15} /><span className="tool-description"><strong>{skill ? skillLabel(skill, active, state === 'awaiting_approval') + ': ' + skill.name : info.label}</strong>{!skill && info.target && info.kind !== 'command' && <span title={info.target}>{info.target}</span>}{info.kind === 'command' && info.target && <code title={info.target}>{info.target}</code>}</span><span className="tool-state">{skill ? 'Agent selected' : <><StatusIcon size={13} />{labels[state]}</>}</span></>}>
      {skill && <div className="tool-notice"><p>{skill.description}</p><p>{skill.source && skill.source + ' · '}{skill.path}</p><p>{skill.status === 'partial' ? 'Only an excerpt of the instructions was loaded. ' : ''}This records an instruction read; it does not confirm the skill was followed.</p></div>}
      <ActionPreview action={call} identity={identity} />
      {output && <div className="tool-result"><div className="detail-label">{state === 'failed' ? 'Error output' : 'Result'}<CopyButton text={textContent(output.content)} /></div><TextOutput storageKey={identity && identity + ':result:full'} text={textContent(output.content) || '(No text output)'} /></div>}
      {!output && <p className="tool-notice">{active ? 'Waiting for a result from the agent server.' : 'This conversation has no saved result for this action.'}</p>}
    </Disclosure>
  </div>;
}
