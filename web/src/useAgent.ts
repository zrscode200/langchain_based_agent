import { useCallback, useEffect, useRef, useState } from 'react';
import { mergeChunk, mergeUpdate, pendingInterrupts, readSSE, type Catalog, type Json, type Message, type Mode, type Project, type Snapshot, type Task, type Thread } from './protocol.ts';
import type { DecisionReceipt } from './timeline.ts';

const empty: Snapshot = { values: {}, next: [], interrupts: [] };
export function useAgent() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState('');
  const [threads, setThreads] = useState<Thread[]>([]);
  const [threadId, setThreadId] = useState('');
  const [state, setState] = useState<Snapshot>(empty);
  const [messages, setMessages] = useState<Message[]>([]);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [pendingResults, setPendingResults] = useState<string[]>([]);
  const [catalog, setCatalog] = useState<Catalog>({ skills: [], tools: [] });
  const [mode, setMode] = useState<Mode>('manual');
  const [status, setStatus] = useState<'loading' | 'ready' | 'running' | 'disconnected'>('loading');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [runId, setRunId] = useState('');
  const [activity, setActivity] = useState<Json[]>([]);
  const [receipts, setReceipts] = useState<DecisionReceipt[]>([]);
  const submittedInterrupts = useRef(new Set<string>());
  const [backend, setBackend] = useState('');
  const [creating, setCreating] = useState(false);
  const creation = useRef(false);
  const titleRevisions = useRef(new Map<string, number>());
  const token = useRef('');
  const selection = useRef({ projectId: '', threadId: '', epoch: 0 });
  const stream = useRef<AbortController | null>(null);
  const busy = useRef(false);
  const lastEvent = useRef('');
  const turnId = useRef('');
  const projectLoad = useRef(0);
  const bootstrapLoad = useRef(0);
  const compaction = useRef('');
  const project = projects.find(p => p.id === projectId);
  const thread = threads.find(t => t.thread_id === threadId);

  const request = useCallback(async (route: string, method = 'GET', body?: unknown, signal?: AbortSignal, headers: Record<string, string> = {}) => {
    const result = await fetch('/api' + route, { method, headers: { 'x-workspace-token': token.current, ...(body === undefined ? {} : { 'content-type': 'application/json' }), ...headers }, body: body === undefined ? undefined : JSON.stringify(body), signal });
    if (!result.ok) { const data = await result.json().catch(() => ({})); throw new Error(data.detail || `Request failed (${result.status}).`); }
    return result;
  }, []);
  const data = useCallback(async (route: string, method = 'GET', body?: unknown) => (await request(route, method, body)).json(), [request]);
  const route = (p = selection.current.projectId, t = selection.current.threadId) => `/projects/${p}/threads${t ? '/' + t : ''}`;
  const refreshTitle = useCallback(async (p: string, id: string) => {
    const key = p + '/' + id;
    const revision = (titleRevisions.current.get(key) || 0) + 1;
    titleRevisions.current.set(key, revision);
    const row = await data(`/projects/${p}/threads/${id}`);
    if (selection.current.projectId === p && titleRevisions.current.get(key) === revision)
      setThreads(old => old.map(t => t.thread_id === id ? { ...t, metadata: row.metadata } : t));
  }, [data]);

  const bootstrap = useCallback(async () => {
    const load = ++bootstrapLoad.current;
    setStatus('loading'); setError('');
    try {
      const response = await fetch('/api/bootstrap');
      if (!response.ok) throw new Error('Open the workspace through its Node launcher.');
      const result = await response.json();
      if (bootstrapLoad.current !== load) return;
      token.current = result.token;
      setProjects(result.projects); setBackend(result.backend);
      const saved = localStorage.getItem('lc.workspace.project');
      setProjectId(result.projects.find((p: Project) => p.id === saved)?.id || result.projects[0]?.id || '');
    } catch (e: any) { if (bootstrapLoad.current === load) { setError(e.message); setStatus('disconnected'); } }
  }, []);
  useEffect(() => { void bootstrap(); return () => { bootstrapLoad.current++; stream.current?.abort(); }; }, [bootstrap]);

  const selectThread = useCallback((id: string) => setThreadId(id), []);
  useEffect(() => {
    if (!projectId) return;
    stream.current?.abort(); busy.current = false; compaction.current = '';
    selection.current = { projectId, threadId: '', epoch: selection.current.epoch + 1 };
    const load = ++projectLoad.current;
    setThreadId(''); setThreads([]); setMessages([]); setState(empty); setTasks([]); setRunId(''); setCatalog({ skills: [], tools: [] }); setError(''); setStatus('loading');
    localStorage.setItem('lc.workspace.project', projectId);
    data(route(projectId, '')).then(rows => {
      if (projectLoad.current !== load || selection.current.projectId !== projectId) return;
      setThreads(rows);
      const saved = localStorage.getItem(`lc.workspace.thread.${projectId}`);
      setThreadId(rows.find((r: Thread) => r.thread_id === saved)?.thread_id || rows[0]?.thread_id || '');
      setStatus('ready');
    }).catch(e => { if (projectLoad.current === load && selection.current.projectId === projectId) { setError(e.message); setStatus('disconnected'); } });
  }, [projectId, data]);

  const refresh = useCallback(async (epoch = selection.current.epoch) => {
    const { projectId: p, threadId: t } = selection.current;
    if (!p || !t) return;
    const base = route(p, t);
    const [snapshot, background, runs, liveMode] = await Promise.all([data(base + '/state'), data(base + '/background', 'POST', { operation: 'list' }), data(base + '/runs'), data(base + '/mode')]);
    if (selection.current.epoch !== epoch) return;
    const visible = busy.current ? hideSubmitted(snapshot, submittedInterrupts.current) : snapshot;
    setState(visible); setTasks(background.tasks); setPendingResults(background.pending_results || []);
    setMode(liveMode.mode);
    void refreshTitle(p, t).catch(() => {});
    if (!busy.current) { setMessages(old => runs.length && lastEvent.current ? old : snapshot.values?.messages || []); setRunId(runs[0]?.run_id || ''); setStatus(runs.length ? 'running' : 'ready'); }
  }, [data, refreshTitle]);

  useEffect(() => {
    if (threadId && !threads.some(t => t.thread_id === threadId)) return;
    stream.current?.abort(); stream.current = null; busy.current = false; compaction.current = '';
    selection.current = { projectId, threadId, epoch: selection.current.epoch + 1 };
    const epoch = selection.current.epoch;
    setMessages([]); setTasks([]); setPendingResults([]); setState(empty); setRunId(''); setActivity([]); setReceipts([]); submittedInterrupts.current.clear(); setError(''); setNotice(''); setMode('manual'); setCatalog({ skills: [], tools: [] });
    lastEvent.current = ''; turnId.current = '';
    if (!threadId) return;
    setStatus('loading');
    localStorage.setItem(`lc.workspace.thread.${projectId}`, threadId);
    const base = route(projectId, threadId);
    void refresh(epoch).catch(e => { if (selection.current.epoch === epoch) { setError(e.message); setStatus('disconnected'); } });
    void data(base + '/catalog').then(c => { if (selection.current.epoch === epoch) setCatalog(c); }).catch(() => { if (selection.current.epoch === epoch) setNotice('Skill and tool catalogs need the web adapter in the agent backend. See WEB_UI.md.'); });
    void data(base + '/mode').then(v => { if (selection.current.epoch === epoch) setMode(v.mode); }).catch(e => { if (selection.current.epoch === epoch) setError(e.message); });
    let polling = false;
    const timer = setInterval(() => {
      if (polling || document.hidden || selection.current.epoch !== epoch) return;
      polling = true;
      refresh(epoch).catch(e => { if (selection.current.epoch === epoch) { setError(e.message); if (!busy.current) setStatus('disconnected'); } }).finally(() => { polling = false; });
    }, 3500);
    return () => clearInterval(timer);
  }, [threadId, projectId, data, refresh]);

  async function newThread() {
    const p = selection.current.projectId;
    if (!p || creation.current) return;
    creation.current = true; setCreating(true);
    try { const row = await data(route(p, ''), 'POST', {}); if (selection.current.projectId !== p) return; setThreads(old => [row, ...old]); setThreadId(row.thread_id); }
    catch (e: any) { if (selection.current.projectId === p) setError(e.message); }
    finally { creation.current = false; setCreating(false); }
  }
  async function consume(response: Response, epoch: number) {
    const reset = !lastEvent.current;
    const seen = new Set<string>();
    await readSSE(response, event => {
      if (selection.current.epoch !== epoch) return;
      if (event.id && event.id === lastEvent.current) return;
      if (event.id) lastEvent.current = event.id;
      if (event.event === 'metadata' && event.data.run_id) setRunId(event.data.run_id);
      if (event.event === 'error') throw new Error(event.data.message || event.data.error || 'The agent run failed.');
      // Child namespaces belong in task details, never mixed into the main chat.
      if (event.event === 'messages' && Array.isArray(event.data)) {
        const [message, metadata] = event.data;
        if (!(metadata?.langgraph_checkpoint_ns || '').includes('|')) {
          const first = !seen.has(message.id); seen.add(message.id);
          setMessages(old => mergeChunk(reset && first ? old.filter(m => m.id !== message.id) : old, message));
        }
      }
      if (event.event === 'updates') {
        setMessages(old => mergeUpdate(old, event.data));
        if (event.data?.__interrupt__) setState(old => ({ ...old, interrupts: event.data.__interrupt__.filter((i: Json) => !submittedInterrupts.current.has(i.id)) }));
      }
      if (event.event === 'custom') setActivity(old => [...old.slice(-49), typeof event.data === 'object' ? event.data : { message: String(event.data) }]);
    });
  }
  async function run(text?: string, skill?: string, responses?: Json, model?: string, onSubmitted?: () => void) {
    if (busy.current || !selection.current.threadId) return false;
    const { epoch, projectId: p, threadId: id } = selection.current, base = route();
    busy.current = true; setError(''); setStatus('running'); setRunId(''); lastEvent.current = '';
    if (!responses) turnId.current = crypto.randomUUID();
    stream.current = new AbortController();
    let submitted = false;
    try {
      const response = await request(base + '/run', 'POST', { text, skill, responses, model, continue: text === undefined && !responses, turn_id: turnId.current || crypto.randomUUID() }, stream.current.signal);
      submitted = true;
      if (selection.current.epoch === epoch && responses) {
        const interrupts = pendingInterrupts(state).filter(i => Object.hasOwn(responses, i.id));
        submittedInterrupts.current = new Set(Object.keys(responses));
        setReceipts(old => [...old, { id: crypto.randomUUID(), afterMessage: messages.at(-1)?.id, interrupts, responses }]);
        setState(old => hideSubmitted(old, submittedInterrupts.current));
      }
      if (selection.current.epoch === epoch && text) {
        setMessages(old => [...old, { id: 'optimistic-' + turnId.current, type: 'human', content: text }]);
        onSubmitted?.();
      }
      if (text) void refreshTitle(p, id).catch(() => {});
      await consume(response, epoch);
    } catch (e: any) {
      if (selection.current.epoch === epoch && e.name !== 'AbortError') { setError(e.message + (submitted ? ' Reconnect to inspect the existing run; your message will not be resent.' : '')); setStatus('disconnected'); }
    } finally {
      if (selection.current.epoch === epoch) {
        busy.current = false; stream.current = null;
        try { await refresh(epoch); } catch { setStatus('disconnected'); }
      }
    }
    return submitted;
  }
  async function reconnect() {
    const epoch = selection.current.epoch, base = route();
    if (!token.current || !selection.current.projectId) { await bootstrap(); return; }
    if (!selection.current.threadId) {
      setError(''); setStatus('loading');
      try {
        const rows = await data(route(selection.current.projectId, ''));
        if (selection.current.epoch !== epoch) return;
        setThreads(rows); setThreadId(rows[0]?.thread_id || ''); setStatus('ready');
      } catch (e: any) { if (selection.current.epoch === epoch) { setError(e.message); setStatus('disconnected'); } }
      return;
    }
    if (busy.current) return;
    setError('');
    try {
      const runs = await data(base + '/runs');
      if (selection.current.epoch !== epoch) return;
      if (runs[0]) {
        busy.current = true; setStatus('running'); setRunId(runs[0].run_id);
        stream.current = new AbortController();
        await consume(await request(base + '/join?run=' + encodeURIComponent(runs[0].run_id), 'GET', undefined, stream.current.signal, { 'Last-Event-ID': lastEvent.current }), epoch);
      }
    } catch (e: any) { if (selection.current.epoch === epoch) setError(e.message); }
    finally { if (selection.current.epoch === epoch) { busy.current = false; stream.current = null; await refresh(epoch).catch(e => { setError(e.message); setStatus('disconnected'); }); } }
  }
  async function cancel() {
    if (!runId) return;
    const epoch = selection.current.epoch;
    try { await data(route() + (compaction.current ? '/compact-cancel' : '/cancel'), 'POST', compaction.current ? { operation_id: compaction.current } : { run_id: runId }); if (selection.current.epoch === epoch) { stream.current?.abort(); setNotice('Cancellation requested. Saved work remains available.'); await refresh(epoch); } }
    catch (e: any) { if (selection.current.epoch === epoch) setError(e.message); }
  }
  async function changeMode(value: Mode) {
    const epoch = selection.current.epoch;
    try { await data(route() + '/mode', 'PUT', { mode: value }); if (selection.current.epoch === epoch) setMode(value); }
    catch (e: any) { if (selection.current.epoch === epoch) setError(e.message); }
  }
  async function compact() {
    if (busy.current || !threadId) return;
    const epoch = selection.current.epoch;
    compaction.current = crypto.randomUUID(); setRunId(compaction.current);
    busy.current = true; setStatus('running'); setError(''); setNotice('Compacting conversation history…');
    try {
      const response = await data(route() + '/compact', 'POST', { operation_id: compaction.current });
      if (selection.current.epoch === epoch) {
        const result = response.result || response;
        setNotice(result.status === 'compacted' ? `Compacted ${result.messages_offloaded} messages; ${result.messages_kept} remain in context.` : `Compaction: ${result.status || 'finished'}. ${result.error || ''}`);
      }
    } catch (e: any) { if (selection.current.epoch === epoch) { setNotice(''); setError(e.message + ' Refresh saved state before retrying.'); } }
    finally { if (selection.current.epoch === epoch) { compaction.current = ''; busy.current = false; await refresh(epoch).catch(e => { setError(e.message); setStatus('disconnected'); }); } }
  }
  async function rename(title: string, id = threadId) {
    const p = selection.current.projectId, key = p + '/' + id;
    titleRevisions.current.set(key, (titleRevisions.current.get(key) || 0) + 1);
    await data(route(p, id), 'PATCH', { title });
    titleRevisions.current.set(key, (titleRevisions.current.get(key) || 0) + 1);
    if (selection.current.projectId === p) setThreads(old => old.map(t => t.thread_id === id ? { ...t, metadata: { ...t.metadata, title: title.trim() } } : t));
  }
  function changeProject(id: string) {
    stream.current?.abort(); busy.current = false; compaction.current = '';
    selection.current = { projectId: id, threadId: '', epoch: selection.current.epoch + 1 };
    setThreadId(''); setProjectId(id);
  }
  const capture = () => { const epoch = selection.current.epoch; return () => selection.current.epoch === epoch; };
  return { projects, project, projectId, setProjectId: changeProject, threads, thread, threadId, selectThread, newThread, creating, state, messages, receipts, tasks, pendingResults, catalog, mode, status, error, setError, notice, setNotice, runId, activity, backend, run, reconnect, cancel, compact, refresh, changeMode, rename, data, request, capture, base: route(projectId, threadId), interrupts: pendingInterrupts(state) };
}
function hideSubmitted(snapshot: Snapshot, submitted: Set<string>): Snapshot {
  return { ...snapshot, interrupts: snapshot.interrupts?.filter(i => !submitted.has(i.id)), tasks: snapshot.tasks?.map(t => ({ ...t, interrupts: t.interrupts?.filter((i: Json) => !submitted.has(i.id)) })) };
}
export type Agent = ReturnType<typeof useAgent>;
