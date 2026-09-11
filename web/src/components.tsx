import { useEffect, useRef, useState, type ReactNode } from 'react';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { Check, ChevronDown, ChevronRight, Copy, FileText, Folder, ArrowLeft, X, ShieldCheck, CircleHelp, LoaderCircle, ExternalLink } from 'lucide-react';
import type { Json } from './protocol.ts';
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
export function Mark({ small = false }: { small?: boolean }) {
  return <svg width={small ? 18 : 26} height={small ? 18 : 26} viewBox="0 0 28 28" fill="none" aria-hidden="true"><path d="M14 2 25 8.5v11L14 26 3 19.5v-11L14 2Z" stroke="currentColor" strokeWidth="1.6"/><path d="m3.5 8.5 10.5 6 10.5-6M14 14.5V26M8 5.5 19 12v10" stroke="currentColor" strokeWidth="1.6"/></svg>;
}
export function Modal({ title, close, children, wide = false }: { title: string; close: () => void; children: ReactNode; wide?: boolean }) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => { const dialog = ref.current!; const previous = document.activeElement as HTMLElement | null; dialog.showModal(); return () => { dialog.close(); previous?.focus(); }; }, []);
  return <dialog ref={ref} className={'modal ' + (wide ? 'wide' : '')} aria-label={title} onCancel={e => { e.preventDefault(); close(); }} onClick={e => { if (e.target === e.currentTarget) close(); }}><div className="modal-header"><h2>{title}</h2><IconButton label="Close dialog" onClick={close}><X size={18} /></IconButton></div>{children}</dialog>;
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
  const html = `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'"><style>html{color-scheme:light;background:#fff}body{font:15px system-ui;padding:20px;color:#26313a}img{max-width:100%}</style>${file.text}`;
  return <section className="artifact" aria-label="File preview"><div className="artifact-toolbar"><FileText size={16} /><strong title={file.path}>{file.path}</strong><span>{Math.ceil(file.size / 1024)} KB</span><IconButton label="Close file preview" onClick={close}><X size={16} /></IconButton></div><div className="artifact-actions"><div className="segmented"><button className={!source ? 'active' : ''} onClick={() => setSource(false)}>Preview</button><button className={source ? 'active' : ''} onClick={() => setSource(true)}>Source</button></div><button className="text-button" onClick={reference}>Reference in chat</button><CopyButton text={file.text} /></div>
    <div className="artifact-content">{source || file.kind === 'code' ? <pre className="source-code">{file.text}</pre> : file.kind === 'html' ? <iframe title={file.path} sandbox="" referrerPolicy="no-referrer" srcDoc={html} /> : <Prose>{file.text}</Prose>}</div>
  </section>;
}
