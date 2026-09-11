import { createHash, randomBytes, randomUUID } from 'node:crypto';
import { HttpError, listFiles, readFile } from './files.ts';
import { conversationTitles } from './titles.ts';
import { allowedDecisions, pendingInterrupts, validAnswer, type Json, type Project, type Workspace, type Snapshot } from '../src/protocol.ts';

export type Config = { projects: Project[]; backend: string; apiKey?: string; origin: string; fetch?: typeof fetch };
const json = (body: unknown, status = 200) => Response.json(body, { status });
const validId = (id: string) => /^[\w-]{1,128}$/.test(id);
export function validateResponses(state: Snapshot, responses: Json) {
  const interrupts = pendingInterrupts(state);
  if (!interrupts.length || !responses || typeof responses !== 'object' || Array.isArray(responses) || Object.keys(responses).sort().join() !== interrupts.map(i => i.id).sort().join()) throw new HttpError(409, 'The pending request changed. Refresh before deciding.');
  for (const item of interrupts) {
    const reply = responses[item.id];
    if (item.value.type === 'ask_user') {
      if (!Array.isArray(reply?.answers) || reply.answers.length !== item.value.questions?.length || reply.answers.some((x: unknown, i: number) => !validAnswer(item.value.questions[i], x))) throw new HttpError(400, 'Answer every required question using its requested format.');
    } else if (Array.isArray(item.value.action_requests)) {
      const actions = item.value.action_requests;
      if (!Array.isArray(reply?.decisions) || reply.decisions.length !== actions.length) throw new HttpError(400, 'Review every proposed action.');
      actions.forEach((action: Json, i: number) => {
        const d = reply.decisions[i];
        if (!d || !['approve', 'reject'].includes(d.type) || !allowedDecisions(item.value, action.name).includes(d.type) || Object.keys(d).some(k => !['type', 'message'].includes(k)) || (d.message !== undefined && typeof d.message !== 'string')) throw new HttpError(400, 'This decision is not supported by the pending action.');
      });
    } else throw new HttpError(409, 'This request requires a native client capability.');
  }
}
export function createApp(config: Config) {
  if (config.projects.length !== 1) throw new Error('The web UI requires exactly one startup project.');
  const projects = [Object.freeze({ ...config.projects[0] })];
  const token = randomBytes(32).toString('hex');
  const upstreamUrl = new URL(config.backend);
  if (!['http:', 'https:'].includes(upstreamUrl.protocol) || !['127.0.0.1', 'localhost', '[::1]'].includes(upstreamUrl.hostname) || upstreamUrl.username || upstreamUrl.password || upstreamUrl.pathname !== '/' || upstreamUrl.search || upstreamUrl.hash) throw new Error('The backend must be a fixed loopback HTTP origin.');
  const fetcher = config.fetch || fetch;
  async function upstream(route: string, method = 'GET', body?: unknown, headers: Record<string, string> = {}, signal?: AbortSignal) {
    const result = await fetcher(new URL(route, upstreamUrl), { method, headers: { ...(body === undefined ? {} : { 'content-type': 'application/json' }), ...(config.apiKey ? { 'x-api-key': config.apiKey } : {}), ...headers }, body: body === undefined ? undefined : JSON.stringify(body), redirect: 'error', signal: signal || AbortSignal.timeout(30_000) });
    if (!result.ok) {
      let message = `Agent server returned ${result.status}.`;
      try { const data = await result.json(); message = typeof data.detail === 'string' ? data.detail : typeof data.message === 'string' ? data.message : message; } catch { /* keep safe status */ }
      throw new HttpError(result.status, message);
    }
    return result;
  }
  const upJson = async (route: string, method = 'GET', body?: unknown) => {
    const result = await upstream(route, method, body);
    return result.status === 204 ? {} : result.json();
  };
  const titles = conversationTitles(upJson);
  async function owned(project: Project, thread: string): Promise<Workspace> {
    const record = await upJson(`/threads/${thread}`);
    if (record.metadata?.cwd !== project.path) throw new HttpError(404, 'Conversation is not in this workspace.');
    const { workspace } = await upJson(`/dcode/threads/${thread}/workspace`, 'POST', { cwd: project.path });
    if (workspace.cwd !== project.path) throw new HttpError(409, 'Workspace binding changed.');
    return workspace;
  }
  const modeKey = (thread: string) => createHash('sha256').update(thread).digest('hex');
  async function getMode(thread: string) {
    try {
      const data = await upJson(`/store/items?namespace=${encodeURIComponent('deepagents_code.approval_mode')}&key=${modeKey(thread)}`);
      // The live store returns JSON null for a conversation with no saved mode.
      const mode = data?.value?.mode;
      return ['manual', 'auto', 'yolo'].includes(mode) ? mode : 'manual';
    }
    catch (error) { if (error instanceof HttpError && error.status === 404) return 'manual'; throw error; }
  }
  async function api(request: Request) {
    const url = new URL(request.url);
    if (url.origin !== config.origin || request.headers.get('origin') && request.headers.get('origin') !== config.origin || ['cross-site', 'same-site'].includes(request.headers.get('sec-fetch-site') || '')) throw new HttpError(403, 'Open this workspace from its local address.');
    if (url.pathname === '/api/bootstrap' && request.method === 'GET') return json({ token, projects, backend: upstreamUrl.origin });
    if (request.headers.get('x-workspace-token') !== token) throw new HttpError(403, 'Session expired. Reload the workspace.');
    const parts = url.pathname.split('/').filter(Boolean);
    if (parts[0] !== 'api' || parts[1] !== 'projects') throw new HttpError(404, 'Unknown route.');
    let body: Json = {};
    if (['POST', 'PUT', 'PATCH'].includes(request.method)) {
      if (!request.headers.get('content-type')?.startsWith('application/json')) throw new HttpError(415, 'Expected JSON.');
      const raw = await request.text();
      if (Buffer.byteLength(raw) > 1_048_576) throw new HttpError(413, 'Request is too large.');
      try { body = JSON.parse(raw); } catch { throw new HttpError(400, 'Invalid JSON.'); }
      if (!body || typeof body !== 'object' || Array.isArray(body)) throw new HttpError(400, 'Expected an object.');
    }
    if (parts.length === 2) {
      if (request.method === 'GET') return json(projects);
      throw new HttpError(405, 'This app is bound to its startup project. Launch a separate agent and web UI for another project.');
    }
    const project = projects.find(p => p.id === parts[2]);
    if (!project) throw new HttpError(404, 'Unknown workspace.');
    if (parts[3] === 'files' && request.method === 'GET') return json(url.searchParams.has('read') ? await readFile(project.path, url.searchParams.get('path') || '') : await listFiles(project.path, url.searchParams.get('path') || ''));
    if (parts[3] !== 'threads') throw new HttpError(404, 'Unknown route.');
    if (parts.length === 4) {
      if (request.method === 'GET') return json(await upJson('/threads/search', 'POST', { metadata: { cwd: project.path, graph_id: 'agent' }, limit: 100, offset: Math.max(0, Number(url.searchParams.get('offset')) || 0), sort_by: 'updated_at', sort_order: 'desc' }));
      if (request.method === 'POST') {
        const thread = randomUUID();
        await upJson(`/dcode/threads/${thread}/workspace`, 'POST', { cwd: project.path });
        return json(await upJson(`/threads/${thread}`), 201);
      }
      throw new HttpError(405, 'Method not allowed.');
    }
    const thread = parts[4];
    if (!validId(thread)) throw new HttpError(400, 'Invalid conversation ID.');
    const workspace = await owned(project, thread);
    const prefix = `/threads/${thread}`;
    if (parts.length === 5 && request.method === 'GET') return json(await titles.read(thread));
    if (parts.length === 5 && request.method === 'PATCH') {
      if (typeof body.title !== 'string' || !body.title.trim() || body.title.length > 160) throw new HttpError(400, 'Use a title between 1 and 160 characters.');
      return json(await titles.rename(thread, body.title));
    }
    const operation = parts[5];
    if (operation === 'state' && request.method === 'GET') return json(await upJson(prefix + '/state?subgraphs=true'));
    if (operation === 'catalog' && request.method === 'GET') return json(await upJson(`/lc-factory/threads/${thread}/web`, 'POST', { workspace, operation: 'catalog' }));
    if (operation === 'mode') {
      if (request.method === 'GET') return json({ mode: await getMode(thread) });
      if (request.method === 'PUT' && ['manual', 'auto', 'yolo'].includes(body.mode)) {
        await upJson('/store/items', 'PUT', { namespace: ['deepagents_code', 'approval_mode'], key: modeKey(thread), value: { mode: body.mode }, index: false });
        return json({ mode: body.mode });
      }
      throw new HttpError(400, 'Choose Manual, Auto, or YOLO.');
    }
    if (operation === 'background' && request.method === 'POST') {
      if (!['list', 'inspect', 'conversation', 'message', 'cancel', 'resume'].includes(body.operation)) throw new HttpError(400, 'Unknown task operation.');
      return json(await upJson(`/lc-factory/threads/${thread}/background`, 'POST', { workspace, operation: body.operation, task_id: body.task_id, transcript_page: body.transcript_page, before: body.before, after: body.after, message_id: body.message_id, offset: body.offset, revision: body.revision, responses: body.responses }));
    }
    if (operation === 'runs' && request.method === 'GET') {
      const [running, pending] = await Promise.all([upJson(prefix + '/runs?status=running'), upJson(prefix + '/runs?status=pending')]);
      return json([...running, ...pending]);
    }
    if (operation === 'cancel' && request.method === 'POST' && validId(body.run_id || '')) return json(await upJson(`${prefix}/runs/${body.run_id}/cancel?wait=1&action=interrupt`, 'POST'));
    if (operation === 'compact' && request.method === 'POST') {
      if (!validId(body.operation_id || '')) throw new HttpError(400, 'Missing compaction operation ID.');
      const mode = await getMode(thread), operationId = body.operation_id;
      const result = await (await upstream(`/dcode/threads/${thread}/offload`, 'POST', { operation_id: operationId, context: { workspace, thread_id: thread, approval_mode: mode, approval_mode_key: modeKey(thread), auto_approve: mode !== 'manual' }, hook_responses: {} }, {}, request.signal)).json();
      if (result.status === 'interrupt') {
        await upJson(`/dcode/threads/${thread}/offload/${operationId}/cancel`, 'POST');
        throw new HttpError(409, 'Compaction requires a configured command hook. Use the native TUI for this operation.');
      }
      return json(result);
    }
    if (operation === 'compact-cancel' && request.method === 'POST' && validId(body.operation_id || '')) return json(await upJson(`/dcode/threads/${thread}/offload/${body.operation_id}/cancel`, 'POST'));
    if (operation === 'join' && request.method === 'GET' && validId(url.searchParams.get('run') || '')) return upstream(`${prefix}/runs/${url.searchParams.get('run')}/stream?cancel_on_disconnect=false`, 'GET', undefined, { 'Last-Event-ID': request.headers.get('last-event-id') || '' }, request.signal);
    if (operation === 'run' && request.method === 'POST') {
      const mode = await getMode(thread);
      const state = await upJson(prefix + '/state?subgraphs=true') as Snapshot;
      const previousUser = (state.values.messages || []).findLast((m: Json) => ['human', 'user'].includes(m.type || m.role));
      const savedTurn = previousUser?.additional_kwargs?.deepagents_code_user_prompt?.turn_id;
      const turnId = (body.responses || body.continue) && typeof savedTurn === 'string' ? savedTurn : randomUUID();
      const payload: Json = { assistant_id: 'agent', context: { workspace, thread_id: thread, turn_id: turnId, approval_mode: mode, approval_mode_key: modeKey(thread), auto_approve: mode !== 'manual' }, stream_mode: ['messages-tuple', 'updates', 'custom'], stream_subgraphs: true, stream_resumable: true, on_disconnect: 'continue', multitask_strategy: 'reject' };
      if (body.model !== undefined) {
        if (typeof body.model !== 'string' || body.model.length > 200 || !/^[\w.-]+:[\w./:@-]+$/.test(body.model)) throw new HttpError(400, 'Use a provider:model identifier.');
        payload.context.model = body.model;
      }
      if (body.responses) { validateResponses(state, body.responses); payload.command = { resume: body.responses }; }
      else if (body.continue === true) {
        if (pendingInterrupts(state).length || !state.next?.length) throw new HttpError(409, 'This conversation has no interrupted turn to continue.');
        payload.input = null;
      }
      else {
        if (pendingInterrupts(state).length || state.next?.length) throw new HttpError(409, 'Resolve the paused turn before starting another message.');
        if (typeof body.text !== 'string' || !body.text.trim() || body.text.length > 100_000) throw new HttpError(400, 'Write a message up to 100,000 characters.');
        let message: Json = { role: 'user', content: body.text };
        if (body.skill) message = await upJson(`/lc-factory/threads/${thread}/web`, 'POST', { workspace, operation: 'invoke', skill: body.skill, args: body.text });
        // Port of upstream user_prompt_metadata. Only literal user input is
        // authorization evidence; expanded skill/file bodies never become it.
        message.additional_kwargs = { ...message.additional_kwargs, deepagents_code_user_prompt: { literal_user_text: body.text, referenced_paths: [], turn_id: turnId } };
        payload.input = { messages: [message] };
      }
      const response = await upstream(prefix + '/runs/stream', 'POST', payload, {}, request.signal);
      // Start naming as soon as the run is accepted. Metadata failure must not
      // delay live output or make an accepted message look safe to resend.
      if (payload.input?.messages) void titles.initialize(thread, body.text).catch(() => {});
      return response;
    }
    throw new HttpError(404, 'Unknown operation.');
  }
  return async (request: Request) => {
    let response: Response;
    try { response = await api(request); }
    catch (error: any) {
      if (error instanceof HttpError) response = json({ detail: error.message }, error.status);
      else if (error?.code === 'ENOENT') response = json({ detail: 'File no longer exists. Refresh the folder.' }, 404);
      else response = json({ detail: 'Cannot reach the agent server. Check the backend terminal, then reconnect.' }, 502);
    }
    const headers = new Headers(response.headers);
    headers.set('cache-control', 'no-store'); headers.set('x-content-type-options', 'nosniff');
    // Never forward upstream cookies, CORS grants or server credentials.
    headers.delete('set-cookie'); headers.delete('access-control-allow-origin');
    return new Response(response.body, { status: response.status, headers });
  };
}
