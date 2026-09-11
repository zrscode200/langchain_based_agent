import { useEffect, useRef, useState } from 'react';
import { ArrowUp, ChevronRight, Folder, Home, LoaderCircle } from 'lucide-react';
import { Modal } from './components.tsx';
import type { Agent } from './useAgent.ts';
import './workspaces.css';

type FolderListing = { path: string; parent: string | null; entries: { name: string; path: string }[]; limited: boolean };
export function AddWorkspace({ agent, close }: { agent: Agent; close: () => void }) {
  const [folder, setFolder] = useState(agent.project?.path || '');
  const [name, setName] = useState('');
  const [listing, setListing] = useState<FolderListing | null>(null);
  const [filter, setFilter] = useState('');
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const revision = useRef(0);
  const alive = useRef(true);
  const submitting = useRef(false);
  async function browse(path?: string) {
    const current = ++revision.current;
    setLoading(true); setError('');
    try {
      const result: FolderListing = await agent.data('/projects/browse' + (path ? '?path=' + encodeURIComponent(path) : ''));
      if (!alive.current || revision.current !== current) return;
      setListing(result); setFolder(result.path); setFilter('');
    } catch (e: any) { if (alive.current && revision.current === current) setError(e.message); }
    finally { if (alive.current && revision.current === current) setLoading(false); }
  }
  useEffect(() => {
    alive.current = true;
    void browse(agent.project?.path);
    return () => { alive.current = false; revision.current++; };
  }, []);
  async function add() {
    if (submitting.current || !folder.trim()) return;
    submitting.current = true; setSaving(true); setError('');
    // Prevent a late directory listing from replacing the submitted path.
    revision.current++; setLoading(false);
    try {
      const project = await agent.addWorkspace(folder, name);
      if (!alive.current) return;
      agent.setProjectId(project.id);
      close();
    } catch (e: any) { if (alive.current) setError(e.message); }
    finally { submitting.current = false; if (alive.current) setSaving(false); }
  }
  return <Modal title="Add workspace" close={() => { if (!submitting.current) close(); }}>
    <div className="add-workspace-body">
      <p className="workspace-intro">Choose a project folder on this computer. Its files, instructions and conversations will be available here.</p>
      <form className="workspace-path-form" onSubmit={e => { e.preventDefault(); void browse(folder); }}>
        <label className="field-label" htmlFor="workspace-path">Folder path</label>
        <div className="workspace-path-row"><input id="workspace-path" autoComplete="off" spellCheck={false} placeholder="/path/to/project or ~/Projects/project" value={folder} disabled={saving} onChange={e => { revision.current++; setLoading(false); setFolder(e.target.value); }} /><button className="button" disabled={saving || loading || !folder.trim()} type="submit">Go</button></div>
      </form>
      <div className="folder-browser" aria-busy={loading}>
        <div className="folder-browser-toolbar"><button className="button" aria-label="Parent folder" disabled={saving || loading || !listing?.parent} onClick={() => void browse(listing!.parent!)}><ArrowUp size={15} /></button><button className="button" disabled={saving || loading} onClick={() => void browse()}><Home size={14} />Home</button><span title={listing?.path}>{loading ? 'Loading folders…' : listing?.path || 'Choose a folder'}</span></div>
        <input className="folder-filter" aria-label="Filter folders" placeholder="Filter folders…" value={filter} disabled={saving || loading} onChange={e => setFilter(e.target.value)} />
        <div className="folder-browser-list" aria-label="Folders">
          {loading ? <p className="folder-browser-empty"><LoaderCircle size={16} className="spin" />Loading folders…</p> : listing?.entries.filter(entry => entry.name.toLowerCase().includes(filter.toLowerCase())).map(entry => <button className="folder-browser-row" key={entry.path} disabled={saving} onClick={() => void browse(entry.path)}><Folder size={17} /><span>{entry.name}</span><ChevronRight size={14} /></button>)}
          {!loading && listing && !listing.entries.some(entry => entry.name.toLowerCase().includes(filter.toLowerCase())) && <p className="folder-browser-empty">{filter ? 'No matching folders.' : 'No subfolders. You can add this folder.'}</p>}
        </div>
        {listing?.limited && <p className="field-hint">Showing a limited list. Enter a folder path to open it directly.</p>}
      </div>
      <form className="workspace-confirm-form" onSubmit={e => { e.preventDefault(); void add(); }}>
        <label className="field-label" htmlFor="workspace-name">Display name <span>(optional)</span><input id="workspace-name" placeholder="Defaults to the folder name" maxLength={80} value={name} disabled={saving} onChange={e => setName(e.target.value)} /></label>
        {error && <p className="inline-error" role="alert">{error}</p>}
        <p className="field-hint">Adding a workspace saves it on this computer. It does not move or upload files.</p>
        <div className="workspace-dialog-actions"><button className="button" type="button" disabled={saving} onClick={close}>Cancel</button><button className="button primary" type="submit" disabled={saving || loading || !folder.trim()}>{saving ? <><LoaderCircle size={14} className="spin" />Adding…</> : 'Add workspace'}</button></div>
      </form>
    </div>
  </Modal>;
}
