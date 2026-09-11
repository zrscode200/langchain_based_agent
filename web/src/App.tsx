import { useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties } from 'react';
import { ArrowUp, BookOpen, Bot, Check, ChevronDown, ChevronLeft, ChevronRight, CircleHelp, Code2, Command, Download, FolderOpen, Layers, LoaderCircle, MessageSquare, MoreHorizontal, PanelLeftClose, PanelLeftOpen, Plus, Search, ShieldCheck, Sparkles, Square, Terminal, Workflow, X, RotateCcw, Pencil, Settings } from 'lucide-react';
import { ArtifactView, Files, IconButton, Mark, Modal, Prose, type Artifact } from './components.tsx';
import { Approvals } from './approvals.tsx';
import { Conversation } from './conversation.tsx';
import { Attention } from './attention.tsx';
import { AppearanceSettings } from './appearance.tsx';
import { TasksPanel } from './tasks-panel.tsx';
import { useTaskRequests } from './useTaskRequests.ts';
import './tasks.css';
import { InspectorResizeHandle, useInspectorSize } from './inspector-resize.tsx';
import { useAgent, type Agent } from './useAgent.ts';
import { type Json, type Task } from './protocol.ts';

import { composerCommands, filterCommands, parseSlash, type ComposerCommand } from './commands.ts';
import { conversationTitle } from './conversations.ts';

type Panel = 'tasks' | 'files' | 'agent' | null;
const labels: Record<string, string> = { queued: 'Queued', running: 'Running', needs_approval: 'Needs approval', needs_input: 'Needs input', completed: 'Completed', failed: 'Failed', cancelled: 'Cancelled', timed_out: 'Timed out' };
const activeStatuses = ['running', 'queued', 'needs_approval', 'needs_input'];


function AgentPanel({ agent, chooseSkill, open, tab, setTab }: { tab: string; setTab: (tab: string) => void; agent: Agent; chooseSkill: (path: string) => void; open: (file: Artifact) => void }) {
  const [filter, setFilter] = useState('');
  useEffect(() => setFilter(''), [tab, agent.projectId]);
  const readInstructions = () => {
    const current = agent.capture();
    void agent.data(`/projects/${agent.projectId}/files?read=1&path=AGENTS.md`).then(file => { if (current()) open(file); }).catch(e => { if (current()) agent.setError(e.message); });
  };
  return <div className="definition-panel"><div className="panel-intro"><p>The instructions and capabilities available to this workspace.</p></div><div className="panel-tabs">{['skills', 'tools', 'context'].map(t => <button key={t} className={tab === t ? 'active' : ''} onClick={() => setTab(t)}>{t[0].toUpperCase() + t.slice(1)} {t === 'skills' ? agent.catalog.skills.length : t === 'tools' ? agent.catalog.tools.length : ''}</button>)}</div>
    {tab !== 'context' && <input className="search-input" aria-label={'Filter ' + tab} placeholder={'Find a ' + (tab === 'skills' ? 'skill' : 'tool') + '…'} value={filter} onChange={e => setFilter(e.target.value)} />}
    {tab === 'skills' && <><button className="definition-link" onClick={readInstructions}><BookOpen size={17} /><div><strong>Project instructions</strong><span>Open AGENTS.md</span></div><ChevronRight size={15} /></button>{agent.catalog.skills.filter(s => (s.name + s.description).toLowerCase().includes(filter.toLowerCase())).map(skill => <div className="skill-card" key={skill.path}><div><span className="skill-icon"><Sparkles size={16} /></span><h3>{skill.name}</h3></div><p>{skill.description}</p><div className="skill-footer"><span title={skill.path}>{skill.source || 'Workspace skill'}</span><button className="text-button" onClick={() => chooseSkill(skill.path)}>Use skill <ChevronRight size={13} /></button></div></div>)}{!agent.catalog.skills.length && <div className="panel-empty"><BookOpen size={28} /><p>No skills were returned for this workspace.</p></div>}</>}
    {tab === 'tools' && agent.catalog.tools.filter(t => (t.name + t.description).toLowerCase().includes(filter.toLowerCase())).map(tool => <details className="catalog-tool" key={tool.name}><summary><Terminal size={15} /><code>{tool.name}</code><ChevronDown size={13} /></summary><Prose>{tool.description}</Prose></details>)}
    {tab === 'context' && <><dl className="context-list"><dt>Effective model</dt><dd>{agent.state.values._model_spec || agent.catalog.model || 'Server default'}</dd><dt>Approval mode</dt><dd>{agent.mode}</dd><dt>Workspace</dt><dd>{agent.project?.path}</dd><dt>Conversation</dt><dd>{agent.threadId || 'Not started'}</dd><dt>Checkpoint</dt><dd>{agent.state.checkpoint?.checkpoint_id || 'No checkpoint yet'}</dd><dt>Messages retained</dt><dd>{agent.messages.length}</dd></dl><details className="tool-card"><summary><Code2 size={14} />Saved state</summary><pre>{JSON.stringify(agent.state, null, 2)}</pre></details>{agent.activity.length > 0 && <details className="tool-card"><summary>Recent custom events</summary><pre>{JSON.stringify(agent.activity, null, 2)}</pre></details>}<p className="scope-note">{agent.catalog.notice || 'The backend owns configuration, model credentials, and persistence.'}</p></>}
  </div>;
}

export default function App() {
  const agent = useAgent();
  const taskRequests = useTaskRequests(agent);
  const [selectedTask, setSelectedTask] = useState('');
  const [expandedTask, setExpandedTask] = useState(false);
  const [attentionTask, setAttentionTask] = useState<string | null>(null);
  const attention = useRef<HTMLDivElement>(null);
  const [panel, setPanel] = useState<Panel>(null);
  const [agentTab, setAgentTab] = useState('skills');
  const [sidebar, setSidebar] = useState(() => window.innerWidth > 700);
  const inspectorSize = useInspectorSize(sidebar);
  const [draft, setDraft] = useState('');
  const [skill, setSkill] = useState('');
  const [model, setModel] = useState('');
  const [artifact, setArtifact] = useState<Artifact | null>(null);
  const [modal, setModal] = useState<'search' | 'settings' | 'actions' | 'help' | 'rename' | 'conversation-settings' | null>(null);
  const [search, setSearch] = useState('');
  const [rename, setRename] = useState<{ id: string; value: string; inline: boolean } | null>(null);
  const [renaming, setRenaming] = useState(false);
  const [renameError, setRenameError] = useState('');
  const [atBottom, setAtBottom] = useState(true);
  const [commandError, setCommandError] = useState('');
  const [dismissedFor, setDismissedFor] = useState<string | null>(null);
  const [literalDraft, setLiteralDraft] = useState<string | null>(null);
  const [commandIndex, setCommandIndex] = useState(0);
  const composer = useRef<HTMLTextAreaElement>(null);
  const commandBox = useRef<HTMLDivElement>(null);
  const commandList = useRef<HTMLDivElement>(null);
  const draftKey = useRef('');
  const draftRevision = useRef(0);
  const scroll = useRef<HTMLDivElement>(null);
  const renameBusy = useRef(false);
  const activeTasks = agent.tasks.filter(t => activeStatuses.includes(t.status)).length;
  const taskAttention = agent.tasks.filter(t => ['needs_approval', 'needs_input'].includes(t.status)).length;
  const selectedSkill = agent.catalog.skills.find(s => s.path === skill);
  const isBusy = agent.status === 'running';
  const commands = useMemo(() => composerCommands(agent.catalog.skills).filter(c => !agent.attached || !['new', 'compact'].includes(c.id)), [agent.catalog.skills, agent.attached]);
  const slash = literalDraft === draft ? null : parseSlash(draft);
  const matches = slash ? filterCommands(commands, slash.name) : [];
  const menuOpen = !!slash && dismissedFor !== draft;
  const selectedCommand = matches[Math.min(commandIndex, matches.length - 1)];
  const canSend = !!agent.threadId && !isBusy && !agent.interrupts.length && !agent.state.next?.length && !['loading', 'disconnected'].includes(agent.status);
  const todo: Json[] = agent.state.values.todos || [];
  const toggle = (value: Panel) => setPanel(current => current === value ? null : value);
  const updateDraft = (text: string) => {
    draftRevision.current++;
    setDraft(text); localStorage.setItem(draftKey.current, text);
    setCommandError(''); setLiteralDraft(current => current !== null && parseSlash(current)?.name === parseSlash(text)?.name ? text : null); setDismissedFor(null); setCommandIndex(0);
  };
  const compose = (text: string) => { updateDraft(text); composer.current?.focus(); };
  const chooseSkill = (path: string) => { draftRevision.current++; setSkill(path); composer.current?.focus(); };
  const beginRename = (id = agent.threadId, value?: string, inline = true) => {
    if (!id || renameBusy.current) return;
    setRename({ id, value: value ?? conversationTitle(agent.threads.find(t => t.thread_id === id)), inline });
    setRenameError('');
    if (!inline) setModal('rename');
  };
  const closeRename = () => { setRename(null); setRenameError(''); if (modal === 'rename') setModal(null); };
  async function saveTitle() {
    if (!rename?.value.trim() || renameBusy.current) return;
    const target = rename, current = agent.capture();
    renameBusy.current = true; setRenaming(true); setRenameError('');
    try { await agent.rename(target.value, target.id); if (current()) closeRename(); }
    catch (e: any) { if (current()) setRenameError(e.message); }
    finally { renameBusy.current = false; setRenaming(false); }
  }
  useEffect(() => {
    setAtBottom(true); setSelectedTask(''); setExpandedTask(false); setAttentionTask(null); setArtifact(null); setSkill(''); setModel(''); setRename(null); setRenameError(''); setModal(null);
    setCommandError(''); setLiteralDraft(null); setDismissedFor(null); setCommandIndex(0);
    draftKey.current = `lc.draft.${agent.projectId}.${agent.threadId}`;
    setDraft(localStorage.getItem(draftKey.current) || '');
  }, [agent.projectId, agent.threadId]);
  useLayoutEffect(() => { if (atBottom) scroll.current?.scrollTo({ top: scroll.current.scrollHeight, behavior: 'instant' }); }, [agent.messages, agent.interrupts, atBottom]);
  useEffect(() => {
    commandList.current?.querySelector('[aria-selected="true"]')?.scrollIntoView({ block: 'nearest' });
  }, [commandIndex, draft]);
  useEffect(() => {
    const key = (e: KeyboardEvent) => { if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); setModal('search'); } };
    window.addEventListener('keydown', key); return () => window.removeEventListener('keydown', key);
  }, []);
  useEffect(() => {
    if (!menuOpen) return;
    const dismiss = (e: PointerEvent) => { if (!commandBox.current?.contains(e.target as Node)) setDismissedFor(draft); };
    document.addEventListener('pointerdown', dismiss); return () => document.removeEventListener('pointerdown', dismiss);
  }, [menuOpen, draft]);

  function executeCommand(command: ComposerCommand) {
    const args = slash?.args || '';
    if (command.kind === 'skill') {
      if (!agent.catalog.skills.some(s => s.path === command.skillPath)) { setCommandError('This skill is no longer available. Refresh the catalog.'); return; }
      chooseSkill(command.skillPath!); compose(args); return;
    }
    if (args && !['rename', 'model'].includes(command.action || '')) {
      setCommandError(`${command.name} does not take message text. Choose a skill or use this as a message.`); return;
    }
    if (['rename', 'compact'].includes(command.action || '') && !agent.threadId) {
      setCommandError('Start a conversation first.'); return;
    }
    if (command.action === 'model' && args && !/^[\w.-]+:[\w./:@-]+$/.test(args)) {
      setCommandError('Use /model provider:model, or /model to open settings.'); return;
    }
    switch (command.action) {
      case 'model': if (args && !agent.attached) setModel(args); setModal('conversation-settings'); break;
      case 'settings': setModal('settings'); break;
      case 'skills': case 'tools': setAgentTab(command.action); setPanel('agent'); break;
      case 'tasks': setPanel('tasks'); break;
      case 'files': setPanel('files'); break;
      case 'rename': beginRename(agent.threadId, args || undefined); break;
      case 'new': void agent.newThread(); break;
      case 'compact': setModal('actions'); break;
      case 'help': setModal('help'); break;
    }
    updateDraft('');
  }
  async function send() {
    if (slash) {
      const exact = commands.find(c => c.name === slash.name);
      if (exact) executeCommand(exact);
      else { setDismissedFor(null); setCommandError(`“${slash.name}” is not a web command. Select an available command or use this as message text.`); }
      return;
    }
    if (!draft.trim() || !canSend) return;
    if (skill && !selectedSkill) { setCommandError('The selected skill is unavailable. Choose it again before sending.'); return; }
    const text = draft, savedKey = draftKey.current, revision = draftRevision.current, current = agent.capture();
    await agent.run(text, skill || undefined, undefined, model || undefined, () => {
      if (current() && draftRevision.current === revision) {
        if (localStorage.getItem(savedKey) === text) localStorage.removeItem(savedKey);
        setDraft(''); setSkill(''); setLiteralDraft(null);
      }
    });
  }
  function exportChat() {
    const blob = new Blob([JSON.stringify({ project: agent.project?.name, thread_id: agent.threadId, messages: agent.messages, exported_at: new Date().toISOString() }, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob); const a = document.createElement('a'); a.href = url; a.download = `conversation-${agent.threadId}.json`; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  const titleEditor = rename && <form className={rename.inline ? 'inline-title-form' : 'settings-body'} onSubmit={e => { e.preventDefault(); void saveTitle(); }}>
    <input autoFocus aria-label="Conversation title" value={rename.value} maxLength={160} disabled={renaming} onFocus={e => e.target.select()} onChange={e => setRename({ ...rename, value: e.target.value })} onKeyDown={e => { if (e.key === 'Escape') { e.preventDefault(); closeRename(); } }} />
    <button className="button primary" disabled={!rename.value.trim() || renaming}>Save</button>
    <button className="button" type="button" onClick={closeRename}>Cancel</button>
    {renameError && <span className="inline-error" role="alert">{renameError}</span>}
  </form>;

  return <div className={'workspace ' + (sidebar ? '' : 'sidebar-hidden') + (panel ? ' has-panel' : '') + (inspectorSize.dragging ? ' resizing-panel' : '') + (panel && artifact && inspectorSize.overlayArtifact ? ' compact-artifact' : '') + (panel === 'tasks' && expandedTask ? ' expanded-task' : '')} style={{ '--inspector-width': `${inspectorSize.width}px` } as CSSProperties}>
    <aside className="sidebar" aria-label="Project and conversations">
      <header className="sidebar-project" aria-label="Current project">
        <span className="sidebar-project-icon"><FolderOpen size={20} /></span>
        <div className="sidebar-project-copy"><span className="eyebrow">Project</span><strong title={agent.project?.name}>{agent.project?.name || 'Connecting…'}</strong><span className="sidebar-project-path" title={agent.project?.path}>{agent.project?.path || 'Loading project folder'}</span></div>
        <IconButton label="Hide sidebar" onClick={() => setSidebar(false)}><PanelLeftClose size={17} /></IconButton>
      </header>
      {agent.attached ? <p className="scope-note" role="status">Connected to your terminal conversation. Return to the terminal to hand control back.</p> : <button className="new-conversation" onClick={() => void agent.newThread()} disabled={!agent.projectId || agent.creating}><Plus size={17} />{agent.creating ? 'Creating conversation…' : 'New conversation'}</button>}
      <button className="sidebar-search" onClick={() => setModal('search')}><Search size={16} /><span>Search conversations</span><kbd>⌘ K</kbd></button>
      <div className="conversation-heading"><span className="eyebrow">Conversations</span><span>{agent.threads.length}</span></div>
      <nav className="conversation-list">{agent.threads.map(t => <div key={t.thread_id} className={'conversation-item ' + (t.thread_id === agent.threadId ? 'active' : '')}>
        <button className="conversation-row" onClick={() => { agent.selectThread(t.thread_id); if (window.innerWidth <= 700) setSidebar(false); }} aria-current={t.thread_id === agent.threadId ? 'page' : undefined}><MessageSquare size={15} /><span>{conversationTitle(t)}</span>{t.status === 'busy' && <span className="status-dot running" />}{t.status === 'interrupted' && <span className="status-dot needs_approval" />}</button>
        <IconButton label={'Rename ' + conversationTitle(t)} onClick={() => beginRename(t.thread_id, undefined, false)}><Pencil size={13} /></IconButton>
      </div>)}{!agent.threads.length && <p className="sidebar-empty">Your conversations will live here.</p>}{agent.threads.length >= 100 && <p className="scope-note">Showing the 100 most recent conversations.</p>}</nav>
      <nav className="sidebar-tools" aria-label="Project resources"><button onClick={() => toggle('files')} className={panel === 'files' ? 'active' : ''}><FolderOpen size={17} />Project files</button><button onClick={() => toggle('agent')} className={panel === 'agent' ? 'active' : ''}><Bot size={17} />Agent setup</button></nav>
      <div className="sidebar-footer"><button className="sidebar-settings" onClick={() => setModal('settings')}><Settings size={17} /><span>Settings</span></button><span className="sidebar-connection" role="status" aria-label={agent.status === 'disconnected' ? 'Connection interrupted' : agent.status === 'loading' ? 'Connecting' : 'Connected'} title={agent.status === 'disconnected' ? 'Connection interrupted' : agent.status === 'loading' ? 'Connecting' : 'Connected to the local agent'}><span className={'status-dot ' + (agent.status === 'disconnected' ? 'failed' : agent.status === 'loading' ? 'queued' : 'completed')} /></span></div>
    </aside>
    <main className="main">
      <header className="topbar">
        <div className="conversation-title">{!sidebar && <IconButton label="Show sidebar" onClick={() => setSidebar(true)}><PanelLeftOpen size={18} /></IconButton>}<span className="breadcrumb-project">{agent.project?.name || 'Workspace'}</span><ChevronRight size={14} />
          {rename?.inline ? titleEditor : <button className="editable-title" title="Rename conversation" disabled={!agent.threadId} onClick={() => beginRename()}><strong>{agent.thread ? conversationTitle(agent.thread) : 'Start a conversation'}</strong><Pencil size={13} /></button>}
        </div>
        <div className="topbar-actions"><button className={'toolbar-button ' + (panel === 'tasks' ? 'active' : '') + (taskAttention ? ' needs-attention' : '')} aria-label={'Background work' + (taskAttention ? ` · ${taskAttention} need${taskAttention === 1 ? 's' : ''} you` : activeTasks ? ` · ${activeTasks} active` : '')} title={taskAttention ? 'A background task is waiting for your decision' : undefined} onClick={() => toggle('tasks')}><Workflow size={16} /><span>Background work</span>{taskAttention > 0 ? <em className="attention-count">{taskAttention}</em> : activeTasks > 0 && <b>{activeTasks}</b>}</button><IconButton label="Conversation actions" onClick={() => setModal('actions')} disabled={!agent.threadId}><MoreHorizontal size={19} /></IconButton></div>
      </header>
      <div className="working-surface"><section className="conversation" aria-label="Conversation">
        <div className="conversation-scroll" ref={scroll} onWheel={event => { if (event.deltaY < 0) setAtBottom(false); }} onClickCapture={event => { if ((event.target as HTMLElement).closest('.disclosure-toggle')) setAtBottom(false); }} onScroll={() => { const el = scroll.current!; setAtBottom(el.scrollHeight - el.scrollTop - el.clientHeight < 24); }}>
          {agent.messages.length || agent.interrupts.length ? <Conversation key={agent.projectId + ':' + agent.threadId} scope={agent.threadId} messages={agent.messages} interrupts={agent.interrupts} running={isBusy} disconnected={agent.status === 'disconnected'} receipts={agent.receipts} showRequests={false} submit={async responses => { const sent = await agent.run(undefined, undefined, responses, model || undefined); if (!sent) throw new Error('Decision was not accepted. Refresh and review the current request.'); }} /> : <div className="welcome"><div className="welcome-label"><span className="status-dot completed" />YOUR PROJECT, IN FOCUS</div><div className="welcome-mark"><Mark /></div><h1>What will we<br /><span>work on today?</span></h1><p>{agent.threadId ? <>Ask a question or describe your next task.<br />Type <kbd>/</kbd> for skills and commands.</> : <>Choose New conversation in the sidebar to begin.</>}</p></div>}
          <div className="messages conversation-status">
            {todo.length > 0 && <details className="plan conversation-plan"><summary>Current plan · {todo.filter(t => t.status === 'completed').length}/{todo.length}</summary>{todo.map((item, i) => <div className={'todo ' + item.status} key={i}>{item.status === 'completed' ? <Check size={15} /> : item.status === 'in_progress' ? <LoaderCircle size={15} className="spin" /> : <span className="todo-ring" />}<span>{item.content}</span></div>)}</details>}
          </div>
        </div>
        <div className="composer-region">
          <div ref={attention}><Attention key={agent.base} requests={taskRequests} interrupts={agent.interrupts} running={isBusy} atBottom={atBottom} selected={attentionTask} select={setAttentionTask} inspect={task => { setSelectedTask(task.task_id); setPanel('tasks'); }} submitMain={async responses => { if (!await agent.run(undefined, undefined, responses, model || undefined)) throw new Error('Decision was not accepted. Refresh and review this request.'); }} /></div>
          {!atBottom && <button className="jump-button" onClick={() => { setAtBottom(true); scroll.current?.scrollTo({ top: scroll.current.scrollHeight, behavior: 'smooth' }); }}>{isBusy ? 'New activity' : 'Jump to latest'} <ChevronDown size={14} /></button>}
          {!!agent.state.next?.length && !agent.interrupts.length && !isBusy && <div className="notice-banner"><span>This turn was interrupted. Continue from its saved checkpoint when you’re ready.</span><button className="text-button" onClick={() => void agent.run()}>Continue turn</button></div>}
          {agent.error && <div className="error-banner" role="alert"><CircleHelp size={17} /><span>{agent.error}</span><button onClick={() => void agent.reconnect()}>{agent.status === 'disconnected' ? 'Reconnect' : 'Refresh'}</button><IconButton label="Dismiss error" onClick={() => agent.setError('')}><X size={14} /></IconButton></div>}
          {agent.notice && <div className="notice-banner"><span>{agent.notice}</span><IconButton label="Dismiss notice" onClick={() => agent.setNotice('')}><X size={14} /></IconButton></div>}
          <div ref={commandBox} className={'composer ' + (agent.interrupts.length ? 'paused' : '')}>
            {menuOpen && <div className="slash-menu">
              <div className="slash-heading"><strong>Skills & commands</strong><span>↑ ↓ to choose · Enter or Tab</span></div>
              <div id="composer-commands" role="listbox" aria-label="Skills and commands" ref={commandList} className="slash-options">
                {matches.map((command, i) => <button key={command.id} id={'composer-command-' + i} type="button" role="option" aria-selected={command === selectedCommand} tabIndex={-1} className="slash-option" onMouseDown={e => e.preventDefault()} onClick={() => executeCommand(command)}>
                  {command.kind === 'skill' ? <Sparkles size={16} /> : <Command size={16} />}<span><strong>{command.name}</strong><span>{command.description}</span></span><small>{command.kind === 'skill' ? 'Skill' : 'Command'}</small>
                </button>)}
              </div>
              {!matches.length && <p className="slash-empty">No matching web command or loaded skill. TUI-only commands are not available here.</p>}
              <div className="slash-footer"><span>{selectedCommand?.kind === 'skill' ? 'Choose a skill, then write and send your message.' : 'Commands open controls without sending a message.'}</span><button className="text-button" onClick={() => { draftRevision.current++; setLiteralDraft(draft); setDismissedFor(draft); setCommandError(''); composer.current?.focus(); }}>Use as message text</button></div>
            </div>}
            {commandError && <p className="composer-error" role="alert">{commandError}</p>}
            {selectedSkill && <div className="selected-skill"><Sparkles size={14} /><span>{selectedSkill.name}</span><IconButton label="Remove selected skill" onClick={() => chooseSkill('')}><X size={12} /></IconButton></div>}
            <textarea ref={composer} role="combobox" aria-label="Message the agent" aria-autocomplete="list" aria-haspopup="listbox" aria-expanded={menuOpen} aria-controls={menuOpen ? 'composer-commands' : undefined} aria-activedescendant={menuOpen && selectedCommand ? 'composer-command-' + matches.indexOf(selectedCommand) : undefined} placeholder={agent.interrupts.length ? 'The agent is waiting for your response above…' : 'Message the agent · / for skills and commands'} value={draft} onChange={e => updateDraft(e.target.value)} rows={3} onKeyDown={e => {
              if (e.nativeEvent.isComposing || e.keyCode === 229) return;
              if (menuOpen) {
                if (e.key === 'Escape') { e.preventDefault(); setDismissedFor(draft); return; }
                if (['ArrowDown', 'ArrowUp'].includes(e.key)) { e.preventDefault(); if (matches.length) setCommandIndex(i => (i + (e.key === 'ArrowDown' ? 1 : -1) + matches.length) % matches.length); return; }
                if ((e.key === 'Tab' && !e.shiftKey || e.key === 'Enter' && !e.shiftKey) && selectedCommand) { e.preventDefault(); executeCommand(selectedCommand); return; }
              }
              if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); void send(); }
            }} />
            <div className="composer-toolbar"><div><IconButton label="Reference a project file" onClick={() => toggle('files')}><Plus size={19} /></IconButton><button className={'composer-chip session-chip ' + agent.mode} aria-label="Model and approval settings" onClick={() => setModal('conversation-settings')}><ShieldCheck size={14} /><span>{(model || agent.state.values._model_spec || agent.catalog.model || 'Server default').split(':').pop()}</span><span className="session-mode">· {agent.mode === 'manual' ? 'Ask first' : agent.mode === 'auto' ? 'Auto review' : 'YOLO'}</span><ChevronDown size={12} /></button></div><div>{isBusy ? <button className="send-button stop" aria-label="Stop agent" disabled={!agent.runId} onClick={() => void agent.cancel()}><Square size={15} fill="currentColor" /></button> : <button className="send-button" aria-label={slash ? 'Run command' : 'Send message'} onClick={() => void send()} disabled={!draft.trim() || (!slash && !canSend)}><ArrowUp size={20} /></button>}</div></div>
          </div>
          <div className="composer-caption"><span>{activeTasks && !isBusy ? `You can keep chatting · ${activeTasks} background task${activeTasks === 1 ? '' : 's'}` : '/ for skills and commands'}</span><span>Enter to send <span>·</span> Shift + Enter for a new line</span></div>
        </div>
      </section>{artifact && <ArtifactView key={artifact.path} file={artifact} close={() => setArtifact(null)} reference={() => compose(draft + `${draft ? '\n' : ''}Please refer to the project file \`${artifact.path}\`. `)} />}</div>
    </main>
    {panel && <aside id="workspace-inspector" className="inspector" aria-label={panel + ' panel'}><InspectorResizeHandle sizing={inspectorSize} /><div className="inspector-heading"><div>{panel === 'tasks' ? <Workflow size={18} /> : panel === 'agent' ? <Bot size={18} /> : <FolderOpen size={18} />}<h2>{panel === 'tasks' ? 'Background work' : panel === 'agent' ? 'Agent setup' : 'Project files'}</h2></div><IconButton label="Close inspector" onClick={() => setPanel(null)}><X size={17} /></IconButton></div><div className="inspector-body">{panel === 'tasks' ? <TasksPanel agent={agent} compose={text => { compose(text); if (window.innerWidth <= 1000) setPanel(null); }} selected={selectedTask} setSelected={setSelectedTask} requests={taskRequests} expanded={expandedTask} setExpanded={setExpandedTask} review={id => { setAttentionTask(id); setExpandedTask(false); if (window.innerWidth <= 1000) setPanel(null); requestAnimationFrame(() => attention.current?.querySelector<HTMLButtonElement>('.attention-heading button')?.focus()); }} /> : panel === 'agent' ? <AgentPanel tab={agentTab} setTab={setAgentTab} agent={agent} chooseSkill={chooseSkill} open={setArtifact} /> : <Files key={agent.projectId} agent={agent} open={setArtifact} />}</div></aside>}
    {modal === 'search' && <Modal title="Find a conversation" close={() => setModal(null)}><div className="command-search"><Search size={19} /><input autoFocus aria-label="Search conversations" placeholder="Search by conversation title…" value={search} onChange={e => setSearch(e.target.value)} /><kbd>ESC</kbd></div><div className="command-results">{agent.threads.filter(t => conversationTitle(t).toLowerCase().includes(search.toLowerCase())).map(t => <button key={t.thread_id} onClick={() => { agent.selectThread(t.thread_id); setModal(null); }}><MessageSquare size={16} /><span>{conversationTitle(t)}</span>{t.thread_id === agent.threadId && <Check size={15} />}</button>)}</div><div className="command-footer"><Command size={13} /> K to open · Escape to close</div></Modal>}
    {modal === 'settings' && <Modal title="Settings" close={() => setModal(null)}><div className="settings-body"><AppearanceSettings /><span className="eyebrow">This app</span><dl className="settings-meta"><dt>Project</dt><dd>{agent.project?.name}</dd><dt>Folder</dt><dd>{agent.project?.path}</dd><dt>Agent server</dt><dd>{agent.backend}</dd><dt>Connection</dt><dd>{agent.status === 'disconnected' ? 'Disconnected' : agent.status === 'loading' ? 'Connecting' : 'Connected'}</dd></dl></div></Modal>}
    {modal === 'conversation-settings' && <Modal title="Conversation settings" close={() => setModal(null)}><div className="settings-body"><span className="eyebrow">Model</span><label className="field-label">Model for the next turn<input disabled={agent.attached} placeholder={agent.state.values._model_spec || agent.catalog.model || 'Use the server default'} value={model} onChange={e => setModel(e.target.value)} /></label><p className="field-hint">{agent.attached ? "Return to the terminal to change the model. " : "Use provider:model. "}Credentials and model policy stay on the backend. The effective model appears in Agent setup → Context after a successful turn.</p><span className="eyebrow">Tool approvals</span><div className="mode-options">{([['manual', 'Ask first', 'Review actions yourself.'], ['auto', 'Auto review', 'The configured classifier reviews actions.'], ['yolo', 'YOLO', 'Approve actions automatically within server policy.']] as const).map(([value, name, description]) => <button disabled={!agent.threadId} className={agent.mode === value ? 'selected' : ''} key={value} onClick={() => void agent.changeMode(value)}><span className="radio">{agent.mode === value && <i />}</span><div><strong>{name}</strong><p>{description}</p></div></button>)}</div><p className="field-hint">A mode change also applies to this conversation’s background agents at their next approval boundary.</p></div></Modal>}
    {modal === 'rename' && <Modal title="Rename conversation" close={closeRename}>{titleEditor}</Modal>}
    {modal === 'actions' && <Modal title="Conversation actions" close={() => setModal(null)}><div className="settings-body"><button className="button" disabled={agent.attached || isBusy || !!agent.interrupts.length || !!agent.state.next?.length} onClick={() => { setModal(null); void agent.compact(); }}><Layers size={15} />Compact conversation</button><p className="field-hint">{agent.attached && "Return to the terminal to compact this conversation. "}Summarize older history with the configured model. This uses model tokens; saved archives follow the backend retention policy.</p><button className="button" onClick={exportChat}><Download size={15} />Export conversation JSON</button><button className="button" onClick={() => { setModal(null); void agent.reconnect(); }}><RotateCcw size={15} />Reconnect and refresh</button></div></Modal>}
    {modal === 'help' && <Modal title="Web commands" close={() => setModal(null)}><div className="settings-body command-help"><p>Type / in the composer to search commands and loaded skills. Use ↑ ↓, then Enter or Tab to choose. Escape closes the menu; Shift + Enter adds a new line.</p><p>A skill is attached to your next message. Add your request and send when ready. Commands open controls directly; they do not go to the model.</p><dl>{commands.filter(c => c.kind === 'command').map(c => <div key={c.id}><dt>{c.name}</dt><dd>{c.description}</dd></div>)}</dl><p>These are the supported web commands. Native TUI commands such as authentication or shell operations are not executed here.</p></div></Modal>}
  </div>;
}
